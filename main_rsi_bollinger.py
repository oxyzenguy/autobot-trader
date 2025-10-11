#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
📘 RSI + Bollinger Band 단타 스캘핑 전략 백테스트 실행 파일
-----------------------------------------------------------
- 데이터: data/*.csv (분봉)
- 전략: strategy/rsi_bollinger.py
- 결과: 수익률, 거래 통계, 거래 로그, 엑셀 저장

Author: Python Auto-Trading Dev
Date: 2025
"""

import os
import pandas as pd
from config import INVESTMENTS
from strategy.rsi_bollinger import backtest_strategy


# ================================
# 📊 설정
# ================================
MARKET = "KRW-SOL"          # "KRW-XRP"로 변경 가능
INTERVAL = "minute1"         # 1분봉
RESULTS_DIR = "results"
DATA_DIR = "data"
os.makedirs(RESULTS_DIR, exist_ok=True)


# ================================
# ⏳ 데이터 불러오기
# ================================
def load_local_data(market: str) -> pd.DataFrame:
    """
    data 폴더 내의 CSV 파일을 불러옵니다.
    예: data/202401_202509_KRW_SOL_merged.csv
    """
    filename = f"202401_202509_{market.replace('-', '_')}_merged.csv"
    filepath = os.path.join(DATA_DIR, filename)

    if not os.path.exists(filepath):
        raise FileNotFoundError(f"데이터 파일이 존재하지 않습니다: {filepath}")

    print(f"\n로컬 CSV 불러오는 중: {filepath}")
    df = pd.read_csv(filepath)

    # 컬럼 정리
    df.columns = [c.strip().lower() for c in df.columns]
    if "datetime" not in df.columns:
        # 자동 감지
        datetime_col = [c for c in df.columns if "time" in c.lower()][0]
        df.rename(columns={datetime_col: "datetime"}, inplace=True)

    # datetime 변환
    df["datetime"] = pd.to_datetime(df["datetime"])
    df.sort_values("datetime", inplace=True)
    df.reset_index(drop=True, inplace=True)

    print(f"데이터 로드 완료 ({len(df)}개 캔들, {df['datetime'].iloc[0]} ~ {df['datetime'].iloc[-1]})")
    return df


# ================================
# 🧠 백테스트 실행
# ================================
def run_backtest():
    df = load_local_data(MARKET)

    invest_info = INVESTMENTS.get(MARKET, {"total": 1_000_000, "unit": 5_000})
    initial_balance = invest_info["total"]
    unit_size = invest_info["unit"]

    print("\n======================================================================")
    print("RSI + Bollinger Band 전략 백테스트 시작")
    print("======================================================================")
    print(f"데이터 기간: {df['datetime'].iloc[0]} ~ {df['datetime'].iloc[-1]}")
    print(f"총 캔들 수: {len(df)}개")
    print(f"초기 자본: {initial_balance:,.0f}원")
    print(f"1회 매수 금액(Unit): {unit_size:,.0f}원")
    print("======================================================================")

    # 전략 실행
    result = backtest_strategy(
        df=df,
        initial_balance=initial_balance,
        trade_unit=unit_size
    )

    # ================================
    # 💾 결과 저장
    # ================================
    start = df["datetime"].iloc[0].strftime("%Y%m%d")
    end = df["datetime"].iloc[-1].strftime("%Y%m%d")
    filename = f"backtest_RSI_BB_{MARKET}_{INTERVAL}_{start}_{end}.xlsx"
    filepath = os.path.join(RESULTS_DIR, filename)

    with pd.ExcelWriter(filepath) as writer:
        result["summary"].to_excel(writer, sheet_name="요약")
        result["trades"].to_excel(writer, sheet_name="거래내역", index=False)
        result["equity_curve"].to_excel(writer, sheet_name="잔고추이", index=False)

    print("\n백테스트 완료")
    print(f"결과 파일: {filepath}")
    print("======================================================================")
    print(result["summary"])
    print("======================================================================\n")


# ================================
# 🚀 실행
# ================================
if __name__ == "__main__":
    run_backtest()
