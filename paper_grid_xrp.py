"""
========================================================================================
🚀 [AutoBot Trader] KRW-XRP 순수 60단 5단 독립 스쿼드 그리드 가상 매매 엔진 (Model G60)
========================================================================================
[모델 정의: Model G60 (최우선 추천 전략)]
- 대상 마켓: KRW-XRP (리플)
- 운용 자본금: 1,000,000 KRW
- 1회 주문 단위: 10,000 KRW
- 그리드 간격: 0.50% 하락 시마다 매수 (최대 60단계 = 600,000원 운용 한도)
- 1회 익절 목표: +1.20% (개별 짤짤이 익절 유지 ➔ 자본 회전율 극대화)
- 5단계 스쿼드 바스켓 이중 익절: 5단 단위 묶음 평단(VWAP) 대비 +1.20% 터치 시 스쿼드 일괄 익절!
  (스쿼드 1: L1~5, 스쿼드 2: L6~10 ... 스쿼드 12: L56~60)
- 안전 현금 비축: 400,000 KRW (40% 안전 현금 항시 보존, 폭락장 자산 붕괴 방어)
- 곰마파(Gommapa) 비상 물타기: OFF (비활성화 - 순수 독립 스쿼드 회수율 극대화)
========================================================================================
"""

import os
import sys

# macOS(Darwin) 멀티스레드 환경에서 fork() 시 Segfault 11 크래시 원천 방지
os.environ["OBJC_DISABLE_INITIALIZE_FORK_SAFETY"] = "YES"

import time
import json
import math
import shutil
import sqlite3
import tempfile
import requests
from datetime import datetime
from typing import Dict, List, Any, Optional, Tuple

from utils.order_executor import preflight_check_buy, preflight_check_sell

import atexit

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, "paper_grid_state_KRW_XRP.json")
DB_FILE = os.path.join(BASE_DIR, "paper_trade_history.db")
LOCK_FILE = os.path.join(BASE_DIR, ".paper_grid_KRW_XRP.lock")
HALT_FLAG_FILE = os.path.join(BASE_DIR, ".paper_grid_KRW_XRP.corrupted_halt")

_lock_fd = None

def acquire_paper_lock() -> bool:
    """단일 실행 보장을 위한 배타적 프로세스 잠금 획득"""
    global _lock_fd
    try:
        import fcntl
        _lock_fd = open(LOCK_FILE, "w")
        fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _lock_fd.write(f"{os.getpid()}\n")
        _lock_fd.flush()
        return True
    except (IOError, OSError):
        return False

def release_paper_lock() -> None:
    """프로세스 잠금 해제"""
    global _lock_fd
    if _lock_fd:
        try:
            import fcntl
            fcntl.flock(_lock_fd, fcntl.LOCK_UN)
            _lock_fd.close()
        except (IOError, OSError):
            pass
        _lock_fd = None

atexit.register(release_paper_lock)

MARKET = "KRW-XRP"
TICKER = "XRP"
INITIAL_CAPITAL = 1_000_000.0  # 100만 원
UNIT_KRW = 10_000.0            # 1회 10,000원
STEP_PCT = 0.005               # 0.5% 간격
PROFIT_PCT = 0.012             # +1.2% 익절 (황금 밸런스)
MAX_LEVELS = 60                # 최대 60레벨 (Model G60: 600,000원 운용 한도)
GRID_BUDGET = 600_000.0        # 그리드 최대 예산 60만 원
SAFETY_CASH_BUFFER = 400_000.0 # 40만 원 안전 현금 항시 보존
USE_GOMMAPA = False            # Model G60: 곰마파 비상탈출 OFF (순수 5단 독립 스쿼드)
FEE_RATE = 0.0005              # 업비트 0.05% taker fee
SLIPPAGE = 0.0002              # 0.02% 슬리피지
POLL_INTERVAL_SEC = 5          # 5초 주기 감시

# --- 5단계 스쿼드 바스켓 설정 ---
SQUAD_SIZE = 5                   # 5단계 단위 스쿼드 바스켓 (레벨 1~5: 스쿼드 1 ... 56~60: 스쿼드 12)
SQUAD_TP_PCT = 0.012             # +1.2% 스쿼드 VWAP 바스켓 익절 목표
MIN_SQUAD_TRANCHES_FOR_TP = 2    # 스쿼드 내 최소 2개 이상 보유 시 바스켓 익절 활성화

# --- 곰마파 비상 탈출 모드 설정 (USE_GOMMAPA = True 설정 시에만 사용) ---
GOMMAPA_ESCAPE_PROFIT_PCT = 0.035  # 전체 평단가 대비 +3.5% 반등 시 전량 익절 탈출
GOMMAPA_STAGES = [
    {"stage": 1, "dip_ratio": 0.90, "amount": 100_000.0},  # 바닥 대비 -10% 하락 시 10만 원
    {"stage": 2, "dip_ratio": 0.85, "amount": 150_000.0},  # 직전 대비 -15% 하락 시 15만 원
    {"stage": 3, "dip_ratio": 0.80, "amount": 250_000.0},  # 직전 대비 -20% 하락 시 25만 원
]


def send_telegram_alert(message: str) -> None:
    """텔레그램 알림 발송 (실패 시 무시)"""
    try:
        from utils.bot import send_message
        send_message(message)
    except Exception as e:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] [WARN] 텔레그램 발송 실패: {e}")


def init_db() -> None:
    """가상 매매 전용 SQLite DB 초기화"""
    conn = None
    try:
        conn = sqlite3.connect(DB_FILE, timeout=10.0)
        c = conn.cursor()
        c.execute('''
            CREATE TABLE IF NOT EXISTS paper_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                market TEXT,
                side TEXT,
                action TEXT,
                price REAL,
                volume REAL,
                cost_or_revenue REAL,
                pnl REAL,
                level INTEGER,
                strategy TEXT
            )
        ''')
        conn.commit()
    finally:
        if conn:
            conn.close()


def log_paper_trade(market: str, side: str, action: str, price: float, volume: float, cost_or_revenue: float, pnl: float, level: int, strategy: str = "GRID_SCALPER", timestamp: Optional[str] = None) -> None:
    """가상 체결 기록 저장"""
    conn = None
    try:
        conn = sqlite3.connect(DB_FILE, timeout=10.0)
        c = conn.cursor()
        now_str = timestamp or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        c.execute('''
            INSERT INTO paper_trades (timestamp, market, side, action, price, volume, cost_or_revenue, pnl, level, strategy)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (now_str, market, side, action, price, volume, cost_or_revenue, pnl, level, strategy))
        conn.commit()
    except Exception as e:
        print(f"[ERROR] 가상 체결 DB 저장 실패: {e}")
    finally:
        if conn:
            conn.close()


_last_runtime_state: Optional[Dict[str, Any]] = None


def commit_tick_events(events: List[Dict[str, Any]], state: Optional[Dict[str, Any]] = None) -> None:
    """
    P1/P2: 틱 처리의 원자성 및 영속성 보장 (Transactional Outbox 패턴):
    상태 JSON에 보존된 pending_events와 전달된 events를 모두 안전하게 커밋합니다.
    DB 장애 시 이벤트를 유실하지 않고 보존하며, 복구 시 누락 없이 DB에 영속화됩니다.
    커밋 성공 시 디스크와 메모리(state)의 pending_events를 모두 제거하여 중복 발송을 방지합니다.
    """
    global _last_runtime_state
    target_state = state if state is not None else _last_runtime_state

    disk_state = None
    try:
        disk_state = load_state()
    except Exception:
        pass
    if disk_state is None:
        disk_state = {}

    pending = disk_state.setdefault("pending_events", [])
    for ev in events:
        if ev not in pending:
            pending.append(ev)

    if not pending:
        return

    committed_indices = set()
    conn = None
    try:
        conn = sqlite3.connect(DB_FILE, timeout=10.0)
        c = conn.cursor()
        for idx, ev in enumerate(pending):
            ev_type = ev.get("type")
            if ev_type == "trade":
                p = ev["payload"]
                ts = p.get("timestamp") or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                # 멱등성 보장: 동일 타임스탬프, 액션, 가격, 수량, 레벨 중복 삽입 방지
                c.execute('''
                    SELECT 1 FROM paper_trades
                    WHERE timestamp = ? AND action = ? AND price = ? AND volume = ? AND level = ?
                    LIMIT 1
                ''', (ts, p["action"], p["price"], p["volume"], p["level"]))
                if not c.fetchone():
                    c.execute('''
                        INSERT INTO paper_trades (timestamp, market, side, action, price, volume, cost_or_revenue, pnl, level, strategy)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (ts, p["market"], p["side"], p["action"], p["price"], p["volume"], p["cost_or_revenue"], p["pnl"], p["level"], p.get("strategy", "GRID_SCALPER")))
            elif ev_type == "alert":
                msg = ev["msg"]
                now_str = ev.get("now_str", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                print(f"\n[{now_str}] {msg.replace('<b>','').replace('</b>','')}")
                send_telegram_alert(msg)
            committed_indices.add(idx)
        conn.commit()
    except Exception as e:
        print(f"[ERROR] 가상 체결 이벤트 DB 커밋 실패: {e}")
        committed_indices.clear()
    finally:
        if conn:
            conn.close()

    # DB 커밋 성공 시 성공한 이벤트만 제거, 실패 시 outbox 큐 보존
    if committed_indices:
        committed_events = [pending[i] for i in committed_indices]
        disk_state["pending_events"] = [ev for i, ev in enumerate(pending) if i not in committed_indices]

        # 디스크 저장을 먼저 수행하고 예외 발생 시 전파 (기록 유실 방지)
        try:
            save_state(disk_state)
        except Exception as e:
            print(f"[ERROR] commit_tick_events 상태 파일 디스크 저장 실패: {e}")
            # 디스크 저장 실패 시 메모리 outbox에서도 이벤트를 제거하지 않고 보존하여 다음 틱에서 재시도
            raise

        # 디스크 저장이 완벽히 성공한 후에만 메모리(target_state)에서도 committed_events 제거
        if target_state is not None and isinstance(target_state, dict):
            target_pending = target_state.get("pending_events", [])
            target_state["pending_events"] = [ev for ev in target_pending if ev not in committed_events]
    else:
        disk_state["pending_events"] = pending
        if events:
            try:
                save_state(disk_state)
            except Exception as e:
                print(f"[ERROR] commit_tick_events 미처리 상태 디스크 저장 실패: {e}")
                raise

    if target_state is not None:
        _last_runtime_state = target_state

    # 체결 이벤트 성공 시 대시보드 클라우드 스냅샷 동기화 트리거
    if any(ev.get("type") == "trade" for i, ev in enumerate(pending) if i in committed_indices):
        try:
            from utils.sync_manager import trigger_snapshot_sync
            trigger_snapshot_sync()
        except Exception:
            pass


def validate_state_invariants(s: Dict[str, Any], halt_flag: Optional[str] = None) -> None:
    """
    P1: 전략 및 회계 상태 필드 간 모순(Invariants) 엄격 검증:
    1. 회계/전략 필수 필드 존재, 타입, 유한성(Finite), 양수 범위 검증
    2. 그리드 레벨(1~MAX_LEVELS) 중복 배제 및 tp_p > buy_p 검증
    3. 곰마파 스테이지(1~3) 중복 배제 및 필수 수치 검증
    4. 상태 필드 간 상호 모순 검증:
       - GRID_SCALPER 모드: 곰마파 트랜치가 없어야 함 (len == 0), gommapa_base_p == 0.0, 활성 그리드가 있으면 base_price > 0
       - GOMMAPA_ESCAPE 모드: gommapa_base_p > 0, base_price > 0, 포지션이 0개일 수 없음
    5. 자본 보존 법칙 (Capital Conservation Law):
       available_krw + sum(grid costs) + sum(gommapa costs) == initial_capital + realized_pnl (오차 <= 10.0 KRW)
    모순 발견 시 즉시 .paper_grid_KRW_XRP.corrupted_halt 파일을 생성하고 RuntimeError를 발생시킵니다.
    """
    if halt_flag is None:
        state_dir = os.path.dirname(STATE_FILE)
        halt_flag = os.path.join(state_dir, ".paper_grid_KRW_XRP.corrupted_halt")

    def _fail(msg: str):
        try:
            with open(halt_flag, "w", encoding="utf-8") as hf:
                hf.write(f"Halted at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\nReason: {msg}\n")
        except Exception:
            pass
        raise RuntimeError(f"🚨 [안전 차단] {msg}")

    # 1. 예수금 (available_krw) 검증
    if "available_krw" not in s or not isinstance(s["available_krw"], (int, float)) or not math.isfinite(s["available_krw"]) or s["available_krw"] < 0:
        _fail("회계 핵심 필드 'available_krw'가 누락되었거나 비정상입니다. 자산 부풀리기를 방지하기 위해 안전 중단합니다.")

    # 2. 운용 모드 (mode) 검증
    if "mode" not in s or s["mode"] not in ("GRID_SCALPER", "GOMMAPA_ESCAPE"):
        _fail("전략 핵심 필드 'mode'가 누락되었거나 비정상('GRID_SCALPER' 또는 'GOMMAPA_ESCAPE' 필수)입니다.")

    # 3. 기준 가격 (base_price) 검증
    if "base_price" not in s or not isinstance(s["base_price"], (int, float)) or not math.isfinite(s["base_price"]) or s["base_price"] < 0:
        _fail("전략 핵심 필드 'base_price'가 누락되었거나 비정상입니다.")

    # 4. 곰마파 기준 가격 (gommapa_base_p) 검증
    if "gommapa_base_p" not in s or not isinstance(s["gommapa_base_p"], (int, float)) or not math.isfinite(s["gommapa_base_p"]) or s["gommapa_base_p"] < 0:
        _fail("전략 핵심 필드 'gommapa_base_p'가 누락되었거나 비정상입니다.")

    # 5. 보유 그리드 포지션 (active_grids) 검증
    if "active_grids" not in s or not isinstance(s["active_grids"], list):
        _fail("포지션 핵심 필드 'active_grids'가 누락되었거나 리스트 형식이 아닙니다. (빈 목록으로 임의 대체 불가)")

    seen_grid_levels = set()
    total_grid_cost = 0.0
    for g in s["active_grids"]:
        if not isinstance(g, dict):
            _fail(f"active_grids 항목이 딕셔너리가 아닙니다: {g}")
        for req_k in ("lvl", "buy_p", "vol", "cost", "tp_p"):
            if req_k not in g:
                _fail(f"그리드 포지션 필수 필드 '{req_k}' 누락: {g}")

        lvl = g["lvl"]
        if type(lvl) is not int or not (1 <= lvl <= MAX_LEVELS):
            _fail(f"그리드 레벨(lvl) 범위 오류 (1~{MAX_LEVELS}): {g}")
        if lvl in seen_grid_levels:
            _fail(f"그리드 레벨 중복 감지 (lvl={lvl}): {g}")
        seen_grid_levels.add(lvl)

        if "squad" in g:
            sq = g["squad"]
            expected_sq = (lvl - 1) // SQUAD_SIZE + 1
            if type(sq) is not int or sq != expected_sq:
                _fail(f"그리드 스쿼드 ID 불일치 (lvl={lvl}, squad={sq}, expected={expected_sq}): {g}")

        for num_k in ("buy_p", "vol", "cost", "tp_p"):
            val = g[num_k]
            if not (isinstance(val, (int, float)) and math.isfinite(val) and val > 0):
                _fail(f"그리드 수치 오류 ({num_k}={val}): {g}")

        if g["tp_p"] <= g["buy_p"]:
            _fail(f"그리드 익절 목표가(tp_p={g['tp_p']})는 진입가(buy_p={g['buy_p']})보다 커야 합니다: {g}")

        total_grid_cost += float(g["cost"])

    # 6. 곰마파 트랜치 (gommapa_tranches) 검증
    if "gommapa_tranches" not in s or not isinstance(s["gommapa_tranches"], list):
        _fail("포지션 핵심 필드 'gommapa_tranches'가 누락되었거나 리스트 형식이 아닙니다. (빈 목록으로 임의 대체 불가)")

    seen_gom_stages = set()
    total_gom_cost = 0.0
    for t in s["gommapa_tranches"]:
        if not isinstance(t, dict):
            _fail(f"gommapa_tranches 항목이 딕셔너리가 아닙니다: {t}")
        for req_k in ("stage", "buy_p", "vol", "cost"):
            if req_k not in t:
                _fail(f"곰마파 트랜치 필수 필드 '{req_k}' 누락: {t}")

        stage = t["stage"]
        if type(stage) is not int or stage not in (1, 2, 3):
            _fail(f"곰마파 스테이지(stage) 범위 오류 (1~3): {t}")
        if stage in seen_gom_stages:
            _fail(f"곰마파 스테이지 중복 감지 (stage={stage}): {t}")
        seen_gom_stages.add(stage)

        for num_k in ("buy_p", "vol", "cost"):
            val = t[num_k]
            if not (isinstance(val, (int, float)) and math.isfinite(val) and val > 0):
                _fail(f"곰마파 수치 오류 ({num_k}={val}): {t}")

        total_gom_cost += float(t["cost"])

    # 7. 상태 필드 간 상호 모순 검증 (Cross-Field Contradiction)
    mode = s["mode"]
    if mode == "GRID_SCALPER":
        if len(s["gommapa_tranches"]) > 0:
            _fail(f"상태 모순 감지: GRID_SCALPER 모드이나 곰마파 트랜치가 {len(s['gommapa_tranches'])}개 남아있습니다.")
        if s["gommapa_base_p"] != 0.0:
            _fail(f"상태 모순 감지: GRID_SCALPER 모드이나 gommapa_base_p({s['gommapa_base_p']})가 0이 아닙니다.")
        if len(s["active_grids"]) > 0 and s["base_price"] <= 0:
            _fail(f"상태 모순 감지: 보유 그리드가 {len(s['active_grids'])}개 있으나 base_price({s['base_price']})가 0 이하입니다.")
        if USE_GOMMAPA and len(s["active_grids"]) >= MAX_LEVELS:
            _fail(f"상태 모순 감지: 그리드가 {len(s['active_grids'])}개 도달했으나 mode가 GOMMAPA_ESCAPE로 전환되지 않았습니다.")
        elif not USE_GOMMAPA and len(s["active_grids"]) > MAX_LEVELS:
            _fail(f"상태 모순 감지: 보유 그리드 수({len(s['active_grids'])})가 최대 허용 단수({MAX_LEVELS})를 초과했습니다.")
    elif mode == "GOMMAPA_ESCAPE":
        if s["gommapa_base_p"] <= 0:
            _fail(f"상태 모순 감지: GOMMAPA_ESCAPE 모드이나 gommapa_base_p({s['gommapa_base_p']})가 0 이하입니다.")
        if s["base_price"] <= 0:
            _fail(f"상태 모순 감지: GOMMAPA_ESCAPE 모드이나 base_price({s['base_price']})가 0 이하입니다.")
        if len(s["active_grids"]) == 0 and len(s["gommapa_tranches"]) == 0:
            _fail("상태 모순 감지: GOMMAPA_ESCAPE 모드이나 보유 중인 그리드 및 곰마파 포지션이 모두 0개입니다.")

    # 8. 자본 보존 법칙 (Capital Conservation Law) 검증
    initial_cap = float(s.get("initial_capital", INITIAL_CAPITAL))
    realized_pnl = float(s.get("realized_pnl", 0.0))
    expected_capital = initial_cap + realized_pnl
    actual_book_capital = float(s["available_krw"]) + total_grid_cost + total_gom_cost
    capital_delta = abs(actual_book_capital - expected_capital)
    if capital_delta > 10.0:
        _fail(
            f"자본 보존 법칙 위배: 장부 자산({actual_book_capital:,.1f}원 = 예수금 {s['available_krw']:,.1f} + 포지션원가 {total_grid_cost+total_gom_cost:,.1f}) "
            f"!= 기대 자본({expected_capital:,.1f}원 = 원금 {initial_cap:,.1f} + 실현손익 {realized_pnl:,.1f}), 오차: {capital_delta:,.2f}원"
        )


def load_state() -> Dict[str, Any]:
    """가상 매매 상태 파일 로드 또는 신규 초기화"""
    state_dir = os.path.dirname(STATE_FILE)
    state_basename = os.path.basename(STATE_FILE)
    halt_flag = os.path.join(state_dir, ".paper_grid_KRW_XRP.corrupted_halt")

    # 1. 관리자 개입 대기 차단 플래그 확인 (P1-1: 손상 발생 후 재시작 계속 차단)
    if os.path.exists(halt_flag):
        raise RuntimeError(
            "🚨 [안전 차단] 상태 손상 또는 회계 오류 차단 플래그(.paper_grid_KRW_XRP.corrupted_halt)가 존재합니다. "
            "데이터 보존을 위해 관리자의 명시적 복구/리셋 전에는 재시작이 차단됩니다."
        )

    # 2. 상태 파일이 없는데 과거 손상 백업 파일이 남아있는 경우 (무단 삭제 후 100만원 재생성 방지)
    try:
        corrupted_backups = [f for f in os.listdir(state_dir) if f.startswith(f"{state_basename}.corrupted.")]
    except OSError:
        corrupted_backups = []

    if not os.path.exists(STATE_FILE) and corrupted_backups:
        raise RuntimeError(
            "🚨 [안전 차단] 과거 손상 백업 파일이 존재하며 활성 상태 파일이 없습니다. "
            "새 계좌 자동 생성을 방지하기 위해 재시작이 차단됩니다. (상태 복구 후 시작하세요)"
        )

    # 3. 기존 상태 파일 로드 및 무결성 검증
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                s = json.load(f)
            if not isinstance(s, dict):
                raise ValueError("상태 파일 내용이 JSON 객체가 아닙니다.")
        except Exception as e:
            backup_path = f"{STATE_FILE}.corrupted.{int(time.time())}"
            try:
                shutil.copyfile(STATE_FILE, backup_path)
                print(f"[CRITICAL] 상태 파일 손상 감지({e})! 손상 파일을 백업했습니다: {backup_path}")
            except Exception as be:
                print(f"[CRITICAL] 상태 파일 백업 복사 실패: {be}")

            try:
                with open(halt_flag, "w", encoding="utf-8") as hf:
                    hf.write(f"Corrupted at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\nReason: {e}\n")
            except Exception:
                pass

            raise RuntimeError(
                f"🚨 [안전 차단] 상태 파일 손상 감지({e}). "
                "데이터 보존 및 새 계좌 자동 생성 방지를 위해 재시작이 계속 차단됩니다."
            )

        # 3-1. SQUAD_SIZE 변경 및 구버전 호환: 각 그리드의 squad ID 자동 동기화
        for g in s.get("active_grids", []):
            if isinstance(g, dict) and "lvl" in g and isinstance(g["lvl"], int):
                g["squad"] = (g["lvl"] - 1) // SQUAD_SIZE + 1

        # 4. 회계 및 전략 핵심 필드와 상호 모순 엄격 검증
        validate_state_invariants(s, halt_flag=halt_flag)

        # 5. 비회계/표시용·통계용 필드만 안전 기본값 보완 (active_grids, gommapa_tranches, mode 등은 절대 제외)
        display_defaults = {
            "market": MARKET,
            "strategy": "GRID_SCALPER",
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "initial_capital": INITIAL_CAPITAL,
            "realized_pnl": 0.0,
            "win_count": 0,
            "total_trades": 0,
            "last_update": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
        for k, def_v in display_defaults.items():
            if k not in s:
                s[k] = def_v

        # 스쿼드 ID 보완 (구버전 호환)
        for g in s.get("active_grids", []):
            if "squad" not in g and "lvl" in g:
                g["squad"] = (g["lvl"] - 1) // SQUAD_SIZE + 1

        # 자산 평가액 및 MDD 보정 (누락 시에만)
        if "total_equity" not in s:
            pos_val = (
                sum(g["vol"] * g["buy_p"] for g in s["active_grids"]) +
                sum(t["vol"] * t["buy_p"] for t in s["gommapa_tranches"])
            )
            s["total_equity"] = s["available_krw"] + pos_val
        if "peak_equity" not in s:
            s["peak_equity"] = s["total_equity"]
        if "max_drawdown_pct" not in s:
            s["max_drawdown_pct"] = 0.0

        return s

    # 6. 신규 계좌 초기 생성 (손상/차단 플래그가 없을 때만 허용)
    initial_state = {
        "market": MARKET,
        "strategy": "GRID_SCALPER",
        "mode": "GRID_SCALPER",
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "initial_capital": INITIAL_CAPITAL,
        "available_krw": INITIAL_CAPITAL,
        "total_equity": INITIAL_CAPITAL,
        "peak_equity": INITIAL_CAPITAL,
        "max_drawdown_pct": 0.0,
        "realized_pnl": 0.0,
        "win_count": 0,
        "total_trades": 0,
        "base_price": 0.0,
        "active_grids": [],
        "gommapa_tranches": [],
        "gommapa_base_p": 0.0,
        "last_update": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }
    save_state(initial_state)
    return initial_state


def save_state(state: Dict[str, Any]) -> None:
    """원자적(Atomic) 및 고유 임시 파일 기반 상태 저장 (불변식 사전 검증 + 디렉터리 fsync)"""
    state_dir = os.path.dirname(STATE_FILE)
    halt_flag = os.path.join(state_dir, ".paper_grid_KRW_XRP.corrupted_halt")
    validate_state_invariants(state, halt_flag=halt_flag)

    state["last_update"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    temp_path = None
    try:
        fd, temp_path = tempfile.mkstemp(
            dir=state_dir,
            prefix=f".paper_state_{os.getpid()}_",
            suffix=".tmp"
        )
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, STATE_FILE)
        temp_path = None
        global _last_runtime_state
        _last_runtime_state = state

        # 디렉터리 fsync로 파일시스템 메타데이터 영속화 보장
        try:
            dir_fd = os.open(state_dir, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.unlink(temp_path)
            except OSError:
                pass


def get_current_price(market: str = MARKET) -> Optional[float]:
    """업비트 현재가 안전 조회"""
    try:
        url = f"https://api.upbit.com/v1/ticker?markets={market}"
        r = requests.get(url, timeout=3)
        if r.status_code == 200:
            data = r.json()
            if data and len(data) > 0:
                p = float(data[0].get("trade_price", 0.0) or 0.0)
                if math.isfinite(p) and p > 0:
                    return p
        else:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] [WARN] 현재가 API 응답 비정상 (Status: {r.status_code}): {r.text[:100]}")
    except Exception as e:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] 현재가 조회 예외: {e}")
    return None


def process_paper_tick(state: Dict[str, Any], curr_p: float, now_str: Optional[str] = None) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """
    단일 시장 틱(Tick) 순수 상태 전이 함수 (격리 테스트 가능):
    P1: 부수 효과(DB 기록, 텔레그램 알림)를 직접 실행하지 않고 events 리스트로 반환하여
        상태 저장이 성공적으로 완료된 이후에만 원자적으로 커밋할 수 있도록 보장합니다.
    P2: 호가/슬리피지 체결 모델:
        급락 갭다운 시 모든 레벨이 최저가(curr_p)에 일괄 체결되는 왜곡을 방지하고,
        각 그리드 단계의 목표 호가(target_p)를 기준으로 슬리피지 및 틱당 최대 체결 수(MAX_GRID_BUYS_PER_TICK)를 적용합니다.
    """
    if now_str is None:
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    events: List[Dict[str, Any]] = []
    active_grids = state.get("active_grids", [])
    gommapa_tranches = state.get("gommapa_tranches", [])
    mode = state.get("mode", "GRID_SCALPER")
    base_p = float(state.get("base_price", 0.0))

    # -------------------------------------------------------------
    # [MODE: GOMMAPA_ESCAPE] 곰마파 비상 탈출 모드 로직
    # -------------------------------------------------------------
    if mode == "GOMMAPA_ESCAPE":
        # 전체 합산 물량 및 평단가(VWAP) 계산
        grid_vols = sum(g["vol"] for g in active_grids)
        grid_costs = sum(g["cost"] for g in active_grids)
        gom_vols = sum(t["vol"] for t in gommapa_tranches)
        gom_costs = sum(t["cost"] for t in gommapa_tranches)
        total_vols = grid_vols + gom_vols
        total_costs = grid_costs + gom_costs
        combined_vwap = total_costs / total_vols if total_vols > 0 else curr_p
        target_escape_p = combined_vwap * (1.0 + GOMMAPA_ESCAPE_PROFIT_PCT)

        # 1. 곰마파 탈출 조건 체크: 전체 평단가 대비 +3.5% 도달 시 전량 청산!
        if curr_p >= target_escape_p:
            clean_total = round(total_vols, 8)
            ok_sell, clean_vol, sell_err = preflight_check_sell(MARKET, clean_total, curr_p, clean_total)
            if not ok_sell:
                print(f"[WARN] 곰마파 탈출 전량 매도 사전 검증 미달: {sell_err}")
            else:
                exit_p = target_escape_p * (1.0 - SLIPPAGE)
                rev = exit_p * clean_vol * (1.0 - FEE_RATE)
                cost = total_costs  # 매수 시 투입된 실제 현금 비용 (수수료는 volume에서 기차감)
                net_pnl = rev - cost

                state["available_krw"] += rev
                state["realized_pnl"] += net_pnl
                state["total_trades"] += 1
                if net_pnl > 0:
                    state["win_count"] += 1

                state["active_grids"] = []
                state["gommapa_tranches"] = []
                state["mode"] = "GRID_SCALPER"
                state["gommapa_base_p"] = 0.0
                state["base_price"] = curr_p

                events.append({
                    "type": "trade",
                    "payload": {
                        "market": MARKET,
                        "side": "ask",
                        "action": "GOMMAPA_ESCAPE_EXIT",
                        "price": exit_p,
                        "volume": clean_vol,
                        "cost_or_revenue": rev,
                        "pnl": net_pnl,
                        "level": 0,
                        "strategy": "GOMMAPA_ESCAPE",
                        "timestamp": now_str
                    }
                })

                msg = (
                    f"🎉 <b>[곰마파 탈출 대성공!]</b> {MARKET} 전체 평단 +{GOMMAPA_ESCAPE_PROFIT_PCT*100:.1f}% 도달 전량 익절!\n"
                    f"• 매도가: {exit_p:,.0f}원 (전체 평단: {combined_vwap:,.0f}원)\n"
                    f"• 실현 손익: <b>+{net_pnl:,.0f}원</b>\n"
                    f"• 💰 원금 및 수익 회수 완료 (총 평가자산: {state['available_krw']:,.0f}원)\n"
                    f"• 🔄 [그리드 스캘퍼] 1단계로 완전 리셋!"
                )
                events.append({"type": "alert", "msg": msg, "now_str": now_str})

        else:
            # 2. 곰마파 단계별 배수 물타기 체크 (최대 3단계 = 50만 원 소진)
            filled_stages = {t["stage"] for t in gommapa_tranches}
            gom_base = float(state.get("gommapa_base_p", curr_p))

            # 단계별 매수 트리거 계산
            stage_targets = {
                1: gom_base * 0.90,                  # 바닥 대비 -10% (10만 원)
                2: (gom_base * 0.90) * 0.85,         # 추가 -15% (15만 원)
                3: ((gom_base * 0.90) * 0.85) * 0.80 # 추가 -20% (25만 원)
            }

            for s_cfg in GOMMAPA_STAGES:
                stg = s_cfg["stage"]
                amt = s_cfg["amount"]
                t_price = stage_targets[stg]

                if stg not in filled_stages and curr_p <= t_price:
                    ok_buy, clean_amt, buy_err = preflight_check_buy(MARKET, amt, state["available_krw"], FEE_RATE, fee_inclusive=True)
                    if ok_buy:
                        gap_pct = max(0.0, (t_price - curr_p) / t_price)
                        effective_slippage = SLIPPAGE + min(0.005, gap_pct * 0.1)
                        buy_p = t_price * (1.0 + effective_slippage)
                        vol = math.floor(((clean_amt * (1.0 - FEE_RATE)) / buy_p) * 1e8) / 1e8
                        state["available_krw"] -= clean_amt

                        new_gom_tranche = {
                            "stage": stg,
                            "buy_p": buy_p,
                            "vol": vol,
                            "cost": clean_amt,
                            "buy_time": now_str
                        }
                        state["gommapa_tranches"].append(new_gom_tranche)
                        filled_stages.add(stg)

                        # 새 합산 평단가 계산
                        n_grid_vols = sum(g["vol"] for g in active_grids)
                        n_grid_costs = sum(g["cost"] for g in active_grids)
                        n_gom_vols = sum(t["vol"] for t in state["gommapa_tranches"])
                        n_gom_costs = sum(t["cost"] for t in state["gommapa_tranches"])
                        tot_den = n_grid_vols + n_gom_vols
                        new_vwap = (n_grid_costs + n_gom_costs) / tot_den if tot_den > 0 else curr_p

                        events.append({
                            "type": "trade",
                            "payload": {
                                "market": MARKET,
                                "side": "bid",
                                "action": f"GOMMAPA_BUY_STG_{stg}",
                                "price": buy_p,
                                "volume": vol,
                                "cost_or_revenue": clean_amt,
                                "pnl": 0.0,
                                "level": stg,
                                "strategy": "GOMMAPA_ESCAPE",
                                "timestamp": now_str
                            }
                        })

                        msg = (
                            f"🚨 <b>[곰마파 배수 매수 체결]</b> {MARKET} {stg}차 투입!\n"
                            f"• 매수가: {buy_p:,.0f}원 (투입: {clean_amt:,.0f}원)\n"
                            f"• 📉 수직 낙하 새 평단: <b>{new_vwap:,.0f}원</b>\n"
                            f"• 🎯 탈출 목표가: {new_vwap * (1.0 + GOMMAPA_ESCAPE_PROFIT_PCT):,.0f}원 (+3.5%)\n"
                            f"• 잔여 비상금: {state['available_krw']:,.0f}원"
                        )
                        events.append({"type": "alert", "msg": msg, "now_str": now_str})

            # 3. 곰마파 모드 중에도 일부 상위 그리드가 반등하여 스쿼드 바스켓 or 개별 목표가에 닿으면 익절
            # 3-A. 스쿼드 바스켓 익절 체크
            squads_held = {}
            for g in active_grids:
                sq = g.get("squad", (g["lvl"] - 1) // SQUAD_SIZE + 1)
                squads_held.setdefault(sq, []).append(g)

            squads_to_exit = set()
            squad_exit_targets = {}
            for sq, g_list in sorted(squads_held.items()):
                if len(g_list) >= MIN_SQUAD_TRANCHES_FOR_TP:
                    sq_vol = sum(x["vol"] for x in g_list)
                    sq_cost = sum(x["cost"] for x in g_list)
                    if sq_vol > 0:
                        sq_vwap = sq_cost / sq_vol
                        sq_target = sq_vwap * (1.0 + SQUAD_TP_PCT)
                        if curr_p >= sq_target:
                            squads_to_exit.add(sq)
                            squad_exit_targets[sq] = (sq_vwap, sq_target, sq_vol, sq_cost)

            if squads_to_exit:
                surviving_grids = []
                for g in active_grids:
                    sq = g.get("squad", (g["lvl"] - 1) // SQUAD_SIZE + 1)
                    if sq in squads_to_exit:
                        ok_sell, clean_vol, sell_err = preflight_check_sell(MARKET, g["vol"], curr_p, g["vol"])
                        if not ok_sell:
                            print(f"[WARN] 곰마파 중 스쿼드 {sq} 레벨 {g['lvl']} 매도 사전 검증 미달: {sell_err}")
                            surviving_grids.append(g)
                            continue

                        exit_p = curr_p * (1.0 - SLIPPAGE)
                        rev = exit_p * clean_vol * (1.0 - FEE_RATE)
                        cost = g["cost"]
                        net_pnl = rev - cost

                        state["available_krw"] += rev
                        state["realized_pnl"] += net_pnl
                        state["total_trades"] += 1
                        if net_pnl > 0:
                            state["win_count"] += 1

                        events.append({
                            "type": "trade",
                            "payload": {
                                "market": MARKET, "side": "ask", "action": f"SQUAD_{sq}_TP_LVL_{g['lvl']}",
                                "price": exit_p, "volume": clean_vol, "cost_or_revenue": rev, "pnl": net_pnl,
                                "level": g["lvl"], "strategy": f"SQUAD_{sq}_BASKET", "timestamp": now_str
                            }
                        })
                    else:
                        surviving_grids.append(g)

                for sq in sorted(squads_to_exit):
                    sq_vwap, sq_target, sq_vol, sq_cost = squad_exit_targets[sq]
                    msg = (
                        f"📦 <b>[곰마파 중 스쿼드 일괄 익절]</b> {MARKET} 스쿼드 {sq} ({len(squads_held[sq])}개 레벨) 청산!\n"
                        f"• 매도가: {curr_p * (1.0 - SLIPPAGE):,.0f}원 (스쿼드 평단: {sq_vwap:,.0f}원, 목표가: {sq_target:,.0f}원)\n"
                        f"• 💰 스쿼드 원금 및 수익 회수 완료 (잔여 예수금: {state['available_krw']:,.0f}원)"
                    )
                    events.append({"type": "alert", "msg": msg, "now_str": now_str})

                active_grids = surviving_grids

            # 3-B. 잔여 그리드 개별 목표가 익절 체크
            surviving_grids = []
            for g in active_grids:
                if curr_p >= g["tp_p"]:
                    ok_sell, clean_vol, sell_err = preflight_check_sell(MARKET, g["vol"], curr_p, g["vol"])
                    if not ok_sell:
                        print(f"[WARN] 곰마파 중 그리드 {g['lvl']} 매도 사전 검증 미달: {sell_err}")
                        surviving_grids.append(g)
                        continue

                    exit_p = g["tp_p"] * (1.0 - SLIPPAGE)
                    rev = exit_p * clean_vol * (1.0 - FEE_RATE)
                    cost = g["cost"]  # 매수 시 실제 투입된 현금 (수수료는 볼륨에서 차감됨)
                    net_pnl = rev - cost
                    state["available_krw"] += rev
                    state["realized_pnl"] += net_pnl
                    state["total_trades"] += 1
                    if net_pnl > 0:
                        state["win_count"] += 1
                    events.append({
                        "type": "trade",
                        "payload": {
                            "market": MARKET, "side": "ask", "action": f"GRID_TP_LVL_{g['lvl']}",
                            "price": exit_p, "volume": clean_vol, "cost_or_revenue": rev, "pnl": net_pnl,
                            "level": g["lvl"], "strategy": "GRID_SCALPER", "timestamp": now_str
                        }
                    })
                else:
                    surviving_grids.append(g)
            state["active_grids"] = surviving_grids

            # 만약 모든 그리드와 곰마파 물량이 소진되었으면 정상 그리드로 복귀
            if len(state["active_grids"]) == 0 and len(state["gommapa_tranches"]) == 0:
                state["mode"] = "GRID_SCALPER"
                state["gommapa_base_p"] = 0.0
                state["base_price"] = curr_p

    # -------------------------------------------------------------
    # [MODE: GRID_SCALPER] 평상시 그리드 스캘퍼 모드 로직
    # -------------------------------------------------------------
    else:
        # 1-A. [5단계 스쿼드 바스켓 익절 체크]
        squads_held = {}
        for g in active_grids:
            sq = g.get("squad", (g["lvl"] - 1) // SQUAD_SIZE + 1)
            squads_held.setdefault(sq, []).append(g)

        squads_to_exit = set()
        squad_exit_targets = {}
        for sq, g_list in sorted(squads_held.items()):
            if len(g_list) >= MIN_SQUAD_TRANCHES_FOR_TP:
                sq_vol = sum(x["vol"] for x in g_list)
                sq_cost = sum(x["cost"] for x in g_list)
                if sq_vol > 0:
                    sq_vwap = sq_cost / sq_vol
                    sq_target = sq_vwap * (1.0 + SQUAD_TP_PCT)
                    if curr_p >= sq_target:
                        squads_to_exit.add(sq)
                        squad_exit_targets[sq] = (sq_vwap, sq_target, sq_vol, sq_cost)

        if squads_to_exit:
            surviving_grids = []
            for g in active_grids:
                sq = g.get("squad", (g["lvl"] - 1) // SQUAD_SIZE + 1)
                if sq in squads_to_exit:
                    ok_sell, clean_vol, sell_err = preflight_check_sell(MARKET, g["vol"], curr_p, g["vol"])
                    if not ok_sell:
                        print(f"[WARN] 스쿼드 {sq} 레벨 {g['lvl']} 매도 사전 검증 미달: {sell_err}")
                        surviving_grids.append(g)
                        continue

                    exit_p = curr_p * (1.0 - SLIPPAGE)
                    rev = exit_p * clean_vol * (1.0 - FEE_RATE)
                    cost = g["cost"]
                    net_pnl = rev - cost

                    state["available_krw"] += rev
                    state["realized_pnl"] += net_pnl
                    state["total_trades"] += 1
                    if net_pnl > 0:
                        state["win_count"] += 1

                    events.append({
                        "type": "trade",
                        "payload": {
                            "market": MARKET, "side": "ask", "action": f"SQUAD_{sq}_TP_LVL_{g['lvl']}",
                            "price": exit_p, "volume": clean_vol, "cost_or_revenue": rev, "pnl": net_pnl,
                            "level": g["lvl"], "strategy": f"SQUAD_{sq}_BASKET", "timestamp": now_str
                        }
                    })
                else:
                    surviving_grids.append(g)

            for sq in sorted(squads_to_exit):
                sq_vwap, sq_target, sq_vol, sq_cost = squad_exit_targets[sq]
                msg = (
                    f"📦 <b>[스쿼드 바스켓 일괄 익절]</b> {MARKET} 스쿼드 {sq} ({len(squads_held[sq])}개 레벨) 청산 완료!\n"
                    f"• 매도가: {curr_p * (1.0 - SLIPPAGE):,.0f}원 (스쿼드 평단: {sq_vwap:,.0f}원, 목표가: {sq_target:,.0f}원)\n"
                    f"• 💰 스쿼드 원금 및 수익 회수 완료 (잔여 예수금: {state['available_krw']:,.0f}원)"
                )
                events.append({"type": "alert", "msg": msg, "now_str": now_str})

            active_grids = surviving_grids

        # 1-B. 잔여 그리드 개별 익절(+1.2%) 체크 (Dual Exit)
        surviving_grids = []
        for g in active_grids:
            if curr_p >= g["tp_p"]:
                ok_sell, clean_vol, sell_err = preflight_check_sell(MARKET, g["vol"], curr_p, g["vol"])
                if not ok_sell:
                    print(f"[WARN] 그리드 {g['lvl']} 매도 사전 검증 미달: {sell_err}")
                    surviving_grids.append(g)
                    continue

                exit_p = g["tp_p"] * (1.0 - SLIPPAGE)
                rev = exit_p * clean_vol * (1.0 - FEE_RATE)
                cost = g["cost"]  # 매수 시 실제 투입된 현금 (수수료는 볼륨에서 차감됨)
                net_pnl = rev - cost

                state["available_krw"] += rev
                state["realized_pnl"] += net_pnl
                state["total_trades"] += 1
                if net_pnl > 0:
                    state["win_count"] += 1

                events.append({
                    "type": "trade",
                    "payload": {
                        "market": MARKET, "side": "ask", "action": f"GRID_TP_LVL_{g['lvl']}",
                        "price": exit_p, "volume": clean_vol, "cost_or_revenue": rev, "pnl": net_pnl,
                        "level": g["lvl"], "strategy": "GRID_SCALPER", "timestamp": now_str
                    }
                })

                msg = (
                    f"🔵 <b>[가상 익절]</b> {MARKET} 레벨 {g['lvl']} 체결!\n"
                    f"• 매도가: {exit_p:,.0f}원 (진입: {g['buy_p']:,.0f}원)\n"
                    f"• 실현 손익: <b>+{net_pnl:,.0f}원 (+{((exit_p/g['buy_p'])-1)*100:.2f}%)</b>\n"
                    f"• 누적 익절: {state['win_count']}회 | 누적 수익: +{state['realized_pnl']:,.0f}원"
                )
                events.append({"type": "alert", "msg": msg, "now_str": now_str})
            else:
                surviving_grids.append(g)

        state["active_grids"] = surviving_grids

        # 2. 보유 포지션이 0개일 때: 새로운 그리드 사이클 시작 (1단계 즉시 진입)
        if len(state["active_grids"]) == 0:
            state["base_price"] = curr_p
            base_p = curr_p
            curr_grid_cost = sum(g["cost"] for g in state["active_grids"])
            if curr_grid_cost + UNIT_KRW <= GRID_BUDGET:
                ok_buy, clean_amt, buy_err = preflight_check_buy(MARKET, UNIT_KRW, state["available_krw"], FEE_RATE, fee_inclusive=True)
                if ok_buy:
                    buy_p = curr_p * (1.0 + SLIPPAGE)
                    vol = math.floor(((clean_amt * (1.0 - FEE_RATE)) / buy_p) * 1e8) / 1e8
                    state["available_krw"] -= clean_amt
                    tp_p = buy_p * (1.0 + PROFIT_PCT)

                    new_grid = {
                        "lvl": 1,
                        "squad": 1,
                        "buy_p": buy_p,
                        "vol": vol,
                        "cost": clean_amt,
                        "tp_p": tp_p,
                        "buy_time": now_str
                    }
                    state["active_grids"].append(new_grid)
                    events.append({
                        "type": "trade",
                        "payload": {
                            "market": MARKET, "side": "bid", "action": "GRID_BUY_LVL_1",
                            "price": buy_p, "volume": vol, "cost_or_revenue": clean_amt,
                            "pnl": 0.0, "level": 1, "strategy": "GRID_SCALPER", "timestamp": now_str
                        }
                    })
                    msg = (
                        f"🟢 <b>[가상 매수]</b> {MARKET} 레벨 1 신규 진입 (기준가: {base_p:,.0f}원)\n"
                        f"• 매수가: {buy_p:,.0f}원 (투자: {clean_amt:,.0f}원)\n"
                        f"• 목표 매도가: {tp_p:,.0f}원 (+{PROFIT_PCT*100:.1f}%)\n"
                        f"• 보유 그리드: 1개 / 잔여 예수금: {state['available_krw']:,.0f}원"
                    )
                    events.append({"type": "alert", "msg": msg, "now_str": now_str})
                else:
                    print(f"[WARN] 1단계 신규 매수 사전 검증 미달: {buy_err}")

        # 3. 추가 하락 단계 매수 체크 (Level 2 ~ MAX_LEVELS): 기준가 대비 -0.5% * (lvl - 1)
        active_levels = {g["lvl"] for g in state["active_grids"]}
        curr_grid_cost = sum(g["cost"] for g in state["active_grids"])
        for lvl in range(2, MAX_LEVELS + 1):
            target_p = base_p * (1.0 - STEP_PCT * (lvl - 1))
            if lvl not in active_levels and curr_p <= target_p and curr_grid_cost + UNIT_KRW <= GRID_BUDGET:
                ok_buy, clean_amt, buy_err = preflight_check_buy(MARKET, UNIT_KRW, state["available_krw"], FEE_RATE, fee_inclusive=True)
                if ok_buy:
                    # 체결가는 실시간 관측가(curr_p)를 기준으로 체결하여 관측가 초과 매수 왜곡 방지 (백테스트 체결 모델과 일관성 유지)
                    exec_p = min(target_p, curr_p)
                    buy_p = exec_p * (1.0 + SLIPPAGE)

                    vol = math.floor(((clean_amt * (1.0 - FEE_RATE)) / buy_p) * 1e8) / 1e8
                    state["available_krw"] -= clean_amt
                    tp_p = buy_p * (1.0 + PROFIT_PCT)

                    squad_id = (lvl - 1) // SQUAD_SIZE + 1
                    new_grid = {
                        "lvl": lvl,
                        "squad": squad_id,
                        "buy_p": buy_p,
                        "vol": vol,
                        "cost": clean_amt,
                        "tp_p": tp_p,
                        "buy_time": now_str
                    }
                    state["active_grids"].append(new_grid)
                    active_levels.add(lvl)
                    curr_grid_cost += clean_amt

                    events.append({
                        "type": "trade",
                        "payload": {
                            "market": MARKET, "side": "bid", "action": f"GRID_BUY_LVL_{lvl}",
                            "price": buy_p, "volume": vol, "cost_or_revenue": clean_amt,
                            "pnl": 0.0, "level": lvl, "strategy": "GRID_SCALPER", "timestamp": now_str
                        }
                    })

                    msg = (
                        f"🟢 <b>[가상 매수]</b> {MARKET} 레벨 {lvl} 진입\n"
                        f"• 매수가: {buy_p:,.0f}원 (투자: {clean_amt:,.0f}원)\n"
                        f"• 목표 매도가: {tp_p:,.0f}원 (+{PROFIT_PCT*100:.1f}%)\n"
                        f"• 보유 그리드: {len(state['active_grids'])}개 / 잔여 예수금: {state['available_krw']:,.0f}원"
                    )
                    events.append({"type": "alert", "msg": msg, "now_str": now_str})
                else:
                    print(f"[WARN] 레벨 {lvl} 매수 사전 검증 미달: {buy_err}")

        # 4. 곰마파 탈출 모드로 국면 전환 (USE_GOMMAPA = True 설정 시에만 발동)
        if USE_GOMMAPA:
            lvl_max_target_p = base_p * (1.0 - STEP_PCT * (MAX_LEVELS - 1))
            is_max_lvl_filled = any(g.get("lvl", 0) >= MAX_LEVELS for g in state["active_grids"])
            is_price_under_max = (curr_p <= lvl_max_target_p)
            is_all_grids_filled = (len(state["active_grids"]) >= MAX_LEVELS)

            if is_max_lvl_filled or is_price_under_max or is_all_grids_filled:
                state["mode"] = "GOMMAPA_ESCAPE"
                if not state.get("gommapa_base_p"):
                    state["gommapa_base_p"] = min([g["buy_p"] for g in state["active_grids"]], default=lvl_max_target_p)
                if "gommapa_tranches" not in state or state["gommapa_tranches"] is None:
                    state["gommapa_tranches"] = []

                grid_vols = sum(g["vol"] for g in state["active_grids"])
                grid_costs = sum(g["cost"] for g in state["active_grids"])
                grid_vwap = grid_costs / grid_vols if grid_vols > 0 else curr_p

                msg = (
                    f"🚨 <b>[국면 전환: 곰마파 탈출 모드 가동]</b> {MARKET}\n"
                    f"• 그리드 {MAX_LEVELS}단계({MAX_LEVELS * UNIT_KRW:,.0f}원) 전량 체결 도달\n"
                    f"• 그리드 전체 평단: <b>{grid_vwap:,.0f}원</b> (바닥가: {state['gommapa_base_p']:,.0f}원)\n"
                    f"• 🛡️ 잔여 비상금 {state['available_krw']:,.0f}원으로 [곰마파 3단계 배수 물타기] 대기 (-10%, -15%, -20%)\n"
                    f"• 🎯 목표: 전체 평단가 대비 +{GOMMAPA_ESCAPE_PROFIT_PCT*100:.1f}% 반등 시 전량 익절 탈출!"
                )
                events.append({"type": "alert", "msg": msg, "now_str": now_str})

    # -------------------------------------------------------------
    # 3. 자산 평가 및 MDD 실시간 업데이트
    # -------------------------------------------------------------
    pos_val = (
        sum(g["vol"] * curr_p for g in state.get("active_grids", [])) +
        sum(t["vol"] * curr_p for t in state.get("gommapa_tranches", []))
    )
    tot_equity = state["available_krw"] + pos_val
    state["total_equity"] = tot_equity
    state["peak_equity"] = max(state.get("peak_equity", tot_equity), tot_equity)
    dd = (state["peak_equity"] - tot_equity) / state["peak_equity"] * 100.0
    state["max_drawdown_pct"] = max(state.get("max_drawdown_pct", 0.0), dd)
    if events:
        pending = state.setdefault("pending_events", [])
        for ev in events:
            if ev not in pending:
                pending.append(ev)
    return state, events


def send_paper_status_briefing(state: Dict[str, Any], curr_p: float) -> None:
    """텔레그램 정기 상태 브리핑 발송 (대시보드 미표시 대응)"""
    try:
        tot_equity = state.get("total_equity", state.get("available_krw", 0.0))
        init_cap = state.get("initial_capital", INITIAL_CAPITAL)
        ret_pct = ((tot_equity / init_cap) - 1.0) * 100.0
        mode = state.get("mode", "GRID_SCALPER")
        mode_str = f"순수 그리드 스캘퍼 ({MAX_LEVELS}단 / 5단 스쿼드)" if mode == "GRID_SCALPER" else "🚨 곰마파 비상탈출"

        active_grids = state.get("active_grids", [])
        gom_tranches = state.get("gommapa_tranches", [])

        # 스쿼드별 집계
        squads = {}
        for g in active_grids:
            sq = g.get("squad", (g["lvl"] - 1) // SQUAD_SIZE + 1)
            squads.setdefault(sq, []).append(g)

        squad_lines = []
        for sq, g_list in sorted(squads.items()):
            sq_vol = sum(x["vol"] for x in g_list)
            sq_cost = sum(x["cost"] for x in g_list)
            sq_vwap = sq_cost / sq_vol if sq_vol > 0 else 0
            sq_tp = sq_vwap * (1.0 + SQUAD_TP_PCT)
            squad_lines.append(f"  • 스쿼드 {sq} ({len(g_list)}개 레벨): 평단 {sq_vwap:,.0f}원 ➔ 목표 {sq_tp:,.0f}원")

        squad_summary = "\n".join(squad_lines) if squad_lines else "  • 보유 포지션 없음"

        msg = (
            f"📊 <b>[KRW-XRP 모의투자 현황 브리핑 - Model G60]</b>\n"
            f"• 현재가: {curr_p:,.0f}원 | 국면: <b>{mode_str}</b>\n"
            f"• 총 자산: <b>{tot_equity:,.0f}원 ({ret_pct:+.2f}%)</b> (원금: {init_cap:,.0f}원)\n"
            f"• 잔여 예수금: {state['available_krw']:,.0f}원 (안전 현금 {SAFETY_CASH_BUFFER/10000:.0f}만 원 보존)\n"
            f"• 실현 손익: <b>+{state.get('realized_pnl', 0.0):,.0f}원</b> ({state.get('win_count', 0)}회 익절)\n"
            f"• 최고 낙폭(MDD): {state.get('max_drawdown_pct', 0.0):.2f}%\n\n"
            f"📦 <b>스쿼드 바스켓 현황 ({len(active_grids)}개 레벨 보유 / 최대 {MAX_LEVELS}단):</b>\n"
            f"{squad_summary}\n"
        )
        if gom_tranches:
            gom_lines = [f"  • {t['stage']}차 투입: {t['buy_p']:,.0f}원 ({t['cost']:,.0f}원)" for t in gom_tranches]
            msg += f"\n🚨 <b>곰마파 투입 내역:</b>\n" + "\n".join(gom_lines) + "\n"

        msg += f"\n💡 10/31 오전까지 모의 운영 (매일 09:00, 18:00 정기 브리핑)"
        send_telegram_alert(msg)
    except Exception as e:
        print(f"[WARN] 텔레그램 정기 브리핑 실패: {e}")


def run_paper_trading():
    if not acquire_paper_lock():
        print("[CRITICAL] 이미 다른 paper_grid_xrp 프로세스가 실행 중입니다! 중복 실행 방지를 위해 즉시 종료합니다.")
        sys.exit(1)

    init_db()
    state = load_state()

    print("=" * 75)
    print(f"🚀 [가상 매매] KRW-XRP 순수 60단 독립 5단 스쿼드 그리드 가동 (Model G60)")
    print(f" • 시작 자본금:    {state['initial_capital']:,.0f} KRW")
    print(f" • 현재 예수금:    {state['available_krw']:,.0f} KRW (안전 현금: {SAFETY_CASH_BUFFER:,.0f} KRW 보존)")
    print(f" • 1회 주문 단위:  {UNIT_KRW:,.0f} KRW")
    print(f" • 그리드 간격:    {STEP_PCT * 100:.2f}% (최대 {MAX_LEVELS}단계 = {GRID_BUDGET:,.0f}원 운용)")
    print(f" • 개별 짤짤이 익절: +{PROFIT_PCT * 100:.2f}% (수수료 제외 순익 약 +1.10%)")
    print(f" • 스쿼드 바스켓:  5단계 묶음 평단(VWAP) 대비 +{SQUAD_TP_PCT * 100:.2f}% 일괄 익절")
    print(f" • 곰마파 모듈:    {'ON (활성화)' if USE_GOMMAPA else 'OFF (비활성화 - 순수 스쿼드)'}")
    print(f" • 상태 저장:      {STATE_FILE}")
    print(f" • 로그/DB:        {DB_FILE}")
    print("=" * 75)

    send_telegram_alert(
        f"🧪 <b>[가상 매매 Model G60 전환]</b> {MARKET} 순수 60단 5단 독립 스쿼드 그리드\n"
        f"• 가상 원금: {INITIAL_CAPITAL:,.0f}원 (그리드 예산: {GRID_BUDGET:,.0f}원 / {MAX_LEVELS}단계)\n"
        f"• 안전 현금: <b>{SAFETY_CASH_BUFFER:,.0f}원 항시 보존</b> (곰마파 비상 물타기 OFF)\n"
        f"• 그리드: {STEP_PCT*100:.1f}% 하락 매수 / +{PROFIT_PCT*100:.1f}% 개별 짤짤이 익절\n"
        f"• 📦 스쿼드 바스켓: 5단계 묶음 평단 +{SQUAD_TP_PCT*100:.1f}% 도달 시 일괄 익절 (스쿼드 1~12)\n"
        f"• 검증 기간: 10월 31일 오전까지 모의 운영 (Model G60 최우선 모델 검증)"
    )

    last_heartbeat = 0
    last_briefing_slot = ""
    BRIEFING_SCHEDULE = ["09:00", "18:00"]
    briefing_sent_on_startup = False

    try:
        while True:
            try:
                curr_p = get_current_price(MARKET)
                if curr_p is None or curr_p <= 0:
                    time.sleep(POLL_INTERVAL_SEC)
                    continue

                state, events = process_paper_tick(state, curr_p)

                # P1: 1. 상태 JSON을 디스크에 원자적으로 먼저 저장 (fsync 보장)
                save_state(state)

                # P1: 2. 저장이 성공적으로 완료된 후에만 부수 효과(DB 기록 및 텔레그램 알림) 순차 발송
                commit_tick_events(events, state=state)

                # 초기 가동 직후 1회 브리핑 발송
                if not briefing_sent_on_startup:
                    send_paper_status_briefing(state, curr_p)
                    briefing_sent_on_startup = True

                # 정기 스케줄 브리핑 (09:00, 18:00 KST)
                now_hm = datetime.now().strftime("%H:%M")
                today_str = datetime.now().strftime("%Y-%m-%d")
                slot_key = f"{today_str}_{now_hm}"
                if now_hm in BRIEFING_SCHEDULE and last_briefing_slot != slot_key:
                    send_paper_status_briefing(state, curr_p)
                    last_briefing_slot = slot_key

                # 1시간 주기 하트비트 로그
                if time.time() - last_heartbeat >= 3600:
                    last_heartbeat = time.time()
                    tot_equity = state["total_equity"]
                    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    ret_pct = ((tot_equity / state["initial_capital"]) - 1.0) * 100.0
                    curr_mode_kr = "그리드 스캘퍼" if state.get("mode") == "GRID_SCALPER" else "🚨 곰마파 탈출 모드"
                    print(
                        f"[{now_str}] 💓 [가상 봇 하트비트] XRP: {curr_p:,.0f}원 | 모드: {curr_mode_kr} | "
                        f"총 자산: {tot_equity:,.0f}원 ({ret_pct:+.2f}%) | "
                        f"익절: {state['win_count']}회 | 보유 그리드: {len(state.get('active_grids', []))}개 | 곰마파: {len(state.get('gommapa_tranches', []))}개"
                    )

                # 실거래 봇(main.py) 생존 교차 감시
                try:
                    from utils.cross_watchdog import run_cross_watchdog_tick
                    run_cross_watchdog_tick()
                except Exception:
                    pass

            except Exception as e:
                import traceback
                print(f"[{datetime.now().strftime('%H:%M:%S')}] [ERROR] 가상 매매 루프 에러: {e}\n{traceback.format_exc()}")

            time.sleep(POLL_INTERVAL_SEC)
    finally:
        release_paper_lock()


if __name__ == "__main__":
    run_paper_trading()
