"""Production-loop regressions for residual BUY ledger and balance failures."""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

import main
import test_p1_alt_order_safety as alt_safety
from utils import db_logger
from test_p1_alt_order_safety import FakeExchange


class TestP1BuyBalanceSafety(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="autotrade-p1-buy-balance-")
        self.addCleanup(self.temp.cleanup)
        base = patch.object(main, "BASE_DIR", self.temp.name)
        database = patch.object(db_logger, "DB_PATH", os.path.join(self.temp.name, "trades.db"))
        base.start()
        database.start()
        self.addCleanup(database.stop)
        self.addCleanup(base.stop)
        self.runner = alt_safety.TestAltOrderSafetyCandidates()
        self.protected = main.PROTECTED_BALANCES["KRW-ETH"]

    def bear_state(self, quantity: float):
        return {
            "market": "KRW-ETH", "current_regime": "BEAR",
            "initial_entry_done": True, "bot_quantity": quantity,
            "bot_avg_price": 100_000.0,
            "tranches": [{"step": 1, "volume": quantity, "buy_price": 100_000.0}],
        }

    def bull_state(self, quantity: float = 0.0):
        return {
            "market": "KRW-ETH", "current_regime": "BULL",
            "initial_entry_done": True, "bot_quantity": quantity,
            "bot_avg_price": 100_000.0,
            "tranches": (
                [{"step": 1, "volume": quantity, "buy_price": 100_000.0}]
                if quantity else []
            ),
        }

    def run_bear_buy(self, exchange: FakeExchange, state: dict):
        return self.runner.run_tick(
            exchange, state, 100_000.0, False, "MARTINGALE_BUY_DIP", persist_state=True
        )

    def test_small_existing_position_adds_confirmed_market_fill(self):
        exchange = FakeExchange(self.protected + 0.03, 100_000.0)
        state = self.bear_state(0.03)
        _, logged = self.run_bear_buy(exchange, state)
        self.assertEqual(1, len(exchange.market_buys))
        self.assertAlmostEqual(0.13, state["bot_quantity"])
        self.assertAlmostEqual(0.13, exchange.holdings - self.protected)
        self.assertEqual("fake-buy", logged[0]["order_uuid"])
        self.assertAlmostEqual(0.1, logged[0]["volume"])

    def test_stale_existing_quantity_halts_before_buy(self):
        exchange = FakeExchange(self.protected + 0.03, 100_000.0)
        state = self.bear_state(0.1)
        saved, logged = self.run_bear_buy(exchange, state)
        self.assertEqual([], exchange.market_buys)
        self.assertEqual([], logged)
        self.assertTrue(saved[-1]["trading_halted"])
        self.assertAlmostEqual(0.1, state["bot_quantity"])

    def test_none_balance_with_empty_ledger_never_submits_buy(self):
        class NoBalanceExchange(FakeExchange):
            def get_balance(self, currency: str, verbose: bool = False):
                if currency == "ETH" and verbose:
                    return None
                return super().get_balance(currency, verbose)

        exchange = NoBalanceExchange(self.protected, 115_000.0)
        state = self.bull_state()
        saved, logged = self.runner.run_tick(exchange, state, 115_000.0, True, "BUY")
        self.assertEqual([], exchange.market_buys)
        self.assertEqual([], logged)
        self.assertTrue(saved[-1]["trading_halted"])

    def test_none_balance_field_with_empty_ledger_never_submits_buy(self):
        class MissingAmountExchange(FakeExchange):
            def get_balance(self, currency: str, verbose: bool = False):
                if currency == "ETH" and verbose:
                    return {"balance": None, "locked": 0.0}
                return super().get_balance(currency, verbose)

        exchange = MissingAmountExchange(self.protected, 115_000.0)
        saved, logged = self.runner.run_tick(exchange, self.bull_state(), 115_000.0, True, "BUY")
        self.assertEqual([], exchange.market_buys)
        self.assertEqual([], logged)
        self.assertTrue(saved[-1]["trading_halted"])

    def test_exception_balance_with_empty_ledger_never_submits_buy(self):
        exchange = FakeExchange(self.protected, 115_000.0)
        exchange.balance_fails = True
        state = self.bull_state()
        saved, logged = self.runner.run_tick(exchange, state, 115_000.0, True, "BUY")
        self.assertEqual([], exchange.market_buys)
        self.assertEqual([], logged)
        self.assertTrue(saved[-1]["trading_halted"])

    def test_none_available_balance_before_initial_buy_halts(self):
        class NoAvailableExchange(FakeExchange):
            def get_balance(self, currency: str, verbose: bool = False):
                if currency == "ETH" and not verbose:
                    return None
                return super().get_balance(currency, verbose)

        exchange = NoAvailableExchange(self.protected, 115_000.0)
        state = self.bull_state()
        state["initial_entry_done"] = False
        saved, logged = self.runner.run_tick(exchange, state, 115_000.0, True, "HOLD")
        self.assertEqual([], exchange.market_buys)
        self.assertEqual([], logged)
        self.assertTrue(saved[-1]["trading_halted"])

    def test_valid_balance_preserves_normal_buy(self):
        exchange = FakeExchange(self.protected, 115_000.0)
        state = self.bull_state()
        _, logged = self.runner.run_tick(exchange, state, 115_000.0, True, "BUY")
        self.assertEqual(1, len(exchange.market_buys))
        self.assertAlmostEqual(0.1, state["bot_quantity"])
        self.assertEqual("fake-buy", logged[0]["order_uuid"])

    def test_partial_limit_fill_only_adds_incremental_quantity(self):
        exchange, state, _ = self.runner.start_bear_orders(persist_state=True)
        identifier = exchange.limit_buys[0]["uuid"]
        exchange.settle_buy(identifier, "wait", [(0.02, 96_000.0)])
        self.runner.run_tick(exchange, state, 80_000.0, False, "HOLD",
                             complete_tick=True, persist_state=True)
        self.assertAlmostEqual(0.12, state["bot_quantity"])
        self.assertAlmostEqual(0.02, state["bear_limit_buys"][identifier]["executed_volume"])

    def test_rechecking_same_uuid_does_not_credit_fill_twice(self):
        exchange, state, _ = self.runner.start_bear_orders(persist_state=True)
        identifier = exchange.limit_buys[0]["uuid"]
        exchange.settle_buy(identifier, "wait", [(0.02, 96_000.0)])
        for _ in range(2):
            self.runner.run_tick(exchange, state, 80_000.0, False, "HOLD",
                                 complete_tick=True, persist_state=True)
            self.assertAlmostEqual(0.12, state["bot_quantity"])
            self.assertAlmostEqual(0.02, state["bear_limit_buys"][identifier]["executed_volume"])

    def test_protected_holdings_are_not_sold_for_new_position(self):
        exchange = FakeExchange(self.protected + 0.03, 100_000.0)
        state = self.bear_state(0.1)
        self.run_bear_buy(exchange, state)
        self.assertGreaterEqual(exchange.holdings, self.protected)
        self.assertLessEqual(sum(exchange.limit_sells), exchange.holdings - self.protected + 1e-12)

    def test_trailing_recovery_halts_buy_and_sell(self):
        exchange = FakeExchange(self.protected + 0.1, 115_000.0)
        state = self.bull_state(0.1)
        recovery_path = main.get_trailing_exit_recovery_path("KRW-ETH")
        with open(recovery_path, "w", encoding="utf-8") as handle:
            handle.write('{"kind":"TRAILING_STOP_EXIT","market":"KRW-ETH",'
                         '"uuid":null,"volume":0.1,"avg_price":100000,"cycles":0}')
        _, logged = self.runner.run_tick(exchange, state, 115_000.0, True, "BUY")
        self.assertEqual([], exchange.market_buys)
        self.assertEqual([], exchange.limit_sells)
        self.assertEqual([], logged)
        self.assertTrue(os.path.exists(recovery_path))


if __name__ == "__main__":
    unittest.main(verbosity=2)
