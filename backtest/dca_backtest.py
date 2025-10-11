import pandas as pd
from datetime import datetime

def run_dca_backtest(df: pd.DataFrame, 
                     start_date: str, 
                     end_date: str, 
                     monthly_investment: float, 
                     buy_day: int = 1) -> pd.DataFrame:
    """
    적립식 백테스트
    - df: 캔들 데이터
    - start_date, end_date: 투자 기간 (YYYY-MM-DD)
    - monthly_investment: 매월 투자 금액 (원화)
    - buy_day: 매월 매수 일자 (1=1일)
    """
    # 투자 시작과 종료 날짜 필터링
    df = df.copy()
    df = df[(df["candle_date_time_utc"] >= pd.to_datetime(start_date)) &
            (df["candle_date_time_utc"] <= pd.to_datetime(end_date))]

    df = df.reset_index(drop=True)

    # 매수 내역 DataFrame
    purchases = []

    for idx, row in df.iterrows():
        date = row["candle_date_time_utc"]
        # 매수일이면 매수 실행
        if date.day == buy_day:
            price = row["trade_price"]
            quantity = monthly_investment / price
            purchases.append({"date": date, "price": price, "quantity": quantity})
    
    # 누적 보유량 계산
    df["cum_quantity"] = 0.0
    total_quantity = 0.0
    purchase_dates = {p["date"].date(): p for p in purchases}

    for idx, row in df.iterrows():
        date = row["candle_date_time_utc"].date()
        if date in purchase_dates:
            total_quantity += purchase_dates[date]["quantity"]
        df.at[idx, "cum_quantity"] = total_quantity

    # 포트폴리오 가치
    df["portfolio_value"] = df["trade_price"] * df["cum_quantity"]

    # 매수 내역을 DataFrame으로도 반환할 수 있음
    purchases_df = pd.DataFrame(purchases)

    return df, purchases_df
