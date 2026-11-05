#!/usr/bin/env python3
"""
Unit tests for strategy/keltner_trend.py:
- Keltner channel calculations
- Signal generation (BUY, EXIT, HOLD)
- Boundary condition checks
"""

import unittest
import numpy as np
import pandas as pd
from strategy.keltner_trend import calculate_keltner_channel, get_keltner_signals


class TestKeltnerTrend(unittest.TestCase):
    def setUp(self):
        # Create a synthetic 4H dataset with 250 bars
        np.random.seed(42)
        n = 250
        dates = pd.date_range("2026-01-01", periods=n, freq="4h")
        
        # Base price around 3,000,000
        trend = np.linspace(2500000, 3500000, n)
        noise = np.random.normal(0, 20000, n)
        close = trend + noise
        high = close + np.random.uniform(5000, 25000, n)
        low = close - np.random.uniform(5000, 25000, n)
        open_p = close + np.random.uniform(-10000, 10000, n)
        
        self.df = pd.DataFrame({
            "dt": dates,
            "open": open_p,
            "high": high,
            "low": low,
            "close": close,
            "volume": np.random.uniform(100, 1000, n)
        })

    def test_keltner_channel_calculation(self):
        res_df = calculate_keltner_channel(self.df, ema_period=20, atr_period=14, multiplier=2.0, macro_ema_period=200)
        
        # Check required columns exist
        self.assertIn("ema20", res_df.columns)
        self.assertIn("ema200", res_df.columns)
        self.assertIn("atr14", res_df.columns)
        self.assertIn("kc_upper", res_df.columns)
        self.assertIn("kc_lower", res_df.columns)
        
        # Upper band must be greater than ema20
        last_row = res_df.iloc[-1]
        self.assertGreater(last_row["kc_upper"], last_row["ema20"])
        self.assertLess(last_row["kc_lower"], last_row["ema20"])
        
        # Check band width equals 4 * ATR
        band_width = last_row["kc_upper"] - last_row["kc_lower"]
        expected_width = 4.0 * last_row["atr14"]
        self.assertAlmostEqual(band_width, expected_width, places=4)

    def test_signals_buy_on_breakout_above_200_ema(self):
        # Modify last row to break out above upper band while above 200 EMA
        df = self.df.copy()
        calc = calculate_keltner_channel(df)
        upper = calc["kc_upper"].iloc[-1]
        ema200 = calc["ema200"].iloc[-1]
        
        # Push close above both
        breakout_price = max(upper, ema200) + 100000
        df.loc[df.index[-1], "close"] = breakout_price
        df.loc[df.index[-1], "high"] = breakout_price + 10000
        
        sig = get_keltner_signals(df)
        self.assertTrue(sig["valid"])
        self.assertEqual(sig["signal"], "BUY")
        self.assertTrue(sig["is_macro_bull"])
        self.assertTrue(sig["is_breakout"])

    def test_signals_exit_when_below_20_ema(self):
        df = self.df.copy()
        calc = calculate_keltner_channel(df)
        ema20 = calc["ema20"].iloc[-1]
        
        # Push close below 20 EMA
        drop_price = ema20 - 50000
        df.loc[df.index[-1], "close"] = drop_price
        df.loc[df.index[-1], "low"] = drop_price - 10000
        
        sig = get_keltner_signals(df)
        self.assertTrue(sig["valid"])
        self.assertEqual(sig["signal"], "EXIT")
        self.assertTrue(sig["is_exit"])

    def test_signals_hold_when_below_200_ema(self):
        df = self.df.copy()
        calc = calculate_keltner_channel(df)
        ema200 = calc["ema200"].iloc[-1]
        
        # Push close below 200 EMA even if it looks high
        df.loc[df.index[-1], "close"] = ema200 - 10000
        
        sig = get_keltner_signals(df)
        self.assertTrue(sig["valid"])
        self.assertFalse(sig["is_macro_bull"])
        self.assertNotEqual(sig["signal"], "BUY")


if __name__ == "__main__":
    unittest.main()
