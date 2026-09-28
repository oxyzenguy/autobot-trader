"""Fake-exchange reproductions of altcoin order and ledger risks."""

from __future__ import annotations

import copy
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import main


class EndTick(BaseException):
    """Stop the production loop after the observed order decision."""


class FakeExchange:
    def __init__(self, holdings: float, price: float, open_orders: list[dict] | None = None):
        self.holdings = holdings
        self.price = price
        self.open_orders = open_orders if open_orders is not None else []
        self.balance_fails = False
        self.buy_volume = 0.1
        self.market_buys: list[float] = []
        self.limit_sells: list[float] = []
        self.cancelled: list[str] = []
        self.order_lookups: list[str] = []
        self.orders: dict[str, dict] = {}
        self.limit_buys: list[dict] = []
        self.order_error: Exception | None = None

    def get_balance(self, currency: str, verbose: bool = False):
        if currency == "KRW":
            return 100_000.0
        if currency == "ETH" and verbose:
            if self.balance_fails:
                raise ConnectionError("fake balance query failed")
            return {"balance": self.holdings, "locked": 0.0}
        if currency == "ETH":
            return self.holdings
        raise AssertionError(f"Unexpected balance query: {currency}")

    def get_order(self, identifier: str, state: str | None = None):
        if identifier == "KRW-ETH" and state == "wait":
            return copy.deepcopy(self.open_orders)
        self.order_lookups.append(identifier)
        if self.order_error is not None:
            raise self.order_error
        if identifier in self.orders:
            return copy.deepcopy(self.orders[identifier])
        if identifier == "basket-1":
            return {"state": "cancel"}
        if identifier == "fake-buy":
            return {
                "state": "done",
                "executed_volume": str(self.buy_volume),
                "trades": [{"funds": str(self.buy_volume * self.price)}],
                "paid_fee": "0",
            }
        raise AssertionError(f"Unexpected order query: {identifier}")

    def cancel_order(self, identifier: str):
        self.cancelled.append(identifier)
        self.open_orders = [order for order in self.open_orders if order["uuid"] != identifier]
        if identifier in self.orders:
            self.orders[identifier]["state"] = "cancel"
        return {"uuid": identifier}

    def buy_market_order(self, market: str, amount: float):
        assert market == "KRW-ETH"
        self.market_buys.append(amount)
        self.holdings += self.buy_volume
        return {"uuid": "fake-buy"}

    def sell_limit_order(self, market: str, price: float, volume: float):
        assert market == "KRW-ETH"
        self.limit_sells.append(volume)
        identifier = f"basket-new-{len(self.limit_sells)}"
        order = {"uuid": identifier, "side": "ask", "price": str(price), "state": "wait"}
        self.open_orders.append(order.copy())
        self.orders[identifier] = order
        return {"uuid": identifier}

    def buy_limit_order(self, market: str, price: float, volume: float):
        assert market == "KRW-ETH"
        identifier = f"limit-buy-{len(self.limit_buys) + 1}"
        order = {
            "uuid": identifier, "side": "bid", "price": str(price), "volume": str(volume),
            "state": "wait", "executed_volume": "0", "trades": [],
        }
        self.limit_buys.append(order.copy())
        self.open_orders.append(order.copy())
        self.orders[identifier] = order
        return {"uuid": identifier}

    def settle_buy(self, identifier: str, status: str, trades: list[tuple[float, float]]):
        order = self.orders[identifier]
        executed = sum(volume for volume, _ in trades)
        self.holdings += executed - float(order["executed_volume"])
        order.update(
            state=status,
            executed_volume=str(executed),
            trades=[{"volume": str(volume), "price": str(price), "funds": str(volume * price)}
                    for volume, price in trades],
        )
        if status != "wait":
            self.open_orders = [order for order in self.open_orders if order["uuid"] != identifier]


class TestAltOrderSafetyCandidates(unittest.TestCase):
    def run_tick(
        self, exchange: FakeExchange, state: dict, price: float, is_bull: bool,
        signal: str, stop_when_tranches_added: bool = False,
        complete_tick: bool = False, persist_state: bool = False,
    ):
        saved: list[dict] = []
        logged: list[dict] = []
        output = io.StringIO()
        real_save = main.save_strategy_state
        price_queries = 0

        def get_price(market: str):
            nonlocal price_queries
            price_queries += 1
            if complete_tick and price_queries > 1:
                raise EndTick()  # The next iteration, not elapsed time, ends this tick.
            return price

        def stop_after_decision(seconds: float):
            if complete_tick:
                if state.get("trading_halted"):
                    raise EndTick()
                return  # Never actually sleep or poll in these tests.
            if seconds == 1 or (stop_when_tranches_added and seconds == 0.2):
                return
            raise EndTick()

        def save_state(market: str, value: dict):
            saved.append(copy.deepcopy(value))
            if persist_state:
                real_save(market, value)
            if stop_when_tranches_added and len(value.get("tranches", [])) == 2:
                raise EndTick()

        with (
            patch.multiple(
                main,
                acquire_market_process_lock=lambda market: object(),
                init_db=lambda: None,
                get_upbit_client=lambda: exchange,
                load_strategy_state=lambda market: state,
                save_strategy_state=save_state,
                check_krw_balance_alert=lambda *args, **kwargs: 100_000.0,
                send_telegram_alert=lambda message: None,
                safe_get_current_price=get_price,
                get_hybrid_regime_and_signals=lambda market: {
                    "is_bull": is_bull,
                    "signal": signal,
                    "ma5": price,
                },
                log_real_trade=lambda **trade: logged.append(trade),
            ),
            patch.object(main.time, "sleep", side_effect=stop_after_decision),
            patch("requests.sessions.Session.request", side_effect=AssertionError("Real HTTP forbidden")) as http,
            redirect_stdout(output),
        ):
            with self.assertRaises(EndTick):
                main.run_trading_strategy("KRW-ETH")

        http.assert_not_called()
        self.assertNotIn("[CRITICAL] 메인 루프 에러", output.getvalue())
        return saved, logged

    def start_bear_orders(self, persist_state: bool = False):
        exchange = FakeExchange(main.PROTECTED_BALANCES["KRW-ETH"] + 0.1, 80_000.0)
        state = {
            "market": "KRW-ETH", "current_regime": "BEAR", "initial_entry_done": True,
            "bot_quantity": 0.1, "bot_avg_price": 100_000.0,
            "tranches": [{"step": 1, "volume": 0.1, "buy_price": 100_000.0}],
        }
        saved, _ = self.run_tick(
            exchange, state, 80_000.0, False, "HOLD", complete_tick=True, persist_state=persist_state
        )
        self.assertEqual(3, len(exchange.limit_buys))
        self.assertEqual({order["uuid"] for order in exchange.limit_buys}, set(state["bear_limit_buys"]))
        self.assertEqual([], exchange.market_buys)
        return exchange, state, saved

    def assert_buy_accounting(self, state: dict, exchange: FakeExchange, volume: float, funds: float):
        self.assertAlmostEqual(0.1 + volume, state["bot_quantity"])
        self.assertAlmostEqual(0.1 + volume, sum(tr["volume"] for tr in state["tranches"]))
        self.assertAlmostEqual((10_000.0 + funds) / (0.1 + volume), state["bot_avg_price"])
        for identifier, recorded in state["bear_limit_buys"].items():
            credited = sum(tr["volume"] for tr in state["tranches"] if tr.get("order_uuid") == identifier)
            actual = float(exchange.orders[identifier]["executed_volume"])
            self.assertLessEqual(credited, actual + 1e-12)
            self.assertLessEqual(recorded["executed_volume"], actual + 1e-12)
        self.assertLessEqual(
            state["bot_quantity"], exchange.holdings - main.PROTECTED_BALANCES["KRW-ETH"] + 1e-12
        )

    def test_cancelled_zero_buy_does_not_create_a_phantom_tranche(self):
        protected = main.PROTECTED_BALANCES["KRW-ETH"]
        exchange = FakeExchange(
            protected + 0.1,
            100_000.0,
            [
                {"uuid": "basket-1", "side": "ask"},
                {"uuid": "dca-1", "side": "bid"},
            ],
        )
        exchange.orders = {
            "dca-1": {"uuid": "dca-1", "state": "wait", "executed_volume": "0", "trades": []},
            "dca-2": {"uuid": "dca-2", "state": "cancel", "executed_volume": "0", "trades": []},
            "dca-3": {"uuid": "dca-3", "state": "cancel", "executed_volume": "0", "trades": []},
        }
        existing_qty = exchange.holdings - protected
        state = {
            "market": "KRW-ETH",
            "current_regime": "BEAR",
            "bot_quantity": existing_qty,
            "bot_avg_price": 100_000.0,
            "tranches": [{"step": 1, "volume": existing_qty, "buy_price": 100_000.0}],
            "bear_limit_buys": {
                uuid: {"units": 1, "executed_volume": 0.0, "executed_funds": 0.0, "state": "wait"}
                for uuid in exchange.orders
            },
        }

        saved, logged = self.run_tick(
            exchange, state, 100_000.0, False, "HOLD", stop_when_tranches_added=True
        )

        self.assertEqual(1, len(state["tranches"]))
        self.assertAlmostEqual(0.1, sum(tr["volume"] for tr in state["tranches"]))
        self.assertAlmostEqual(0.1, state["bot_quantity"])
        self.assertEqual([], exchange.cancelled)
        self.assertEqual(set(exchange.orders), set(exchange.order_lookups))
        self.assertEqual([], exchange.market_buys)
        self.assertEqual([], logged)
        self.assertAlmostEqual(0.1, exchange.holdings - protected)
        self.assertEqual("cancel", saved[-1]["bear_limit_buys"]["dca-2"]["state"])

    def test_accepted_bid_uuids_are_saved_without_crediting_unfilled_volume(self):
        exchange, state, saved = self.start_bear_orders()
        self.assertEqual([1, 2, 4], [record["units"] for record in state["bear_limit_buys"].values()])
        for order in exchange.limit_buys:
            snapshots = [snapshot for snapshot in saved if order["uuid"] in snapshot.get("bear_limit_buys", {})]
            self.assertTrue(snapshots)
            self.assertEqual(0.0, snapshots[0]["bear_limit_buys"][order["uuid"]]["executed_volume"])
        self.assert_buy_accounting(state, exchange, 0.0, 0.0)

    def test_per_uuid_wait_partial_cancel_done_and_zero_are_idempotent(self):
        for status in ("wait", "cancel", "done"):
            for trades in ([], [(0.02, 96_000.0), (0.03, 94_000.0)]):
                with self.subTest(status=status, trades=trades):
                    exchange, state, _ = self.start_bear_orders()
                    identifier = exchange.limit_buys[0]["uuid"]
                    exchange.settle_buy(identifier, status, trades)
                    # An unrelated deposit must never become bot-added buy volume.
                    exchange.holdings += 0.25
                    volume = sum(volume for volume, _ in trades)
                    funds = sum(volume * price for volume, price in trades)
                    for _ in range(2):
                        self.run_tick(exchange, state, 80_000.0, False, "HOLD", complete_tick=True)
                        self.assert_buy_accounting(state, exchange, volume, funds)
                        self.assertEqual(status, state["bear_limit_buys"][identifier]["state"])
                        self.assertEqual(2 if trades else 1, len(state["tranches"]))
                    self.assertIn(identifier, exchange.order_lookups)
                    if status == "wait":
                        self.assertEqual(3, len([o for o in exchange.open_orders if o["side"] == "bid"]))
                        self.assertEqual([], exchange.cancelled)
                    elif trades:
                        # Replacement buys also retain their UUIDs, not just the initial ladder.
                        self.assertEqual(4, len(exchange.limit_buys))
                        self.assertIn(exchange.limit_buys[-1]["uuid"], state["bear_limit_buys"])
                    if not trades:
                        self.assertEqual([], exchange.cancelled)
                        self.assertEqual(1, len(exchange.limit_sells))

    def test_partial_progress_is_weighted_once_across_json_restarts(self):
        with tempfile.TemporaryDirectory(prefix="autotrade-bear-fill-") as directory, \
                patch.object(main, "BASE_DIR", directory), \
                patch.dict(main._last_saved_state_strings, {}, clear=True):
            exchange, _, _ = self.start_bear_orders(persist_state=True)
            identifier = exchange.limit_buys[0]["uuid"]
            phases = (
                ("wait", [(0.02, 96_000.0)]),
                ("wait", [(0.02, 96_000.0)]),
                ("wait", [(0.02, 96_000.0), (0.03, 94_000.0)]),
                ("cancel", [(0.02, 96_000.0), (0.03, 94_000.0), (0.01, 93_000.0)]),
            )
            tranche_id = None
            for status, trades in phases:
                exchange.settle_buy(identifier, status, trades)
                state = main.load_strategy_state("KRW-ETH")
                self.run_tick(
                    exchange, state, 80_000.0, False, "HOLD", complete_tick=True, persist_state=True
                )
                reloaded = main.load_strategy_state("KRW-ETH")
                volume = sum(volume for volume, _ in trades)
                funds = sum(volume * price for volume, price in trades)
                self.assert_buy_accounting(reloaded, exchange, volume, funds)
                tranche = next(tr for tr in reloaded["tranches"] if tr.get("order_uuid") == identifier)
                self.assertAlmostEqual(funds / volume, tranche["buy_price"])
                if tranche_id is None:
                    tranche_id = tranche["tranche_id"]
                self.assertEqual(tranche_id, tranche["tranche_id"])
            exchange.order_lookups.clear()
            self.run_tick(exchange, reloaded, 80_000.0, False, "HOLD", complete_tick=True)
            self.assertNotIn(identifier, exchange.order_lookups)  # Canceled partial is terminal.
            self.assert_buy_accounting(reloaded, exchange, 0.06, 5_670.0)

    def test_multiple_filled_uuids_keep_their_own_volume_price_and_units(self):
        exchange, state, _ = self.start_bear_orders()
        first, second, third = [order["uuid"] for order in exchange.limit_buys]
        exchange.settle_buy(first, "done", [(0.02, 96_000.0)])
        exchange.settle_buy(second, "cancel", [(0.04, 92_000.0)])
        exchange.settle_buy(third, "cancel", [])
        self.run_tick(exchange, state, 80_000.0, False, "HOLD", complete_tick=True)
        self.assert_buy_accounting(state, exchange, 0.06, 5_600.0)
        tranches = {tr["order_uuid"]: tr for tr in state["tranches"] if "order_uuid" in tr}
        self.assertEqual({first, second}, set(tranches))
        for identifier, volume, price, units in ((first, 0.02, 96_000.0, 1), (second, 0.04, 92_000.0, 2)):
            self.assertAlmostEqual(volume, tranches[identifier]["volume"])
            self.assertAlmostEqual(price, tranches[identifier]["buy_price"])
            self.assertEqual(units, tranches[identifier]["units"])

    def test_restart_after_fill_checkpoint_does_not_lose_rebalance_or_recredit(self):
        with tempfile.TemporaryDirectory(prefix="autotrade-bear-checkpoint-") as directory, \
                patch.object(main, "BASE_DIR", directory), \
                patch.dict(main._last_saved_state_strings, {}, clear=True):
            exchange, state, _ = self.start_bear_orders(persist_state=True)
            identifier = exchange.limit_buys[0]["uuid"]
            exchange.settle_buy(identifier, "done", [(0.02, 96_000.0)])
            self.run_tick(
                exchange, state, 80_000.0, False, "HOLD",
                stop_when_tranches_added=True, persist_state=True
            )
            self.assertEqual([], exchange.cancelled)
            reloaded = main.load_strategy_state("KRW-ETH")
            self.assertTrue(reloaded["bear_buy_rebalance_pending"])
            self.run_tick(
                exchange, reloaded, 80_000.0, False, "HOLD", complete_tick=True, persist_state=True
            )
            self.assert_buy_accounting(main.load_strategy_state("KRW-ETH"), exchange, 0.02, 1_920.0)
            self.assertEqual(1, len(exchange.cancelled))
            self.assertEqual(2, len(exchange.limit_sells))
            self.assertFalse(reloaded["bear_buy_rebalance_pending"])

    def test_sold_partial_tranche_does_not_reset_uuid_execution_checkpoint(self):
        exchange, state, _ = self.start_bear_orders()
        identifier = exchange.limit_buys[0]["uuid"]
        exchange.settle_buy(identifier, "wait", [(0.02, 96_000.0)])
        self.run_tick(exchange, state, 80_000.0, False, "HOLD", complete_tick=True)
        # Model the existing tranche-sale contract: remove only the sold tranche.
        state["tranches"] = [tr for tr in state["tranches"] if tr.get("order_uuid") != identifier]
        state["bot_quantity"] = 0.1
        state["bot_avg_price"] = 100_000.0
        exchange.holdings -= 0.02
        exchange.settle_buy(identifier, "wait", [(0.02, 96_000.0), (0.03, 94_000.0)])
        self.run_tick(exchange, state, 80_000.0, False, "HOLD", complete_tick=True)
        self.assert_buy_accounting(state, exchange, 0.03, 2_820.0)
        self.assertAlmostEqual(0.05, state["bear_limit_buys"][identifier]["executed_volume"])

    def test_unknown_legacy_uuid_is_not_inferred_from_count_or_account_balance(self):
        exchange, state, _ = self.start_bear_orders()
        del state["bear_limit_buys"]
        exchange.settle_buy(exchange.limit_buys[0]["uuid"], "done", [(0.02, 96_000.0)])
        self.run_tick(exchange, state, 80_000.0, False, "HOLD", complete_tick=True)
        self.assertAlmostEqual(0.1, state["bot_quantity"])
        self.assertEqual(1, len(state["tranches"]))
        self.assertEqual([], exchange.cancelled)
        self.assertEqual([], exchange.order_lookups)

    def test_unconfirmed_or_invalid_uuid_execution_halts_without_credit(self):
        responses = (
            None,
            {"error": {"name": "order_not_found"}},
            {"state": "wait", "executed_volume": "0.02", "trades": []},
            {"state": "done", "executed_volume": "NaN", "trades": []},
            {"state": "cancel", "executed_volume": "-0.01", "trades": []},
        )
        for response in responses:
            with self.subTest(response=response):
                exchange, state, _ = self.start_bear_orders()
                identifier = exchange.limit_buys[0]["uuid"]
                exchange.orders[identifier] = response
                self.run_tick(exchange, state, 80_000.0, False, "HOLD", complete_tick=True)
                self.assertTrue(state["trading_halted"])
                self.assertEqual(identifier, state["pending_order_uuid"])
                self.assertAlmostEqual(0.1, state["bot_quantity"])
                self.assertEqual(1, len(state["tranches"]))
                self.assertEqual([], exchange.cancelled)
        exchange, state, _ = self.start_bear_orders()
        exchange.order_error = ConnectionError("fake order query failed")
        self.run_tick(exchange, state, 80_000.0, False, "HOLD", complete_tick=True)
        self.assertTrue(state["trading_halted"])
        self.assertAlmostEqual(0.1, state["bot_quantity"])

    def test_limit_buy_status_uuid_must_match_tracked_order(self):
        protected = main.PROTECTED_BALANCES["KRW-ETH"]
        exchange = FakeExchange(protected, 100_000.0)
        accepted = exchange.buy_limit_order("KRW-ETH", 100_000.0, 0.1)
        identifier = accepted["uuid"]
        exchange.settle_buy(identifier, "done", [(0.1, 100_000.0)])
        exchange.orders[identifier]["uuid"] = "different-order"
        state = {
            "bot_quantity": 0.0,
            "bot_avg_price": 0.0,
            "tranches": [],
            "bear_limit_buys": {
                identifier: {
                    "units": 1,
                    "state": "wait",
                    "executed_volume": 0.0,
                    "executed_funds": 0.0,
                },
            },
        }
        with (
            patch.object(main, "save_strategy_state"),
            patch.object(main, "send_telegram_alert"),
        ):
            self.assertFalse(main.reconcile_bear_limit_buys(exchange, "KRW-ETH", state))

        self.assertTrue(state["trading_halted"])
        self.assertEqual(identifier, state["pending_order_uuid"])
        self.assertEqual([], state["tranches"])
        self.assertEqual(0.0, state["bot_quantity"])

    def test_small_existing_position_is_overwritten_by_new_buy(self):
        protected = main.PROTECTED_BALANCES["KRW-ETH"]
        exchange = FakeExchange(protected + 0.03, 100_000.0)
        state = {
            "market": "KRW-ETH",
            "current_regime": "BEAR",
            "initial_entry_done": True,
            "bot_quantity": 0.03,
            "bot_avg_price": 100_000.0,
            "tranches": [{"step": 1, "volume": 0.03, "buy_price": 100_000.0}],
        }

        _, logged = self.run_tick(exchange, state, 100_000.0, False, "MARTINGALE_BUY_DIP")

        self.assertEqual([main.INVESTMENTS["KRW-ETH"]["unit"]], exchange.market_buys)
        self.assertEqual("MARTINGALE_BUY_INITIAL", logged[0]["action"])
        self.assertAlmostEqual(0.13, exchange.holdings - protected)
        self.assertAlmostEqual(0.1, state["bot_quantity"])
        self.assertAlmostEqual(0.03, sum(tr["volume"] for tr in state["tranches"]))

    def test_failed_balance_query_halts_before_buying_over_existing_position(self):
        protected = main.PROTECTED_BALANCES["KRW-ETH"]
        exchange = FakeExchange(protected + 0.1, 115_000.0)
        exchange.balance_fails = True
        exchange.buy_volume = 0.08
        state = {
            "market": "KRW-ETH",
            "current_regime": "BULL",
            "initial_entry_done": True,
            "bot_quantity": 0.1,
            "bot_avg_price": 100_000.0,
            "tranches": [{"step": 1, "volume": 0.1, "buy_price": 100_000.0}],
        }

        saved, logged = self.run_tick(exchange, state, 115_000.0, True, "BUY")

        self.assertEqual([], exchange.market_buys)
        self.assertEqual([], logged)
        self.assertAlmostEqual(0.1, exchange.holdings - protected)
        self.assertTrue(saved[-1]["trading_halted"])
        self.assertAlmostEqual(0.1, saved[-1]["bot_quantity"])
