#!/usr/bin/env python3
"""
Unit tests for Top10 Scalper Entry Signal Ablation Engine (backtest/top10_entry_ablation_engine.py).
"""

import unittest
from datetime import datetime, timedelta
from pathlib import Path
import pandas as pd
import numpy as np

from backtest.top10_entry_ablation_engine import (
    get_tick_size,
    quantize_price,
    calculate_wilder_rsi,
    load_2year_hourly_candles,
    generate_common_trigger_events,
    run_entry_ablation_simulation,
    compute_comprehensive_metrics
)


class TestTop10EntryAblationEngine(unittest.TestCase):
    def setUp(self):
        np.random.seed(42)
        base_dt = datetime(2024, 10, 1, 0, 0)
        n_bars = 100

        dts = [base_dt + timedelta(hours=i) for i in range(n_bars)]
        prices = [1000.0]
        for i in range(1, n_bars):
            prices.append(prices[-1] + np.random.uniform(-10.0, 10.0))

        df = pd.DataFrame({
            "dt": dts,
            "opening_price": prices,
            "high_price": [p + 5.0 for p in prices],
            "low_price": [p - 5.0 for p in prices],
            "trade_price": prices,
            "candle_acc_trade_volume": [1000.0] * n_bars,
            "candle_acc_trade_price": [1000.0 * p for p in prices]
        })
        # Add required columns
        df["vol_24h"] = df["candle_acc_trade_price"].rolling(24).sum()
        df["ret_24h"] = 0.01
        df["ret_4h"] = 0.01
        df["natr"] = 2.0
        df["rsi14"] = calculate_wilder_rsi(df["trade_price"], 14)
        ma20 = df["trade_price"].rolling(20).mean()
        std20 = df["trade_price"].rolling(20).std(ddof=0)
        df["bb_upper"] = ma20 + 2 * std20
        df["bb_lower"] = ma20 - 2 * std20
        df["pct_b"] = (df["trade_price"] - df["bb_lower"]) / (df["bb_upper"] - df["bb_lower"] + 1e-9)
        df["tick_ratio"] = 0.001
        df["prev_high"] = df["high_price"].shift(1)
        df["is_bullish"] = df["trade_price"] > df["opening_price"]
        df["break_prev_high"] = df["trade_price"] > df["prev_high"]
        df["rsi_up"] = df["rsi14"] > df["rsi14"].shift(1)
        df["pct_b_up"] = df["pct_b"] > df["pct_b"].shift(1)

        self.synth_dfs = {"KRW-BTC": df.set_index("dt")}

    def test_tick_and_quantization(self):
        self.assertEqual(get_tick_size(50_000_000), 1000.0)
        self.assertEqual(get_tick_size(1500.0), 5.0)
        self.assertEqual(quantize_price(1503.2, mode="floor"), 1500.0)
        self.assertEqual(quantize_price(1503.2, mode="ceil"), 1505.0)

    def test_common_trigger_generation_independence(self):
        triggers, events_by_dt = generate_common_trigger_events(self.synth_dfs)
        self.assertIsInstance(triggers, list)
        self.assertIsInstance(events_by_dt, dict)
        if triggers:
            first_sig = triggers[0]
            self.assertIn("sig_id", first_sig)
            self.assertIn("dt", first_sig)
            self.assertIn("market", first_sig)

    def test_entry_models_execution(self):
        for model in ("E0", "E1", "E2", "E3"):
            res = run_entry_ablation_simulation(
                self.synth_dfs,
                entry_model=model,
                sizing_mode="unconstrained_signal"
            )
            self.assertIn("trades", res)
            self.assertIn("total_raw_triggers", res)
            metrics = compute_comprehensive_metrics(res["trades"])
            self.assertIn("gross_pf", metrics)
            self.assertIn("net_pf", metrics)


if __name__ == "__main__":
    unittest.main()
