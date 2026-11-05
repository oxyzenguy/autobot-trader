#!/usr/bin/env python3
"""
test_legacy_eth_trailing_exit.py
================================
Unit tests for legacy_eth_trailing_exit.py
"""

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from legacy_eth_trailing_exit import (
    LegacyEthTrailingRunner,
    TRIGGER_PRICE,
    TRAILING_DROP_PCT,
    TOTAL_INITIAL_QTY,
    TOTAL_TRANCHES,
    TRANCHE_PCT,
)


class FakeUpbitClient:
    def __init__(self, eth_balance: float = 0.40756151):
        self.eth_balance = eth_balance
        self.order_counter = 0
        self.orders = {}

    def get_balance(self, ticker: str):
        if ticker == "ETH":
            return self.eth_balance
        return 1_000_000.0

    def sell_market_order(self, market: str, volume: float):
        self.order_counter += 1
        uuid = f"fake-legacy-sell-uuid-{self.order_counter}"
        self.eth_balance -= volume
        self.orders[uuid] = {
            "uuid": uuid,
            "state": "done",
            "market": market,
            "side": "ask",
            "ord_type": "market",
            "price": None,
            "executed_volume": volume,
            "paid_fee": volume * 3_650_000.0 * 0.0005,
            "trades": [{"funds": volume * 3_650_000.0}],
        }
        return {"uuid": uuid}

    def get_order(self, uuid: str):
        return self.orders.get(uuid)


class TestLegacyEthTrailingExit(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.state_file = Path(self.temp_dir.name) / "test_legacy_eth_state.json"
        self.runner = LegacyEthTrailingRunner(
            trigger_price=3_620_000.0,
            trailing_drop_pct=0.015,
            cooldown_hours=12.0,
            total_initial_qty=0.40756151,
            total_tranches=10,
            state_file=self.state_file,
        )
        self.fake_client = FakeUpbitClient(eth_balance=0.40756151)
        self.runner.upbit = self.fake_client

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_01_init_and_state(self):
        """Initial state should be WAITING_TRIGGER with 0 completed tranches."""
        state = self.runner.state
        self.assertEqual(state["current_status"], "WAITING_TRIGGER")
        self.assertEqual(state["completed_tranches"], 0)
        self.assertEqual(state["total_tranches"], 10)
        self.assertAlmostEqual(state["remaining_qty"], 0.40756151, places=7)
        self.assertFalse(state["active_trailing"])
        self.assertEqual(state["peak_price"], 0.0)

    @patch("legacy_eth_trailing_exit.send_telegram_alert")
    def test_02_trigger_activation_and_peak(self, mock_tg):
        """When price >= 3,620,000 KRW, trailing activates and tracks peak."""
        # 1. Price below trigger
        with patch.object(self.runner, "get_current_price", return_value=3_550_000.0):
            self.runner.run_tick()
            self.assertFalse(self.runner.state["active_trailing"])
            self.assertEqual(self.runner.state["current_status"], "WAITING_TRIGGER")

        # 2. Price breaks 3,620,000 KRW
        with patch.object(self.runner, "get_current_price", return_value=3_630_000.0):
            self.runner.run_tick()
            self.assertTrue(self.runner.state["active_trailing"])
            self.assertEqual(self.runner.state["peak_price"], 3_630_000.0)
            self.assertEqual(self.runner.state["current_status"], "TRAILING")

        # 3. Price surges higher
        with patch.object(self.runner, "get_current_price", return_value=3_700_000.0):
            self.runner.run_tick()
            self.assertTrue(self.runner.state["active_trailing"])
            self.assertEqual(self.runner.state["peak_price"], 3_700_000.0)

    @patch("legacy_eth_trailing_exit.send_telegram_alert")
    def test_03_slip_below_floor_deactivates_without_selling(self, mock_tg):
        """If price falls below 3,620,000 KRW, trailing is paused to prevent loss."""
        # Activate at 3,625,000
        with patch.object(self.runner, "get_current_price", return_value=3_625_000.0):
            self.runner.run_tick()
            self.assertTrue(self.runner.state["active_trailing"])

        # Price dips to 3,610,000 (below 3.62M floor)
        with patch.object(self.runner, "get_current_price", return_value=3_610_000.0):
            self.runner.run_tick()
            self.assertFalse(self.runner.state["active_trailing"])
            self.assertEqual(self.runner.state["current_status"], "WAITING_TRIGGER")
            self.assertEqual(self.runner.state["completed_tranches"], 0)
            self.assertEqual(self.fake_client.eth_balance, 0.40756151)

    @patch("legacy_eth_trailing_exit.send_telegram_alert")
    @patch("legacy_eth_trailing_exit.log_real_trade")
    def test_04_trailing_callback_sell_execution(self, mock_db, mock_tg):
        """When price pulls back by 1.5% from peak (above 3.62M), tranche 1 is sold."""
        # 1. Peak reached at 3,700,000 KRW
        with patch.object(self.runner, "get_current_price", return_value=3_700_000.0):
            self.runner.run_tick()

        # Stop price = 3,700,000 * (1 - 0.015) = 3,644,500 KRW
        # Price drops to 3,644,000 KRW (<= 3,644,500 and >= 3,620,000)
        with patch.object(self.runner, "get_current_price", return_value=3_644_000.0):
            self.runner.run_tick()

        state = self.runner.state
        self.assertEqual(state["completed_tranches"], 1)
        expected_sold = round(0.40756151 * 0.10, 8)  # 0.04075615
        self.assertAlmostEqual(state["cumulative_executed_volume"], expected_sold, places=7)
        self.assertAlmostEqual(state["remaining_qty"], 0.40756151 - expected_sold, places=7)
        self.assertEqual(state["current_status"], "COOLDOWN")
        self.assertGreater(state["cooldown_until"], time.time())
        self.assertEqual(len(state["history"]), 1)
        mock_db.assert_called_once()

    @patch("legacy_eth_trailing_exit.send_telegram_alert")
    def test_05_cooldown_enforcement(self, mock_tg):
        """During cooldown period, runner must not place any order."""
        # Force cooldown state
        self.runner.state["completed_tranches"] = 1
        self.runner.state["cooldown_until"] = time.time() + 3600.0
        self.runner.state["current_status"] = "COOLDOWN"

        # Price at 3,800,000 KRW
        with patch.object(self.runner, "get_current_price", return_value=3_800_000.0):
            self.runner.run_tick()
            self.assertEqual(self.runner.state["completed_tranches"], 1)
            self.assertEqual(self.runner.state["current_status"], "COOLDOWN")

        # Once cooldown passes
        self.runner.state["cooldown_until"] = time.time() - 10.0
        with patch.object(self.runner, "get_current_price", return_value=3_610_000.0):
            self.runner.run_tick()
            self.assertEqual(self.runner.state["current_status"], "WAITING_TRIGGER")

    @patch("legacy_eth_trailing_exit.send_telegram_alert")
    @patch("legacy_eth_trailing_exit.log_real_trade")
    def test_06_tenth_tranche_cleans_remaining_dust(self, mock_db, mock_tg):
        """At tranche 10, exact remaining balance is sold and marked COMPLETED."""
        # 9 tranches completed, 0.04075616 remaining
        sold_so_far = round(0.40756151 * 0.10, 8) * 9
        rem = round(0.40756151 - sold_so_far, 8)
        self.runner.state["completed_tranches"] = 9
        self.runner.state["cumulative_executed_volume"] = sold_so_far
        self.runner.state["remaining_qty"] = rem
        self.runner.state["active_trailing"] = True
        self.runner.state["peak_price"] = 3_700_000.0
        self.runner.state["current_status"] = "TRAILING"

        # Trigger exit
        with patch.object(self.runner, "get_current_price", return_value=3_640_000.0):
            self.runner.run_tick()

        state = self.runner.state
        self.assertEqual(state["completed_tranches"], 10)
        self.assertEqual(state["remaining_qty"], 0.0)
        self.assertEqual(state["current_status"], "COMPLETED")
        self.assertEqual(state["cooldown_until"], 0.0)


if __name__ == "__main__":
    unittest.main()
