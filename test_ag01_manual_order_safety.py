"""AG-01 regressions: the bot must only cancel/count orders whose UUID it owns.

Fake exchange only; no network, no operational JSON/SQLite.
"""

from __future__ import annotations

import copy
import unittest
from unittest.mock import patch

import main
import test_p1_squad_exit as _squad

SquadFakeExchange = _squad.SquadFakeExchange

MANUAL_ASK = {"uuid": "manual-ask", "side": "ask", "price": "5000000", "volume": "0.5"}
MANUAL_BID = {"uuid": "manual-bid", "side": "bid", "price": "1000000", "volume": "0.5"}
BOT_ASK = {"uuid": "bot-ask", "side": "ask", "price": "107520", "volume": "0.1"}
BOT_BID = {"uuid": "bot-bid", "side": "bid", "price": "90000", "volume": "0.1"}


def make_exchange(open_orders):
    exchange = SquadFakeExchange(0.1, 100_000.0)
    exchange.open_orders = copy.deepcopy(open_orders)
    for o in open_orders:
        exchange.orders[o["uuid"]] = {**o, "state": "wait", "executed_volume": "0", "trades": []}
    return exchange


def owned_state():
    return {
        "basket_limit_ask": {"uuid": "bot-ask", "price": 107520.0, "volume": 0.1},
        "bear_limit_buys": {
            "bot-bid": {"units": 1, "squad_id": 1, "squad_stage": 2, "state": "wait",
                        "executed_volume": 0.0, "executed_funds": 0.0},
        },
    }


class TestCancelScope(unittest.TestCase):
    def setUp(self):
        p_alert = patch.object(main, "send_telegram_alert", lambda msg: None)
        p_alert.start()
        self.addCleanup(p_alert.stop)

        p_req = patch("requests.post", side_effect=AssertionError("live HTTP forbidden"))
        p_req.start()
        self.addCleanup(p_req.stop)

        p_save = patch.object(main, "save_strategy_state", lambda m, s: None)
        p_save.start()
        self.addCleanup(p_save.stop)

    def test_empty_state_with_only_manual_orders_preserves_them(self):
        exchange = make_exchange([MANUAL_ASK, MANUAL_BID])
        self.assertTrue(main.cancel_all_orders(exchange, "KRW-ETH", state={}))
        self.assertEqual([], exchange.cancelled)
        self.assertEqual(2, len(exchange.open_orders))

    def test_none_state_fails_safely_without_any_exchange_call(self):
        class Spy(SquadFakeExchange):
            def __init__(self):
                super().__init__(0.1, 100_000.0)
                self.queries = 0

            def get_order(self, identifier, state=None):
                self.queries += 1
                return super().get_order(identifier, state)

        exchange = Spy()
        exchange.open_orders = [copy.deepcopy(MANUAL_ASK)]
        self.assertFalse(main.cancel_all_orders(exchange, "KRW-ETH", state=None))
        self.assertFalse(main.cancel_all_orders(exchange, "KRW-ETH"))
        self.assertFalse(main.cancel_order_and_wait(exchange, "manual-ask", market="KRW-ETH", state=None))
        self.assertEqual([], exchange.cancelled)
        self.assertEqual(0, exchange.queries)

    def test_mixed_orders_cancel_only_bot_orders(self):
        exchange = make_exchange([MANUAL_ASK, BOT_ASK, MANUAL_BID, BOT_BID])
        self.assertTrue(main.cancel_all_orders(exchange, "KRW-ETH", state=owned_state()))
        self.assertEqual({"bot-ask", "bot-bid"}, set(exchange.cancelled))
        self.assertEqual({"manual-ask", "manual-bid"}, {o["uuid"] for o in exchange.open_orders})

    def test_single_cancel_refuses_unowned_uuid(self):
        exchange = make_exchange([MANUAL_BID])
        self.assertFalse(main.cancel_order_and_wait(exchange, "manual-bid", market="KRW-ETH", state=owned_state()))
        self.assertEqual([], exchange.cancelled)

    def test_malformed_or_unavailable_order_list_blocks_and_cancels_nothing(self):
        for bad in (None, {"error": "x"}, [{"side": "bid"}], ["junk"]):
            exchange = make_exchange([BOT_BID])
            with patch.object(exchange, "get_order", return_value=bad):
                self.assertFalse(main.cancel_all_orders(exchange, "KRW-ETH", state=owned_state()))
            self.assertEqual([], exchange.cancelled)

        class Raises(SquadFakeExchange):
            def get_order(self, identifier, state=None):
                raise ConnectionError("down")

        self.assertFalse(main.cancel_all_orders(Raises(0.1, 1.0), "KRW-ETH", state=owned_state()))

    def test_owned_order_with_missing_or_invalid_side_blocks_cancel(self):
        for bad_side in (None, "", "BID", "sell", 1):
            order = {"uuid": "bot-bid", "price": "90000", "volume": "0.1"}
            if bad_side is not None:
                order["side"] = bad_side
            self.assertIsNone(main.partition_bot_open_orders([order], owned_state()))
            exchange = make_exchange([BOT_ASK])
            exchange.open_orders = [copy.deepcopy(BOT_ASK), order]
            self.assertFalse(main.cancel_all_orders(exchange, "KRW-ETH", state=owned_state()))
            self.assertEqual([], exchange.cancelled)

    def test_unowned_order_with_invalid_side_is_ignored(self):
        manual = {"uuid": "manual-x", "price": "1", "volume": "1"}
        self.assertEqual(([BOT_ASK], []), main.partition_bot_open_orders([manual, BOT_ASK], owned_state()))

    def test_cancel_failure_is_reported_as_failure(self):
        exchange = make_exchange([BOT_BID])
        with patch.object(exchange, "cancel_order", return_value=None):
            self.assertFalse(main.cancel_all_orders(exchange, "KRW-ETH", state=owned_state()))
        with patch.object(exchange, "cancel_order", side_effect=ConnectionError("down")):
            self.assertFalse(main.cancel_all_orders(exchange, "KRW-ETH", state=owned_state()))

    def test_partition_ignores_manual_and_does_not_infer_from_price_or_size(self):
        small_manual_ask = {"uuid": "small-manual", "side": "ask", "price": "1", "volume": "0.0001"}
        result = main.partition_bot_open_orders([small_manual_ask, MANUAL_BID, BOT_ASK, BOT_BID], owned_state())
        self.assertEqual(([BOT_ASK], [BOT_BID]), result)
        self.assertEqual(([], []), main.partition_bot_open_orders([small_manual_ask], {}))
        self.assertIsNone(main.partition_bot_open_orders([small_manual_ask], None))
        self.assertIsNone(main.partition_bot_open_orders([{"side": "bid"}], {}))

    def test_partial_fill_during_bot_cancel_is_settled_exactly_once(self):
        exchange = make_exchange([BOT_BID])
        exchange.orders["bot-bid"].update(state="cancel", executed_volume="0.03",
                                           trades=[{"volume": "0.03", "price": "90000", "funds": "2700"}])
        state = {
            **owned_state(),
            "bot_quantity": 0.1, "bot_avg_price": 100_000.0,
            "tranches": [{"step": 1, "volume": 0.1, "buy_price": 100_000.0}],
        }
        logged = []
        with patch.object(main, "log_real_trade", side_effect=lambda **t: logged.append(t)), \
             patch.object(main, "save_strategy_state", lambda m, s: None):
            self.assertTrue(main.cancel_order_and_wait(exchange, "bot-bid", market="KRW-ETH", state=state))
            self.assertTrue(main.cancel_order_and_wait(exchange, "bot-bid", market="KRW-ETH", state=state))
        self.assertAlmostEqual(0.13, state["bot_quantity"])
        self.assertAlmostEqual(0.13, sum(t["volume"] for t in state["tranches"]))
        self.assertEqual(1, len(logged))
        self.assertAlmostEqual(0.03, logged[0]["volume"])
        self.assertAlmostEqual(2700.0, logged[0]["cost_or_revenue"])
        self.assertEqual("bot-bid", logged[0]["order_uuid"])
        self.assertAlmostEqual(0.03, state["bear_limit_buys"]["bot-bid"]["executed_volume"])
        self.assertAlmostEqual(2700.0, state["bear_limit_buys"]["bot-bid"]["executed_funds"])
        self.assertAlmostEqual((10_000.0 + 2700.0) / 0.13, state["bot_avg_price"])


class TestLoopIgnoresManualOrders(unittest.TestCase):
    run_strategy_tick = _squad.TestSquadExit.run_strategy_tick

    def _state(self):
        return {
            "market": "KRW-ETH", "current_regime": "BEAR", "initial_entry_done": True,
            "bot_quantity": 0.1, "bot_avg_price": 100_000.0,
            "tranches": [{"tranche_id": 1, "step": 1, "squad_id": 1, "squad_stage": 1,
                          "volume": 0.1, "buy_price": 100_000.0}],
            "basket_limit_ask": {"uuid": "bot-ask", "price": 100_800.0, "volume": 0.1},
            "bear_limit_buys": {
                f"bot-bid-{i}": {"units": 1, "squad_id": 1, "squad_stage": i + 2, "state": "wait",
                                 "executed_volume": 0.0, "executed_funds": 0.0}
                for i in range(3)
            },
        }

    def _exchange(self, extra):
        exchange = SquadFakeExchange(0.1 + main.PROTECTED_BALANCES["KRW-ETH"], 100_000.0)
        orders = [{"uuid": "bot-ask", "side": "ask", "price": "100800", "volume": "0.1"}]
        orders += [{"uuid": f"bot-bid-{i}", "side": "bid", "price": str(90000 - i * 1000), "volume": "0.1"}
                   for i in range(3)]
        orders += extra
        exchange.open_orders = orders
        for o in orders:
            exchange.orders[o["uuid"]] = {**o, "state": "wait", "executed_volume": "0", "trades": []}
        return exchange

    def test_manual_orders_do_not_change_bot_plan_or_get_cancelled(self):
        base = self._exchange([])
        self.run_strategy_tick(base, self._state(), 100_000.0)
        mixed = self._exchange([MANUAL_ASK, MANUAL_BID,
                                {"uuid": "manual-bid2", "side": "bid", "price": "70000", "volume": "0.1"},
                                {"uuid": "manual-bid3", "side": "bid", "price": "60000", "volume": "0.1"}])
        self.run_strategy_tick(mixed, self._state(), 100_000.0)
        self.assertEqual(base.limit_buys, mixed.limit_buys)
        self.assertEqual(base.limit_sells, mixed.limit_sells)
        self.assertEqual([], mixed.cancelled)
        self.assertTrue({"manual-ask", "manual-bid"} <= {o["uuid"] for o in mixed.open_orders})

    def test_uuid_less_open_order_blocks_new_orders(self):
        state = self._state()
        state["bear_limit_buys"].pop("bot-bid-2")
        exchange = self._exchange([])
        exchange.open_orders = [o for o in exchange.open_orders if o["uuid"] != "bot-bid-2"]
        exchange.open_orders.append({"side": "bid", "price": "1", "volume": "1"})
        self.run_strategy_tick(exchange, state, 100_000.0)
        self.assertEqual([], exchange.limit_buys)
        self.assertEqual([], exchange.limit_sells)
        self.assertEqual([], exchange.market_buys)
        self.assertEqual([], exchange.cancelled)

    def test_owned_order_with_invalid_side_blocks_new_orders(self):
        for bad in ({}, {"side": "weird"}):
            exchange = self._exchange([])
            exchange.open_orders = [o for o in exchange.open_orders if o["uuid"] != "bot-bid-2"]
            exchange.open_orders.append({"uuid": "bot-bid-2", "price": "88000", "volume": "0.1", **bad})
            self.run_strategy_tick(exchange, self._state(), 100_000.0)
            self.assertEqual([], exchange.limit_buys)
            self.assertEqual([], exchange.limit_sells)
            self.assertEqual([], exchange.market_buys)
            self.assertEqual([], exchange.cancelled)

    def test_open_order_query_error_blocks_new_orders(self):
        exchange = self._exchange([])
        with patch.object(exchange, "get_order", side_effect=lambda i, state=None: None if state == "wait" else {}):
            self.run_strategy_tick(exchange, self._state(), 100_000.0)
        self.assertEqual([], exchange.limit_buys)
        self.assertEqual([], exchange.cancelled)


if __name__ == "__main__":
    unittest.main()
