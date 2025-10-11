import pyupbit
import pandas as pd
import csv
import os
from datetime import datetime
from autobot_trader.util.log_signal import log_signal

# RSI 전략 불러오기
from rsi_strategies import (
    rsi30_70_cross,
    rsi40_60_cross,
    rsi30_static,
    rsi50_cross,
    rsi_obov_static
)

# 익절 / 손절 조건 (%)
TAKE_PROFIT = 0.01   # 1% 익절
STOP_LOSS = -0.01    # -1% 손절

def backtest_strategy(strategy_func, strategy_name, ticker="KRW-BTC", interval="day", count=1095):
    df = pyupbit.get_ohlcv(ticker, interval=interval, count=count)
    if df is None or df.empty:
        print(f"❌ 데이터 로딩 실패: {ticker}")
        return

    SAVE_DIR = os.path.join(os.getcwd(), "backtest")
    os.makedirs(SAVE_DIR, exist_ok=True)
    LOG_FILE = os.path.join(SAVE_DIR, f"backtest_{strategy_name}.csv")

    with open(LOG_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["time", "ticker", "strategy", "signal", "price"])

    holding = False
    buy_price = 0

    for i in range(1, len(df)):
        current = df.iloc[i]
        current_time = current.name.strftime("%Y-%m-%d %H:%M:%S")
        price = current["close"]

        def write_log(signal):
            with open(LOG_FILE, "a", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow([current_time, ticker, strategy_name, signal, price])

        if holding:
            pnl = (price - buy_price) / buy_price
            if pnl >= TAKE_PROFIT:
                log_signal(strategy_name, ticker, "sell", price, backtest=True)
                write_log("sell")
                print(f"[익절] {current_time} {ticker} {price} 수익률: {pnl*100:.2f}%")
                holding = False
                continue
            elif pnl <= STOP_LOSS:
                log_signal(strategy_name, ticker, "sell", price, backtest=True)
                write_log("sell")
                print(f"[손절] {current_time} {ticker} {price} 수익률: {pnl*100:.2f}%")
                holding = False
                continue

        sliced_df = df.iloc[:i+1]
        try:
            signal = strategy_func(df=sliced_df)
        except Exception as e:
            print(f"⚠️ {strategy_name} 시그널 오류: {e}")
            continue

        if signal == "buy" and not holding:
            buy_price = price
            log_signal(strategy_name, ticker, "buy", price, backtest=True)
            write_log("buy")
            print(f"[매수] {current_time} {ticker} {price}")
            holding = True
        elif signal:
            print(f"[DEBUG] {strategy_name} → 시그널: {signal} at {current_time}")

    # 루프 종료 후 holding 상태면 마지막 종가로 강제 매도
    if holding:
        final_time = df.iloc[-1].name.strftime("%Y-%m-%d %H:%M:%S")
        final_price = df.iloc[-1]["close"]
        log_signal(strategy_name, ticker, "sell", final_price, backtest=True)
        with open(LOG_FILE, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([final_time, ticker, strategy_name, "sell", final_price])
        print(f"[강제정리] {final_time} {ticker} {final_price}")

# 전체 전략 실행
def run_all_backtests():
    strategy_map = {
        "rsi30_70_cross": rsi30_70_cross,
        "rsi40_60_cross": rsi40_60_cross,
        "rsi30_static": rsi30_static,
        "rsi50_cross": rsi50_cross,
        "rsi_obov_static": rsi_obov_static
    }

    for name, func in strategy_map.items():
        print(f"\n=== ✅ {name.upper()} 전략 백테스트 시작 ===")
        backtest_strategy(func, name, ticker="KRW-BTC")

if __name__ == "__main__":
    run_all_backtests()
