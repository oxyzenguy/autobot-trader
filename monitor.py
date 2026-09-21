import time
import warnings
warnings.filterwarnings("ignore")
from utils.analytics import get_total_account_summary, get_all_active_strategies

print("=" * 75)
print("📡 실전 매매(Real Trading) 실시간 모니터링 시작 (종료: Ctrl + C)")
print("=" * 75)

while True:
    try:
        acc = get_total_account_summary()
        strats = get_all_active_strategies()

        krw_warn = "🚨[예수금부족!]" if acc["is_krw_warning"] else ""
        acc_part = f"총자산: {acc['total_equity']:,.0f}원({acc['growth_pct']:+.2f}%) | 예수금: {acc['krw_balance']:,.0f}원 {krw_warn}"

        strat_parts = []
        for s in strats:
            strat_parts.append(f"{s['ticker']}: {s['current_price']:,.0f}원(수익률: {s['strategy_return_pct']:+.2f}%, 미체결: {len(s['open_orders'])}건)")

        now = time.strftime("%H:%M:%S")
        print(f"[{now}] {acc_part} | " + " | ".join(strat_parts))
    except Exception as e:
        print(f"모니터링 오류: {e}")
    time.sleep(5)
