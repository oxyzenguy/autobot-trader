import pandas as pd

def get_bollinger_signal(df=None, amount=12000):
    if df is None or len(df) < 20:
        return None
    df = df.copy()
    df['ma20'] = df['close'].rolling(window=20).mean()
    df['stddev'] = df['close'].rolling(window=20).std()
    df['upper'] = df['ma20'] + 2 * df['stddev']
    df['lower'] = df['ma20'] - 2 * df['stddev']

    if df['close'].iloc[-2] < df['lower'].iloc[-2] and df['close'].iloc[-1] > df['lower'].iloc[-1]:
        return {"signal": "buy", "reason": "볼린저 하단 반등 발생", "amount": amount}
    elif df['close'].iloc[-2] > df['upper'].iloc[-2] and df['close'].iloc[-1] < df['upper'].iloc[-1]:
        return {"signal": "sell", "reason": "볼린저 상단 돌파 실패 후 하락", "amount": None}
    return None
