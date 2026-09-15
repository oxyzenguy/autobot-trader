import time
import json
import warnings
warnings.filterwarnings("ignore")
import pyupbit

print("=" * 75)
print("📡 실시간 가상매매 모니터링 시작 (종료하려면 Ctrl + C를 누르세요)")
print("=" * 75)

while True:
    try:
        parts = []
        for ticker in ["SOL", "ETH"]:
            market = f"KRW-{ticker}"
            with open(f"paper_state_KRW_{ticker}.json", "r") as f:
                d = json.load(f)
            p = pyupbit.get_current_price(market) or 0
            eq = d["krw_balance"] + (d["coin_balance"] * p)
            ret = ((eq - d["initial_capital"]) / d["initial_capital"]) * 100
            parts.append(f"{ticker}: {p:,.0f}원(자산 {eq:,.0f}원, {ret:+.2f}%, 주문 {len(d['open_orders'])}건)")
        
        now = time.strftime("%H:%M:%S")
        print(f"[{now}] " + " | ".join(parts))
    except Exception:
        pass
    time.sleep(5)
