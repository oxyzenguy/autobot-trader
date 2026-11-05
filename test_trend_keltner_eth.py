#!/usr/bin/env python3
"""
Unit tests for trend_keltner_eth.py:
- Main bot exit completion detection
- Balance protection isolation
- Buy / Sell execution flow
- Emergency Stop Loss trigger
- 4H candle close evaluation
"""

import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

import pandas as pd
from trend_keltner_eth import KeltnerTrendRunner


class FakeUpbitClient:
    def __init__(self, krw: float = 1_000_000.0, eth: float = 0.5, price: float = 3_250_000.0):
        self.krw = krw
        self.eth = eth
        self.price = price
        self.orders = {}
        self.order_counter = 0

    def get_balance(self, currency: str):
        if currency == "KRW":
            return self.krw
        if currency == "ETH":
            return self.eth
        return 0.0

    def get_current_price(self, market: str):
        return self.price

    def get_orderbook(self, market: str):
        return [{"orderbook_units": [{"ask_price": self.price, "bid_price": self.price - 1000}]}]

    def buy_market_order(self, market: str, amount: float):
        self.order_counter += 1
        uuid = f"buy-order-{self.order_counter}"
        vol = round(amount / self.price, 6)
        self.krw -= amount
        self.eth += vol
        self.orders[uuid] = {
            "uuid": uuid,
            "state": "done",
            "executed_volume": str(vol),
            "trades": [{"funds": str(amount)}],
            "paid_fee": str(amount * 0.0005)
        }
        return {"uuid": uuid}

    def sell_market_order(self, market: str, volume: float):
        self.order_counter += 1
        uuid = f"sell-order-{self.order_counter}"
        funds = volume * self.price
        self.eth = max(0.0, self.eth - volume)
        self.krw += funds * 0.9995
        self.orders[uuid] = {
            "uuid": uuid,
            "state": "done",
            "executed_volume": str(volume),
            "trades": [{"funds": str(funds)}],
            "paid_fee": str(funds * 0.0005)
        }
        return {"uuid": uuid}

    def get_order(self, uuid: str):
        return self.orders.get(uuid, {"state": "cancel"})


class TestTrendKeltnerETH(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.state_file = Path(self.tmp_dir.name) / "test_keltner_state.json"
        self.fake_client = FakeUpbitClient(krw=1_000_000.0, eth=0.50756151)
        self.runner = KeltnerTrendRunner(
            market="KRW-ETH",
            allocated_budget=500_000.0,
            emergency_sl_pct=0.10,
            poll_interval=1.0,
            state_file=self.state_file
        )
        self.runner.upbit = self.fake_client

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_main_bot_exit_check(self):
        # 1. Main bot still holding
        with patch("trend_keltner_eth.MAIN_BOT_STATE_FILE", Path(self.tmp_dir.name) / "mb_holding.json"):
            with open(Path(self.tmp_dir.name) / "mb_holding.json", "w") as f:
                json.dump({"bot_quantity": 0.1165, "tranches": [{"id": 1}]}, f)
            self.assertFalse(self.runner.is_main_bot_exit_completed())

        # 2. Main bot completed
        with patch("trend_keltner_eth.MAIN_BOT_STATE_FILE", Path(self.tmp_dir.name) / "mb_done.json"):
            with open(Path(self.tmp_dir.name) / "mb_done.json", "w") as f:
                json.dump({"bot_quantity": 0.0, "tranches": []}, f)
            self.assertTrue(self.runner.is_main_bot_exit_completed())

    def test_safe_balances_deducts_protected_holdings(self):
        # Total eth is 0.50756151, protected is 0.40756151 -> safe is 0.10
        with patch("trend_keltner_eth.PROTECTED_BALANCES", {"KRW-ETH": 0.40756151}):
            krw, safe_eth = self.runner.get_safe_balances()
            self.assertEqual(krw, 1_000_000.0)
            self.assertAlmostEqual(safe_eth, 0.10, places=6)

    def test_execute_buy_and_sell_flow(self):
        with patch("trend_keltner_eth.send_telegram_alert"), patch("trend_keltner_eth.log_real_trade"):
            # 1. Buy
            bought = self.runner.execute_buy(3_250_000.0, "Breakout test")
            self.assertTrue(bought)
            self.assertTrue(self.runner.state["in_position"])
            self.assertGreater(self.runner.state["position_volume"], 0.0)
            self.assertAlmostEqual(self.runner.state["entry_price"], 3_250_000.0, delta=10.0)

            # 2. Normal Exit with profit at 3,500,000
            self.fake_client.price = 3_500_000.0
            sold = self.runner.execute_sell(3_500_000.0, "20 EMA Exit")
            self.assertTrue(sold)
            self.assertFalse(self.runner.state["in_position"])
            self.assertEqual(self.runner.state["position_volume"], 0.0)
            self.assertGreater(self.runner.state["realized_pnl_krw"], 0.0)
            self.assertEqual(self.runner.state["trade_count"], 1)
            self.assertEqual(self.runner.state["win_count"], 1)

    def test_emergency_stop_loss_triggered_at_minus_10pct(self):
        with patch("trend_keltner_eth.send_telegram_alert"), patch("trend_keltner_eth.log_real_trade"):
            # Setup an active position entered at 3,500,000
            self.runner.state["in_position"] = True
            self.runner.state["position_volume"] = 0.05
            self.runner.state["entry_price"] = 3_500_000.0
            self.runner.state["entry_cost_krw"] = 175_000.0
            self.runner.save_state()

            # Current price dropped to 3,140,000 (-10.3%)
            self.fake_client.get_orderbook = lambda m: [{"orderbook_units": [{"ask_price": 3_140_000.0}]}]
            self.fake_client.get_current_price = lambda m: 3_140_000.0

            # Mock 4H candle so it doesn't trigger 4H logic
            with patch("trend_keltner_eth.fetch_4h_candles_upbit", return_value=None):
                self.runner.run_tick()

            # Position should be closed via emergency SL
            self.assertFalse(self.runner.state["in_position"])
            self.assertEqual(self.runner.state["position_volume"], 0.0)
            self.assertEqual(self.runner.state["trade_count"], 1)
            self.assertEqual(self.runner.state["win_count"], 0)


if __name__ == "__main__":
    unittest.main()
