import time
import schedule
import pyupbit
import os
from dotenv import load_dotenv
from datetime import datetime
from inspect import signature

from autobot_trader.strategies.moving_average import get_moving_average_signal
from autobot_trader.strategies.rsi import get_rsi_signal
from autobot_trader.strategies.bollinger import get_bollinger_signal
from autobot_trader.strategies.trend_following import get_trend_following_signal
from autobot_trader.strategies.grid_trading import get_grid_trading_signal
from autobot_trader.strategies.volatility_breakout import get_volatility_breakout_signal
from autobot_trader.strategies.momentum import get_momentum_signal

from autobot_trader.telegram_bot import send_message
from autobot_trader.log_signal import log_signal
from autobot_trader.order_executor import market_buy, market_sell
from autobot_trader.db_logger import log_trade, init_db, get_last_trade_time

# ✅ 환경 변수 및 API 초기화
load_dotenv()
ACCESS_KEY = os.getenv("UPBIT_ACCESS_KEY")
SECRET_KEY = os.getenv("UPBIT_SECRET_KEY")
upbit = pyupbit.Upbit(ACCESS_KEY, SECRET_KEY)
init_db()

# ✅ 전략별 자금 설정 (KRW 단위)
STRATEGY_BUDGETS = {
    "moving_average": 10000,
    "rsi": 8000,
    "bollinger": 12000,
    "trend_following": 15000,
    "grid_trading": 10000,
    "volatility_breakout": 10000,
    "momentum": 10000
}

# ✅ 전략별 사용 OHLCV interval 설정
STRATEGY_INTERVALS = {
    "rsi": "minute1",
    "bollinger": "minute15",
    "moving_average": "minute30",
    "trend_following": "minute60",
    "grid_trading": "minute1",
    "volatility_breakout": "day",
    "momentum": "minute15"
}

# ✅ 전략별 중복 매수 제한 시간 (분 단위)
DUPLICATE_BUY_COOLDOWN = {
    "moving_average": 30,
    "rsi": 10,
    "bollinger": 30,
    "trend_following": 60,
    "grid_trading": 5,
    "volatility_breakout": 1440,  # 1일
    "momentum": 30
}

# ✅ 손절/익절 기준
TAKE_PROFIT = 0.05
STOP_LOSS = -0.03
POSITION_HISTORY = {}

def run_strategy(name, func, ticker):
    print(f"\n⏱️ [{name}] 전략 실행 중... ({ticker})")
    try:
        sig = signature(func)
        if "df" in sig.parameters:
            interval = STRATEGY_INTERVALS.get(name, "day")
            df = pyupbit.get_ohlcv(ticker, interval=interval, count=50)
            if df is None or len(df) < 2:
                print("❌ OHLCV 데이터 부족")
                return
            signal = func(df)
        else:
            signal = func()

        price = pyupbit.get_current_price(ticker)
        if price is None:
            print("❌ 현재가 조회 실패")
            return

        last_time, last_side = get_last_trade_time(ticker, name)
        cooldown = DUPLICATE_BUY_COOLDOWN.get(name, 30)
        time_diff = (datetime.now() - last_time).total_seconds() / 60

        if signal == "buy" and last_side == "buy" and time_diff < cooldown:
            print(f"⛔ {name} 최근 매수({time_diff:.1f}분 전), 재매수 차단")
            return

        if signal == "buy":
            amount = STRATEGY_BUDGETS.get(name, 10000)
            if amount < 5000:
                print("⛔ 최소 주문 금액 미만")
                return
            result = market_buy(ticker, amount)
            if result:
                volume = float(result['executed_volume'])
                log_trade(ticker, "buy", volume, price, name)
                log_signal(name, ticker, signal, price)
                POSITION_HISTORY[name + ticker] = (price, volume)
                send_message(f"📈 <b>[{name}] {ticker} 매수 완료</b>\n<code>{price:,.0f}원</code>")

        elif signal == "sell":
            balance = upbit.get_balance(ticker)
            if balance is None or balance < 0.0001:
                print("⛔ 매도할 잔고 부족")
                return
            key = name + ticker
            buy_price, volume = POSITION_HISTORY.get(key, (None, None))
            if buy_price:
                pnl = (price - buy_price) / buy_price
                if pnl < STOP_LOSS:
                    print(f"📉 [{name}] 손절 조건 실행: {pnl*100:.2f}%")
                elif pnl > TAKE_PROFIT:
                    print(f"💰 [{name}] 익절 조건 실행: {pnl*100:.2f}%")
            result = market_sell(ticker, balance)
            if result:
                log_trade(ticker, "sell", balance, price, name)
                log_signal(name, ticker, signal, price)
                send_message(f"📉 <b>[{name}] {ticker} 매도 완료</b>\n<code>{price:,.0f}원</code>")
                POSITION_HISTORY.pop(key, None)
        else:
            print(f"💤 [{name}] 시그널 없음")

    except Exception as e:
        send_message(f"⚠️ <b>[{name}] 전략 실행 오류 ({ticker})</b>: {e}")

def schedule_strategies():
    print("🛠️ 전략별 실행 주기 스케줄링...")
    strategies = {
        "moving_average": get_moving_average_signal,
        "rsi": get_rsi_signal,
        "bollinger": get_bollinger_signal,
        "trend_following": get_trend_following_signal,
        "grid_trading": get_grid_trading_signal,
        "volatility_breakout": get_volatility_breakout_signal,
        "momentum": get_momentum_signal
    }

    tickers = ["KRW-BTC", "KRW-ETH"]

    for name, func in strategies.items():
        for ticker in tickers:
            if name in ["grid_trading", "rsi"]:
                schedule.every(1).minutes.do(run_strategy, name, func, ticker)
            elif name in ["bollinger", "momentum"]:
                schedule.every(15).minutes.do(run_strategy, name, func, ticker)
            elif name in ["moving_average", "trend_following"]:
                schedule.every(30).minutes.do(run_strategy, name, func, ticker)
            elif name == "volatility_breakout":
                schedule.every().day.at("09:01").do(run_strategy, name, func, ticker)

def main():
    print("🚀 전략 다중 자동매매 루프 시작")
    schedule_strategies()
    while True:
        schedule.run_pending()
        time.sleep(1)

if __name__ == "__main__":
    main()
