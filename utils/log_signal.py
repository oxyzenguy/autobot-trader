import csv
import os
from datetime import datetime

def log_signal(strategy, ticker, signal, price, backtest=False):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    row = [now, ticker, strategy, signal, price]

    # 기본 실행 로그 (원하는 경우만 사용)
    with open("signal_log.csv", "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(row)

    # 백테스트 로그 저장
    if backtest:
        os.makedirs("backtest", exist_ok=True)
        filename = f"backtest/backtest_{strategy}.csv"
        with open(filename, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(row)
