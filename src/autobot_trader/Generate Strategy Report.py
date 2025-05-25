import pandas as pd
import matplotlib.pyplot as plt
import os
from jinja2 import Environment, FileSystemLoader

TRADING_FEE = 0.001

# 🛠️ 백테스트 파일 위치 자동 탐색
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../"))
BACKTEST_DIR = os.path.join(BASE_DIR, "backtest")
REPORT_DIR = BASE_DIR

def load_backtest(strategy_name):
    path = os.path.join(BACKTEST_DIR, f"backtest_{strategy_name}.csv")
    if not os.path.exists(path):
        print(f"❌ 파일 없음: {path}")
        return None
    df = pd.read_csv(path, names=["time", "ticker", "strategy", "signal", "price"])
    df["time"] = pd.to_datetime(df["time"])
    return df

def analyze_trades(df):
    buys = df[df["signal"] == "buy"].reset_index(drop=True)
    sells = df[df["signal"] == "sell"].reset_index(drop=True)
    trade_count = min(len(buys), len(sells))

    trades = pd.DataFrame({
        "buy_time": buys["time"].iloc[:trade_count],
        "buy_price": buys["price"].iloc[:trade_count],
        "sell_time": sells["time"].iloc[:trade_count],
        "sell_price": sells["price"].iloc[:trade_count],
    })
    trades["return_pct"] = ((trades["sell_price"] * (1 - TRADING_FEE) - trades["buy_price"] * (1 + TRADING_FEE)) / (trades["buy_price"] * (1 + TRADING_FEE))) * 100
    trades["cumulative"] = (1 + trades["return_pct"] / 100).cumprod()
    trades["drawdown"] = trades["cumulative"] / trades["cumulative"].cummax() - 1

    total_return = trades["cumulative"].iloc[-1] - 1
    max_dd = trades["drawdown"].min() * 100

    return trades, total_return * 100, max_dd

def plot_performance(trades, strategy):
    plt.figure(figsize=(12, 6))
    plt.plot(trades["sell_time"], trades["cumulative"], label=strategy)
    plt.title(f"{strategy.upper()} 누적 수익률")
    plt.xlabel("Date")
    plt.ylabel("Cumulative Return")
    plt.grid(True)
    plt.legend()
    plt.tight_layout()
    img_path = os.path.join(REPORT_DIR, f"report_{strategy}.png")
    plt.savefig(img_path)
    plt.close()
    return img_path

def render_html_report(strategy, trades, total_return, max_dd, img_path):
    env = Environment(loader=FileSystemLoader(REPORT_DIR))
    template = env.get_template("report_template.html")
    output = template.render(
        strategy=strategy,
        trades=trades.round(2).to_dict(orient="records"),
        total_return=round(total_return, 2),
        max_dd=round(max_dd, 2),
        image_path=os.path.basename(img_path)
    )
    html_path = os.path.join(REPORT_DIR, f"report_{strategy}.html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(output)
    print(f"✅ {html_path} 생성 완료")

def generate_all_reports():
    strategy_list = [
        "moving_average", "rsi", "bollinger",
        "trend_following", "grid_trading",
        "volatility_breakout", "momentum"
    ]
    for strategy in strategy_list:
        df = load_backtest(strategy)
        if df is None or df.empty:
            continue
        trades, total_return, max_dd = analyze_trades(df)
        img_path = plot_performance(trades, strategy)
        render_html_report(strategy, trades, total_return, max_dd, img_path)

if __name__ == "__main__":
    generate_all_reports()
