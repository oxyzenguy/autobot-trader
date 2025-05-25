import pyupbit
import pandas as pd
from datetime import datetime, timedelta
from autobot_trader.strategies.moving_average import get_moving_average_signal
from autobot_trader.strategies.rsi import get_rsi_signal
from autobot_trader.strategies.bollinger import get_bollinger_signal
from autobot_trader.log_signal import log_signal

# 백테스트 대상 코인
TICKERS = ["KRW-BTC", "KRW-ETH", "KRW-TRX", "KRW-SOL"]
# 1년치 데이터
TO = datetime.now()
FROM = TO - timedelta(days=365)

def fetch_ohlcv(ticker):
    return pyupbit.get_ohlcv(ticker, interval="day", to=TO.strftime("%Y-%m-%d"))

def simulate_strategy(strategy_name, strategy_func):
    for ticker in TICKERS:
        df = fetch_ohlcv(ticker)
        if df is None:
            print(f"❌ {ticker} 데이터 없음")
            continue

        for i in range(30, len(df)):
            sliced = df.iloc[:i].copy()
            price = df.iloc[i]["close"]
            signal = strategy_func(sliced)

            if signal in ["buy", "sell"]:
                log_signal(strategy_name, ticker, signal, price, backtest=True)

if __name__ == "__main__":
    simulate_strategy("moving_average", get_moving_average_signal)
    simulate_strategy("rsi", get_rsi_signal)
    simulate_strategy("bollinger", get_bollinger_signal)
