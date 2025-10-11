"""
과거 1분봉 데이터 수집 스크립트
"""
import pandas as pd
import requests
import time
from datetime import datetime, timedelta

print("프로그램 시작!")  # ← 디버깅용

def collect_minute1_historical(
    ticker: str = "KRW-SOL",
    start_date_str: str = "2024-10-01",
    end_date_str: str = "2025-01-01"
):
    """1분봉 과거 데이터를 수집합니다."""
    
    print("="*60)
    print("📥 과거 데이터 수집 시작")
    print(f"   티커: {ticker}")
    print(f"   기간: {start_date_str} ~ {end_date_str}")
    print("="*60)
    
    all_data = []
    
    start_date = pd.Timestamp(start_date_str)
    end_date = pd.Timestamp(end_date_str)
    current_date = end_date
    
    iteration = 0
    
    while current_date >= start_date:
        iteration += 1
        to_str = current_date.strftime('%Y-%m-%dT%H:%M:%S')
        
        url = "https://api.upbit.com/v1/candles/minutes/1"
        params = {'market': ticker, 'to': to_str, 'count': 200}
        
        try:
            response = requests.get(url, params=params)
            
            if response.status_code != 200:
                print(f"⚠️  API 오류: {response.status_code}")
                time.sleep(1)
                continue
            
            data = response.json()
            
            if not data:
                print(f"⚠️  데이터 없음 ({to_str})")
                break
            
            df = pd.DataFrame(data)
            df['candle_date_time_kst'] = pd.to_datetime(df['candle_date_time_kst'])
            
            min_time = df['candle_date_time_kst'].min()
            max_time = df['candle_date_time_kst'].max()
            
            all_data.append(df)
            
            if iteration % 100 == 0:
                total_collected = len(all_data) * 200
                print(f"📊 진행: {iteration}회, {total_collected:,}개 수집, 현재: {min_time}")
            
            if min_time <= start_date:
                print(f"✅ 목표 도달! {iteration}회 호출")
                break
            
            current_date = min_time - timedelta(minutes=1)
            time.sleep(0.12)
            
        except KeyboardInterrupt:
            print("\n⚠️  사용자 중단")
            break
        except Exception as e:
            print(f"⚠️  오류: {e}")
            import traceback
            traceback.print_exc()
            time.sleep(1)
            continue
    
    if all_data:
        print("\n📦 데이터 병합 중...")
        final_df = pd.concat(all_data, ignore_index=True)
        
        print(f"   중복 제거 전: {len(final_df):,}개")
        final_df = final_df.drop_duplicates(subset=['candle_date_time_kst'])
        print(f"   중복 제거 후: {len(final_df):,}개")
        
        final_df = final_df.sort_values('candle_date_time_kst')
        
        print(f"\n✅ 수집 완료!")
        print(f"   총 캔들: {len(final_df):,}개")
        print(f"   범위: {final_df['candle_date_time_kst'].min()}")
        print(f"        ~ {final_df['candle_date_time_kst'].max()}")
        
        today = datetime.now().strftime("%Y%m%d")
        filename = f"historical_minute1_{today}.csv"
        
        final_df.to_csv(filename, index=False)
        print(f"\n💾 저장 완료: {filename}")
        
        return final_df
    else:
        print("❌ 수집된 데이터 없음")
        return None


# 메인 실행
if __name__ == "__main__":
    print("메인 실행 시작!")  # ← 디버깅용
    
    try:
        TICKER = "KRW-SOL"
        START_DATE = "2024-10-01"
        END_DATE = "2025-01-01"
        
        print(f"설정 확인:")
        print(f"  티커: {TICKER}")
        print(f"  시작: {START_DATE}")
        print(f"  종료: {END_DATE}")
        print()
        
        df = collect_minute1_historical(
            ticker=TICKER,
            start_date_str=START_DATE,
            end_date_str=END_DATE
        )
        
        if df is not None:
            print("\n" + "="*60)
            print("🎉 수집 성공!")
            print("="*60)
        else:
            print("\n" + "="*60)
            print("❌ 수집 실패")
            print("="*60)
    
    except Exception as e:
        print(f"\n❌ 오류 발생: {e}")
        import traceback
        traceback.print_exc()
    
    finally:
        print("\n프로그램 종료")
        input("Enter 키를 눌러 종료...")  # ← 창이 바로 닫히지 않도록
