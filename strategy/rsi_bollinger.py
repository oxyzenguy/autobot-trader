import pandas as pd
import numpy as np
from typing import Dict, Any

# ------------------- 기존 지표 함수 재활용 -------------------
def calc_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(window=period, min_periods=period).mean()
    avg_loss = loss.rolling(window=period, min_periods=period).mean()
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return rsi.fillna(50)

def calc_bollinger(close: pd.Series, period: int = 20, n_std: float = 2.0):
    ma = close.rolling(window=period, min_periods=period).mean()
    std = close.rolling(window=period, min_periods=period).std()
    upper = ma + n_std * std
    lower = ma - n_std * std
    return upper, ma, lower

# ------------------- 백테스트용 전략 함수 -------------------
import pandas as pd
import numpy as np

def backtest_strategy(df: pd.DataFrame, initial_balance=1_000_000, trade_unit=5_000, rsi_buy=30, rsi_sell=70, boll_width=2.0):
    """
    RSI + Bollinger Band 백테스트 함수
    """

    balance = initial_balance
    coin = 0
    trade_log = []
    equity_curve = []

    # RSI 계산
    delta = df["close"].diff()
    gain = np.where(delta > 0, delta, 0)
    loss = np.where(delta < 0, -delta, 0)
    avg_gain = pd.Series(gain).rolling(14).mean()
    avg_loss = pd.Series(loss).rolling(14).mean()
    rs = avg_gain / avg_loss
    df["RSI"] = 100 - (100 / (1 + rs))

    # 볼린저 밴드 계산
    df["MA20"] = df["close"].rolling(20).mean()
    df["STD20"] = df["close"].rolling(20).std()
    df["Upper"] = df["MA20"] + boll_width * df["STD20"]
    df["Lower"] = df["MA20"] - boll_width * df["STD20"]

    position = False
    entry_price = 0

    for i in range(20, len(df)):
        price = df["close"].iloc[i]
        rsi = df["RSI"].iloc[i]
        lower = df["Lower"].iloc[i]
        upper = df["Upper"].iloc[i]
        current_time = df["datetime"].iloc[i]

        # 매수 조건: RSI < 30 & 종가 < 하단 밴드
        if not position and rsi < rsi_buy and price < lower:
            if balance >= trade_unit:
                coin += trade_unit / price
                balance -= trade_unit
                entry_price = price
                position = True
                trade_log.append((current_time, "BUY", price, trade_unit))

        # 매도 조건: RSI > 70 & 종가 > 상단 밴드
        elif position and rsi > rsi_sell and price > upper:
            balance += coin * price
            trade_amount = coin * price
            coin = 0
            position = False
            trade_log.append((current_time, "SELL", price, trade_amount))
        
        equity_curve.append((current_time, balance + coin * price))


    # 마지막 보유 코인 정산
    if coin > 0:
        balance += coin * df["close"].iloc[-1]
        coin = 0

    final_value = balance
    profit = final_value - initial_balance
    roi = (final_value / initial_balance - 1) * 100 if initial_balance > 0 else 0

    summary = pd.Series({
        "Initial Balance": initial_balance,
        "Final Balance": final_value,
        "Profit": profit,
        "Return (%)": roi,
        "Total Trades": len(trade_log)
    })

    trades_df = pd.DataFrame(trade_log, columns=["datetime", "type", "price", "amount"])
    equity_df = pd.DataFrame(equity_curve, columns=["datetime", "equity"])

    return {
        "summary": summary,
        "trades": trades_df,
        "equity_curve": equity_df
    }
