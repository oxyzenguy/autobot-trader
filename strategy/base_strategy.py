import pandas as pd

def simple_strategy(df: pd.DataFrame) -> pd.DataFrame:
    """
    아주 간단한 전략: 아무것도 안함. 
    단지 백테스트용 신호 칼럼만 생성
    """
    df["signal"] = 0  # 0=포지션 없음
    return df
