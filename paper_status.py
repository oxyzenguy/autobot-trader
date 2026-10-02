"""
CLI Status tool for XRP Grid Scalper & Gommapa Paper Trading
Usage: ./venv/bin/python3 paper_status.py
"""

import os
import json
import sqlite3
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, "paper_grid_state_KRW_XRP.json")
DB_FILE = os.path.join(BASE_DIR, "paper_trade_history.db")

def print_status():
    if not os.path.exists(STATE_FILE):
        print("[!] 아직 가상 매매 상태 파일(paper_grid_state_KRW_XRP.json)이 없습니다.")
        return

    with open(STATE_FILE, "r", encoding="utf-8") as f:
        state = json.load(f)

    init_cap = state.get("initial_capital", 1_000_000.0)
    tot_eq = state.get("total_equity", init_cap)
    avail_krw = state.get("available_krw", init_cap)
    real_pnl = state.get("realized_pnl", 0.0)
    win_cnt = state.get("win_count", 0)
    mdd = state.get("max_drawdown_pct", 0.0)
    grids = state.get("active_grids", [])
    gom_tranches = state.get("gommapa_tranches", [])
    mode = state.get("mode", "GRID_SCALPER")
    base_p = state.get("base_price", 0.0)
    created_at = state.get("created_at", "N/A")
    last_up = state.get("last_update", "N/A")

    ret_pct = ((tot_eq / init_cap) - 1.0) * 100.0

    print("=" * 75)
    print("📊 [KRW-XRP 그리드 스캘퍼 & 곰마파] 가상 매매 실시간 현황")
    print("=" * 75)
    mode_str = "🟢 [정상] 그리드 스캘퍼 모드 (0.5% 매수 / +1.2% 익절)" if mode == "GRID_SCALPER" else "🚨 [비상] 곰마파 비상 탈출 모드 가동 중"
    print(f" • 현재 운용 모드:   {mode_str}")
    print(f" • 시작 일시:       {created_at} (마지막 갱신: {last_up})")
    print(f" • 가상 투자 원금:   {init_cap:,.0f} KRW")
    print(f" • 현재 총 평가자산: {tot_eq:,.0f} KRW ({ret_pct:+.2f}%)")
    print(f" • 잔여 가상 예수금: {avail_krw:,.0f} KRW")
    print(f" • 누적 실현 손익:   {real_pnl:+,.0f} KRW")
    print(f" • 최대 낙폭 (MDD):  {mdd:.2f}%")
    print(f" • 누적 익절 횟수:   {win_cnt}회")
    print(f" • 기준 가격 (Base): {base_p:,.0f} KRW")
    print(f" • 보유 포지션:     그리드 {len(grids)}단계 / 곰마파 {len(gom_tranches)}단계")

    if mode == "GOMMAPA_ESCAPE":
        tot_vols = sum(g["vol"] for g in grids) + sum(t["vol"] for t in gom_tranches)
        tot_costs = sum(g["cost"] for g in grids) + sum(t["cost"] for t in gom_tranches)
        vwap = tot_costs / tot_vols if tot_vols > 0 else 0.0
        escape_target = vwap * 1.035
        print("\n [🚨 곰마파 비상 탈출 모드 상세]")
        print(f"   - 전체 합산 평단가: {vwap:,.0f}원")
        print(f"   - 전량 탈출 목표가: {escape_target:,.0f}원 (+3.5% 기술적 반등 시 전량 익절)")
        if gom_tranches:
            print("   - 투입된 곰마파 배수 물타기:")
            for t in gom_tranches:
                print(f"     • {t['stage']}차 투입: 매수가 {t['buy_p']:,.0f}원 | 금액 {t['cost']:,.0f}원 | {t['buy_time']}")

    if grids:
        print("\n [현재 보유 중인 그리드 상세 (최근 5개)]")
        for g in grids[-5:]:
            print(f"   - 레벨 {g['lvl']:2d}: 진입가 {g['buy_p']:,.0f}원 | 목표 매도가 {g['tp_p']:,.0f}원 (+1.2%) | 수량 {g['vol']:.2f} XRP")

    # DB 최근 체결 기록
    if os.path.exists(DB_FILE):
        try:
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            c.execute("SELECT timestamp, side, action, price, pnl, strategy FROM paper_trades ORDER BY id DESC LIMIT 5")
            rows = c.fetchall()
            conn.close()
            if rows:
                print("\n [최근 가상 체결 기록 (최신 5건)]")
                for r in rows:
                    side_str = "🟢 매수" if r[1] == "bid" else "🔵 익절"
                    pnl_str = f" (+{r[4]:,.0f}원)" if r[4] > 0 else ""
                    strat_str = f"[{r[5]}] " if len(r) > 5 and r[5] else ""
                    print(f"   • [{r[0]}] {strat_str}{side_str} {r[2]} | 가격: {r[3]:,.0f}원{pnl_str}")
        except Exception as e:
            print(f"   (체결 기록 조회 예외: {e})")

    print("=" * 75)

if __name__ == "__main__":
    print_status()
