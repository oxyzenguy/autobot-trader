#!/usr/bin/env python3
"""
Fetch recent 7 days of 1-minute candle data from Upbit API for KRW-XRP, KRW-ETH, KRW-SOL.
7 days * 24h * 60m = 10,080 candles per market.
"""

import time
import requests
import pandas as pd
from pathlib import Path
from datetime import datetime

BASE_DIR = Path(__file__).resolve().parent.parent
OUT_DIR = BASE_DIR / "data" / "candles_1m_7d"
OUT_DIR.mkdir(parents=True, exist_ok=True)

MARKETS = ["KRW-XRP", "KRW-ETH", "KRW-SOL"]
TOTAL_CANDLES = 10080  # 7 days of 1-minute bars
BATCH_SIZE = 200
URL = "https://api.upbit.com/v1/candles/minutes/1"


def fetch_market_1m(market: str, total_candles: int = TOTAL_CANDLES) -> Path:
    target_file = OUT_DIR / f"{market}_1m_7d.csv"
    print(f"\n[INFO] {market} 1분봉 7일치 수집 시작 (목표: {total_candles:,}개)...")

    all_candles = []
    to_time = None
    fetched = 0

    while fetched < total_candles:
        count = min(BATCH_SIZE, total_candles - fetched)
        params = {"market": market, "count": count}
        if to_time:
            params["to"] = to_time

        try:
            r = requests.get(URL, params=params, timeout=10)
            if r.status_code == 429:
                print("  [WARN] Rate limit (429), pausing 1s...")
                time.sleep(1.0)
                continue
            if r.status_code != 200:
                print(f"  [ERROR] HTTP {r.status_code}: {r.text}")
                time.sleep(1.0)
                continue

            data = r.json()
            if not data:
                print("  [INFO] 더 이상 과거 데이터가 없습니다.")
                break

            all_candles.extend(data)
            fetched += len(data)
            to_time = data[-1]["candle_date_time_utc"]

            if fetched % 2000 == 0 or fetched >= total_candles:
                oldest_kst = data[-1]["candle_date_time_kst"]
                print(f"  -> {fetched:,} / {total_candles:,} 수집 중... (가장 과거: {oldest_kst})")

            time.sleep(0.08)
        except Exception as e:
            print(f"  [EXCEPTION] {e}, 재시도 중...")
            time.sleep(1.0)

    if not all_candles:
        print(f"[ERROR] {market} 수집 실패 (0건)")
        return target_file

    df = pd.DataFrame(all_candles)
    df.rename(columns={
        "candle_date_time_kst": "dt",
        "opening_price": "opening_price",
        "high_price": "high_price",
        "low_price": "low_price",
        "trade_price": "trade_price",
        "candle_acc_trade_volume": "volume",
        "candle_acc_trade_price": "value",
    }, inplace=True)

    # Sort chronological (oldest to newest)
    df = df.sort_values(by="dt").reset_index(drop=True)
    cols = ["dt", "opening_price", "high_price", "low_price", "trade_price", "volume", "value"]
    df = df[cols]
    df.to_csv(target_file, index=False)
    print(f"[SUCCESS] {market} 저장 완료 -> {target_file} (총 {len(df):,}개 행, 기간: {df['dt'].iloc[0]} ~ {df['dt'].iloc[-1]})")
    return target_file


def main():
    start_t = time.time()
    for m in MARKETS:
        fetch_market_1m(m)
    print(f"\n[DONE] 전 마켓 1분봉 데이터 수집 완료! 총 소요시간: {time.time() - start_t:.1f}초")


if __name__ == "__main__":
    main()
