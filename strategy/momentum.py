import pandas as pd
import json
import os
from datetime import datetime

GRID_COUNT = 10
STATE_FILE = "grid_state.json"
COIN_UNIT = 0.001  # 매수 수량
MAX_DRAWDOWN = -0.1  # 최대 허용 손실률 (-10%)

def get_signal(df, amount=10000):
    return get_momentum_signal(df, amount=amount)

def get_momentum_signal(df=None, amount=10000):
    if df is None or len(df) < 15:
        return None

    df = df.copy()
    df.loc[:, "rsi"] = calculate_rsi(df["close"])
    curr_rsi = df["rsi"].iloc[-1]
    prev_rsi = df["rsi"].iloc[-2]

    if prev_rsi < 60 and curr_rsi > 60:
        return {"signal": "buy", "reason": f"RSI 상승 돌파 (전: {prev_rsi:.2f} → 현: {curr_rsi:.2f})", "amount": amount}
    elif prev_rsi > 70 and curr_rsi < 70:
        return {"signal": "sell", "reason": f"RSI 하락 전환 (전: {prev_rsi:.2f} → 현: {curr_rsi:.2f})", "amount": None}
    return None


def calculate_rsi(series, period=14):
    delta = series.diff()
    gain = delta.where(delta > 0, 0)
    loss = -delta.where(delta < 0, 0)
    avg_gain = gain.rolling(window=period).mean()
    avg_loss = loss.rolling(window=period).mean()
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return rsi
