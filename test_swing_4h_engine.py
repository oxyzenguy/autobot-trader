#!/usr/bin/env python3
"""
Unit tests for 4-hour Swing Backtest Engine (backtest/swing_4h_engine.py).
"""

import unittest
from datetime import datetime, timedelta
import pandas as pd
import numpy as np

from backtest.swing_4h_engine import (
    get_tick_size,
    quantize_price,
    prepare_swing_indicators,
    run_sw1_simulation,
    compute_swing_metrics
)


class TestSwing4hEngine(unittest.TestCase):
    def setUp(self):
        np.random.seed(42)
        base_dt = datetime(2024, 9, 1, 0, 0)
        n_bars = 120

        dts = [base_dt + timedelta(hours=4 * i) for i in range(n_bars)]
        # Price trend: first 30 bars flat around 1000, then surges to 1500, then drops
        prices = [1000.0]
        for i in range(1, 30):
            prices.append(prices[-1] + np.random.uniform(-5.0, 5.0))
        for i in range(30, 70):
            prices.append(prices[-1] + np.random.uniform(5.0, 20.0))  # strong bull
        for i in range(70, n_bars):
            prices.append(prices[-1] + np.random.uniform(-20.0, 5.0))  # bear

        df = pd.DataFrame({
            "dt": dts,
            "opening_price": prices,
            "high_price": [p + 10.0 for p in prices],
            "low_price": [p - 10.0 for p in prices],
            "trade_price": prices,
            "volume": [100.0] * 30 + [500.0] * 40 + [100.0] * 50,
            "value": [100.0 * p for p in prices]
        })
        self.synth_dfs = {"KRW-BTC": df.set_index("dt")}

    def test_indicators_preparation(self):
        prepared = prepare_swing_indicators(self.synth_dfs)
        df = prepared["KRW-BTC"]

        self.assertIn("high_n", df.columns)
        self.assertIn("vol_ma_n", df.columns)
        self.assertIn("atr14", df.columns)
        self.assertIn("breakout_signal", df.columns)

        # Ensure atr14 is positive
        valid_atr = df["atr14"].dropna()
        self.assertTrue((valid_atr > 0).all())

    def test_sw1_simulation_accounting_and_audit(self):
        prepared = prepare_swing_indicators(self.synth_dfs)
        res = run_sw1_simulation(prepared, capital=1_000_000.0, max_positions=1, warmup_bars=25)

        trades = res["trades"]
        self.assertGreater(len(trades), 0)

        # 1. Total trades == sum of exit reason categories
        reasons_sum = res["initial_sl_count"] + res["trailing_stop_count"]
        # Plus any END_OF_DATA
        end_data_count = sum(1 for t in trades if t["reason"] == "END_OF_DATA")
        self.assertEqual(len(trades), reasons_sum + end_data_count)

        # 2. Sum of trade PnLs == total asset change
        tot_pnl = sum(t["pnl"] for t in trades)
        asset_change = res["final_cash"] - 1_000_000.0
        self.assertAlmostEqual(tot_pnl, asset_change, delta=1.0)

    def test_state_inheritance_matches_continuous(self):
        prepared = prepare_swing_indicators(self.synth_dfs)
        df = prepared["KRW-BTC"]
        split_dt = df.index[len(df) // 2]

        # Continuous run
        res_cont = run_sw1_simulation(prepared, capital=1_000_000.0, max_positions=1, warmup_bars=25)

        # Split run: Part 1
        res_part1 = run_sw1_simulation(
            prepared, capital=1_000_000.0, max_positions=1, warmup_bars=25,
            end_date=str(split_dt), liquidate_at_end=False
        )

        # Split run: Part 2 with inheritance
        res_part2 = run_sw1_simulation(
            prepared, capital=1_000_000.0, max_positions=1, warmup_bars=25,
            start_date=str(split_dt),
            initial_cash=res_part1["final_cash"],
            initial_positions=res_part1["unclosed_positions"],
            initial_trades=res_part1["trades"],
            liquidate_at_end=True
        )

        # Trades count and final cash must match
        self.assertEqual(len(res_cont["trades"]), len(res_part2["trades"]))
        self.assertAlmostEqual(res_cont["final_cash"], res_part2["final_cash"], delta=1.0)


if __name__ == "__main__":
    unittest.main()
