import pandas as pd

def get_volatility_breakout_signal(df: pd.DataFrame, k: float = 0.5, amount=10000):
    if df is None or len(df) < 2:
        return None
    yesterday = df.iloc[-2]
    today = df.iloc[-1]
    target_price = yesterday["close"] + (yesterday["high"] - yesterday["low"]) * k
    current_price = today["close"]

    print(f"[volatility_breakout] Target: {target_price:.2f}, Current: {current_price:.2f}")

    if current_price > target_price:
        return {"signal": "buy", "reason": f"가격이 돌파 기준({target_price:.2f}) 상회", "amount": amount}
    return None
