#!/usr/bin/env python3
"""
KRW-SOL 업비트 서브포켓(G60 ➔ Cascade 자동전환) 실거래 실행 스크립트
- 대상: KRW-SOL (솔라나)
- 모드: EXIT_ONLY (신규 매수 차단, 기존 24단 평단 대비 +0.6% 바스켓 일괄 익절 대기)
- 청산 후: 신규 Cascade 전략(1.0% 매수 / 0.6% 익절 / 200단)으로 자동 전환 실행
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
        exit_only=True,
        basket_exit_pct=0.006,            # 평단가 대비 +0.6% 바스켓 일괄 익절
        transition_to_cascade=True,       # 청산 후 Cascade로 자동 전환
        cascade_step_pct=0.010,          # 신규 Cascade 매수 간격 1.0%
        cascade_profit_pct=0.006,        # 신규 Cascade 전량 익절 마진 0.6%
        cascade_max_levels=200,          # 200단 버퍼
        cascade_pocket_name="pocket1_cascade",
        cascade_env_prefix="UPBIT_POCKET"
    )
    runner.run()


if __name__ == "__main__":
    main()
