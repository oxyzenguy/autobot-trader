#!/usr/bin/env python3
"""
신규 서브포켓2 바스켓 리셋 전략 실행 스크립트: KRW-SOL (솔라나)
- 설정: 1.8% 하락 매수 / 0.8% 전량 익절 리셋 (최대 200단 한도)
- 1회 단위: 10,000 KRW
- 독립 서브포켓2 전용 계정 (UPBIT_POCKET2_ACCESS_KEY) 격리 가동
- 전용 장부: pocket2_basket_state_KRW_SOL.json
- 전용 데이터베이스: pocket2_basket_trade_history.db
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
        step_pct=0.018,        # 1.8% 하락 간격
        profit_pct=0.008,      # 0.8% 전량 익절 마진
        unit_krw=10_000.0,     # 1Unit 10,000원
        max_levels=200,        # 최대 200단
        max_open_buys=3,       # 하단 3개 대기 매수
        poll_interval_sec=2.0, # 2초 폴링
        pocket_name="pocket2_basket",
        env_prefix="UPBIT_POCKET2"
    )
    runner.run()


if __name__ == "__main__":
    main()
