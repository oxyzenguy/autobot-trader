#!/usr/bin/env python3
"""
KRW-ETH 업비트 서브포켓(G60) 실거래 그리드 실행 스크립트
- 대상: KRW-ETH (이더리움)
- 모드: EXIT_ONLY (신규 매수 전면 차단, 보유 21단 평단 대비 +1.0% 바스켓 일괄 매도 대기)
- 청산 후: 매도 대금 SOL / XRP 예산으로 50:50 배분 전환
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
        step_pct=0.006,
        profit_pct=0.007,
        squad_tp_pct=0.007,
        max_levels=30,
        unit_krw=10_000.0,
        grid_budget=300_000.0,
        poll_interval_sec=5,
        exit_only=True,
        basket_exit_pct=0.01
    )
    runner.run()


if __name__ == "__main__":
    main()
