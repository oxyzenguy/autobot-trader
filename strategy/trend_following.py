import pandas as pd
import json
import os
from datetime import datetime

GRID_COUNT = 10
STATE_FILE = "grid_state.json"
COIN_UNIT = 0.001  # 매수 수량
MAX_DRAWDOWN = -0.1  # 최대 허용 손실률 (-10%)

def get_signal(df, amount=10000):
    return get_trend_following_signal(df, amount=amount)

def get_trend_following_signal(df=None, amount=15000):
    if df is None or len(df) < 100:
        return None

    df = df.copy()
    short_ma = df["close"].rolling(window=20).mean()
    long_ma = df["close"].rolling(window=100).mean()
    price = df["close"].iloc[-1]

    prev_short = short_ma.iloc[-2]
    curr_short = short_ma.iloc[-1]
    prev_long = long_ma.iloc[-2]
    curr_long = long_ma.iloc[-1]

    print(f"[trend_following] 단기: {curr_short:.2f}, 장기: {curr_long:.2f}, 현재가: {price:.2f}")

    # 추세 전환 + 현재가가 장기이평선 이상일 때만 매수
    if prev_short <= prev_long and curr_short > curr_long and price > curr_long:
        return {
            "signal": "buy",
            "reason": "단기 이평선이 장기 상향 돌파 + 가격 장기선 상회",
            "amount": amount
        }
    elif prev_short >= prev_long and curr_short < curr_long and price < curr_long:
        return {
            "signal": "sell",
            "reason": "단기 이평선이 장기 하향 돌파 + 가격 장기선 하회",
            "amount": None
        }

    return None
