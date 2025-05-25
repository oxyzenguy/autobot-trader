import pyupbit

def get_momentum_signal(df=None, amount=10000):
    if df is None or len(df) < 15:
        return None

    df = df.copy()
    df.loc[:, "rsi"] = calculate_rsi(df["close"])
    curr_rsi = df["rsi"].iloc[-1]

    if curr_rsi > 70:
        return {"signal": "buy", "reason": f"RSI 강세 돌파 ({curr_rsi:.2f})", "amount": amount}
    elif curr_rsi < 30:
        return {"signal": "sell", "reason": f"RSI 약세 하락 ({curr_rsi:.2f})", "amount": None}
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
