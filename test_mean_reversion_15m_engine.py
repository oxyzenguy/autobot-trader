#!/usr/bin/env python3
"""
========================================================================================
🧪 [AutoBot Trader] 15분봉 평균 회귀(Mean Reversion) 엔진 단위 테스트 슈트
========================================================================================
1. test_01_mr0_execution_and_causality: RSI <= 25 트리거 후 익봉 시가 진입 인과성
2. test_02_mr1_bounce_confirmation: RSI 25 이하 후 30 상향 돌파 시 익봉 시가 진입
3. test_03_mr2_bb_recovery: 볼린저 밴드 하단 이탈 후 밴드 안 복귀 양봉 시 익봉 시가 진입
4. test_04_conservative_sl_priority: 동일 봉 TP/SL 동시 터치 시 보수적 손절(SL) 우선
5. test_05_gross_vs_net_accounting: Gross(비용 0%) vs Net(비용 0.14%) 분해 정합성 검증
6. test_06_period_split_inheritance: 기간 분할 상태 승계 100% 완전 일치
========================================================================================
"""

import unittest
import pandas as pd
import numpy as np

from backtest.mean_reversion_15m_engine import (
    run_mean_reversion_backtest,
    prepare_mean_reversion_indicators
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


class TestMeanReversion15mEngine(unittest.TestCase):
    def test_01_mr0_execution_and_causality(self):
        """[검증 1] MR0: RSI <= 25 발생 봉(t) 완성 -> 익봉(t+1) 시가 체결 인과성"""
        df = create_synthetic_candles(n_bars=35, base_price=100000.0)
        dates = df.index
        # 20봉에서 급락 발생 -> 20봉 완성 시 RSI <= 25 달성
        df.loc[dates[20], "opening_price"] = 100000.0
        df.loc[dates[20], "high_price"] = 100000.0
        df.loc[dates[20], "low_price"] = 90000.0
        df.loc[dates[20], "trade_price"] = 90000.0

        ind = prepare_mean_reversion_indicators({"KRW-BTC": df})["KRW-BTC"]
        rsi_20 = ind.loc[dates[20], "rsi14"]
        self.assertLessEqual(rsi_20, 25.0)

        # 21봉 시가 90,000에 진입 -> 22봉에서 고가 93,000으로 TP(+2%) 달성
        df.loc[dates[21], "opening_price"] = 90000.0
        df.loc[dates[21], "high_price"] = 90500.0
        df.loc[dates[21], "low_price"] = 89800.0
        df.loc[dates[21], "trade_price"] = 90200.0

        df.loc[dates[22], "opening_price"] = 90200.0
        df.loc[dates[22], "high_price"] = 93000.0
        df.loc[dates[22], "low_price"] = 90100.0
        df.loc[dates[22], "trade_price"] = 92500.0

        res = run_mean_reversion_backtest(
            {"KRW-BTC": df},
            strategy_model="MR0",
            capital=1_000_000.0
        )
        trades = res["trades"]
        self.assertGreater(len(trades), 0)
        t0 = trades[0]
        # 20봉 완성 후 21봉 시가에 정확히 진입했는지 검증
        self.assertEqual(t0["entry_dt"], str(dates[21]))
        self.assertEqual(t0["raw_entry_p"], 90000.0)


    def test_02_mr1_bounce_confirmation(self):
        """[검증 2] MR1: RSI <= 25 도달 후 30 상향 돌파 시 익봉 시가 진입"""
        df = create_synthetic_candles(n_bars=35, base_price=100000.0)
        dates = df.index
        # 20~23봉 급락 -> 23봉 RSI <= 25
        for k in range(20, 24):
            df.loc[dates[k], "opening_price"] = 100000.0 - (k - 19) * 2500.0
            df.loc[dates[k], "low_price"] = 100000.0 - (k - 19) * 3000.0
            df.loc[dates[k], "trade_price"] = 100000.0 - (k - 19) * 2500.0
            df.loc[dates[k], "high_price"] = df.loc[dates[k], "opening_price"]

        # 24봉: 강한 반등 양봉 -> RSI 30 상향 돌파
        df.loc[dates[24], "opening_price"] = 90000.0
        df.loc[dates[24], "low_price"] = 89900.0
        df.loc[dates[24], "trade_price"] = 94000.0
        df.loc[dates[24], "high_price"] = 94100.0

        ind = prepare_mean_reversion_indicators({"KRW-BTC": df})["KRW-BTC"]
        self.assertLessEqual(ind.loc[dates[23], "rsi14"], 25.0)
        self.assertGreater(ind.loc[dates[24], "rsi14"], 30.0)

        # 25봉 시가 진입
        df.loc[dates[25], "opening_price"] = 94000.0
        df.loc[dates[26], "high_price"] = 97000.0

        res = run_mean_reversion_backtest(
            {"KRW-BTC": df},
            strategy_model="MR1",
            capital=1_000_000.0
        )
        trades = res["trades"]
        self.assertGreater(len(trades), 0)
        self.assertEqual(trades[0]["entry_dt"], str(dates[25]))

    def test_03_mr2_bb_recovery(self):
        """[검증 3] MR2: 볼린저 밴드 하단 이탈 후 밴드 안으로 복귀 양봉 마감 시 진입"""
        df = create_synthetic_candles(n_bars=35, base_price=100000.0)
        dates = df.index

        # 22봉: 볼린저 밴드 하단 밖으로 급락 이탈 (저가 95,000, 종가 95,500)
        df.loc[dates[22], "opening_price"] = 99000.0
        df.loc[dates[22], "low_price"] = 95000.0
        df.loc[dates[22], "trade_price"] = 95500.0
        df.loc[dates[22], "high_price"] = 99000.0

        # 23봉: 밴드 하단 위로 복귀하는 양봉 마감 (시가 95,500, 저가 95,400, 종가 99,500)
        df.loc[dates[23], "opening_price"] = 95500.0
        df.loc[dates[23], "low_price"] = 95400.0
        df.loc[dates[23], "high_price"] = 99600.0
        df.loc[dates[23], "trade_price"] = 99500.0

        ind = prepare_mean_reversion_indicators({"KRW-BTC": df})["KRW-BTC"]
        bb_lower_23 = ind.loc[dates[23], "bb_lower"]
        self.assertGreaterEqual(99500.0, bb_lower_23)

        # 24봉 시가 진입
        df.loc[dates[24], "opening_price"] = 99500.0
        df.loc[dates[25], "high_price"] = 103000.0

        res = run_mean_reversion_backtest(
            {"KRW-BTC": df},
            strategy_model="MR2",
            capital=1_000_000.0
        )
        trades = res["trades"]
        self.assertGreater(len(trades), 0)
        self.assertEqual(trades[0]["entry_dt"], str(dates[24]))

    def test_04_conservative_sl_priority(self):
        """[검증 4] 동일 봉에서 고가 TP와 저가 SL 동시 도달 시 보수적 STOP_LOSS 우선"""
        df = create_synthetic_candles(n_bars=35, base_price=100000.0)
        dates = df.index
        # 20봉: 급락 발생 -> RSI <= 25 달성
        df.loc[dates[20], "opening_price"] = 100000.0
        df.loc[dates[20], "high_price"] = 100000.0
        df.loc[dates[20], "low_price"] = 90000.0
        df.loc[dates[20], "trade_price"] = 90000.0

        # 21봉 시가 90,000 진입 후 바로 그 봉에서 고가 +5%, 저가 -5% 동시 터치
        df.loc[dates[21], "opening_price"] = 90000.0
        df.loc[dates[21], "high_price"] = 95000.0
        df.loc[dates[21], "low_price"] = 85000.0
        df.loc[dates[21], "trade_price"] = 90000.0

        res = run_mean_reversion_backtest(
            {"KRW-BTC": df},
            strategy_model="MR0",
            capital=1_000_000.0
        )
        trades = res["trades"]
        self.assertGreater(len(trades), 0)
        self.assertEqual(trades[0]["reason"], "STOP_LOSS")
        self.assertEqual(res["simultaneous_both_count"], 1)

    def test_05_gross_vs_net_accounting(self):
        """[검증 5] Gross PnL - Net PnL = Total Friction (수수료+슬리피지 잠식액) 정확성"""
        df = create_synthetic_candles(n_bars=35, base_price=100000.0)
        dates = df.index
        # 20봉 급락
        df.loc[dates[20], "opening_price"] = 100000.0
        df.loc[dates[20], "high_price"] = 100000.0
        df.loc[dates[20], "low_price"] = 90000.0
        df.loc[dates[20], "trade_price"] = 90000.0

        # 21봉 시가 90,000 진입
        df.loc[dates[21], "opening_price"] = 90000.0
        df.loc[dates[21], "high_price"] = 90500.0
        df.loc[dates[21], "low_price"] = 89800.0
        df.loc[dates[21], "trade_price"] = 90200.0

        # 22봉에서 고가 93,000으로 익절
        df.loc[dates[22], "opening_price"] = 90200.0
        df.loc[dates[22], "high_price"] = 93000.0
        df.loc[dates[22], "low_price"] = 90100.0
        df.loc[dates[22], "trade_price"] = 92500.0

        res = run_mean_reversion_backtest(
            {"KRW-BTC": df},
            strategy_model="MR0",
            capital=1_000_000.0,
            fee_rate=0.0005,
            slippage=0.0002
        )
        t = res["trades"][0]
        diff = t["gross_pnl"] - t["pnl"]
        self.assertAlmostEqual(t["friction_krw"], diff, places=4)
        self.assertGreater(t["friction_krw"], 0.0)

    def test_06_period_split_inheritance(self):
        """[검증 6] 분할 실행 시 상태 승계 100% 완전 일치 (Continuous == Split)"""
        df = create_synthetic_candles(n_bars=40, base_price=100000.0)
        dates = df.index
        # 15봉 급락
        df.loc[dates[15], "opening_price"] = 100000.0
        df.loc[dates[15], "high_price"] = 100000.0
        df.loc[dates[15], "low_price"] = 90000.0
        df.loc[dates[15], "trade_price"] = 90000.0

        # 16봉 시가 진입
        df.loc[dates[16], "opening_price"] = 90000.0
        df.loc[dates[16], "high_price"] = 90500.0
        df.loc[dates[16], "low_price"] = 89800.0
        df.loc[dates[16], "trade_price"] = 90200.0

        # 30봉에서 익절
        df.loc[dates[30], "opening_price"] = 90200.0
        df.loc[dates[30], "high_price"] = 93000.0
        df.loc[dates[30], "low_price"] = 90100.0
        df.loc[dates[30], "trade_price"] = 92500.0


        dfs = {"KRW-BTC": df}
        # 1. 연속 1회 실행
        full_res = run_mean_reversion_backtest(dfs, strategy_model="MR0", capital=1_000_000.0, sizing_mode="strict_fixed")

        # 2. 25봉 기준으로 분할 실행
        split_dt = dates[25]
        p1_res = run_mean_reversion_backtest(
            dfs,
            strategy_model="MR0",
            capital=1_000_000.0,
            end_date=str(split_dt),
            liquidate_at_end=False,
            sizing_mode="strict_fixed"
        )
        p2_res = run_mean_reversion_backtest(
            dfs,
            strategy_model="MR0",
            capital=1_000_000.0,
            start_date=str(dates[26]),
            initial_positions=p1_res["terminal_positions"],
            initial_pending_orders=p1_res["terminal_pending_orders"],
            initial_cash=p1_res["terminal_cash"],
            liquidate_at_end=True,
            sizing_mode="strict_fixed"
        )

        combined_trades = p1_res["trades"] + p2_res["trades"]
        self.assertEqual(len(full_res["trades"]), len(combined_trades))
        self.assertAlmostEqual(full_res["final_equity"], p2_res["final_equity"], places=2)


if __name__ == "__main__":
    unittest.main()
