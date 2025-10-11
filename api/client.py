import requests
import pandas as pd
from datetime import datetime

def fetch_day_candles(market: str, to: str = None, count: int = 200) -> pd.DataFrame:
    """
    업비트 일봉 캔들 데이터 요청
    - market: KRW-BTC
    - to: 마지막 캔들 시각 (exclusive, ISO8601)
    - count: 최대 200
    """
    url = "https://api.upbit.com/v1/candles/days"
    params = {
        "market": market,
        "count": count
    }
    if to:
        params["to"] = to

    headers = {"accept": "application/json"}
    response = requests.get(url, params=params, headers=headers)
    response.raise_for_status()
    data = response.json()

    if not data:
        return pd.DataFrame()  # 빈 결과 처리

    df = pd.DataFrame(data)
    df["candle_date_time_utc"] = pd.to_datetime(df["candle_date_time_utc"])
    df["candle_date_time_kst"] = pd.to_datetime(df["candle_date_time_kst"])
    df = df.sort_values("candle_date_time_utc").reset_index(drop=True)
    return df
