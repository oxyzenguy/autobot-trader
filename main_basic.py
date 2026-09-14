from api.client import fetch_day_candles
from datetime import datetime
import pandas as pd

def main():
    market = "KRW-BTC"
    end_date = datetime(2022, 12, 1)
    start_date = datetime(2020, 1, 1)

    all_data = []
    to = end_date.strftime("%Y-%m-%d %H:%M:%S")

    while True:
        df = fetch_day_candles(market=market, to=to, count=200)
        if df.empty:
            break

        earliest_date = df["candle_date_time_utc"].min()
        print(f"Fetched {len(df)} rows. Earliest date: {earliest_date}")

        all_data.append(df)

        # to 파라미터는 exclusive 이므로 하루 더 이전으로 설정
        to_dt = earliest_date
        to = (to_dt - pd.Timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")

        if to_dt <= start_date:
            break

    # 모든 데이터 합치기
    if all_data:
        result_df = pd.concat(all_data).sort_values("candle_date_time_utc").reset_index(drop=True)
        print(result_df[["candle_date_time_utc", "trade_price"]].head())
        print(result_df[["candle_date_time_utc", "trade_price"]].tail())

        # 예: CSV로 저장
        result_df.to_csv("BTC_day_candles_2020_2022.csv", index=False)
        print(f"총 {len(result_df)}행 저장 완료.")

    else:
        print("데이터를 불러오지 못했습니다.")

if __name__ == "__main__":
    main()
