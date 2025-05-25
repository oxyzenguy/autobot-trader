import pandas as pd
from autobot_trader.telegram_bot import send_message

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

    if prev_rsi < 30 and curr_rsi > 30:
        return {
            "signal": "buy",
            "reason": f"RSI 반등 (전: {prev_rsi:.2f} → 현: {curr_rsi:.2f})",
            "amount": amount
        }
    elif prev_rsi > 70 and curr_rsi < 70:
        return {
            "signal": "sell",
            "reason": f"RSI 하락 (전: {prev_rsi:.2f} → 현: {curr_rsi:.2f})",
            "amount": None
        }

    return None