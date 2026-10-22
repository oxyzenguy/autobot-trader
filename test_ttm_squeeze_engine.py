#!/usr/bin/env python3
"""
Unit tests for TTM Squeeze backtest engine (backtest/ttm_squeeze_engine.py).
"""

import unittest
from datetime import datetime, timedelta
import pandas as pd
import numpy as np

from backtest.ttm_squeeze_engine import (
    get_tick_size,
    quantize_price,
    calculate_linear_regression_endpoint,
    prepare_ttm_squeeze_indicators,
    check_ttm_squeeze_signal,
    run_ttm_squeeze_backtest
)


class TestTTMSqueezeEngine(unittest.TestCase):
    def setUp(self):
        # Generate synthetic 15-minute price series with squeeze and expansion
        np.random.seed(42)
        base_dt = datetime(2025, 10, 1, 0, 0)
        n_bars = 120

        dts = [base_dt + timedelta(minutes=15 * i) for i in range(n_bars)]
        prices = [1000.0]
        # First 40 bars: low volatility compression (squeeze building)
        for i in range(1, 40):
            prices.append(prices[-1] + np.random.uniform(-1.0, 1.0))
        # Bars 40 to 80: volatility expansion upwards (squeeze firing)
        for i in range(40, 80):
            prices.append(prices[-1] + np.random.uniform(0.5, 4.0))
        # Bars 80 to 120: choppy oscillation
        for i in range(80, n_bars):
            prices.append(prices[-1] + np.random.uniform(-3.0, 3.0))

        df = pd.DataFrame({
            "dt": dts,
            "opening_price": prices,
            "high_price": [p + 2.0 for p in prices],
            "low_price": [p - 2.0 for p in prices],
            "trade_price": prices,
            "candle_acc_trade_volume": [100.0] * n_bars,
            "candle_acc_trade_price": [100.0 * p for p in prices]
        })
        self.raw_dfs = {"KRW-TEST": df}

    def test_tick_size_and_quantization(self):
        self.assertEqual(get_tick_size(100_000_000), 1000.0)
        self.assertEqual(get_tick_size(1500.0), 5.0)
        self.assertEqual(get_tick_size(15.0), 0.1)

        self.assertEqual(quantize_price(1503.2, mode="floor"), 1500.0)
        self.assertEqual(quantize_price(1503.2, mode="ceil"), 1505.0)
        self.assertEqual(quantize_price(1503.2, mode="round"), 1505.0)

    def test_linear_regression_endpoint(self):
        # A straight line y = 2x + 1 for x in 0..19
        s = pd.Series([2 * x + 1 for x in range(25)])
        ep = calculate_linear_regression_endpoint(s, length=20)
        # At index 19 (x=19), y should be exactly 2*19 + 1 = 39.0
        self.assertAlmostEqual(ep.iloc[19], 39.0, places=5)
        # At index 24 (x=24), endpoint for last 20 elements (5..24) is 2*24 + 1 = 49.0
        self.assertAlmostEqual(ep.iloc[24], 49.0, places=5)

    def test_indicator_preparation(self):
        prepared = prepare_ttm_squeeze_indicators(self.raw_dfs)
        df = prepared["KRW-TEST"]

        self.assertIn("bb_upper", df.columns)
        self.assertIn("kc_upper", df.columns)
        self.assertIn("squeeze_on", df.columns)
        self.assertIn("momentum", df.columns)
        self.assertIn("mom_diff", df.columns)
        self.assertIn("ema200_1h", df.columns)

        # Check that bb_upper >= bb_lower
        valid_bb = df[["bb_upper", "bb_lower"]].dropna()
        self.assertTrue((valid_bb["bb_upper"] >= valid_bb["bb_lower"]).all())

        # Check squeeze_on is boolean
        self.assertEqual(df["squeeze_on"].dtype, bool)

    def test_signal_generation_lookahead_free(self):
        prepared = prepare_ttm_squeeze_indicators(self.raw_dfs)
        df = prepared["KRW-TEST"]

        # Signal check at early index should be False (< 40 bars)
        self.assertFalse(check_ttm_squeeze_signal("KRW-TEST", df, 10))

    def test_backtest_execution_and_dual_accounting(self):
        prepared = prepare_ttm_squeeze_indicators(self.raw_dfs)
        res = run_ttm_squeeze_backtest(
            prepared,
            strategy_model="SQ0",
            capital=1_000_000.0,
            max_positions=1,
            sizing_mode="strict_fixed"
        )

        self.assertIn("gross_pf", res)
        self.assertIn("net_pf", res)
        self.assertIn("tot_gross_pnl", res)
        self.assertIn("tot_net_pnl", res)
        self.assertIn("total_friction", res)

        # Total friction should equal gross_pnl - net_pnl
        self.assertAlmostEqual(res["tot_gross_pnl"] - res["tot_net_pnl"], res["total_friction"], delta=1.0)

    def test_state_inheritance(self):
        prepared = prepare_ttm_squeeze_indicators(self.raw_dfs)
        df = prepared["KRW-TEST"]
        mid_idx = len(df) // 2
        split_dt = df.index[mid_idx]

        # Part 1: Start to split_dt
        res1 = run_ttm_squeeze_backtest(
            prepared,
            strategy_model="SQ0",
            capital=1_000_000.0,
            end_date=str(split_dt),
            liquidate_at_end=False
        )

        # Part 2: split_dt to End, inheriting state
        res2 = run_ttm_squeeze_backtest(
            prepared,
            strategy_model="SQ0",
            capital=1_000_000.0,
            start_date=str(split_dt),
            initial_positions=res1["unclosed_positions"],
            initial_cash=res1["final_cash"],
            initial_trades=res1["trades"],
            liquidate_at_end=True
        )

        # Full run
        res_full = run_ttm_squeeze_backtest(
            prepared,
            strategy_model="SQ0",
            capital=1_000_000.0,
            liquidate_at_end=True
        )

        # Total trades in inherited run should match continuous run
        self.assertEqual(res2["total_trades"], res_full["total_trades"])


if __name__ == "__main__":
    unittest.main()
