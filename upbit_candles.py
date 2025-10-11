import pyupbit
import pandas as pd
from datetime import datetime, date, timedelta
from calendar import monthrange
import time


def collect_monthly_minute_candles(market='KRW-SOL', year=2024, month=1):
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


def collect_multiple_months(market='KRW-SOL', start_year=2024, start_month=1,
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
        print("-" * 60)
        
        df = collect_monthly_minute_candles(market, year, month)
        
        yyyymm = f"{year}{month:02d}"
        results[yyyymm] = df
        
        if not df.empty:
            filename = f"{yyyymm}_{market.replace('-', '_')}_1min.csv"
            
            # CSV 저장 시 인덱스(시간) 포함
            df_save = df.reset_index()
            df_save.rename(columns={'index': 'candle_date_time_kst'}, inplace=True)
            df_save.to_csv(filename, index=False, encoding='utf-8-sig')
            
            print(f"💾 {filename}\n")
        
        if idx < len(months):
            print("⏳ 다음 월까지 3초 대기...\n")
            time.sleep(3)
    
    return results


def merge_monthly_files(market='KRW-SOL', start_year=2024, start_month=1,
                       end_year=2025, end_month=9):
    """월별 파일 병합"""
    all_dfs = []
    current = date(start_year, start_month, 1)
    end_date = date(end_year, end_month, 1)
    
    print("\n" + "="*60)
    print("🔗 월별 파일 병합")
    print("="*60)
    
    while current <= end_date:
        yyyymm = f"{current.year}{current.month:02d}"
        filename = f"{yyyymm}_{market.replace('-', '_')}_1min.csv"
        
        try:
            df = pd.read_csv(filename)
            all_dfs.append(df)
            print(f"✓ {filename}: {len(df):,}개")
        except:
            print(f"✗ {filename}: 파일 없음")
        
        if current.month == 12:
            current = date(current.year + 1, 1, 1)
        else:
            current = date(current.year, current.month + 1, 1)
    
    if all_dfs:
        merged = pd.concat(all_dfs, ignore_index=True)
        merged = merged.drop_duplicates(subset=['candle_date_time_kst'], keep='first')
        merged = merged.sort_values('candle_date_time_kst').reset_index(drop=True)
        print(f"\n✓ 병합 완료: {len(merged):,}개")
        return merged
    
    return pd.DataFrame()


if __name__ == '__main__':
    # pyupbit 설치 확인
    try:
        import pyupbit
        print("✓ pyupbit 라이브러리 로드 성공\n")
    except ImportError:
        print("❌ pyupbit 라이브러리가 설치되지 않았습니다.")
        print("설치 명령: pip install pyupbit\n")
        exit(1)
    
    # 수집 시작
    results = collect_multiple_months(
        market='KRW-SOL',
        start_year=2024,
        start_month=1,
        end_year=2025,
        end_month=9
    )
    
    # 통계
    print("\n" + "="*60)
    print("📊 수집 통계")
    print("="*60)
    total = 0
    for ym, df in results.items():
        cnt = len(df)
        total += cnt
        print(f"{ym}: {cnt:,}개" if cnt > 0 else f"{ym}: 없음")
    print(f"\n총계: {total:,}개")
    
    # 병합
    if total > 0:
        merged = merge_monthly_files('KRW-SOL', 2024, 1, 2025, 9)
        
        if not merged.empty:
            filename = '202401_202509_KRW_SOL_merged.csv'
            merged.to_csv(filename, index=False, encoding='utf-8-sig')
            print(f"\n💾 최종 파일: {filename}")
            
            first = merged.iloc[0]['candle_date_time_kst']
            last = merged.iloc[-1]['candle_date_time_kst']
            print(f"📅 기간: {first} ~ {last}")
    
    print("\n" + "="*60)
    print("✅ 모든 작업 완료")
    print("="*60)
