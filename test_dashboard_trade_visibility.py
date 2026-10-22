"""Regression coverage for intrabar fills and pending-order timeline ordering."""

from __future__ import annotations

import unittest

import pandas as pd

from utils.dashboard_data import filter_trades_for_candles, sort_timeline_items


class TestDashboardTradeVisibility(unittest.TestCase):
    def test_current_hour_fill_is_visible_before_next_candle(self):
        candles = pd.DatetimeIndex(["2026-10-07 03:00:00", "2026-10-07 04:00:00"])
        trades = pd.DataFrame({"timestamp": ["2026-10-07 04:17:59"], "volume": [0.00549753]})
        visible = filter_trades_for_candles(trades, candles, "minute60")
        self.assertEqual(1, len(visible))
        self.assertAlmostEqual(0.00549753, visible.iloc[0]["volume"])
        self.assertNotIn("dt", trades.columns)

    def test_hourly_window_includes_intrabar_but_excludes_next_bar(self):
        candles = pd.DatetimeIndex(["2026-10-07 03:00:00", "2026-10-07 04:00:00"])
        times = ["2026-10-07 02:59:59", "2026-10-07 03:00:00",
                 "2026-10-07 04:59:59", "2026-10-07 05:00:00"]
        visible = filter_trades_for_candles(pd.DataFrame({"timestamp": times}), candles, "minute60")
        self.assertEqual(times[1:3], visible["timestamp"].tolist())

    def test_other_intervals_include_the_whole_last_candle(self):
        for interval, start, inside, end in (
            ("minute240", "2026-10-07 01:00:00", "2026-10-07 04:17:59", "2026-10-07 05:00:00"),
            ("day", "2026-10-06 09:00:00", "2026-10-07 04:17:59", "2026-10-07 09:00:00"),
            ("week", "2026-10-05 09:00:00", "2026-10-11 23:59:59", "2026-10-12 09:00:00"),
            ("month", "2026-02-01 09:00:00", "2026-02-28 23:59:59", "2026-03-01 09:00:00"),
        ):
            with self.subTest(interval=interval):
                trades = pd.DataFrame({"timestamp": [inside, end]})
                visible = filter_trades_for_candles(trades, pd.DatetimeIndex([start]), interval)
                self.assertEqual([inside], visible["timestamp"].tolist())

    def test_empty_or_invalid_inputs_do_not_create_fills(self):
        candles = pd.DatetimeIndex(["2026-10-07 04:00:00"])
        self.assertTrue(filter_trades_for_candles(pd.DataFrame(), candles, "minute60").empty)
        self.assertTrue(filter_trades_for_candles(pd.DataFrame({"timestamp": ["bad"]}), candles, "minute60").empty)
        self.assertTrue(filter_trades_for_candles(pd.DataFrame({"timestamp": []}), pd.DatetimeIndex([]), "minute60").empty)

    def test_recent_fill_is_not_buried_below_older_pending_orders(self):
        def event(time: str, status: str):
            return {"상태": status, "시각": time, "구분": "", "주문가격": "",
                    "수량": "", "주문금액": "", "상세 / 손익": ""}
        pending = event("2026-10-06 19:59:51", "pending")
        filled = event("2026-10-07 04:17:59", "filled")
        replaced = event("2026-10-07 04:18:02", "pending")
        original = [pending, replaced, filled]
        self.assertEqual([replaced, filled, pending], sort_timeline_items(original))
        self.assertEqual([pending, replaced, filled], original)


if __name__ == "__main__":
    unittest.main()
