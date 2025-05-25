import pandas as pd
import matplotlib.pyplot as plt
import os

TRADING_FEE = 0.001  # 0.1% 수수료

def load_log(strategy_name):
    # 항상 analyze_backtest.py 기준으로 파일을 찾음
    file_path = os.path.join(os.path.dirname(__file__), f"backtest_{strategy_name}.csv")
    df = pd.read_csv(file_path, names=["time", "ticker", "strategy", "signal", "price"])
    df["time"] = pd.to_datetime(df["time"])
    return df

def calculate_mdd(equity_curve):
    peak = equity_curve.cummax()
    drawdown = (equity_curve - peak) / peak
    return drawdown.min() * 100

def plot_signals(df, strategy):
    df_sorted = df.sort_values("time")
    buys = df_sorted[df_sorted["signal"] == "buy"]
    sells = df_sorted[df_sorted["signal"] == "sell"]

    plt.figure(figsize=(14, 6))
    plt.plot(df_sorted["time"], df_sorted["price"], label="Price", color="gray")
    plt.scatter(buys["time"], buys["price"], label="Buy", color="green", marker="^", alpha=0.8)
    plt.scatter(sells["time"], sells["price"], label="Sell", color="red", marker="v", alpha=0.8)

    plt.title(f"{strategy.capitalize()} Strategy Signals")
    plt.xlabel("Time")
    plt.ylabel("Price (KRW)")
    plt.legend()
    plt.grid(True)
    plt.tight_layout()
    plt.show()

def calculate_profit(df, fee=TRADING_FEE):
    df = df.sort_values("time")
    buys = df[df["signal"] == "buy"].reset_index(drop=True)
    sells = df[df["signal"] == "sell"].reset_index(drop=True)

    trade_count = min(len(buys), len(sells))
    trades = pd.DataFrame({
        "buy_time": buys["time"].iloc[:trade_count],
        "buy_price": buys["price"].iloc[:trade_count],
        "sell_time": sells["time"].iloc[:trade_count],
        "sell_price": sells["price"].iloc[:trade_count]
    })

    trades["profit_pct"] = (trades["sell_price"] * (1 - fee) - trades["buy_price"] * (1 + fee)) / (trades["buy_price"] * (1 + fee)) * 100
    trades["cumulative"] = (1 + trades["profit_pct"] / 100).cumprod()
    trades["cum_max"] = trades["cumulative"].cummax()
    trades["drawdown"] = trades["cumulative"] / trades["cum_max"] - 1
    max_dd = trades["drawdown"].min() * 100

    total_profit = trades["profit_pct"].sum()
    print(trades)
    print(f"\n📈 누적 수익률: {total_profit:.2f}%")
    print(f"💥 최대 낙폭(MDD): {max_dd:.2f}%")
    return trades

def compare_strategies(strategies):
    plt.figure(figsize=(14, 6))
    for strategy in strategies:
        df = load_log(strategy)
        trades = calculate_profit(df)
        plt.plot(trades["sell_time"], trades["cumulative"], label=strategy)

    plt.title("Strategy Performance Comparison")
    plt.xlabel("Time")
    plt.ylabel("Cumulative Return")
    plt.legend()
    plt.grid(True)
    plt.tight_layout()
    plt.show()

if __name__ == "__main__":
    strategy = "bollinger"  # 또는 "rsi", "moving_average"
    df = load_log(strategy)
    plot_signals(df, strategy)
    calculate_profit(df)
    # compare_strategies(["bollinger", "rsi", "moving_average"])
