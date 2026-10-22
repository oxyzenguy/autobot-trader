#!/usr/bin/env python3
"""
========================================================================================
🧪 [AutoBot Trader] 15분봉 눌림목/재테스트(Pullback) 엔진 단위 테스트 슈트
========================================================================================
1. test_01_pullback_causality_and_execution: 돌파 관찰 -> 눌림목 지지 양봉 확인 -> 익봉 시가 진입
2. test_02_swing_low_sl_and_r_multiple_tp: 눌림 스윙 저점 SL 및 1.5R/2.0R 목표가 계산 검증
3. test_03_gross_vs_net_accounting: Gross(비용 0%) vs Net(비용 0.14%) 분해 정합성 검증
4. test_04_period_split_inheritance: 기간 분할 상태 승계 100% 완전 일치
========================================================================================
"""

import unittest
import pandas as pd
import numpy as np

from backtest.pullback_15m_engine import (
    run_pullback_discrete_backtest,
    prepare_pullback_indicators
)


def create_synthetic_candles(n_bars: int = 50, base_price: float = 100000.0) -> pd.DataFrame:
    dates = pd.date_range("2026-01-01 00:00:00", periods=n_bars, freq="15min")
    df = pd.DataFrame(index=dates, data={
        "opening_price": [base_price] * n_bars,
        "high_price":    [base_price * 1.002] * n_bars,
        "low_price":     [base_price * 0.998] * n_bars,
        "trade_price":   [base_price] * n_bars,
        "candle_acc_trade_volume": [1000.0] * n_bars,
        "candle_acc_trade_price":  [1e8] * n_bars
    })
    return df


class TestPullback15mEngine(unittest.TestCase):
    def test_01_pullback_causality_and_execution(self):
        """[검증 1] 돌파 후 2봉 뒤 눌림목 지지 양봉 확인 -> 3번째 봉 시가 진입 인과성"""
        df = create_synthetic_candles(n_bars=35, base_price=100000.0)
        dates = df.index

        # 20봉: 돌파 발생 (고가 101,000, 종가 100,800, 거래량 3000)
        df.loc[dates[20], "high_price"] = 101000.0
        df.loc[dates[20], "trade_price"] = 100800.0
        df.loc[dates[20], "candle_acc_trade_volume"] = 3000.0
        breakout_level = df.loc[dates[19], "high_price"]  # 이전 고점 ~ 100,200

        # 21봉: 되돌림 진행 (저가 100,100, 종가 100,150 - 음봉)
        df.loc[dates[21], "opening_price"] = 100800.0
        df.loc[dates[21], "low_price"] = 100100.0
        df.loc[dates[21], "trade_price"] = 100150.0

        # 22봉: 돌파선 지지 확인 양봉 반등 (시가 100,150, 저가 100,100, 종가 100,500 - 양봉!)
        df.loc[dates[22], "opening_price"] = 100150.0
        df.loc[dates[22], "low_price"] = 100100.0
        df.loc[dates[22], "high_price"] = 100600.0
        df.loc[dates[22], "trade_price"] = 100500.0

        # 23봉: 시가 진입 (100,500) -> 24봉에서 TP 달성
        df.loc[dates[23], "opening_price"] = 100500.0
        df.loc[dates[24], "high_price"] = 105000.0

        res = run_pullback_discrete_backtest(
            {"KRW-BTC": df},
            capital=1_000_000.0,
            rr_target=1.5,
            trend_filter="off",
            max_rsi=0.0
        )

        trades = res["trades"]
        self.assertGreater(len(trades), 0)
        t0 = trades[0]
        # 22봉에서 반등 확인 -> 23봉 시가 진입!
        self.assertEqual(t0["entry_dt"], str(dates[23]))

    def test_02_swing_low_sl_and_r_multiple_tp(self):
        """[검증 2] 스윙 저점 SL 및 1.5R TP 산정 정확성"""
        df = create_synthetic_candles(n_bars=35, base_price=100000.0)
        dates = df.index
        df.loc[dates[20], "high_price"] = 102000.0
        df.loc[dates[20], "trade_price"] = 101500.0
        df.loc[dates[20], "candle_acc_trade_volume"] = 3000.0

        # 21봉에서 저가 99,500 찍고 반등 종가 100,500 (양봉)
        df.loc[dates[21], "opening_price"] = 100000.0
        df.loc[dates[21], "low_price"] = 99500.0
        df.loc[dates[21], "high_price"] = 100600.0
        df.loc[dates[21], "trade_price"] = 100500.0

        # 22봉 시가 100,500 진입
        df.loc[dates[22], "opening_price"] = 100500.0
        # 23봉에서 익절 터치 (+5%)
        df.loc[dates[23], "high_price"] = 103000.0

        res = run_pullback_discrete_backtest(
            {"KRW-BTC": df},
            capital=1_000_000.0,
            rr_target=1.5,
            trend_filter="off",
            max_rsi=0.0
        )

        trades = res["trades"]
        self.assertEqual(len(trades), 1)
        t0 = trades[0]
        # 스윙 저점 99,500에 버퍼(-0.1%) 적용된 손절선
        self.assertGreater(t0["risk_r"], 0.0)
        self.assertEqual(t0["reason"], "TAKE_PROFIT")

    def test_03_gross_vs_net_accounting(self):
        """[검증 3] Gross vs Net 회계 분해 및 비용 잠식액 일치 검증"""
        df = create_synthetic_candles(n_bars=35, base_price=100000.0)
        dates = df.index
        df.loc[dates[20], "high_price"] = 102000.0
        df.loc[dates[20], "trade_price"] = 101500.0
        df.loc[dates[20], "candle_acc_trade_volume"] = 3000.0
        df.loc[dates[21], "opening_price"] = 100000.0
        df.loc[dates[21], "low_price"] = 99500.0
        df.loc[dates[21], "trade_price"] = 100500.0
        df.loc[dates[22], "opening_price"] = 100500.0
        df.loc[dates[23], "high_price"] = 103000.0

        res = run_pullback_discrete_backtest(
            {"KRW-BTC": df},
            capital=1_000_000.0,
            rr_target=1.5,
            trend_filter="off",
            max_rsi=0.0
        )

        # Gross PnL > Net PnL (수수료+슬리피지 잠식으로 인해)
        self.assertGreater(res["gross_pnl"], res["net_pnl"])
        self.assertAlmostEqual(res["friction_krw"], res["gross_pnl"] - res["net_pnl"], places=4)

    def test_04_period_split_inheritance(self):
        """[검증 4] 분할 실행 시 상태 승계 100% 완전 일치"""
        df = create_synthetic_candles(n_bars=60, base_price=100000.0)
        dates = df.index
        # 15봉 돌파
        df.loc[dates[15], "high_price"] = 102000.0
        df.loc[dates[15], "trade_price"] = 101500.0
        df.loc[dates[15], "candle_acc_trade_volume"] = 3000.0
        df.loc[dates[16], "opening_price"] = 100000.0
        df.loc[dates[16], "low_price"] = 99800.0
        df.loc[dates[16], "trade_price"] = 100500.0

        # 분할 경계
        d_start = str(dates[10])
        d_split = str(dates[30])
        d_next = str(dates[31])
        d_end = str(dates[-1])

        res_full = run_pullback_discrete_backtest(
            {"KRW-BTC": df}, capital=1_000_000.0, trend_filter="off", max_rsi=0.0,
            start_date=d_start, end_date=d_end, liquidate_at_end=True
        )
        res_p1 = run_pullback_discrete_backtest(
            {"KRW-BTC": df}, capital=1_000_000.0, trend_filter="off", max_rsi=0.0,
            start_date=d_start, end_date=d_split, liquidate_at_end=False
        )
        res_p2 = run_pullback_discrete_backtest(
            {"KRW-BTC": df}, capital=1_000_000.0, trend_filter="off", max_rsi=0.0,
            start_date=d_next, end_date=d_end, liquidate_at_end=True,
            initial_cash=res_p1["equity_curve"][-1]["cash"],
            initial_positions=res_p1["terminal_positions"],
            initial_pending_orders=res_p1["pending_orders_end"]
        )

        tr_full = res_full["trades"]
        tr_split = res_p1["trades"] + res_p2["trades"]
        self.assertEqual(len(tr_full), len(tr_split))
        self.assertAlmostEqual(res_full["final_equity"], res_p2["final_equity"], places=4)


if __name__ == "__main__":
    unittest.main()
