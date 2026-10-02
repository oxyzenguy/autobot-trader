"""
========================================================================================
🚀 [AutoBot Trader] KRW-XRP 그리드 스캘퍼 실시간 가상 매매 엔진 (Paper Trading Engine)
========================================================================================
- 대상 마켓: KRW-XRP (리플)
- 운용 자본금: 1,000,000 KRW
- 1회 주문 단위: 5,000 KRW (업비트 최소 주문 기준 극세분할, 총 200회 분할 가능)
- 그리드 간격: 0.5% (하락 시마다 5,000원 분할 매수, 최대 20단계 = 100,000원 운용)
- 익절 목표: +0.8% (업비트 수수료 왕복 0.10% 제외 1회당 순수익 약 +0.7%)
- 안전성: 실제 거래소 주문을 일절 제출하지 않는 100% 무위험 시뮬레이션
- 영속성: paper_grid_state_KRW_XRP.json 및 paper_trade_history.db 에 실시간 저장
========================================================================================
"""

import os
import sys
import time
import json
import sqlite3
import requests
from datetime import datetime
from typing import Dict, List, Any, Optional

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, "paper_grid_state_KRW_XRP.json")
DB_FILE = os.path.join(BASE_DIR, "paper_trade_history.db")

MARKET = "KRW-XRP"
TICKER = "XRP"
INITIAL_CAPITAL = 1_000_000.0  # 100만 원
UNIT_KRW = 10_000.0            # 1회 10,000원
STEP_PCT = 0.005               # 0.5% 간격
PROFIT_PCT = 0.012             # +1.2% 익절 (황금 밸런스 스위트 스팟)
MAX_LEVELS = 50                # 최대 50레벨 (500,000원 한도 운용, 50만 원 안전 현금)
FEE_RATE = 0.0005              # 업비트 0.05% taker fee
SLIPPAGE = 0.0002              # 0.02% 슬리피지
POLL_INTERVAL_SEC = 5          # 5초 주기 감시


def send_telegram_alert(message: str) -> None:
    """텔레그램 알림 발송 (실패 시 무시)"""
    try:
        from utils.bot import send_message
        send_message(message)
    except Exception as e:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] [WARN] 텔레그램 발송 실패: {e}")


def init_db() -> None:
    """가상 매매 전용 SQLite DB 초기화"""
    conn = sqlite3.connect(DB_FILE)
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
    conn.close()


def log_paper_trade(market: str, side: str, action: str, price: float, volume: float, cost_or_revenue: float, pnl: float, level: int) -> None:
    """가상 체결 기록 저장"""
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        c.execute('''
            INSERT INTO paper_trades (timestamp, market, side, action, price, volume, cost_or_revenue, pnl, level, strategy)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (now_str, market, side, action, price, volume, cost_or_revenue, pnl, level, "GRID_SCALPER"))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[ERROR] 가상 체결 DB 저장 실패: {e}")


def load_state() -> Dict[str, Any]:
    """가상 매매 상태 파일 로드 또는 신규 초기화"""
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"[WARN] 상태 파일 로드 실패({e}), 신규 상태로 초기화합니다.")

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    initial_state = {
        "market": MARKET,
        "strategy": "GRID_SCALPER",
        "created_at": now_str,
        "initial_capital": INITIAL_CAPITAL,
        "available_krw": INITIAL_CAPITAL,
        "total_equity": INITIAL_CAPITAL,
        "peak_equity": INITIAL_CAPITAL,
        "max_drawdown_pct": 0.0,
        "realized_pnl": 0.0,
        "win_count": 0,
        "total_trades": 0,
        "base_price": 0.0,
        "active_grids": [],  # list of dicts: {level, buy_p, vol, cost, tp_p, time}
        "last_update": now_str
    }
    save_state(initial_state)
    return initial_state


def save_state(state: Dict[str, Any]) -> None:
    """원자적(Atomic) 상태 파일 저장"""
    state["last_update"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    tmp_path = STATE_FILE + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, STATE_FILE)


def get_current_price(market: str = MARKET) -> Optional[float]:
    """업비트 현재가 안전 조회"""
    try:
        url = f"https://api.upbit.com/v1/ticker?markets={market}"
        r = requests.get(url, timeout=3)
        if r.status_code == 200:
            data = r.json()
            if data and len(data) > 0:
                return float(data[0]["trade_price"])
    except Exception as e:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] 현재가 조회 예외: {e}")
    return None


def run_paper_trading():
    init_db()
    state = load_state()

    print("=" * 70)
    print(f"🚀 [가상 매매] KRW-XRP 10,000원 마이크로 그리드 스캘퍼 가동")
    print(f" • 시작 자본금:    {state['initial_capital']:,.0f} KRW")
    print(f" • 현재 예수금:    {state['available_krw']:,.0f} KRW")
    print(f" • 1회 주문 단위:  {UNIT_KRW:,.0f} KRW (총 {int(INITIAL_CAPITAL // UNIT_KRW)}회 분할)")
    print(f" • 그리드 간격:    {STEP_PCT * 100:.2f}% 하락 시마다 매수 (최대 {MAX_LEVELS}단계)")
    print(f" • 1회 익절 목표:  +{PROFIT_PCT * 100:.2f}% (수수료 제외 순익 약 +1.10%)")
    print(f" • 상태 저장:      {STATE_FILE}")
    print(f" • 로그/DB:        {DB_FILE}")
    print("=" * 70)

    send_telegram_alert(
        f"🧪 <b>[가상 매매 가동]</b> {MARKET} 그리드 스캘퍼 (1.2% 익절형)\n"
        f"• 가상 원금: {INITIAL_CAPITAL:,.0f}원 (1Unit: {UNIT_KRW:,.0f}원 / 50단계)\n"
        f"• 그리드: {STEP_PCT*100:.1f}% 하락 매수 / +{PROFIT_PCT*100:.1f}% 즉시 익절\n"
        f"• 일주일간 무위험 성과 검증 후 실전 투입 여부 결정"
    )

    last_heartbeat = 0

    while True:
        try:
            curr_p = get_current_price(MARKET)
            if curr_p is None or curr_p <= 0:
                time.sleep(POLL_INTERVAL_SEC)
                continue

            active_grids = state.get("active_grids", [])
            base_p = float(state.get("base_price", 0.0))

            # 베이스 가격 초기화 또는 상향 트레일링 (포지션이 0개일 때)
            if len(active_grids) == 0:
                if base_p == 0.0 or curr_p > base_p:
                    state["base_price"] = curr_p
                    base_p = curr_p

            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            # -------------------------------------------------------------
            # 1. 익절(Take Profit) 체크: 체결가 대비 +0.8% 도달 시 매도
            # -------------------------------------------------------------
            surviving_grids = []
            for g in active_grids:
                if curr_p >= g["tp_p"]:
                    # 익절 체결!
                    exit_p = g["tp_p"] * (1 - SLIPPAGE)
                    rev = exit_p * g["vol"] * (1 - FEE_RATE)
                    cost = g["cost"] * (1 + FEE_RATE)
                    net_pnl = rev - cost

                    state["available_krw"] += rev
                    state["realized_pnl"] += net_pnl
                    state["win_count"] += 1
                    state["total_trades"] += 1

                    log_paper_trade(
                        market=MARKET,
                        side="ask",
                        action=f"GRID_TP_LVL_{g['lvl']}",
                        price=exit_p,
                        volume=g["vol"],
                        cost_or_revenue=rev,
                        pnl=net_pnl,
                        level=g["lvl"]
                    )

                    msg = (
                        f"🔵 <b>[가상 익절]</b> {MARKET} 레벨 {g['lvl']} 체결!\n"
                        f"• 매도가: {exit_p:,.0f}원 (진입: {g['buy_p']:,.0f}원)\n"
                        f"• 실현 손익: <b>+{net_pnl:,.0f}원 (+{((exit_p/g['buy_p'])-1)*100:.2f}%)</b>\n"
                        f"• 누적 익절: {state['win_count']}회 | 누적 수익: +{state['realized_pnl']:,.0f}원"
                    )
                    print(f"\n[{now_str}] {msg.replace('<b>','').replace('</b>','')}")
                    send_telegram_alert(msg)
                else:
                    surviving_grids.append(g)

            state["active_grids"] = surviving_grids

            # 모든 그리드가 청산되었으면 기준 가격 재설정
            if len(state["active_grids"]) == 0:
                state["base_price"] = curr_p
                base_p = curr_p

            # -------------------------------------------------------------
            # 2. 매수(Buy) 체크: 기준가 대비 -0.5% * level 도달 시 5,000원 매수
            # -------------------------------------------------------------
            active_levels = {g["lvl"] for g in state["active_grids"]}
            for lvl in range(1, MAX_LEVELS + 1):
                target_p = base_p * (1.0 - STEP_PCT * lvl)
                if lvl not in active_levels and curr_p <= target_p:
                    # 매수 조건 충족!
                    if state["available_krw"] >= UNIT_KRW * (1 + FEE_RATE):
                        buy_p = target_p * (1 + SLIPPAGE)
                        vol = (UNIT_KRW * (1 - FEE_RATE)) / buy_p
                        state["available_krw"] -= UNIT_KRW
                        tp_p = buy_p * (1.0 + PROFIT_PCT)

                        new_grid = {
                            "lvl": lvl,
                            "buy_p": buy_p,
                            "vol": vol,
                            "cost": UNIT_KRW,
                            "tp_p": tp_p,
                            "buy_time": now_str
                        }
                        state["active_grids"].append(new_grid)
                        active_levels.add(lvl)

                        log_paper_trade(
                            market=MARKET,
                            side="bid",
                            action=f"GRID_BUY_LVL_{lvl}",
                            price=buy_p,
                            volume=vol,
                            cost_or_revenue=UNIT_KRW,
                            pnl=0.0,
                            level=lvl
                        )

                        msg = (
                            f"🟢 <b>[가상 매수]</b> {MARKET} 레벨 {lvl} 진입\n"
                            f"• 매수가: {buy_p:,.0f}원 (투자: {UNIT_KRW:,.0f}원)\n"
                            f"• 목표 매도가: {tp_p:,.0f}원 (+{PROFIT_PCT*100:.1f}%)\n"
                            f"• 보유 그리드: {len(state['active_grids'])}개 / 잔여 예수금: {state['available_krw']:,.0f}원"
                        )
                        print(f"\n[{now_str}] {msg.replace('<b>','').replace('</b>','')}")
                        send_telegram_alert(msg)

            # -------------------------------------------------------------
            # 3. 자산 평가 및 MDD 업데이트
            # -------------------------------------------------------------
            pos_val = sum(g["vol"] * curr_p for g in state["active_grids"])
            tot_equity = state["available_krw"] + pos_val
            state["total_equity"] = tot_equity
            state["peak_equity"] = max(state.get("peak_equity", tot_equity), tot_equity)
            dd = (state["peak_equity"] - tot_equity) / state["peak_equity"] * 100.0
            state["max_drawdown_pct"] = max(state.get("max_drawdown_pct", 0.0), dd)

            save_state(state)

            # 1시간 주기 하트비트 로그
            if time.time() - last_heartbeat >= 3600:
                last_heartbeat = time.time()
                ret_pct = ((tot_equity / state["initial_capital"]) - 1.0) * 100.0
                print(
                    f"[{now_str}] 💓 [가상 봇 하트비트] XRP: {curr_p:,.0f}원 | "
                    f"총 평가자산: {tot_equity:,.0f}원 ({ret_pct:+.2f}%) | "
                    f"익절: {state['win_count']}회 | 보유: {len(state['active_grids'])}개 레벨"
                )

        except Exception as e:
            import traceback
            print(f"[{datetime.now().strftime('%H:%M:%S')}] [ERROR] 가상 매매 루프 에러: {e}\n{traceback.format_exc()}")

        time.sleep(POLL_INTERVAL_SEC)


if __name__ == "__main__":
    run_paper_trading()
