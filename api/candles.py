import requests
import pandas as pd

def fetch_candles(market: str, interval: str = "day", count: int = 100) -> pd.DataFrame:
    """
    예시: 업비트 캔들 가져오기
    """
    url = f"https://api.upbit.com/v1/candles/{interval}"
    params = {
        "market": market,
        "count": count
    }
    response = requests.get(url, params=params)
    response.raise_for_status()
    data = response.json()

    df = pd.DataFrame(data)
    # 시간 순 정렬
    df["candle_date_time_utc"] = pd.to_datetime(df["candle_date_time_utc"])
    df = df.sort_values("candle_date_time_utc").reset_index(drop=True)
    return df
