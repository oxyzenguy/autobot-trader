# merge_data.py
import pandas as pd

# 기존 데이터
df_existing = pd.read_csv("SOL_minute1_candles.csv", index_col=0, parse_dates=True)
print(f"기존: {len(df_existing):,}개 ({df_existing.index.min()} ~ {df_existing.index.max()})")

# 새로 수집한 데이터
df_new = pd.read_csv("historical_minute1_20251011.csv")
df_new['candle_date_time_kst'] = pd.to_datetime(df_new['candle_date_time_kst'])
df_new = df_new.set_index('candle_date_time_kst')
df_new = df_new.rename(columns={
    'opening_price': 'open',
    'high_price': 'high',
    'low_price': 'low',
    'trade_price': 'close',
    'candle_acc_trade_volume': 'volume'
})
df_new = df_new[['open', 'high', 'low', 'close', 'volume']]
print(f"신규: {len(df_new):,}개 ({df_new.index.min()} ~ {df_new.index.max()})")

# 병합
df_merged = pd.concat([df_existing, df_new])
df_merged = df_merged[~df_merged.index.duplicated(keep='first')]
df_merged = df_merged.sort_index()

print(f"병합: {len(df_merged):,}개 ({df_merged.index.min()} ~ {df_merged.index.max()})")

# 저장
df_merged.to_csv("SOL_minute1_candles.csv")
print("✅ 저장 완료: SOL_minute1_candles.csv")
