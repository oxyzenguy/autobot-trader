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

    def test_bull_small_position_adds_only_confirmed_fill_and_preserves_tranche(self):
        for volume, price in ((0.1, 110_000.0), (0.04, 120_000.0)):
            with self.subTest(volume=volume), patch.dict(main._last_saved_state_strings, {}, clear=True):
                state = self.bull_state(0.03)
                original_tranche = state["tranches"][0].copy()
                exchange = FakeExchange(self.protected + 0.03, price)
                exchange.buy_volume = volume

                _, logged = self.runner.run_tick(
                    exchange, state, 100_000.0, True, "BUY", persist_state=True
                )
                persisted = main.load_strategy_state("KRW-ETH")
                expected_quantity = 0.03 + volume
                expected_average = (0.03 * 100_000.0 + volume * price) / expected_quantity
                self.assertEqual(1, len(exchange.market_buys))
                self.assertAlmostEqual(expected_quantity, exchange.holdings - self.protected)
                self.assertAlmostEqual(expected_quantity, state["bot_quantity"])
                self.assertAlmostEqual(expected_quantity, persisted["bot_quantity"])
                self.assertAlmostEqual(expected_average, state["bot_avg_price"])
                self.assertAlmostEqual(expected_average, persisted["bot_avg_price"])
                self.assertEqual(original_tranche, state["tranches"][0])
                self.assertEqual(original_tranche, persisted["tranches"][0])
                self.assertEqual(2, len(state["tranches"]))
                self.assertAlmostEqual(volume, state["tranches"][1]["volume"])
                self.assertAlmostEqual(volume, logged[0]["volume"])

                # The next BULL tick may repeat the signal, but must not replay the fill.
                _, repeated_logs = self.runner.run_tick(
                    exchange, persisted, 110_000.0, True, "BUY",
                    complete_tick=True, persist_state=True
                )
                self.assertAlmostEqual(expected_quantity, persisted["bot_quantity"])
                self.assertEqual(1, len(exchange.market_buys))
                self.assertEqual([], repeated_logs)

    def test_bull_small_position_wait_partial_remains_uncredited(self):
        class WaitingExchange(FakeExchange):
            def get_order(self, identifier: str, state: str | None = None):
                order = super().get_order(identifier, state)
                if identifier == "fake-buy":
                    order["state"] = "wait"
                return order

        exchange = WaitingExchange(self.protected + 0.03, 100_000.0)
        exchange.buy_volume = 0.04
        state = self.bull_state(0.03)
        _, logged = self.runner.run_tick(
            exchange, state, 100_000.0, True, "BUY",
            complete_tick=True, persist_state=True
        )
        persisted = main.load_strategy_state("KRW-ETH")
        self.assertAlmostEqual(0.07, exchange.holdings - self.protected)
        self.assertAlmostEqual(0.03, state["bot_quantity"])
        self.assertAlmostEqual(0.03, persisted["bot_quantity"])
        self.assertEqual([], logged)
        self.assertTrue(persisted["trading_halted"])
        self.assertEqual("fake-buy", persisted["pending_order_uuid"])

    def test_bull_small_position_fails_closed_on_bad_or_insufficient_balance(self):
        class BadBalanceExchange(FakeExchange):
            response = None

            def get_balance(self, currency: str, verbose: bool = False):
                if currency == "ETH" and verbose:
                    if isinstance(self.response, Exception):
                        raise self.response
                    return self.response
                return super().get_balance(currency, verbose)

        for response in (None, {"balance": None, "locked": 0.0},
                         ConnectionError("fake balance failure")):
            with self.subTest(response=response), patch.dict(main._last_saved_state_strings, {}, clear=True):
                exchange = BadBalanceExchange(self.protected + 0.03, 100_000.0)
                exchange.response = response
                state = self.bull_state(0.03)
                _, logged = self.runner.run_tick(
                    exchange, state, 100_000.0, True, "BUY", persist_state=True
                )
                self.assertEqual([], exchange.market_buys)
                self.assertEqual([], logged)
                self.assertTrue(main.load_strategy_state("KRW-ETH")["trading_halted"])
                self.assertAlmostEqual(0.03, state["bot_quantity"])

        exchange = FakeExchange(self.protected + 0.02, 100_000.0)
        state = self.bull_state(0.03)
        _, logged = self.runner.run_tick(
            exchange, state, 100_000.0, True, "BUY", persist_state=True
        )
        self.assertEqual([], exchange.market_buys)
        self.assertEqual([], logged)
        self.assertTrue(main.load_strategy_state("KRW-ETH")["trading_halted"])
        self.assertAlmostEqual(0.03, state["bot_quantity"])

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
