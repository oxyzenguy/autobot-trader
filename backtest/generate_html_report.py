from jinja2 import Environment, FileSystemLoader
import pandas as pd
import os

TRADING_FEE = 0.001

def generate_report(strategy):
    df = pd.read_csv(f"backtest_{strategy}.csv", names=["time", "ticker", "strategy", "signal", "price"])
    df = df.sort_values("time")
    buys = df[df["signal"] == "buy"].reset_index(drop=True)
    sells = df[df["signal"] == "sell"].reset_index(drop=True)

    trades = pd.DataFrame({
        "buy": buys["price"].values[:len(sells)],
        "sell": sells["price"].values[:len(buys)]
    })
    trades["profit"] = ((trades["sell"] * (1 - TRADING_FEE) - trades["buy"] * (1 + TRADING_FEE)) / (trades["buy"] * (1 + TRADING_FEE))) * 100
    trades["profit"] = trades["profit"].round(2)

    cumulative_return = trades["profit"].sum()

    env = Environment(loader=FileSystemLoader("."))
    template = env.get_template("report_template.html")

    output = template.render(
        strategy=strategy,
        cumulative_return=round(cumulative_return, 2),
        trades=trades.to_dict(orient="records")
    )

    with open(f"report_{strategy}.html", "w", encoding="utf-8") as f:
        f.write(output)
    print(f"✅ HTML 보고서 생성 완료: report_{strategy}.html")

if __name__ == "__main__":
    generate_report("bollinger")
