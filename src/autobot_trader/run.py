import time
import schedule
import pyupbit
import os
from dotenv import load_dotenv
from datetime import datetime

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

# ✅ 환경변수 로드 및 Upbit 연결
load_dotenv()
ACCESS_KEY = os.getenv("UPBIT_ACCESS_KEY")
SECRET_KEY = os.getenv("UPBIT_SECRET_KEY")
upbit = pyupbit.Upbit(ACCESS_KEY, SECRET_KEY)

# ✅ DB 초기화
init_db()

# ✅ 전략 실행 함수
def run_strategy(name, func):
    print(f"⏱️ [{name}] 전략 실행 중...")
    try:
        signal = func()
        ticker = "KRW-BTC"
        price = pyupbit.get_current_price(ticker)

        if price is None:
            print("❌ 가격 조회 실패")
            return

        last_time, last_side = get_last_trade_time(ticker, name)

        # 연속매매 방지: 최근 매수 후 10분 이내면 무시
        if signal == "buy" and last_side == "buy":
            time_diff = (datetime.now() - last_time).total_seconds() / 60
            if time_diff < 10:
                print(f"⛔ 최근 매수({time_diff:.1f}분 전), 재매수 금지")
                return

        if signal == "buy":
            amount = 10000
            if amount < 5000:
                print("⛔ 최소 주문 금액 미만 (5,000원)")
                return

            try:
                result = market_buy(ticker, amount)
                if result:
                    volume = float(result['executed_volume'])
                    log_trade(ticker, "buy", volume, price, name)
                    log_signal(name, ticker, signal, price)
                    send_message(f"📈 <b>[{name}] 매수 완료</b>\n가격: <code>{price:,.0f}원</code>")
            except Exception as e:
                send_message(f"❌ <b>[{name}] 매수 에러</b>: {e}")

        elif signal == "sell":
            balance = upbit.get_balance(ticker)
            if balance is None or balance < 0.0001:
                print("⛔ 매도할 잔고 부족")
                return

            try:
                result = market_sell(ticker, balance)
                if result:
                    log_trade(ticker, "sell", balance, price, name)
                    log_signal(name, ticker, signal, price)
                    send_message(f"📉 <b>[{name}] 매도 완료</b>\n가격: <code>{price:,.0f}원</code>")
            except Exception as e:
                send_message(f"❌ <b>[{name}] 매도 에러</b>: {e}")
        else:
            print(f"💤 [{name}] 시그널 없음")

    except Exception as e:
        send_message(f"⚠️ <b>[{name}] 전략 실행 오류</b>: {e}")

# ✅ 메인 실행 루프
def main():
    print("🚀 전략 다중화 자동매매 루프 시작 (1분마다 실행)")

    # 기존 전략
    schedule.every(1).minutes.do(lambda: run_strategy("moving_average", get_moving_average_signal))
    schedule.every(1).minutes.do(lambda: run_strategy("rsi", get_rsi_signal))
    schedule.every(1).minutes.do(lambda: run_strategy("bollinger", get_bollinger_signal))

    # 추가 전략
    schedule.every(1).minutes.do(lambda: run_strategy("trend_following", get_trend_following_signal))
    schedule.every(1).minutes.do(lambda: run_strategy("grid_trading", get_grid_trading_signal))
    schedule.every(1).minutes.do(lambda: run_strategy("volatility_breakout", get_volatility_breakout_signal))
    schedule.every(1).minutes.do(lambda: run_strategy("momentum", get_momentum_signal))
    
    while True:
        schedule.run_pending()
        time.sleep(1)

if __name__ == "__main__":
    main()
