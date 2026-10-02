"""
CLI Status tool for XRP Grid Scalper Paper Trading
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
    tot_trades = state.get("total_trades", 0)
    mdd = state.get("max_drawdown_pct", 0.0)
    grids = state.get("active_grids", [])
    base_p = state.get("base_price", 0.0)
    created_at = state.get("created_at", "N/A")
    last_up = state.get("last_update", "N/A")

    ret_pct = ((tot_eq / init_cap) - 1.0) * 100.0

    print("=" * 70)
    print("📊 [KRW-XRP 그리드 스캘퍼] 가상 매매 실시간 현황")
    print("=" * 70)
    print(f" • 시작 일시:       {created_at} (마지막 갱신: {last_up})")
    print(f" • 가상 투자 원금:   {init_cap:,.0f} KRW")
    print(f" • 현재 총 평가자산: {tot_eq:,.0f} KRW ({ret_pct:+.2f}%)")
    print(f" • 잔여 가상 예수금: {avail_krw:,.0f} KRW")
    print(f" • 누적 실현 손익:   {real_pnl:+,.0f} KRW")
    print(f" • 최대 낙폭 (MDD):  {mdd:.2f}%")
    print(f" • 누적 익절 횟수:   {win_cnt}회")
    print(f" • 기준 가격 (Base): {base_p:,.0f} KRW")
    print(f" • 현재 보유 그리드: {len(grids)}개 레벨")
    
    if grids:
        print("\n [현재 보유 중인 그리드 상세]")
        for g in grids:
            print(f"   - 레벨 {g['lvl']:2d}: 진입가 {g['buy_p']:,.0f}원 | 목표 매도가 {g['tp_p']:,.0f}원 (+0.8%) | 수량 {g['vol']:.2f} XRP")

    # DB 최근 체결 기록
    if os.path.exists(DB_FILE):
        try:
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            c.execute("SELECT timestamp, side, action, price, pnl FROM paper_trades ORDER BY id DESC LIMIT 5")
            rows = c.fetchall()
            conn.close()
            if rows:
                print("\n [최근 가상 체결 기록 (최신 5건)]")
                for r in rows:
                    side_str = "🟢 매수" if r[1] == "bid" else "🔵 익절"
                    pnl_str = f" (+{r[4]:,.0f}원)" if r[4] > 0 else ""
                    print(f"   • [{r[0]}] {side_str} {r[2]} | 가격: {r[3]:,.0f}원{pnl_str}")
        except Exception as e:
            print(f"   (체결 기록 조회 예외: {e})")

    print("=" * 70)

if __name__ == "__main__":
    print_status()
