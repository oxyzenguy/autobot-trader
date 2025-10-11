import pandas as pd
import json
import os
from datetime import datetime

GRID_COUNT = 10
STATE_FILE = "grid_state.json"
COIN_UNIT = 0.001  # 매수 수량
MAX_DRAWDOWN = -0.1  # 최대 허용 손실률 (-10%)


def get_signal(df, amount=10000):
    return get_moving_average_signal(df, amount=amount)

def get_moving_average_signal(df=None, amount=10000):
    if df is None or len(df) < 30:
        return None

    df = df.copy()
    ma5 = df["close"].rolling(window=5).mean()
    ma20 = df["close"].rolling(window=20).mean()
    price = df["close"].iloc[-1]
    prev_ma5 = ma5.iloc[-2]
    curr_ma5 = ma5.iloc[-1]
    prev_ma20 = ma20.iloc[-2]
    curr_ma20 = ma20.iloc[-1]

    print(f"[moving_average] MA5: {curr_ma5:.2f}, MA20: {curr_ma20:.2f}, Price: {price:.2f}")

    # 골든크로스 + 현재가 이평선 위
    if prev_ma5 < prev_ma20 and curr_ma5 > curr_ma20 and price > curr_ma5:
        return {
            "signal": "buy",
            "reason": "5일선이 20일선을 상향 돌파 + 현재가 지지",
            "amount": amount
        }
    elif prev_ma5 > prev_ma20 and curr_ma5 < curr_ma20 and price < curr_ma5:
        return {
            "signal": "sell",
            "reason": "5일선이 20일선을 하향 이탈 + 현재가 하회",
            "amount": None
        }
    return None
