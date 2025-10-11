import pandas as pd

def compute_rsi(series, period=14):
    delta = series.diff()
    gain = delta.where(delta > 0, 0)
    loss = -delta.where(delta < 0, 0)

    avg_gain = gain.rolling(window=period).mean()
    avg_loss = loss.rolling(window=period).mean()

    # A-V-A-V
    rs = avg_gain / avg_loss.replace(0, 1e-9)  # 0으로 나누는 것을 방지
    rsi = 100 - (100 / (1 + rs))
    
    # loss가 0일 때 rsi는 100이 되어야 함
    rsi[avg_loss == 0] = 100
    # gain과 loss가 모두 0일 때 rsi는 50으로 처리 (중립)
    rsi[(avg_gain == 0) & (avg_loss == 0)] = 50

    return rsi


def rsi30_70_cross(df):
    rsi = compute_rsi(df["close"])
    if len(rsi) < 2:
        return None
    if rsi.iloc[-2] < 30 and rsi.iloc[-1] > 30:
        return "buy"
    elif rsi.iloc[-2] > 70 and rsi.iloc[-1] < 70:
        return "sell"


def rsi40_60_cross(df):
    rsi = compute_rsi(df["close"])
    if len(rsi) < 2:
        return None
    if rsi.iloc[-2] < 40 and rsi.iloc[-1] > 40:
        return "buy"
    elif rsi.iloc[-2] > 60 and rsi.iloc[-1] < 60:
        return "sell"


def rsi30_static(df):
    rsi = compute_rsi(df["close"])
    if len(rsi) < 1:
        return None
    if rsi.iloc[-1] < 30:
        return "buy"
    elif rsi.iloc[-1] > 70:
        return "sell"


def rsi50_cross(df):
    rsi = compute_rsi(df["close"])
    if len(rsi) < 2:
        return None
    if rsi.iloc[-2] < 50 and rsi.iloc[-1] > 50:
        return "buy"
    elif rsi.iloc[-2] > 50 and rsi.iloc[-1] < 50:
        return "sell"


def rsi_obov_static(df):
    rsi = compute_rsi(df["close"])
    if len(rsi) < 1:
        return None
    if rsi.iloc[-1] < 40:
        return "buy"
    elif rsi.iloc[-1] > 60:
        return "sell"
