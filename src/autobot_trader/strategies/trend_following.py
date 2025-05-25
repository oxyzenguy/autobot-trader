import pyupbit

def get_trend_following_signal(df=None, amount=15000):
    if df is None or len(df) < 100:
        return None
    short_ma = df["close"].rolling(window=20).mean()
    long_ma = df["close"].rolling(window=100).mean()

    if short_ma.iloc[-1] > long_ma.iloc[-1] and short_ma.iloc[-2] <= long_ma.iloc[-2]:
        return {"signal": "buy", "reason": "단기추세가 장기추세를 상향 돌파", "amount": amount}
    elif short_ma.iloc[-1] < long_ma.iloc[-1] and short_ma.iloc[-2] >= long_ma.iloc[-2]:
        return {"signal": "sell", "reason": "단기추세가 장기추세를 하향 이탈", "amount": None}
    return None