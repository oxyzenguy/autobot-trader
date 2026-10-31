#!/usr/bin/env python3
"""
KRW-SOL 업비트 서브포켓(G60) 실거래 그리드 실행 스크립트
- 대상: KRW-SOL (솔라나)
- 1회 주문: 10,000원 (실거래 시장가)
- 간격: 0.60% (하락 방어 완충 확대)
- 익절: +0.70% (개별 및 5단 스쿼드 바스켓, 빠른 회전)
- 기본 배정 예산: 300,000원 (30단계)
- ETH 청산 완료 시: 매도 대금 50% 자동 배분 반영 (+106,000원 -> 40단계 동적 확장)
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
        market="KRW-SOL",
        step_pct=0.006,
        profit_pct=0.007,
        squad_tp_pct=0.007,
        max_levels=30,
        unit_krw=10_000.0,
        grid_budget=300_000.0,
        poll_interval_sec=5,
        auto_reallocate_eth=True
    )
    runner.run()


if __name__ == "__main__":
    main()
