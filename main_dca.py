from api.client import fetch_day_candles
from backtest.dca_backtest import run_dca_backtest
from datetime import datetime
import pandas as pd

def main():
    market = "KRW-BTC"
    start_date = "2020-01-01"
    end_date = "2022-12-01"
    monthly_investment = 100000  # 매월 10만원

    # 데이터 수집
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")
    to = end_dt.strftime("%Y-%m-%d %H:%M:%S")
    all_data = []

    while True:
        df = fetch_day_candles(market=market, to=to, count=200)
        if df.empty:
            break

        earliest = df["candle_date_time_utc"].min()
        all_data.append(df)

        to = (earliest - pd.Timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
        if earliest <= datetime.strptime(start_date, "%Y-%m-%d"):
            break

    # 데이터 합치기
    price_df = pd.concat(all_data).sort_values("candle_date_time_utc").reset_index(drop=True)

    # 적립식 백테스트
    result_df, purchases_df = run_dca_backtest(
        df=price_df,
        start_date=start_date,
        end_date=end_date,
        monthly_investment=monthly_investment,
        buy_day=1
    )

    # 출력 확인
    print(result_df[["candle_date_time_utc", "trade_price", "cum_quantity", "portfolio_value"]].tail())
    print(purchases_df)

    # 엑셀로 저장
    with pd.ExcelWriter("BTC_DCA_Backtest.xlsx") as writer:
        result_df.to_excel(writer, sheet_name="Daily Portfolio", index=False)
        purchases_df.to_excel(writer, sheet_name="Purchases", index=False)

    print("엑셀 파일 저장 완료.")

if __name__ == "__main__":
    main()
