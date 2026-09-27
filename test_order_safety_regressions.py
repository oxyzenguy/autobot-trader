"""Isolated reproductions for live order safety invariants."""

from __future__ import annotations

import copy
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import main


class StopAfterOneIteration(BaseException):
    """End the trading loop without letting it reach another market tick."""


class FakeExchange:
    """Keep the account and settled orders entirely in memory."""

    def __init__(self, available_eth: float, price: float) -> None:
        self.available_eth = available_eth
        self.price = price
        self.sells: list[float] = []

    def get_balance(self, currency: str, verbose: bool = False):
        if currency == "KRW":
            return 100_000.0
        if currency == "ETH" and verbose:
            return {"balance": self.available_eth, "locked": 0.0}
        raise AssertionError(f"Unexpected balance query: {currency}")

    def get_order(self, identifier: str, state: str | None = None):
        if identifier == "KRW-ETH" and state == "wait":
            return []
        if identifier == "fake-sell-1" and state is None:
            volume = self.sells[0]
            return {
                "state": "done",
                "executed_volume": str(volume),
                "trades": [{"funds": str(volume * self.price)}],
                "paid_fee": "0",
            }
        raise AssertionError(f"Unexpected order query: {identifier}, {state}")

    def sell_market_order(self, market: str, volume: float):
        assert market == "KRW-ETH"
        assert volume <= self.available_eth
        self.available_eth -= volume
        self.sells.append(volume)
        return {"uuid": "fake-sell-1"}

    def buy_market_order(self, market: str, amount: float):
        raise AssertionError(f"Unexpected buy: {market}, {amount}")


class TestOrderSafetyRegressions(unittest.TestCase):
    def run_one_market_tick(
        self, exchange: FakeExchange, state: dict, price: float,
        persist_state: bool = False, is_bull: bool = True
    ):
        """Exercise the real trading loop while replacing only external boundaries."""
        saved: list[dict] = []
        logged: list[dict] = []
        output = io.StringIO()
        load_state = main.load_strategy_state if persist_state else lambda market: state
        save_state = (
            main.save_strategy_state
            if persist_state
            else lambda market, value: saved.append(copy.deepcopy(value))
        )

        with (
            patch.multiple(
                main,
                acquire_market_process_lock=lambda market: object(),
                init_db=lambda: None,
                get_upbit_client=lambda: exchange,
                load_strategy_state=load_state,
                save_strategy_state=save_state,
                check_krw_balance_alert=lambda *args, **kwargs: 100_000.0,
                send_telegram_alert=lambda message: None,
                safe_get_current_price=lambda market: price,
                get_hybrid_regime_and_signals=lambda market: {
                    "is_bull": is_bull,
                    "signal": "HOLD",
                    "ma5": price,
                },
                log_real_trade=lambda **trade: logged.append(trade),
            ),
            patch.object(main.time, "sleep", side_effect=StopAfterOneIteration),
            redirect_stdout(output),
        ):
            with self.assertRaises(StopAfterOneIteration):
                main.run_trading_strategy("KRW-ETH")

        return saved, logged, output.getvalue()

    def test_restart_trailing_exit_records_and_clears_position(self):
        # Given: a fresh process loads an already active trailing stop in BULL.
        state = {
            "market": "KRW-ETH",
            "current_regime": "BULL",
            "trailing_stop_active": True,
            "trend_peak_price": 120_000.0,
            "bot_quantity": 0.1,
            "bot_avg_price": 100_000.0,
            "tranches": [{"step": 1, "volume": 0.1, "buy_price": 100_000.0}],
        }
        exchange = FakeExchange(
            available_eth=main.PROTECTED_BALANCES["KRW-ETH"] + 0.1,
            price=115_000.0,
        )

        # When: the live strategy confirms a market sell after a >3% peak drop.
        saved, logged, output = self.run_one_market_tick(exchange, state, 115_000.0)

        # Then: its completed sale is recorded and its position is cleared.
        self.assertEqual([0.1], exchange.sells)
        self.assertEqual(1, len(logged), output)
        self.assertEqual(0.0, state["bot_quantity"])
        self.assertEqual(0.0, saved[-1]["bot_quantity"])

    def test_restart_trailing_exit_persists_exchange_ledger_and_json(self):
        # Given: a previously activated trailing position stored outside the live state files.
        protected = main.PROTECTED_BALANCES["KRW-ETH"]
        state = {
            "market": "KRW-ETH",
            "current_regime": "BULL",
            "trailing_stop_active": True,
            "trend_peak_price": 120_000.0,
            "bot_quantity": 0.1,
            "bot_avg_price": 100_000.0,
            "tranches": [{"step": 1, "volume": 0.1, "buy_price": 100_000.0}],
        }
        exchange = FakeExchange(available_eth=protected + 0.1, price=115_000.0)
        with tempfile.TemporaryDirectory(prefix="autotrade-order-test-") as directory:
            state_path = os.path.join(directory, "state.json")
            with open(state_path, "w", encoding="utf-8") as fixture:
                json.dump(state, fixture)

            # When: the actual loader and atomic saver process the completed fake sell.
            with patch.object(main, "get_state_file_path", return_value=state_path):
                _, logged, output = self.run_one_market_tick(
                    exchange, state, 115_000.0, persist_state=True
                )
                reloaded = main.load_strategy_state("KRW-ETH")
                _, bot_quantity = main.get_bot_balance(exchange, "KRW-ETH", "ETH", reloaded)

            # Then: exchange holdings, the bot ledger and its persisted file agree.
            self.assertEqual([0.1], exchange.sells)
            self.assertAlmostEqual(protected, exchange.available_eth)
            self.assertEqual(0.0, bot_quantity)
            self.assertEqual(0.0, reloaded["bot_quantity"])
            self.assertEqual([], reloaded["tranches"])
            self.assertEqual("fake-sell-1", logged[0]["order_uuid"], output)
        self.assertFalse(os.path.exists(directory))

    def test_protected_balance_is_not_sellable(self):
        # Given: the bot's previously recorded holding is gone; only protected ETH remains.
        protected = main.PROTECTED_BALANCES["KRW-ETH"]
        state = {
            "market": "KRW-ETH",
            "current_regime": "BULL",
            "trailing_stop_active": False,
            "bot_quantity": 0.1,
            "bot_avg_price": 100_000.0,
            "tranches": [{"step": 1, "volume": 0.1, "buy_price": 100_000.0}],
        }
        exchange = FakeExchange(available_eth=protected, price=89_000.0)

        # When: the -10% stop-loss branch evaluates the real account balance.
        self.run_one_market_tick(exchange, state, 89_000.0)

        # Then: no order may consume any of the pre-existing protected ETH.
        self.assertEqual([], exchange.sells)
        self.assertEqual(protected, exchange.available_eth)

    def test_tranche_aggregate_sell_never_exceeds_bot_balance(self):
        class TrancheExchange(FakeExchange):
            def __init__(self, available_eth: float):
                super().__init__(available_eth, 115_000.0)
                self.limit_sells: list[float] = []

            def get_balance(self, currency: str, verbose: bool = False):
                if currency == "ETH" and not verbose:
                    return self.available_eth
                return super().get_balance(currency, verbose)

            def sell_limit_order(self, market: str, price: float, volume: float):
                self.limit_sells.append(volume)
                return {"uuid": "fake-limit-1"}

        protected = main.PROTECTED_BALANCES["KRW-ETH"]
        cases = (
            ("below", 0.3, 0.3, 0.2, 0.1),
            ("equal", 0.2, 0.2, 0.2, 0.0),
            ("above", 0.1, 0.2, 0.1, 0.0),
            ("above_with_remainder", 0.15, 0.2, 0.1, 0.05),
            ("zero_sellable", 0.1, 0.0, 0.0, 0.0),
            ("protected_only", 0.0, 0.2, 0.0, 0.0),
        )
        for name, account_excess, bot_quantity, expected_sell, expected_limit in cases:
            with self.subTest(name=name):
                state = {
                    "market": "KRW-ETH",
                    "current_regime": "BEAR",
                    "initial_entry_done": True,
                    "bot_quantity": bot_quantity,
                    "bot_avg_price": 115_000.0,
                    "tranches": [
                        {"step": 1, "volume": 0.1, "buy_price": 100_000.0},
                        {"step": 2, "volume": 0.1, "buy_price": 100_000.0},
                        {"step": 3, "volume": 0.1, "buy_price": 120_000.0},
                    ] if name == "below" else [
                        {"step": 1, "volume": 0.1, "buy_price": 100_000.0},
                        {"step": 2, "volume": 0.1, "buy_price": 100_000.0},
                    ],
                }
                exchange = TrancheExchange(protected + account_excess)

                saved, logged, output = self.run_one_market_tick(
                    exchange, state, 115_000.0, is_bull=False
                )

                self.assertNotIn("[CRITICAL] 메인 루프 에러", output)
                self.assertAlmostEqual(expected_sell, sum(exchange.sells), msg=output)
                self.assertAlmostEqual(protected + account_excess - expected_sell, exchange.available_eth)
                self.assertGreaterEqual(exchange.available_eth + 1e-9, protected)
                self.assertAlmostEqual(expected_limit, sum(exchange.limit_sells), msg=output)
                self.assertLessEqual(
                    sum(exchange.limit_sells), max(0.0, exchange.available_eth - protected) + 1e-9
                )
                if expected_sell:
                    self.assertEqual(expected_sell, sum(trade["volume"] for trade in logged))
                    self.assertAlmostEqual(account_excess - expected_sell, saved[-1]["bot_quantity"])

    def test_unavailable_open_order_list_cannot_count_as_cancelled(self):
        # Given: an open-order query returns an unavailable or malformed response.
        for response in (None, {"error": {"name": "too_many_requests"}}):
            with self.subTest(response=response):
                class UnavailableOrders:
                    def get_order(self, market: str, state: str):
                        assert market == "KRW-ETH" and state == "wait"
                        return response

                # When: the production cancel-all function checks before a replacement sale.
                cancelled = main.cancel_all_orders(UnavailableOrders(), "KRW-ETH")

                # Then: unknown open orders cannot be treated as confirmed cancelled.
                self.assertFalse(cancelled)

        # An actual transport exception is handled separately by the current code.
        class RaisedQuery:
            def get_order(self, market: str, state: str):
                raise ConnectionError("exchange unavailable")

        self.assertFalse(main.cancel_all_orders(RaisedQuery(), "KRW-ETH"))

        class EmptyOrders:
            def get_order(self, market: str, state: str):
                return []

        self.assertTrue(main.cancel_all_orders(EmptyOrders(), "KRW-ETH"))
