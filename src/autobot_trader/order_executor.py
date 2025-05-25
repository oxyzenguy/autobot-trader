# src/autobot_trader/order_executor.py
import pyupbit
import os
from dotenv import load_dotenv

load_dotenv()

ACCESS_KEY = os.getenv("UPBIT_ACCESS_KEY")
SECRET_KEY = os.getenv("UPBIT_SECRET_KEY")

upbit = pyupbit.Upbit(ACCESS_KEY, SECRET_KEY)

def market_buy(ticker, amount_krw):
    try:
        result = upbit.buy_market_order(ticker, amount_krw)
        print(f"✅ 매수 실행: {ticker} - {amount_krw} KRW")
        return result
    except Exception as e:
        print(f"❌ 매수 실패: {e}")
        return None

def market_sell(ticker, volume):
    try:
        result = upbit.sell_market_order(ticker, volume)
        print(f"✅ 매도 실행: {ticker} - {volume} 개")
        return result
    except Exception as e:
        print(f"❌ 매도 실패: {e}")
        return None
