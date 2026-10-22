#!/usr/bin/env python3
"""
KRW-SOL Model G60 순수 60단 5단 독립 스쿼드 그리드 가상매매 실행 스크립트
- 대상: KRW-SOL
- 1회 주문: 10,000원
- 간격: 0.70% (백테스트 검증 최적 파라미터)
- 익절: +1.20% (개별 및 스쿼드 바스켓 이중 익절)
- 안전 현금: 400,000원 항시 보존 (곰마파 OFF)
"""

import sys
import os

# macOS(Darwin) 멀티스레드 환경에서 fork() 시 Segfault 11 크래시 원천 방지
os.environ["OBJC_DISABLE_INITIALIZE_FORK_SAFETY"] = "YES"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from utils.paper_grid_core import PaperGridRunner


def main():
    runner = PaperGridRunner(
        market="KRW-SOL",
        step_pct=0.005,      # 0.50% 촘촘한 간격으로 체결 빈도 극대화
        profit_pct=0.010,    # +1.00% 신속 짤짤이 익절
        squad_tp_pct=0.010,  # +1.00% 5단 스쿼드 바스켓 일괄 익절
        max_levels=60,
        unit_krw=10_000.0,
        initial_capital=1_000_000.0,
        grid_budget=600_000.0,
        safety_cash_buffer=400_000.0,
        use_gommapa=False,
        poll_interval_sec=5
    )
    runner.run()


if __name__ == "__main__":
    main()
