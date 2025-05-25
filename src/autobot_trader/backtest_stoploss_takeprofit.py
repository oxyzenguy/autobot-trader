import pyupbit
import pandas as pd
from datetime import datetime
from autobot_trader.log_signal import log_signal

# 백테스트 파라미터
TAKE_PROFIT = 0.05   # 5% 익절
STOP_LOSS = -0.03    # -3% 손절

def backtest_strategy(strategy_func, strategy_name, ticker="KRW-BTC", interval="day", count=365):
    df = pyupbit.get_ohlcv(ticker, interval=interval, count=count)
    if df is None or df.empty:
        print(f"❌ 데이터 로딩 실패: {ticker}")
        return

    holding = False
    buy_price = 0

    for i in range(1, len(df)):
        current = df.iloc[i]
        current_time = current.name.strftime("%Y-%m-%d %H:%M:%S")
        price = current["close"]

        if holding:
            pnl = (price - buy_price) / buy_price
            if pnl >= TAKE_PROFIT:
                log_signal(strategy_name, ticker, "sell", price, backtest=True)
                print(f"[익절] {current_time} {ticker} {price} 수익률: {pnl*100:.2f}%")
                holding = False
                continue
            elif pnl <= STOP_LOSS:
                log_signal(strategy_name, ticker, "sell", price, backtest=True)
                print(f"[손절] {current_time} {ticker} {price} 수익률: {pnl*100:.2f}%")
                holding = False
                continue

        sliced_df = df.iloc[:i+1]
        try:
            signal = strategy_func(df=sliced_df) if "df" in strategy_func.__code__.co_varnames else strategy_func()
        except Exception as e:
            print(f"⚠️ {strategy_name} 시그널 오류: {e}")
            continue

        if signal == "buy" and not holding:
            buy_price = price
            log_signal(strategy_name, ticker, "buy", price, backtest=True)
            print(f"[매수] {current_time} {ticker} {price}")
            holding = True

# 전체 전략 일괄 실행
def run_all_backtests():
    from autobot_trader.strategies.moving_average import get_moving_average_signal
    from autobot_trader.strategies.rsi import get_rsi_signal
    from autobot_trader.strategies.bollinger import get_bollinger_signal
    from autobot_trader.strategies.trend_following import get_trend_following_signal
    from autobot_trader.strategies.grid_trading import get_grid_trading_signal
    from autobot_trader.strategies.volatility_breakout import get_volatility_breakout_signal
    from autobot_trader.strategies.momentum import get_momentum_signal

    strategy_map = {
        "moving_average": get_moving_average_signal,
        "rsi": get_rsi_signal,
        "bollinger": get_bollinger_signal,
        "trend_following": get_trend_following_signal,
        "grid_trading": get_grid_trading_signal,
        "volatility_breakout": get_volatility_breakout_signal,
        "momentum": get_momentum_signal
    }

    for name, func in strategy_map.items():
        print(f"\n=== ✅ {name.upper()} 전략 백테스트 시작 ===")
        backtest_strategy(func, name, ticker="KRW-BTC")

if __name__ == "__main__":
    run_all_backtests()
