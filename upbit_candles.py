import pyupbit
import pandas as pd
from datetime import datetime, date, timedelta
from calendar import monthrange
import time
import argparse


def collect_monthly_minute_candles(market='KRW-BTC', year=2024, month=1):
    """
    pyupbit을 사용한 월별 1분봉 수집
    """
    first_day = date(year, month, 1)
    last_day_num = monthrange(year, month)[1]
    last_day = date(year, month, last_day_num)
    
    today = date.today()
    if last_day > today:
        last_day = today
    
    print(f"=== {year}년 {month}월 데이터 수집 ===")
    print(f"범위: {first_day} ~ {last_day}")
    
    all_data = []
    current_date = last_day
    
    while current_date >= first_day:
        # 해당 날짜의 데이터 수집
        to_datetime = datetime.combine(current_date, datetime.max.time())
        
        try:
            print(f"  {current_date} 수집 중...", end=" ")
            
            # pyupbit으로 1분봉 데이터 가져오기 (최대 200개)
            df = pyupbit.get_ohlcv(
                ticker=market,
                interval="minute1",
                to=to_datetime.strftime("%Y-%m-%d %H:%M:%S"),
                count=1440  # 하루 1440분
            )
            
            if df is not None and len(df) > 0:
                # 해당 날짜 데이터만 필터링
                df_filtered = df[df.index.date == current_date]
                
                if len(df_filtered) > 0:
                    all_data.append(df_filtered)
                    print(f"✓ {len(df_filtered)}개")
                else:
                    print("데이터 없음")
            else:
                print("응답 없음")
            
            time.sleep(0.2)  # Rate Limit 준수
            
        except Exception as e:
            print(f"오류: {e}")
            time.sleep(1)
        
        current_date -= timedelta(days=1)
    
    # DataFrame 병합
    if all_data:
        result_df = pd.concat(all_data)
        result_df = result_df.sort_index()
        result_df = result_df[~result_df.index.duplicated(keep='first')]
        
        print(f"완료: 총 {len(result_df):,}개\n")
        return result_df
    else:
        print("데이터 없음\n")
        return pd.DataFrame()


def collect_multiple_months(market='KRW-BTC', start_year=2024, start_month=1,
                           end_year=2025, end_month=9):
    """여러 월 일괄 수집"""
    results = {}
    current = date(start_year, start_month, 1)
    end_date = date(end_year, end_month, 1)
    
    months = []
    temp = current
    while temp <= end_date:
        months.append((temp.year, temp.month))
        if temp.month == 12:
            temp = date(temp.year + 1, 1, 1)
        else:
            temp = date(temp.year, temp.month + 1, 1)
    
    print(f"\n{'='*60}")
    print(f"총 {len(months)}개월 수집 시작")
    print(f"{'='*60}\n")
    
    for idx, (year, month) in enumerate(months, 1):
        print(f"[{idx}/{len(months)}] {year}년 {month}월")
        print("-