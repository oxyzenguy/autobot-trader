import pandas as pd
import json
import os
from datetime import datetime

GRID_COUNT = 10
STATE_FILE = "grid_state.json"
COIN_UNIT = 0.001  # 매수 수량
MAX_DRAWDOWN = -0.1  # 최대 허용 손실률 (-10%)


def get_signal(df, amount=10000):
    return get_rsi_signal(df, amount=amount)

def get_rsi_signal(df=None, period=14, amount=8000):
    if df is None or len(df) < period + 1:
        return None

    df = df.copy()
    delta = df['close'].diff()
    gain = delta.where(delta > 0, 0)
    loss = -delta.where(delta < 0, 0)

    avg_gain = gain.rolling(window=period).mean()
    avg_loss = loss.rolling(window=period).mean()
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    df['rsi'] = rsi

    prev_rsi = df['rsi'].iloc[-2]
    curr_rsi = df['rsi'].iloc[-1]
    print(f"[RSI] 전 RSI: {prev_rsi:.2f}, 현 RSI: {curr_rsi:.2f}")

    if prev_rsi < 35 and curr_rsi > 35:
        return {
            "signal": "buy",
            "reason": f"RSI 반등 (전: {prev_rsi:.2f} → 현: {curr_rsi:.2f})",
            "amount": amount
        }
    elif prev_rsi > 65 and curr_rsi < 65:
        return {
            "signal": "sell",
            "reason": f"RSI 하락 전환 (전: {prev_rsi:.2f} → 현: {curr_rsi:.2f})",
            "amount": None
        }

    return None