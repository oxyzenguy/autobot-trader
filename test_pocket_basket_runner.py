#!/usr/bin/env python3
"""
Unit tests for PocketBasketResetRunner logic
"""
import unittest
from unittest.mock import MagicMock, patch
import os
import tempfile
import json
import shutil

from utils.pocket_basket_core import (
    PocketBasketResetRunner,
    adjust_price_to_tick,
    get_upbit_tick_size
)


class TestPocketBasketRunner(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.state_file = os.path.join(self.test_dir, "test_pocket_basket_state.json")
        self.lock_file = os.path.join(self.test_dir, "test_pocket_basket.lock")
        self.db_file = os.path.join(self.test_dir, "test_pocket_trades.db")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_tick_size_and_adjustment(self):
        # ETH: 3,150,000 KRW -> tick is 1000.0
        self.assertEqual(get_upbit_tick_size(3_150_000), 1000.0)
        self.assertEqual(adjust_price_to_tick(3_150_450, method="floor"), 3_150_000.0)
        self.assertEqual(adjust_price_to_tick(3_150_450, method="ceil"), 3_151_000.0)

        # SOL: 137,800 KRW -> tick is 100.0
        self.assertEqual(get_upbit_tick_size(137_800), 100.0)
        self.assertEqual(adjust_price_to_tick(137_845, method="floor"), 137_800.0)
        self.assertEqual(adjust_price_to_tick(137_845, method="ceil"), 137_900.0)

        # XRP: 1,784 KRW -> tick is 1.0
        self.assertEqual(get_upbit_tick_size(1_784), 1.0)
        self.assertEqual(adjust_price_to_tick(1784.6, method="floor"), 1784.0)
        self.assertEqual(adjust_price_to_tick(1784.2, method="ceil"), 1785.0)

    @patch("utils.pocket_basket_core.UpbitPocketClient")
    def test_initial_cycle_and_orders(self, mock_client_cls):
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_client.get_balance.return_value = 100_000.0

        # Mock market buy
        mock_client.buy_market_order_and_wait.return_value = {
            "status": "FILLED",
            "uuid": "buy-uuid-1",
            "price": 3_000_000.0,
            "volume": 0.00333333,
            "funds": 10_000.0,
            "fee": 5.0
        }
        # Mock limit orders
        mock_client.create_limit_order.side_effect = [
            {"uuid": "sell-tp-uuid"},
            {"uuid": "buy-ladder-1"},
            {"uuid": "buy-ladder-2"},
            {"uuid": "buy-ladder-3"},
        ]

        runner = PocketBasketResetRunner(
            market="KRW-ETH",
            step_pct=0.018,
            profit_pct=0.008,
            unit_krw=10_000.0,
            max_levels=200
        )
        runner.state_file = self.state_file
        runner.lock_file = self.lock_file
        runner.db_file = self.db_file
        runner.init_db()

        state = runner._create_empty_state()
        runner.start_new_cycle(state)

        # Verify state
        self.assertEqual(state["cycle_count"], 1)
        self.assertEqual(state["active_levels"], 1)
        self.assertEqual(state["vwap"], 3_000_000.0)
        self.assertIsNotNone(state["pending_sell"])
        self.assertEqual(state["pending_sell"]["uuid"], "sell-tp-uuid")
        # Profit: 3,000,000 * 1.008 = 3,024,000
        self.assertEqual(state["pending_sell"]["price"], 3_024_000.0)
        # 3 pending buy orders
        self.assertEqual(len(state["pending_buys"]), 3)

    @patch("utils.pocket_basket_core.UpbitPocketClient")
    def test_custom_pocket_name_and_isolation(self, mock_client_cls):
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_client.get_balance.return_value = 100_000.0

        runner = PocketBasketResetRunner(
            market="KRW-SOL",
            pocket_name="pocket2_basket",
            env_prefix="UPBIT_POCKET2"
        )
        mock_client_cls.assert_called_with(env_prefix="UPBIT_POCKET2")
        self.assertIn("pocket2_basket_state_KRW_SOL.json", runner.state_file)
        self.assertIn("pocket2_basket_trade_history.db", runner.db_file)


if __name__ == "__main__":
    unittest.main()
