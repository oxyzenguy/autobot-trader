import pandas as pd

def run_backtest(df: pd.DataFrame) -> pd.DataFrame:
    """
    백테스트 예시: 수익 계산
    (signal이 1이면 매수, 0이면 청산)
    """
    df["position"] = df["signal"].shift(1).fillna(0)

    df["returns"] = df["trade_price"].pct_change().fillna(0)
    df["strategy_returns"] = df["returns"] * df["position"]

    df["equity_curve"] = (1 + df["strategy_returns"]).cumprod()
    return df
