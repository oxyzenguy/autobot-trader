from jinja2 import Environment, FileSystemLoader
import pandas as pd
import os

TRADING_FEE = 0.001

# ✅ 전략 목록 수정: 5가지 RSI 기반 전략
STRATEGIES = [
    "rsi30_70_cross",
    "rsi40_60_cross",
    "rsi30_static",
    "rsi50_cross",
    "rsi_obov_static"
]

def load_strategy_result(strategy):
    path = os.path.join("backtest", f"backtest_{strategy}.csv")
    if not os.path.exists(path):
        print(f"❌ 파일 없음: {path}")
        return None

    df = pd.read_csv(path, names=["time", "ticker", "strategy", "signal", "price"])
    df = df.sort_values("time")

    buys = df[df["signal"] == "buy"].reset_index(drop=True)
    sells = df[df["signal"] == "sell"].reset_index(drop=True)
    min_len = min(len(buys), len(sells))

    if min_len == 0:
        print(f"⚠️ {strategy}: 거래 없음 (buy={len(buys)}, sell={len(sells)})")
        return {
            "strategy": strategy,
            "buy_count": len(buys),
            "sell_count": len(sells),
            "trade_count": 0,
            "cumulative_return": 0,
            "trades": []
        }
    
    trades = pd.DataFrame({
        "buy": buys["price"].values[:min_len],
        "sell": sells["price"].values[:min_len]
    })

    # ✅ 수치형 변환
    trades["buy"] = trades["buy"].astype(float)
    trades["sell"] = trades["sell"].astype(float)

    trades["profit"] = ((trades["sell"] * (1 - TRADING_FEE) - trades["buy"] * (1 + TRADING_FEE)) / (trades["buy"] * (1 + TRADING_FEE))) * 100

    cumulative_return = round(trades["profit"].sum(), 2)

    print(f"✅ {strategy}: buy={len(buys)}, sell={len(sells)}, 거래쌍={min_len}, 누적 수익률={cumulative_return:.2f}%")

    return {
        "strategy": strategy,
        "buy_count": len(buys),
        "sell_count": len(sells),
        "trade_count": min_len,
        "cumulative_return": cumulative_return,
        "trades": trades.to_dict(orient="records")
    }

def generate_combined_report():
    results = []
    for strategy in STRATEGIES:
        result = load_strategy_result(strategy)
        if result:
            results.append(result)

    env = Environment(loader=FileSystemLoader("backtest"))
    template = env.get_template("multi_strategy_report_template.html")

    output = template.render(strategy_results=results)

    output_path = os.path.join("backtest", "combined_report.html")
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(output)

    print(f"\n📄 리포트 저장 완료: {output_path}")

if __name__ == "__main__":
    generate_combined_report()
