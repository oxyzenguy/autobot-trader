#!/usr/bin/env python3
"""
포켓1 Cascade 러너: KRW-SOL (솔라나)
- 매수 간격: 1.0% (0.010)
- 바스켓 익절: 0.6% (0.006)
- 최대 버퍼: 200단 (1회 단위 10,000원)
- 서브포켓 계정: UPBIT_POCKET
"""
import sys
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from utils.pocket_basket_core import PocketBasketResetRunner


def main():
    runner = PocketBasketResetRunner(
        market="KRW-SOL",
        step_pct=0.010,
        profit_pct=0.006,
        unit_krw=10_000.0,
        max_levels=200,
        max_open_buys=3,
        poll_interval_sec=2.0,
        pocket_name="pocket1_cascade",
        env_prefix="UPBIT_POCKET"
    )
    runner.run()


if __name__ == "__main__":
    main()
