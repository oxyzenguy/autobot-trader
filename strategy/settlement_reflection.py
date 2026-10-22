"""
의사결정 사후 평가 및 자가학습 복기 에이전트 (Settlement & Reflection Engine)
- 벤치마크: TauricResearch/TradingAgents 'Memory Log' & 'Settlement Reflection'
- 역할:
  1. 전략/파라미터 변경 의사결정($T_0$)을 decision_history.db에 보존
  2. 3일/7일 경과 후 실전 매매 DB(trade_history.db)를 대조하여 사후 정산(Settlement) 채점
  3. 실현 손익, 회전율, 체류 시간, 최대 차수를 대조해 등급(EXCELLENT/GOOD/NEUTRAL/UNDERPERFORM) 판정
  4. 교훈(Reflection Note)을 도출하여 reflection_memory에 영구 보존하고 차기 전략 제안에 반영
"""

import os
import sys
import json
import sqlite3
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

DEFAULT_DECISION_DB = BASE_DIR / "decision_history.db"
DEFAULT_TRADE_DB = BASE_DIR / "trade_history.db"


def get_db_connection(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """SQLite 데이터베이스 연결 객체를 반환합니다."""
    path = db_path or DEFAULT_DECISION_DB
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_decision_db(db_path: Optional[Path] = None) -> None:
    """의사결정 및 사후 복기 테이블 스키마를 초기화합니다."""
    conn = get_db_connection(db_path)
    try:
        with conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS decisions (
                    decision_id TEXT PRIMARY KEY,
                    timestamp TEXT NOT NULL,
                    market TEXT NOT NULL,
                    trigger_type TEXT NOT NULL,
                    before_params TEXT NOT NULL,
                    after_params TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    baseline_metrics TEXT,
                    status TEXT NOT NULL DEFAULT 'PENDING'
                );

                CREATE TABLE IF NOT EXISTS settlements (
                    settlement_id TEXT PRIMARY KEY,
                    decision_id TEXT NOT NULL,
                    market TEXT NOT NULL,
                    settled_at TEXT NOT NULL,
                    horizon_days INTEGER NOT NULL,
                    period_start TEXT NOT NULL,
                    period_end TEXT NOT NULL,
                    actual_pnl REAL NOT NULL DEFAULT 0.0,
                    prior_pnl REAL NOT NULL DEFAULT 0.0,
                    trade_count INTEGER NOT NULL DEFAULT 0,
                    prior_trade_count INTEGER NOT NULL DEFAULT 0,
                    cycle_count INTEGER NOT NULL DEFAULT 0,
                    turnover_krw REAL NOT NULL DEFAULT 0.0,
                    idle_24h_count INTEGER NOT NULL DEFAULT 0,
                    max_idle_hours REAL NOT NULL DEFAULT 0.0,
                    grade TEXT NOT NULL,
                    reflection_note TEXT NOT NULL,
                    raw_metrics TEXT,
                    FOREIGN KEY(decision_id) REFERENCES decisions(decision_id)
                );

                CREATE TABLE IF NOT EXISTS reflection_memory (
                    memory_id TEXT PRIMARY KEY,
                    market TEXT NOT NULL,
                    settlement_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    grade TEXT NOT NULL,
                    rule_or_lesson TEXT NOT NULL,
                    weight REAL DEFAULT 1.0,
                    FOREIGN KEY(settlement_id) REFERENCES settlements(settlement_id)
                );

                CREATE INDEX IF NOT EXISTS idx_decisions_market ON decisions(market);
                CREATE INDEX IF NOT EXISTS idx_settlements_decision ON settlements(decision_id);
                CREATE INDEX IF NOT EXISTS idx_reflections_market ON reflection_memory(market);
            """)

            # 기존 DB 스키마 마이그레이션 (settlements 내 24시간 무체결 컬럼 추가)
            cur = conn.cursor()
            cur.execute("PRAGMA table_info(settlements)")
            existing_cols = {row["name"] for row in cur.fetchall()}
            if "idle_24h_count" not in existing_cols:
                conn.execute("ALTER TABLE settlements ADD COLUMN idle_24h_count INTEGER NOT NULL DEFAULT 0")
            if "max_idle_hours" not in existing_cols:
                conn.execute("ALTER TABLE settlements ADD COLUMN max_idle_hours REAL NOT NULL DEFAULT 0.0")
    finally:
        conn.close()


# --- 1. 의사결정($T_0$) 기록 엔진 ---
def record_decision(
    market: str,
    trigger_type: str,
    before_params: Dict[str, Any],
    after_params: Dict[str, Any],
    reason: str,
    baseline_metrics: Optional[Dict[str, Any]] = None,
    timestamp: Optional[str] = None,
    decision_id: Optional[str] = None,
    db_path: Optional[Path] = None
) -> str:
    """
    전략/파라미터 변경 의사결정 시점을 decision_history.db에 저장합니다.
    """
    init_decision_db(db_path)
    d_id = decision_id or f"DEC_{market.replace('-', '_')}_{int(datetime.now().timestamp())}_{uuid.uuid4().hex[:6]}"
    ts = timestamp or datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    conn = get_db_connection(db_path)
    try:
        with conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO decisions (
                    decision_id, timestamp, market, trigger_type,
                    before_params, after_params, reason, baseline_metrics, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    d_id,
                    ts,
                    market,
                    trigger_type,
                    json.dumps(before_params, ensure_ascii=False),
                    json.dumps(after_params, ensure_ascii=False),
                    reason,
                    json.dumps(baseline_metrics or {}, ensure_ascii=False),
                    "PENDING"
                )
            )
        return d_id
    finally:
        conn.close()


def _parse_ts(ts_str: Any) -> Optional[datetime]:
    """다양한 형식의 타임스탬프 문자열을 안전하게 datetime 객체로 변환합니다."""
    if not ts_str:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(str(ts_str)[:19], fmt)
        except (ValueError, TypeError):
            continue
    return None


# --- 2. 실전 매매 DB 메트릭 추출기 ---
def query_trade_metrics(
    market: str,
    start_ts: str,
    end_ts: str,
    trade_db_path: Optional[Path] = None
) -> Dict[str, Any]:
    """
    지정된 기간 동안의 실전 매매 성과(체결 건수, 실현 손익, 익절 횟수, 총 거래대금, 24시간 무거래 공백)를 조회합니다.
    """
    t_path = trade_db_path or DEFAULT_TRADE_DB
    if not t_path.exists():
        return {
            "trade_count": 0,
            "buy_count": 0,
            "sell_count": 0,
            "net_pnl": 0.0,
            "positive_pnl": 0.0,
            "negative_pnl": 0.0,
            "cycle_count": 0,
            "turnover_krw": 0.0,
            "max_step_reached": 0,
            "idle_24h_count": 0,
            "max_idle_hours": 0.0,
            "total_idle_hours": 0.0,
            "idle_periods": []
        }

    conn = sqlite3.connect(str(t_path))
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT
                id, timestamp, side, volume, price, action, cost_or_revenue, pnl, cycle
            FROM trades
            WHERE market = ? AND timestamp >= ? AND timestamp <= ?
            ORDER BY timestamp ASC
            """,
            (market, start_ts, end_ts)
        )
        rows = cur.fetchall()

        trade_count = len(rows)
        buy_count = sum(1 for r in rows if r["side"] == "bid")
        sell_count = sum(1 for r in rows if r["side"] == "ask")
        net_pnl = sum(r["pnl"] for r in rows if r["pnl"] is not None)
        pos_pnl = sum(r["pnl"] for r in rows if r["pnl"] is not None and r["pnl"] > 0)
        neg_pnl = sum(r["pnl"] for r in rows if r["pnl"] is not None and r["pnl"] < 0)
        turnover = sum(r["cost_or_revenue"] for r in rows if r["cost_or_revenue"] is not None)

        # 익절 사이클 수 계산
        cycle_count = sum(
            1 for r in rows
            if r["side"] == "ask" and r["action"] and ("TAKE_PROFIT" in r["action"] or "PROFIT" in r["action"])
        )

        # 최대 차수/단계 파싱 (action 내 STAGE_X 또는 차수 탐색)
        max_step = 0
        for r in rows:
            act = r["action"] or ""
            if "STAGE_" in act:
                try:
                    s_num = int(act.split("STAGE_")[1].split()[0].split("_")[0])
                    max_step = max(max_step, s_num)
                except Exception:
                    pass
            elif "차수" in act:
                try:
                    s_num = int(act.split("차수")[0].split()[-1])
                    max_step = max(max_step, s_num)
                except Exception:
                    pass

        # 매매 체결 간격 및 24시간 이상 거래 공백(Stagnation / Inactivity) 정밀 계산
        idle_periods = []
        for i in range(len(rows) - 1):
            t1 = _parse_ts(rows[i]["timestamp"])
            t2 = _parse_ts(rows[i + 1]["timestamp"])
            if t1 and t2:
                gap_hours = (t2 - t1).total_seconds() / 3600.0
                if gap_hours >= 24.0:
                    idle_periods.append({
                        "start_time": str(rows[i]["timestamp"]),
                        "end_time": str(rows[i + 1]["timestamp"]),
                        "idle_hours": round(gap_hours, 1),
                        "trigger_action": rows[i]["action"] or "TRADE"
                    })

        if rows:
            t_last = _parse_ts(rows[-1]["timestamp"])
            t_end = _parse_ts(end_ts)
            if t_last and t_end and t_end > t_last:
                trailing_hours = (t_end - t_last).total_seconds() / 3600.0
                if trailing_hours >= 24.0:
                    idle_periods.append({
                        "start_time": str(rows[-1]["timestamp"]),
                        "end_time": str(end_ts),
                        "idle_hours": round(trailing_hours, 1),
                        "trigger_action": rows[-1]["action"] or "FINAL_TRADE"
                    })
        else:
            t_start = _parse_ts(start_ts)
            t_end = _parse_ts(end_ts)
            if t_start and t_end and t_end > t_start:
                total_hours = (t_end - t_start).total_seconds() / 3600.0
                if total_hours >= 24.0:
                    idle_periods.append({
                        "start_time": str(start_ts),
                        "end_time": str(end_ts),
                        "idle_hours": round(total_hours, 1),
                        "trigger_action": "NO_TRADES"
                    })

        idle_24h_count = len(idle_periods)
        max_idle_hours = max((p["idle_hours"] for p in idle_periods), default=0.0)
        total_idle_hours = round(sum(p["idle_hours"] for p in idle_periods), 1)

        return {
            "trade_count": trade_count,
            "buy_count": buy_count,
            "sell_count": sell_count,
            "net_pnl": round(net_pnl, 2),
            "positive_pnl": round(pos_pnl, 2),
            "negative_pnl": round(neg_pnl, 2),
            "cycle_count": cycle_count,
            "turnover_krw": round(turnover, 2),
            "max_step_reached": max_step,
            "idle_24h_count": idle_24h_count,
            "max_idle_hours": round(max_idle_hours, 1),
            "total_idle_hours": total_idle_hours,
            "idle_periods": idle_periods
        }
    finally:
        conn.close()


# --- 3. 사후 정산(Settlement) 및 등급 판정 엔진 ---
def evaluate_decision_settlement(
    decision_id: str,
    horizon_days: int = 7,
    as_of: Optional[datetime] = None,
    db_path: Optional[Path] = None,
    trade_db_path: Optional[Path] = None
) -> Optional[Dict[str, Any]]:
    """
    특정 의사결정에 대해 실전 매매 성과를 채점하고 교훈(Reflection)을 생성합니다.
    """
    init_decision_db(db_path)
    now_dt = as_of or datetime.now()

    conn = get_db_connection(db_path)
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM decisions WHERE decision_id = ?", (decision_id,))
        dec = cur.fetchone()
        if not dec:
            return None

        t0 = datetime.strptime(dec["timestamp"], "%Y-%m-%d %H:%M:%S")
        elapsed_days = (now_dt - t0).total_seconds() / 86400.0

        # 평가 필요 기간 미달 시 대기
        if elapsed_days < (horizon_days * 0.95):
            return None

        period_start = dec["timestamp"]
        end_dt = min(now_dt, t0 + timedelta(days=horizon_days))
        period_end = end_dt.strftime("%Y-%m-%d %H:%M:%S")

        # 대조군(이전 동일 일수) 기간 설정
        prior_start_dt = t0 - timedelta(days=horizon_days)
        prior_start = prior_start_dt.strftime("%Y-%m-%d %H:%M:%S")
        prior_end = period_start

        market = dec["market"]
        actual_m = query_trade_metrics(market, period_start, period_end, trade_db_path)
        prior_m = query_trade_metrics(market, prior_start, prior_end, trade_db_path)

        before_params = json.loads(dec["before_params"])
        after_params = json.loads(dec["after_params"])

        # 24시간 무거래 공백 및 체류 시간 추출
        idle_count = actual_m.get("idle_24h_count", 0)
        max_idle_h = actual_m.get("max_idle_hours", 0.0)

        # 성과 등급 판정 로직
        net_diff = actual_m["net_pnl"] - prior_m["net_pnl"]
        trade_ratio = (actual_m["trade_count"] / max(1, prior_m["trade_count"]))

        if actual_m["net_pnl"] > 0 and (actual_m["net_pnl"] >= prior_m["net_pnl"] or trade_ratio >= 1.5):
            grade = "EXCELLENT"
        elif actual_m["net_pnl"] >= prior_m["net_pnl"] or actual_m["cycle_count"] > prior_m["cycle_count"]:
            grade = "GOOD"
        elif abs(net_diff) < 1500 and actual_m["trade_count"] > 0:
            grade = "NEUTRAL"
        else:
            grade = "UNDERPERFORM"

        # 한국어 교훈(Reflection Note) 합성
        b_margin = before_params.get("profit_margin", 0.0)
        a_margin = after_params.get("profit_margin", 0.0)
        margin_change_str = f"익절 마진 {b_margin} ➔ {a_margin}" if (b_margin and a_margin) else "전략 파라미터 변경"

        # 24시간 무거래 정체 진단 문구 생성
        if idle_count > 0:
            idle_summary = (
                f" [⚠️ 거래 정체 진단: 체결 후 24시간 이상 무거래 공백 {idle_count}회 발생 (최장 {max_idle_h:.1f}시간 무체결 정체). "
                f"변동성 대비 그리드 간격 과대 또는 목표 마진 미도달로 자금이 묶였음을 시사하며, "
                f"24시간 무거래 시 적응형 그리드 폭 축소(호가 압축) 또는 동적 마진 인하 등 능동적 회전율 복원 조치가 권장됨.]"
            )
        else:
            idle_summary = " [✅ 24시간 이상 무거래 공백 없이 자금 연속 회전 유지됨.]"

        if grade in ("EXCELLENT", "GOOD"):
            note = (
                f"{market}: {margin_change_str} 적용 후 {horizon_days}일간 실현손익 {actual_m['net_pnl']:+,.0f}원 "
                f"(이전 대비 {net_diff:+,.0f}원 개선), 체결 {actual_m['trade_count']}건(회전율 {trade_ratio:.1f}배). "
                f"횡보·변동성 국면에서 회전율을 끌어올린 판단이 매우 유효했음.{idle_summary}"
            )
        elif grade == "NEUTRAL":
            note = (
                f"{market}: {margin_change_str} 적용 후 {horizon_days}일간 실현손익 {actual_m['net_pnl']:+,.0f}원, "
                f"체결 {actual_m['trade_count']}건. 시장 횡보 지속으로 이전과 유사한 안정권 흐름 유지.{idle_summary}"
            )
        else:
            note = (
                f"{market}: {margin_change_str} 적용 후 {horizon_days}일간 실현손익 {actual_m['net_pnl']:+,.0f}원, "
                f"체결 {actual_m['trade_count']}건. 가격 침체 국면으로 체류 시간이 길어졌으며, "
                f"추가 하락 완충 구간 및 회전 목표의 재검토가 권장됨.{idle_summary}"
            )

        settlement_id = f"SETTLE_{decision_id}_{horizon_days}D_{uuid.uuid4().hex[:6]}"
        settled_at = now_dt.strftime("%Y-%m-%d %H:%M:%S")

        raw_metrics = {
            "actual": actual_m,
            "prior": prior_m,
            "elapsed_days": round(elapsed_days, 2),
            "before_params": before_params,
            "after_params": after_params,
            "reason": dec["reason"],
            "idle_24h_count": idle_count,
            "max_idle_hours": max_idle_h
        }

        # DB 트랜잭션 기록
        with conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO settlements (
                    settlement_id, decision_id, market, settled_at, horizon_days,
                    period_start, period_end, actual_pnl, prior_pnl,
                    trade_count, prior_trade_count, cycle_count, turnover_krw,
                    idle_24h_count, max_idle_hours,
                    grade, reflection_note, raw_metrics
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    settlement_id,
                    decision_id,
                    market,
                    settled_at,
                    horizon_days,
                    period_start,
                    period_end,
                    actual_m["net_pnl"],
                    prior_m["net_pnl"],
                    actual_m["trade_count"],
                    prior_m["trade_count"],
                    actual_m["cycle_count"],
                    actual_m["turnover_krw"],
                    idle_count,
                    max_idle_h,
                    grade,
                    note,
                    json.dumps(raw_metrics, ensure_ascii=False)
                )
            )

            # reflection_memory 저장
            mem_id = f"MEM_{market.replace('-', '_')}_{uuid.uuid4().hex[:6]}"
            conn.execute(
                """
                INSERT OR REPLACE INTO reflection_memory (
                    memory_id, market, settlement_id, created_at, grade, rule_or_lesson, weight
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    mem_id,
                    market,
                    settlement_id,
                    settled_at,
                    grade,
                    note,
                    1.5 if grade == "EXCELLENT" else 1.0
                )
            )

            # 만약 24시간 이상 정체 구간이 감지된 경우, 자가학습을 위한 전용 교훈(Idle Rule)을 추가 등록
            if idle_count > 0:
                idle_mem_id = f"MEM_IDLE_{market.replace('-', '_')}_{uuid.uuid4().hex[:6]}"
                idle_lesson = (
                    f"[{market}] 체결 후 24시간 무거래 정체 감지({idle_count}회, 최장 {max_idle_h:.1f}h): "
                    f"호가 간격 과대로 인한 자금 체류를 방지하기 위해 24시간 무거래 시 그리드 폭 자동 축소(압축) 필요"
                )
                conn.execute(
                    """
                    INSERT OR REPLACE INTO reflection_memory (
                        memory_id, market, settlement_id, created_at, grade, rule_or_lesson, weight
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        idle_mem_id,
                        market,
                        settlement_id,
                        settled_at,
                        "STAGNATION_ALERT",
                        idle_lesson,
                        1.2
                    )
                )

            # decision 상태 갱신
            new_status = f"SETTLED_{horizon_days}D"
            if horizon_days >= 7:
                new_status = "COMPLETED"
            conn.execute("UPDATE decisions SET status = ? WHERE decision_id = ?", (new_status, decision_id))

        return {
            "settlement_id": settlement_id,
            "decision_id": decision_id,
            "market": market,
            "horizon_days": horizon_days,
            "grade": grade,
            "actual_pnl": actual_m["net_pnl"],
            "prior_pnl": prior_m["net_pnl"],
            "trade_count": actual_m["trade_count"],
            "prior_trade_count": prior_m["trade_count"],
            "cycle_count": actual_m["cycle_count"],
            "idle_24h_count": idle_count,
            "max_idle_hours": max_idle_h,
            "reflection_note": note
        }
    finally:
        conn.close()


# --- 4. 정기 정산 스위퍼 (Sweep Due Settlements) ---
def run_due_settlements(
    as_of: Optional[datetime] = None,
    db_path: Optional[Path] = None,
    trade_db_path: Optional[Path] = None,
    send_telegram: bool = False
) -> List[Dict[str, Any]]:
    """
    기록된 모든 의사결정을 검사하여 3일 또는 7일 만기가 도래한 건들을 일괄 사후 채점합니다.
    """
    init_decision_db(db_path)
    now_dt = as_of or datetime.now()

    conn = get_db_connection(db_path)
    settled_results = []
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM decisions WHERE status != 'COMPLETED' ORDER BY timestamp ASC")
        decisions = cur.fetchall()

        for dec in decisions:
            t0 = datetime.strptime(dec["timestamp"], "%Y-%m-%d %H:%M:%S")
            elapsed = (now_dt - t0).total_seconds() / 86400.0
            status = dec["status"]

            # 7일 정산 우선 검사
            if elapsed >= 6.8 and status != "SETTLED_7D":
                res = evaluate_decision_settlement(
                    dec["decision_id"], horizon_days=7, as_of=now_dt,
                    db_path=db_path, trade_db_path=trade_db_path
                )
                if res:
                    settled_results.append(res)
            # 3일 정산 검사
            elif elapsed >= 2.8 and status == "PENDING":
                res = evaluate_decision_settlement(
                    dec["decision_id"], horizon_days=3, as_of=now_dt,
                    db_path=db_path, trade_db_path=trade_db_path
                )
                if res:
                    settled_results.append(res)

        if send_telegram and settled_results:
            from utils.bot import send_message
            briefing = format_reflection_briefing(settled_results)
            send_message(briefing)

        return settled_results
    finally:
        conn.close()


# --- 5. 과거 교훈(Reflection Memory) 조회 ---
def get_market_reflections(market: str, limit: int = 5, db_path: Optional[Path] = None) -> List[Dict[str, Any]]:
    """특정 마켓에 축적된 과거 교훈 메모리를 반환합니다."""
    init_decision_db(db_path)
    conn = get_db_connection(db_path)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT memory_id, market, created_at, grade, rule_or_lesson, weight
            FROM reflection_memory
            WHERE market = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (market, limit)
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def get_reflection_context_for_advisor(market: str, db_path: Optional[Path] = None) -> str:
    """전략봇(strategy_advisor.py) 프롬프트/브리핑에 삽입할 최근 교훈 요약 텍스트를 생성합니다."""
    mems = get_market_reflections(market, limit=3, db_path=db_path)
    if not mems:
        return "• 과거 정산 교훈: 아직 축적된 실전 사후 평가 데이터가 없습니다."

    lines = ["• <b>과거 의사결정 사후 채점 교훈 (Settlement Memory)</b>:"]
    for m in mems:
        grade_badge = "🟢" if m["grade"] in ("EXCELLENT", "GOOD") else "🟡" if m["grade"] == "NEUTRAL" else "🔴"
        lines.append(f"  {grade_badge} <i>[{m['grade']}]</i> {m['rule_or_lesson']}")
    return "\n".join(lines)


# --- 6. 텔레그램 고품질 HTML 리포트 포매터 ---
def format_reflection_briefing(settlements: List[Dict[str, Any]]) -> str:
    """사후 정산 결과를 텔레그램 고품질 HTML 보고서로 포맷합니다."""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        "🎓 <b>[AutoBot 전략 의사결정 사후 채점(Settlement) 리포트]</b>",
        f"<i>기준 시각: {now_str} (기록된 의사결정 사후 복기 완료)</i>",
        "────────────────────"
    ]

    for s in settlements:
        grade = s.get("grade", "NEUTRAL")
        badge = "🏆 최우수 (EXCELLENT)" if grade == "EXCELLENT" else "🟢 양호 (GOOD)" if grade == "GOOD" else "🟡 보통 (NEUTRAL)" if grade == "NEUTRAL" else "🔴 개선필요 (UNDERPERFORM)"
        mkt = s.get("market") or "KRW-알수없음"
        h_days = s.get("horizon_days", 7)
        lines.append(f"📌 <b>{mkt}</b> ({h_days}일차 사후 정산)")
        lines.append(f"• 종합 판정: <b>{badge}</b>")
        lines.append(f"• 실전 손익: <b>{s.get('actual_pnl', 0.0):+,.0f}원</b> (이전 동기: {s.get('prior_pnl', 0.0):+,.0f}원)")
        lines.append(f"• 체결 횟수: <b>{s.get('trade_count', 0)}건</b> (이전 동기: {s.get('prior_trade_count', 0)}건)")
        lines.append(f"• 사이클 익절: <b>{s.get('cycle_count', 0)}회</b>")
        idle_cnt = s.get("idle_24h_count", 0)
        max_idle = s.get("max_idle_hours", 0.0)
        if idle_cnt > 0:
            lines.append(f"• 24시간 무거래 공백: <b>⚠️ {idle_cnt}회 발생 (최장 {max_idle:.1f}시간 체류)</b>")
        else:
            lines.append(f"• 24시간 무거래 공백: <b>✅ 0회 (원활한 연속 회전)</b>")
        lines.append(f"💡 <b>도출된 교훈 (Reflection)</b>:")
        lines.append(f"  {s.get('reflection_note', '-')}")
        lines.append("────────────────────")

    lines.append("※ 축적된 교훈은 차기 매일 아침 퀀트 전략 제안 시 스스로 반영됩니다.")
    return "\n".join(lines)


# --- 7. Phase 1 초기 기준점(10/04 11:42 KST) 시드 등록기 ---
def seed_phase1_baseline(
    db_path: Optional[Path] = None,
    trade_db_path: Optional[Path] = None
) -> Dict[str, str]:
    """
    2026-10-04 11:42 KST 시점에 시작된 Phase 1 안정화 관측(ETH 0.5% 순수마틴게일, SOL 0.5% 하이브리드)을
    의사결정 1호 데이터로 등록하여 오늘 11:42 7일 만기 사후 채점이 즉시 가능하도록 준비합니다.
    """
    init_decision_db(db_path)
    seed_ts = "2026-10-04 11:42:00"

    eth_id = "DEC_KRW_ETH_PHASE1_BASELINE_20261004"
    sol_id = "DEC_KRW_SOL_PHASE1_BASELINE_20261004"

    record_decision(
        market="KRW-ETH",
        trigger_type="PHASE1_STABILIZATION_SEED",
        before_params={"profit_margin": 1.008, "strategy": "HYBRID", "down_pct": 0.04},
        after_params={"profit_margin": 1.005, "strategy": "PURE_MARTINGALE", "down_pct": 0.007},
        reason="초미세 마틴게일 및 0.5% 마진 적용으로 횡보장 회전율 및 탈출 속도 극대화",
        baseline_metrics={"notes": "Phase 1 관측 시작점"},
        timestamp=seed_ts,
        decision_id=eth_id,
        db_path=db_path
    )

    record_decision(
        market="KRW-SOL",
        trigger_type="PHASE1_STABILIZATION_SEED",
        before_params={"profit_margin": 1.010, "strategy": "HYBRID", "down_pct": 0.04},
        after_params={"profit_margin": 1.005, "strategy": "HYBRID", "down_pct": 0.04},
        reason="솔라나 고변동성 0.5% 단타 마진 적용 및 200선 70% 손절 방어 유지",
        baseline_metrics={"notes": "Phase 1 관측 시작점"},
        timestamp=seed_ts,
        decision_id=sol_id,
        db_path=db_path
    )

    return {"KRW-ETH": eth_id, "KRW-SOL": sol_id}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Settlement & Reflection Engine")
    parser.add_argument("--seed", action="store_true", help="Phase 1 기준점(10/04 11:42) 등록")
    parser.add_argument("--settle", action="store_true", help="만기 도래 의사결정 사후 채점 실행")
    parser.add_argument("--history", type=str, default="", help="특정 마켓 교훈 메모리 조회 (예: ETH)")
    args = parser.parse_args()

    if args.seed:
        ids = seed_phase1_baseline()
        print(f"✅ Phase 1 기준점 등록 완료: {ids}")
    elif args.settle:
        res = run_due_settlements(send_telegram=True)
        print(f"✅ 사후 채점 완료 ({len(res)}건):")
        for r in res:
            print(f"- [{r['grade']}] {r['market']}: {r['reflection_note']}")
    elif args.history:
        m = f"KRW-{args.history.upper()}" if not args.history.startswith("KRW-") else args.history.upper()
        memories = get_market_reflections(m)
        print(f"=== {m} 축적된 교훈 ({len(memories)}건) ===")
        for mem in memories:
            print(f"[{mem['created_at']}] ({mem['grade']}) {mem['rule_or_lesson']}")
    else:
        init_decision_db()
        print("✅ 의사결정 및 사후 복기 DB(decision_history.db) 정상 초기화 완료.")
