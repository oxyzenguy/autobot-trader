import os
import json
import sqlite3
import warnings
warnings.filterwarnings("ignore")
import pyupbit
from datetime import datetime
from config import MIN_KRW_ALERT_THRESHOLD
from utils.analytics import get_total_account_summary, get_all_active_strategies

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(BASE_DIR, "trade_history.db")


def print_status():
    print("\n" + "=" * 70)
    print(f"🚀 AutoBot Trader 실전 매매(Real Trading) 실시간 현황 ({datetime.now().strftime('%Y-%m-%d %H:%M:%S')})")
    print("=" * 70)

    # 1. 전체 계좌 요약
    acc = get_total_account_summary()
    growth_str = f"+{acc['growth_pct']:.2f}%" if acc['growth_pct'] >= 0 else f"{acc['growth_pct']:.2f}%"
    krw_status = "🚨 10만원 미만 경고!" if acc['is_krw_warning'] else "✅ 정상"

    print("\n🏛️ [업비트 전체 계좌 현황]")
    print(f" • 총 평가 자산: {acc['total_equity']:,.0f} KRW (기준 대비 성장률: {growth_str}, {acc['growth_amount']:+,.0f}원)")
    print(f" • 주문가능 예수금: {acc['krw_balance']:,.0f} KRW (묶인 금액: {acc['krw_locked']:,.0f}원) -> 상태: {krw_status}")
    print(f" • 코인 총 평가금액: {acc['total_coin_eval']:,.0f}원 (평가손익: {acc['unrealized_pnl']:+,.0f}원, {acc['coin_pnl_pct']:+.2f}%)")
    print(f" • 전체 투자원금: {acc['total_invested']:,.0f}원")

    print("\n📊 [보유 코인 자산 목록]")
    for c in acc['coins']:
        if c['eval_amount'] > 0:
            avg_s = f"{c['avg_buy_price']:,.0f}원" if c['avg_buy_price'] > 0 else "-"
            cur_s = f"{c['current_price']:,.0f}원"
            pnl_s = f"{c['pnl']:+,.0f}원 ({c['pnl_pct']:+.2f}%)" if c['avg_buy_price'] > 0 else "-"
            print(f" • {c['currency']}: {c['total_balance']:.6f}개 | 평가액: {c['eval_amount']:,.0f}원 ({c['weight_pct']:.1f}%) | 평단: {avg_s} | 시세: {cur_s} | 손익: {pnl_s}")

    # 2. 적용된 전략별 현황
    print("\n" + "-" * 70)
    print("🎯 [현재 적용 전략별 현황]")
    print("-" * 70)

    strategies = get_all_active_strategies()
    for idx, s in enumerate(strategies, 1):
        market = s["market"]
        ticker = s["ticker"]
        ret_s = f"+{s['strategy_return_pct']:.2f}%" if s['strategy_return_pct'] >= 0 else f"{s['strategy_return_pct']:.2f}%"
        pnl_s = f"{s['total_strat_pnl']:+,.0f}원"

        reg_info = s.get("regime_info", {})
        is_bull = reg_info.get("is_bull", False)
        reg_str = reg_info.get("regime_korean", "분석중")
        bot_q = s.get("bot_quantity", 0.0)
        bot_avg = s.get("bot_avg_price", 0.0)
        bot_eval = s.get("bot_eval", 0.0)
        bot_pnl = s.get("bot_unrealized_pnl", 0.0)
        bot_pnl_pct = s.get("bot_pnl_pct", 0.0)
        prot_q = s.get("protected_quantity", 0.0)

        r_state = s.get("runtime_state", {})
        tranches = r_state.get("tranches", [])
        if is_bull:
            from config import BULL_TIME_DCA_INTERVAL_HOURS, MAX_BULL_DCA_STEPS, TRAILING_STOP_TRIGGER, BULL_STOP_LOSS_PCT
            curr_steps = len(tranches) if tranches else (1 if bot_q > 0 else 0)
            sl_price = bot_avg * (1.0 + BULL_STOP_LOSS_PCT) if bot_avg > 0 else 0.0
            ts_active = r_state.get("trailing_stop_active", False)
            ts_status = "🔥 고점 추적 가동 중" if ts_active else f"대기 (+{TRAILING_STOP_TRIGGER*100:.0f}% 도달 시)"
            mode_str = "🚀 상승장 추세모드 (5/20 골든크로스 + 12h 정기적립 + 긴급손절-10%)"
            pyramid_info = f" • 📈 적립/리스크: {curr_steps}/{MAX_BULL_DCA_STEPS}회차 | 12시간 정기적립 (1U) | 🛡️ 긴급손절: {sl_price:,.0f}원({BULL_STOP_LOSS_PCT*100:.1f}%) | 트레일링: {ts_status}"
        else:
            mode_str = "🛡️ 하락장 마틴-매직스플릿 방어 (1-1-2-4 배수, 최대 8 Units / 개별+3% OR 바스켓 익절)"
            pyramid_info = ""

        print(f"\n[전략 {idx}] {s['strategy_name']} ({market})")
        print(f" • 시장 국면: {reg_str} | 현재 모드: {mode_str}")
        print(f" • 200 MA: {reg_info.get('ma200', 0):,.0f}원 ({reg_info.get('distance_ma200_pct', 0):+.2f}%) | 바스켓 익절선: {bot_avg*s['profit_margin']:,.0f}원 (+{(s['profit_margin']-1)*100:.2f}%)")
        print(f" • 배정 원금: {s['initial_capital']:,.0f}원 | 1Unit: {s['unit_krw']:,.0f}원 | 봇 전략 수익률: {ret_s} (순손익: {pnl_s})")
        print(f" • 🤖 봇 운용 포지션: {bot_q:.6f} {ticker} (평단: {bot_avg:,.0f}원 | 평가액: {bot_eval:,.0f}원 | 미실현: {bot_pnl:+,.0f}원, {bot_pnl_pct:+.2f}%)")
        if pyramid_info:
            print(pyramid_info)
        print(f" • 🔒 기존 보유 자산 (안전 보호 중): {prot_q:.6f} {ticker} (계좌 총 잔고: {s.get('account_total_coin_balance', s['coin_balance']):.6f} {ticker})")

        open_orders = s["open_orders"]
        if open_orders:
            print(f" • 미체결 주문 ({len(open_orders)}건 등록 중):")
            for o in open_orders:
                side_str = "🎯 [익절매도]" if o.get("side") == "ask" else "💧 [물타기매수]"
                p = o.get("price", 0.0)
                v = o.get("volume", 0.0)
                cur_p = s["current_price"]
                diff_pct = ((p - cur_p) / cur_p * 100.0) if cur_p > 0 else 0.0
                print(f"    - {side_str} {p:,.0f}원 ({diff_pct:+.2f}%) | 수량: {v:.6f} | 금액: {p*v:,.0f}원")
        else:
            print(" • 미체결 주문: 없음")

    # 3. 최근 실거래 체결 내역
    print("\n" + "-" * 70)
    print("⚡ [최근 실전 체결 내역 (최근 5건)]")
    print("-" * 70)
    if os.path.exists(DB_FILE):
        try:
            conn = sqlite3.connect(DB_FILE)
            cur = conn.cursor()
            cur.execute("""
                SELECT timestamp, ticker, side, price, volume, cost_or_revenue, pnl, action
                FROM trades
                ORDER BY id DESC
                LIMIT 5
            """)
            rows = cur.fetchall()
            conn.close()

            if rows:
                for r in rows:
                    side_s = "매도(ASK)" if r[2] == "ask" else "매수(BID)"
                    pnl_s = f"손익 {r[6]:+,.0f}원" if r[6] else ""
                    print(f" • [{r[0]}] {r[1]} {side_s} {r[7] or ''} - 단가 {r[3]:,.0f}원 | 수량 {r[4]:.6f} | 총액 {r[5]:,.0f}원 {pnl_s}")
            else:
                print(" • 아직 실전 체결 거래 기록이 없습니다.")
        except Exception as e:
            print(f" • 체결 내역 조회 오류: {e}")

    print("=" * 70 + "\n")


if __name__ == "__main__":
    print_status()
