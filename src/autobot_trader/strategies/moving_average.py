import pyupbit

def get_moving_average_signal(df=None, amount=10000):
    if df is None or len(df) < 30:
        return None
    ma5 = df["close"].rolling(window=5).mean()
    ma20 = df["close"].rolling(window=20).mean()

    if ma5.iloc[-2] < ma20.iloc[-2] and ma5.iloc[-1] > ma20.iloc[-1]:
        return {"signal": "buy", "reason": "5일선이 20일선을 상향 돌파", "amount": amount}
    elif ma5.iloc[-2] > ma20.iloc[-2] and ma5.iloc[-1] < ma20.iloc[-1]:
        return {"signal": "sell", "reason": "5일선이 20일선을 하향 이탈", "amount": None}
    return None