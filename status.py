import os
import json
import sqlite3
import warnings
warnings.filterwarnings("ignore")
import pyupbit
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(BASE_DIR, "trade_history.db")


def print_status():
    print("\n" + "=" * 70)
    print(f"🚀 AutoBot Trader 가상매매 실시간 현황 ({datetime.now().strftime('%Y-%m-%d %H:%M:%S')})")
    print("=" * 70)

    for ticker in ["SOL", "ETH"]:
        market = f"KRW-{ticker}"
        state_file = os.path.join(BASE_DIR, f"paper_state_KRW_{ticker}.json")
        if not os.path.exists(state_file):
            continue

        try:
            with open(state_file, "r", encoding="utf-8") as f:
                state = json.load(f)
        except Exception:
            continue

        cur_price = pyupbit.get_current_price(market) or 0.0
        krw = state.get("krw_balance", 0.0)
        coin = state.get("coin_balance", 0.0)
        eval_coin = coin * cur_price
        total_equity = krw + eval_coin
        init_cap = state.get("initial_capital", 1000000.0)
        ret_pct = ((total_equity - init_cap) / init_cap * 100.0) if init_cap > 0 else 0.0
        pnl = state.get("realized_pnl", 0.0)
        cycles = state.get("completed_cycles", 0)
        orders = state.get("open_orders", [])

        regime = state.get("current_regime", "BULL")
        regime_str = "🟢 상승 국면(BULL)" if regime == "BULL" else "🔴 하락 국면(BEAR)"
        sub_strat = state.get("active_sub_strategy", "TREND")
        strat_name = "5/20 MA 추세추종" if sub_strat == "TREND" else "마틴게일 1-2-3-6 방어"

        from utils.real_balance import get_real_coin_status
        r_stat = get_real_coin_status(market)
        r_bal = r_stat.get("current_balance", 0.0)
        r_eval = r_stat.get("current_eval", 0.0)
        r_cost = r_stat.get("total_cost", 0.0)
        r_avg = r_stat.get("current_avg_price", 0.0)
        r_pnl_pct = r_stat.get("total_pnl_pct", 0.0)

        print(f"\n🪙 [{market}] 현재가: {cur_price:,.0f}원 | {regime_str} | 모드: {strat_name}")
        print(f"   📱 [업비트 앱 실계좌] 총보유: {r_bal:.4f} {ticker} | 평가금액: {r_eval:,.0f}원 | 평단: {r_avg:,.0f}원 (수익률: {r_pnl_pct:+.2f}%)")
        print(f"   🤖 [100만원 모의투자] 총자산: {total_equity:,.0f}원 ({ret_pct:+.2f}%) | 현금: {krw:,.0f}원 | 코인: {coin:.6f} {ticker} ({eval_coin:,.0f}원)")
        print(f"   • 모의투자 실현 손익: {pnl:+,.0f}원 | 완료 사이클: {cycles}회")

        # 미체결 주문
        if orders:
            print(f"   📋 미체결 주문 ({len(orders)}건 등록 중):")
            for o in orders:
                side_str = "🎯 [익절매도]" if o.get("side") == "ask" else f"💧 [물타기 {o.get('units', 1)}배]"
                p = o.get("price", 0.0)
                v = o.get("volume", 0.0)
                diff_pct = ((p - cur_price) / cur_price * 100.0) if cur_price > 0 else 0.0
                print(f"      - {side_str} {p:,.0f}원 ({diff_pct:+.2f}%) | 수량: {v:.6f} | 금액: {p*v:,.0f}원")
        else:
            print("   📋 미체결 주문: 없음")

    # 최근 체결 내역 (DB)
    print("\n" + "-" * 70)
    print("⚡ 최근 체결 거래 내역 (최근 10건)")
    print("-" * 70)
    if os.path.exists(DB_FILE):
        try:
            conn = sqlite3.connect(DB_FILE)
            cursor = conn.cursor()
            cursor.execute("""
                SELECT timestamp, market, action, price, volume, cost_or_revenue, pnl, cycle
                FROM paper_trades
                ORDER BY id DESC
                LIMIT 10
            """)
            rows = cursor.fetchall()
            conn.close()

            if rows:
                print(f"{'체결일시':<20} | {'종목':<8} | {'구분':<16} | {'체결단가':>12} | {'체결금액':>12} | {'실현손익':>10}")
                print("-" * 88)
                for r in rows:
                    t_str, mkt, act, prc, vol, cost, pnl_val, cyc = r
                    prc_str = f"{prc:,.0f}원"
                    cost_str = f"{cost:,.0f}원"
                    pnl_disp = f"{pnl_val:+,.0f}원" if pnl_val != 0 else "-"
                    print(f"{t_str:<20} | {mkt:<8} | {act:<16} | {prc_str:>12} | {cost_str:>12} | {pnl_disp:>10}")
            else:
                print("기록된 체결 거래 내역이 없습니다.")
        except Exception as e:
            print(f"거래 내역 조회 중 오류: {e}")
    else:
        print("거래 DB 파일이 아직 생성되지 않았습니다.")

    print("=" * 70 + "\n")


if __name__ == "__main__":
    print_status()
