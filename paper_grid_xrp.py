"""
========================================================================================
🚀 [AutoBot Trader] KRW-XRP 그리드 스캘퍼 & 곰마파 비상 탈출 하이브리드 가상 매매 엔진
========================================================================================
[1단계: 평상시 - 그리드 스캘퍼 모드]
- 대상 마켓: KRW-XRP (리플)
- 운용 자본금: 1,000,000 KRW
- 1회 주문 단위: 10,000 KRW
- 그리드 간격: 0.50% 하락 시마다 매수 (최대 50단계 = 500,000원 운용 한도)
- 1회 익절 목표: +1.20% (수수료 제외 순익 약 +1.10% / 1회당 약 +103원)
- 안전 현금 비축: 500,000 KRW (50% 비상 현금 항시 보존)

[2단계: 비상시 - 곰마파 탈출 모드 (Gommapa Escape Mode)]
- 발동 조건: 50단계(50만 원) 전량 체결 시 (XRP가 -25% 대폭락할 경우) 자동 전환!
- 비상금 투입: 잔여 안전 현금 500,000원으로 3단계 배수 물타기 대기
  • 1차: 50단계 바닥 대비 -10% 하락 시 ➔ 100,000원 투입
  • 2차: 1차 가격 대비 -15% 하락 시 ➔ 150,000원 투입
  • 3차: 2차 가격 대비 -20% 하락 시 ➔ 250,000원 투입 (바닥 싹쓸이)
- 탈출 목표: 전체 합산 평단가 대비 +3.5% 기술적 반등 시 전량 익절 탈출!
- 탈출 완료: 현금 100% 회수 후 다시 [그리드 스캘퍼] 1단계로 완전 리셋!
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

from utils.order_executor import preflight_check_buy, preflight_check_sell

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, "paper_grid_state_KRW_XRP.json")
DB_FILE = os.path.join(BASE_DIR, "paper_trade_history.db")

MARKET = "KRW-XRP"
TICKER = "XRP"
INITIAL_CAPITAL = 1_000_000.0  # 100만 원
UNIT_KRW = 10_000.0            # 1회 10,000원
STEP_PCT = 0.005               # 0.5% 간격
PROFIT_PCT = 0.012             # +1.2% 익절 (황금 밸런스)
MAX_LEVELS = 50                # 최대 50레벨 (500,000원 한도 운용)
FEE_RATE = 0.0005              # 업비트 0.05% taker fee
SLIPPAGE = 0.0002              # 0.02% 슬리피지
POLL_INTERVAL_SEC = 5          # 5초 주기 감시

# --- 곰마파 비상 탈출 모드 설정 ---
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


def log_paper_trade(market: str, side: str, action: str, price: float, volume: float, cost_or_revenue: float, pnl: float, level: int, strategy: str = "GRID_SCALPER") -> None:
    """가상 체결 기록 저장"""
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        c.execute('''
            INSERT INTO paper_trades (timestamp, market, side, action, price, volume, cost_or_revenue, pnl, level, strategy)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (now_str, market, side, action, price, volume, cost_or_revenue, pnl, level, strategy))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[ERROR] 가상 체결 DB 저장 실패: {e}")


def load_state() -> Dict[str, Any]:
    """가상 매매 상태 파일 로드 또는 신규 초기화"""
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                s = json.load(f)
                # 하위 호환 필드 보완
                if "mode" not in s:
                    s["mode"] = "GRID_SCALPER"
                if "gommapa_tranches" not in s:
                    s["gommapa_tranches"] = []
                if "gommapa_base_p" not in s:
                    s["gommapa_base_p"] = 0.0
                return s
        except Exception as e:
            print(f"[WARN] 상태 파일 로드 실패({e}), 신규 상태로 초기화합니다.")

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    initial_state = {
        "market": MARKET,
        "strategy": "GRID_SCALPER",
        "mode": "GRID_SCALPER",  # "GRID_SCALPER" or "GOMMAPA_ESCAPE"
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
        "active_grids": [],
        "gommapa_tranches": [],
        "gommapa_base_p": 0.0,
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

    print("=" * 75)
    print(f"🚀 [가상 매매] KRW-XRP 10,000원 그리드 스캘퍼 & 곰마파 비상탈출 가동")
    print(f" • 시작 자본금:    {state['initial_capital']:,.0f} KRW")
    print(f" • 현재 예수금:    {state['available_krw']:,.0f} KRW")
    print(f" • 1회 주문 단위:  {UNIT_KRW:,.0f} KRW")
    print(f" • 그리드 간격:    {STEP_PCT * 100:.2f}% (최대 {MAX_LEVELS}단계 = 50만 원 운용)")
    print(f" • 1회 익절 목표:  +{PROFIT_PCT * 100:.2f}% (수수료 제외 순익 약 +1.10%)")
    print(f" • 곰마파 비상탈출: 50단계 소진 시 잔여 50만 원으로 배수 물타기 ➔ +{GOMMAPA_ESCAPE_PROFIT_PCT*100:.1f}% 전량 탈출")
    print(f" • 상태 저장:      {STATE_FILE}")
    print(f" • 로그/DB:        {DB_FILE}")
    print("=" * 75)

    send_telegram_alert(
        f"🧪 <b>[가상 매매 가동]</b> {MARKET} 그리드 스캘퍼 + 곰마파 탈출 하이브리드\n"
        f"• 가상 원금: {INITIAL_CAPITAL:,.0f}원 (1Unit: {UNIT_KRW:,.0f}원 / 50단계)\n"
        f"• 그리드: {STEP_PCT*100:.1f}% 하락 매수 / +{PROFIT_PCT*100:.1f}% 즉시 익절\n"
        f"• 🛡️ 비상 안전판: 50단계 소진 시 잔여 50만 원 [곰마파 탈출 모드] 자동 전환\n"
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
            gommapa_tranches = state.get("gommapa_tranches", [])
            mode = state.get("mode", "GRID_SCALPER")
            base_p = float(state.get("base_price", 0.0))

            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

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
                    ok_sell, clean_vol, sell_err = preflight_check_sell(MARKET, total_vols, curr_p, total_vols)
                    if not ok_sell:
                        print(f"[WARN] 곰마파 탈출 전량 매도 사전 검증 미달: {sell_err}")
                    else:
                        exit_p = target_escape_p * (1.0 - SLIPPAGE)
                        rev = exit_p * clean_vol * (1.0 - FEE_RATE)
                        cost = total_costs * (1.0 + FEE_RATE)
                        net_pnl = rev - cost

                        state["available_krw"] += rev
                        state["realized_pnl"] += net_pnl
                        state["win_count"] += 1
                        state["total_trades"] += 1
                        state["active_grids"] = []
                        state["gommapa_tranches"] = []
                        state["mode"] = "GRID_SCALPER"
                        state["base_price"] = curr_p

                        log_paper_trade(
                            market=MARKET,
                            side="ask",
                            action="GOMMAPA_ESCAPE_EXIT",
                            price=exit_p,
                            volume=clean_vol,
                            cost_or_revenue=rev,
                            pnl=net_pnl,
                            level=0,
                            strategy="GOMMAPA_ESCAPE"
                        )

                        msg = (
                            f"🎉 <b>[곰마파 탈출 대성공!]</b> {MARKET} 전체 평단 +{GOMMAPA_ESCAPE_PROFIT_PCT*100:.1f}% 도달 전량 익절!\n"
                            f"• 매도가: {exit_p:,.0f}원 (전체 평단: {combined_vwap:,.0f}원)\n"
                            f"• 실현 손익: <b>+{net_pnl:,.0f}원</b>\n"
                            f"• 💰 원금 및 수익 회수 완료 (총 평가자산: {state['available_krw']:,.0f}원)\n"
                            f"• 🔄 [그리드 스캘퍼] 1단계로 완전 리셋!"
                        )
                        print(f"\n[{now_str}] {msg.replace('<b>','').replace('</b>','')}")
                        send_telegram_alert(msg)

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
                            ok_buy, clean_amt, buy_err = preflight_check_buy(MARKET, amt, state["available_krw"], FEE_RATE)
                            if ok_buy:
                                buy_p = t_price * (1.0 + SLIPPAGE)
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
                                new_vwap = (n_grid_costs + n_gom_costs) / (n_grid_vols + n_gom_vols)

                                log_paper_trade(
                                    market=MARKET,
                                    side="bid",
                                    action=f"GOMMAPA_BUY_STG_{stg}",
                                    price=buy_p,
                                    volume=vol,
                                    cost_or_revenue=amt,
                                    pnl=0.0,
                                    level=stg,
                                    strategy="GOMMAPA_ESCAPE"
                                )

                                msg = (
                                    f"🚨 <b>[곰마파 배수 매수 체결]</b> {MARKET} {stg}차 투입!\n"
                                    f"• 매수가: {buy_p:,.0f}원 (투입: {amt:,.0f}원)\n"
                                    f"• 📉 수직 낙하 새 평단: <b>{new_vwap:,.0f}원</b>\n"
                                    f"• 🎯 탈출 목표가: {new_vwap * (1.0 + GOMMAPA_ESCAPE_PROFIT_PCT):,.0f}원 (+3.5%)\n"
                                    f"• 잔여 비상금: {state['available_krw']:,.0f}원"
                                )
                                print(f"\n[{now_str}] {msg.replace('<b>','').replace('</b>','')}")
                                send_telegram_alert(msg)

                    # 3. 곰마파 모드 중에도 일부 상위 그리드가 반등하여 개별 목표가에 닿으면 개별 익절
                    surviving_grids = []
                    for g in active_grids:
                        if curr_p >= g["tp_p"]:
                            exit_p = g["tp_p"] * (1.0 - SLIPPAGE)
                            rev = exit_p * g["vol"] * (1.0 - FEE_RATE)
                            cost = g["cost"] * (1.0 + FEE_RATE)
                            net_pnl = rev - cost
                            state["available_krw"] += rev
                            state["realized_pnl"] += net_pnl
                            state["win_count"] += 1
                            state["total_trades"] += 1
                            log_paper_trade(
                                market=MARKET, side="ask", action=f"GRID_TP_LVL_{g['lvl']}",
                                price=exit_p, volume=g["vol"], cost_or_revenue=rev, pnl=net_pnl, level=g["lvl"]
                            )
                        else:
                            surviving_grids.append(g)
                    state["active_grids"] = surviving_grids

            # -------------------------------------------------------------
            # [MODE: GRID_SCALPER] 평상시 그리드 스캘퍼 모드 로직
            # -------------------------------------------------------------
            else:
                # 1. 개별 그리드 익절(+1.2%) 체크
                surviving_grids = []
                for g in active_grids:
                    if curr_p >= g["tp_p"]:
                        # 실전 대비 매도 사전 검증
                        ok_sell, clean_vol, sell_err = preflight_check_sell(MARKET, g["vol"], curr_p, g["vol"])
                        if not ok_sell:
                            print(f"[WARN] 그리드 {g['lvl']} 매도 사전 검증 미달: {sell_err}")
                            surviving_grids.append(g)
                            continue

                        exit_p = g["tp_p"] * (1.0 - SLIPPAGE)
                        rev = exit_p * clean_vol * (1.0 - FEE_RATE)
                        cost = g["cost"] * (1.0 + FEE_RATE)
                        net_pnl = rev - cost

                        state["available_krw"] += rev
                        state["realized_pnl"] += net_pnl
                        state["win_count"] += 1
                        state["total_trades"] += 1

                        log_paper_trade(
                            market=MARKET, side="ask", action=f"GRID_TP_LVL_{g['lvl']}",
                            price=exit_p, volume=clean_vol, cost_or_revenue=rev, pnl=net_pnl, level=g["lvl"]
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

                # 2. 보유 포지션이 0개일 때: 새로운 그리드 사이클 시작 (1단계 즉시 진입)
                if len(state["active_grids"]) == 0:
                    state["base_price"] = curr_p
                    base_p = curr_p
                    ok_buy, clean_amt, buy_err = preflight_check_buy(MARKET, UNIT_KRW, state["available_krw"], FEE_RATE)
                    if ok_buy:
                        buy_p = curr_p * (1.0 + SLIPPAGE)
                        vol = math.floor(((clean_amt * (1.0 - FEE_RATE)) / buy_p) * 1e8) / 1e8
                        state["available_krw"] -= clean_amt
                        tp_p = buy_p * (1.0 + PROFIT_PCT)

                        new_grid = {
                            "lvl": 1,
                            "buy_p": buy_p,
                            "vol": vol,
                            "cost": clean_amt,
                            "tp_p": tp_p,
                            "buy_time": now_str
                        }
                        state["active_grids"].append(new_grid)
                        log_paper_trade(
                            market=MARKET, side="bid", action="GRID_BUY_LVL_1",
                            price=buy_p, volume=vol, cost_or_revenue=clean_amt, pnl=0.0, level=1
                        )
                        msg = (
                            f"🟢 <b>[가상 매수]</b> {MARKET} 레벨 1 신규 진입 (기준가: {base_p:,.0f}원)\n"
                            f"• 매수가: {buy_p:,.0f}원 (투자: {clean_amt:,.0f}원)\n"
                            f"• 목표 매도가: {tp_p:,.0f}원 (+{PROFIT_PCT*100:.1f}%)\n"
                            f"• 보유 그리드: 1개 / 잔여 예수금: {state['available_krw']:,.0f}원"
                        )
                        print(f"\n[{now_str}] {msg.replace('<b>','').replace('</b>','')}")
                        send_telegram_alert(msg)
                    else:
                        print(f"[WARN] 1단계 신규 매수 사전 검증 미달: {buy_err}")

                # 3. 추가 하락 단계 매수 체크 (Level 2 ~ 50): 기준가 대비 -0.5% * (lvl - 1)
                active_levels = {g["lvl"] for g in state["active_grids"]}
                for lvl in range(2, MAX_LEVELS + 1):
                    target_p = base_p * (1.0 - STEP_PCT * (lvl - 1))
                    if lvl not in active_levels and curr_p <= target_p:
                        ok_buy, clean_amt, buy_err = preflight_check_buy(MARKET, UNIT_KRW, state["available_krw"], FEE_RATE)
                        if ok_buy:
                            buy_p = target_p * (1.0 + SLIPPAGE)
                            vol = math.floor(((clean_amt * (1.0 - FEE_RATE)) / buy_p) * 1e8) / 1e8
                            state["available_krw"] -= clean_amt
                            tp_p = buy_p * (1.0 + PROFIT_PCT)

                            new_grid = {
                                "lvl": lvl,
                                "buy_p": buy_p,
                                "vol": vol,
                                "cost": clean_amt,
                                "tp_p": tp_p,
                                "buy_time": now_str
                            }
                            state["active_grids"].append(new_grid)
                            active_levels.add(lvl)

                            log_paper_trade(
                                market=MARKET, side="bid", action=f"GRID_BUY_LVL_{lvl}",
                                price=buy_p, volume=vol, cost_or_revenue=clean_amt, pnl=0.0, level=lvl
                            )

                            msg = (
                                f"🟢 <b>[가상 매수]</b> {MARKET} 레벨 {lvl} 진입\n"
                                f"• 매수가: {buy_p:,.0f}원 (투자: {clean_amt:,.0f}원)\n"
                                f"• 목표 매도가: {tp_p:,.0f}원 (+{PROFIT_PCT*100:.1f}%)\n"
                                f"• 보유 그리드: {len(state['active_grids'])}개 / 잔여 예수금: {state['available_krw']:,.0f}원"
                            )
                            print(f"\n[{now_str}] {msg.replace('<b>','').replace('</b>','')}")
                            send_telegram_alert(msg)
                        else:
                            print(f"[WARN] 레벨 {lvl} 매수 사전 검증 미달: {buy_err}")

                # 3. 만약 50단계가 전부 체결되었다면 ➔ [곰마파 탈출 모드]로 국면 전환!
                if len(state["active_grids"]) >= MAX_LEVELS:
                    state["mode"] = "GOMMAPA_ESCAPE"
                    state["gommapa_base_p"] = min(g["buy_p"] for g in state["active_grids"])
                    state["gommapa_tranches"] = []

                    grid_vols = sum(g["vol"] for g in state["active_grids"])
                    grid_costs = sum(g["cost"] for g in state["active_grids"])
                    grid_vwap = grid_costs / grid_vols if grid_vols > 0 else curr_p

                    msg = (
                        f"🚨 <b>[국면 전환: 곰마파 탈출 모드 가동]</b> {MARKET}\n"
                        f"• 그리드 50단계(500,000원) 전량 체결 도달 (-25% 폭락 구간)\n"
                        f"• 그리드 50단계 전체 평단: <b>{grid_vwap:,.0f}원</b> (바닥가: {state['gommapa_base_p']:,.0f}원)\n"
                        f"• 🛡️ 잔여 비상금 {state['available_krw']:,.0f}원으로 [곰마파 3단계 배수 물타기] 대기 (-10%, -15%, -20%)\n"
                        f"• 🎯 목표: 전체 평단가 대비 +{GOMMAPA_ESCAPE_PROFIT_PCT*100:.1f}% 반등 시 전량 익절 탈출!"
                    )
                    print(f"\n[{now_str}] {msg.replace('<b>','').replace('</b>','')}")
                    send_telegram_alert(msg)

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

            save_state(state)

            # 1시간 주기 하트비트 로그
            if time.time() - last_heartbeat >= 3600:
                last_heartbeat = time.time()
                ret_pct = ((tot_equity / state["initial_capital"]) - 1.0) * 100.0
                curr_mode_kr = "그리드 스캘퍼" if state.get("mode") == "GRID_SCALPER" else "🚨 곰마파 탈출 모드"
                print(
                    f"[{now_str}] 💓 [가상 봇 하트비트] XRP: {curr_p:,.0f}원 | 모드: {curr_mode_kr} | "
                    f"총 자산: {tot_equity:,.0f}원 ({ret_pct:+.2f}%) | "
                    f"익절: {state['win_count']}회 | 보유 그리드: {len(state.get('active_grids', []))}개 | 곰마파: {len(state.get('gommapa_tranches', []))}개"
                )

        except Exception as e:
            import traceback
            print(f"[{datetime.now().strftime('%H:%M:%S')}] [ERROR] 가상 매매 루프 에러: {e}\n{traceback.format_exc()}")

        time.sleep(POLL_INTERVAL_SEC)


if __name__ == "__main__":
    run_paper_trading()
