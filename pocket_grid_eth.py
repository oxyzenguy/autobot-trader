#!/usr/bin/env python3
"""
KRW-ETH 업비트 서브포켓(G60) 실거래 그리드 실행 스크립트
- 대상: KRW-ETH (이더리움)
- 1회 주문: 10,000원 (실거래 시장가)
- 간격: 0.50%
- 익절: +0.70% (개별 및 5단 스쿼드 바스켓, Option A-2 빠른 회전 적용)
- 1차 배정 예산: 300,000원 (최대 30단계)
"""
import sys
import os

os.environ["OBJC_DISABLE_INITIALIZE_FORK_SAFETY"] = "YES"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from utils.pocket_grid_core import PocketGridRunner


def main():
    runner = PocketGridRunner(
        market="KRW-ETH",
        step_pct=0.005,
        profit_pct=0.007,
        squad_tp_pct=0.007,
        max_levels=30,
        unit_krw=10_000.0,
        grid_budget=300_000.0,
        poll_interval_sec=5
    )
    runner.run()


if __name__ == "__main__":
    main()
