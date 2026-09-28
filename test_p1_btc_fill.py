"""Fake-only contracts for the production BTC market-buy settlement path."""

from __future__ import annotations

import copy
import sys
import types
import unittest
from datetime import datetime
from unittest.mock import Mock, patch

from strategy import btc_accumulator


class DueDatetime(datetime):
    """Make UTC+9 land inside the production 08:50-08:58 buy window."""

    @classmethod
    def utcnow(cls):
        return cls(2026, 9, 27, 23, 55, 0)


class AfterWindowDatetime(datetime):
    """Keep the same KST day, but advance past the new-order window."""

    @classmethod
    def utcnow(cls):
        return cls(2026, 9, 28, 0, 0, 0)


class FakeExchange:
    def __init__(self, order_status: str = "wait") -> None:
        self.uuid = "fake-btc-order"
        self.order = {
            "uuid": self.uuid,
            "state": order_status,
            "executed_volume": "0",
            "trades": [],
        }
        self.status_queries: list[str] = []
        self.orders: list[tuple[str, float]] = []
        self.balance_queries: list[tuple[str, bool]] = []
        self.query_error = None
        self.on_query = lambda uuid: None
        self.krw_balance = 100_000.0
        self.acknowledgement = {"uuid": self.uuid}

    def get_balance(self, currency: str, verbose: bool = False):
        self.balance_queries.append((currency, verbose))
        if currency == "KRW" and not verbose:
            return self.krw_balance
        if currency == "BTC" and verbose:
            # Account-wide holdings must not be mistaken for this order's fill.
            return {"balance": "0.04", "locked": "0.01", "avg_buy_price": "80000000"}
        raise AssertionError(f"Unexpected balance query: {currency}, verbose={verbose}")

    def buy_market_order(self, market: str, amount: float):
        self.orders.append((market, amount))
        return copy.deepcopy(self.acknowledgement)

    def get_order(self, uuid: str):
        self.status_queries.append(uuid)
        self.on_query(uuid)
        if self.query_error is not None:
            raise self.query_error
        return copy.deepcopy(self.order)


class TestP1BtcFillReproduction(unittest.TestCase):
    def setUp(self):
        self.initial_state = {
            "last_buy_date": None,
            "last_buy_amount": 0,
            "last_buy_tier": 0,
            "total_dca_buys_count": 3,
            "total_dca_invested_krw": 30_000.0,
        }
        self.state = copy.deepcopy(self.initial_state)
        self.saved: list[dict] = []
        self.exchange = FakeExchange()
        self.exchange.on_query = self.assert_pending_saved
        self.alert = Mock()
        self.status = {
            "buy_krw": 10_000,
            "tier": 1,
            "tier_reason": "fake tier",
            "is_valid": True,
            "current_price": 100_000_000.0,
        }

        def start(patcher):
            value = patcher.start()
            self.addCleanup(patcher.stop)
            return value

        start(patch.object(btc_accumulator, "datetime", DueDatetime))
        start(patch.object(btc_accumulator, "BTC_DCA_CLOSING_CHECK_HOUR", 8))
        start(patch.object(btc_accumulator, "load_btc_state", side_effect=lambda: copy.deepcopy(self.state)))
        start(patch.object(btc_accumulator, "save_btc_state", side_effect=self.save_state))
        self.check_status = start(patch.object(btc_accumulator, "check_btc_tiered_status", return_value=self.status))
        self.init_db = start(patch.object(btc_accumulator, "init_db"))
        self.log_trade = start(patch.object(btc_accumulator, "log_real_trade"))
        start(patch.dict(sys.modules, {"main": types.SimpleNamespace(send_telegram_alert=self.alert)}))

        # Fail closed on all live API/network paths, and never wait by wall clock.
        self.forbidden = [
            start(patch.object(btc_accumulator, "get_upbit_client", side_effect=AssertionError("live client"))),
            start(patch.object(btc_accumulator.pyupbit, "Upbit", side_effect=AssertionError("live client"))),
            start(patch.object(btc_accumulator.pyupbit, "get_current_price", side_effect=AssertionError("live quote"))),
            start(patch.object(btc_accumulator.pyupbit, "get_ohlcv", side_effect=AssertionError("live candles"))),
            start(patch("requests.sessions.Session.request", side_effect=AssertionError("live HTTP"))),
            start(patch("socket.socket.connect", side_effect=AssertionError("live socket"))),
            start(patch.object(btc_accumulator.time, "sleep", side_effect=AssertionError("fixed sleep"))),
        ]

    def tearDown(self):
        for boundary in self.forbidden:
            boundary.assert_not_called()

    def save_state(self, state):
        self.state = copy.deepcopy(state)
        self.saved.append(copy.deepcopy(state))

    def assert_pending_saved(self, uuid):
        self.assertEqual(uuid, self.state["pending_buy_order"]["uuid"])

    def execute(self):
        return btc_accumulator.execute_btc_tiered_buy_if_due(self.exchange)

    def set_fill(self, state="done"):
        self.exchange.order.update({
            "state": state,
            "executed_volume": "0.00012",
            "paid_fee": "4.9995",
            "trades": [
                {"volume": "0.00004", "price": "80000000", "funds": "3200"},
                {"volume": "0.00008", "price": "84987500", "funds": "6799"},
            ],
        })

    def assert_no_fill(self):
        self.init_db.assert_not_called()
        self.log_trade.assert_not_called()
        self.alert.assert_not_called()
        for state in [self.state] + self.saved:
            for key, value in self.initial_state.items():
                self.assertEqual(value, state[key], key)

    def assert_fill(self, result, volume, funds, tier=1):
        self.assertEqual({"action": "BOUGHT", "tier": tier, "amount": funds, "reason": "fake tier"}, result)
        self.init_db.assert_called_once_with()
        self.log_trade.assert_called_once()
        trade = self.log_trade.call_args.kwargs
        self.assertEqual("KRW-BTC", trade["market"])
        self.assertEqual("BTC", trade["ticker"])
        self.assertEqual("bid", trade["side"])
        self.assertEqual(f"BTC_TIERED_BUY_T{tier}", trade["action"])
        self.assertEqual("BTC_TIERED_ACCUMULATOR", trade["strategy"])
        self.assertEqual(self.exchange.uuid, trade["order_uuid"])
        self.assertAlmostEqual(volume, trade["volume"])
        self.assertAlmostEqual(funds, trade["cost_or_revenue"])
        self.assertAlmostEqual(funds / volume, trade["price"])
        self.assertEqual("2026-09-28", self.state["last_buy_date"])
        self.assertEqual(funds, self.state["last_buy_amount"])
        self.assertEqual(tier, self.state["last_buy_tier"])
        self.assertEqual(4, self.state["total_dca_buys_count"])
        self.assertEqual(30_000.0 + funds, self.state["total_dca_invested_krw"])
        self.assertNotIn("pending_buy_order", self.state)
        self.alert.assert_called_once()
        # A later due tick must neither resubmit nor log the terminal fill twice.
        self.assertIsNone(self.execute())
        self.assertEqual(1, len(self.exchange.orders))
        self.log_trade.assert_called_once()
        self.alert.assert_called_once()

    def test_uuid_with_no_confirmed_fill_is_not_logged_or_saved_as_bought(self):
        self.assertIsNone(self.execute())
        self.assertEqual([("KRW-BTC", 10_000)], self.exchange.orders)
        self.assertEqual([self.exchange.uuid], self.exchange.status_queries)
        self.assert_no_fill()
        self.assertEqual(self.exchange.uuid, self.state["pending_buy_order"]["uuid"])

    def test_pending_uuid_settles_after_new_order_window_closes(self):
        self.assertIsNone(self.execute())
        self.set_fill()

        with patch.object(btc_accumulator, "datetime", AfterWindowDatetime):
            result = self.execute()

        self.assertEqual(1, len(self.exchange.orders))
        self.assert_fill(result, 0.00012, 9_999)

    def test_waiting_partial_fill_is_credited_once_then_only_new_fill_is_added(self):
        self.exchange.order.update({
            "state": "wait",
            "executed_volume": "0.00004",
            "trades": [{"volume": "0.00004", "price": "80000000", "funds": "3200"}],
        })
        result = self.execute()
        self.assertEqual("PARTIAL", result["action"])
        self.assertEqual(1, self.log_trade.call_count)
        self.assertAlmostEqual(0.00004, self.log_trade.call_args.kwargs["volume"])
        self.assertEqual(3_200, self.log_trade.call_args.kwargs["cost_or_revenue"])
        self.assertIn("pending_buy_order", self.state)

        self.set_fill()
        self.execute()
        self.assertEqual(2, self.log_trade.call_count)
        self.assertAlmostEqual(
            0.00012, sum(call.kwargs["volume"] for call in self.log_trade.call_args_list)
        )
        self.assertEqual(
            9_999, sum(call.kwargs["cost_or_revenue"] for call in self.log_trade.call_args_list)
        )
        self.assertEqual(4, self.state["total_dca_buys_count"])
        self.assertEqual(39_999, self.state["total_dca_invested_krw"])
        self.assertNotIn("pending_buy_order", self.state)
        self.assertEqual(1, len(self.exchange.orders))

    def test_cancel_zero_does_not_log_or_consume_daily_buy(self):
        self.exchange.order["state"] = "cancel"
        self.assertIsNone(self.execute())
        self.assertEqual([("KRW-BTC", 10_000)], self.exchange.orders)
        self.assertEqual([self.exchange.uuid], self.exchange.status_queries)
        self.assert_no_fill()
        self.assertNotIn("pending_buy_order", self.state)

    def test_done_full_uses_actual_volume_and_volume_weighted_trade_price(self):
        self.set_fill()
        result = self.execute()
        self.assertEqual([("KRW-BTC", 10_000)], self.exchange.orders)
        self.assertEqual([self.exchange.uuid], self.exchange.status_queries)
        self.assert_fill(result, 0.00012, 9_999.0)

    def test_cancel_partial_logs_only_executed_volume_and_funds(self):
        self.set_fill("cancel")
        self.exchange.order.update({
            "executed_volume": "0.00005",
            "trades": [
                {"volume": "0.00002", "price": "80000000", "funds": "1600"},
                {"volume": "0.00003", "price": "90000000", "funds": "2700"},
            ],
        })
        self.assert_fill(self.execute(), 0.00005, 4_300.0)

    def test_wait_does_not_resubmit_and_later_done_settles_original_tier(self):
        self.assertIsNone(self.execute())
        # Even changed signals must not overwrite a pending order or its tier.
        self.check_status.return_value = dict(self.status, tier=0, buy_krw=0)
        self.assertIsNone(self.execute())
        self.assert_no_fill()
        self.assertEqual([("KRW-BTC", 10_000)], self.exchange.orders)
        self.assertEqual([self.exchange.uuid] * 2, self.exchange.status_queries)
        self.check_status.assert_called_once_with(self.exchange)
        self.set_fill()
        self.assert_fill(self.execute(), 0.00012, 9_999.0)
        self.check_status.assert_called_once_with(self.exchange)

    def test_wait_with_partial_execution_is_not_finalized_until_cancel(self):
        self.set_fill("wait")
        result = self.execute()
        self.assertEqual("PARTIAL", result["action"])
        self.assertEqual(1, self.log_trade.call_count)
        self.assertAlmostEqual(0.00012, self.log_trade.call_args.kwargs["volume"])
        self.assertIn("pending_buy_order", self.state)
        self.exchange.order["state"] = "cancel"
        self.assertIsNone(self.execute())
        self.log_trade.assert_called_once()
        self.assertEqual(4, self.state["total_dca_buys_count"])
        self.assertEqual(39_999, self.state["total_dca_invested_krw"])
        self.assertNotIn("pending_buy_order", self.state)

    def test_query_exception_keeps_uuid_and_blocks_duplicate_submission(self):
        self.exchange.query_error = RuntimeError("fake settlement outage")
        self.assertIsNone(self.execute())
        self.assertIsNone(self.execute())
        self.assert_no_fill()
        self.assertEqual([("KRW-BTC", 10_000)], self.exchange.orders)
        self.assertEqual([self.exchange.uuid] * 2, self.exchange.status_queries)
        self.assertEqual(self.exchange.uuid, self.state["pending_buy_order"]["uuid"])
        self.exchange.query_error = None
        self.set_fill()
        self.assert_fill(self.execute(), 0.00012, 9_999.0)

    def test_unverified_terminal_response_keeps_uuid_without_logging(self):
        responses = [
            None,
            {"error": {"name": "order_not_found"}},
            {"uuid": "other-order", "state": "done", "executed_volume": "0.0001", "trades": [{"funds": "9000"}]},
            {"uuid": self.exchange.uuid, "state": "done", "executed_volume": "0.0001", "trades": []},
            {"uuid": self.exchange.uuid, "state": "done", "executed_volume": "invalid", "trades": [{"funds": "9000"}]},
        ]
        for order in responses:
            with self.subTest(order=order):
                self.exchange.order = order
                self.assertIsNone(self.execute())
                self.assert_no_fill()
                self.assertEqual([("KRW-BTC", 10_000)], self.exchange.orders)
                self.assertEqual(self.exchange.uuid, self.state["pending_buy_order"]["uuid"])
        self.assertEqual([self.exchange.uuid] * len(responses), self.exchange.status_queries)

    def test_done_zero_does_not_log_a_fill(self):
        self.exchange.order["state"] = "done"
        self.assertIsNone(self.execute())
        self.assert_no_fill()
        self.assertNotIn("pending_buy_order", self.state)

    def test_tier_two_retains_requested_order_amount_but_accounts_actual_funds(self):
        self.status.update(tier=2, buy_krw=20_000)
        self.set_fill()
        result = self.execute()
        self.assertEqual([("KRW-BTC", 20_000)], self.exchange.orders)
        self.assert_fill(result, 0.00012, 9_999.0, tier=2)

    def test_tier_zero_still_skips_without_an_order(self):
        self.status.update(tier=0, buy_krw=0)
        result = self.execute()
        self.assertEqual("SKIP", result["action"])
        self.assertEqual([], self.exchange.orders)
        self.assertEqual([], self.exchange.status_queries)
        self.log_trade.assert_not_called()
        self.assertEqual("2026-09-28", self.state["last_buy_date"])

    def test_invalid_signal_still_does_not_submit(self):
        self.status.update(tier=-1, buy_krw=0, is_valid=False)
        self.assertIsNone(self.execute())
        self.assertEqual([], self.exchange.orders)
        self.assertEqual([], self.exchange.status_queries)
        self.assertEqual([], self.saved)
        self.assert_no_fill()

    def test_outside_due_window_still_does_not_submit(self):
        with patch.object(btc_accumulator, "BTC_DCA_CLOSING_CHECK_HOUR", 9):
            self.assertIsNone(self.execute())
        self.check_status.assert_not_called()
        self.assertEqual([], self.exchange.orders)
        self.assert_no_fill()

    def test_insufficient_balance_still_does_not_submit(self):
        self.exchange.krw_balance = 9_999.0
        self.assertIsNone(self.execute())
        self.assertEqual([], self.exchange.orders)
        self.assertEqual([], self.exchange.status_queries)
        self.assertEqual([], self.saved)
        self.log_trade.assert_not_called()
        self.alert.assert_called_once()

    def test_acknowledgement_without_uuid_does_not_log_or_track_a_fill(self):
        self.exchange.acknowledgement = {"uuid": ""}
        self.assertIsNone(self.execute())
        self.assertEqual([], self.exchange.status_queries)
        self.assertEqual([], self.saved)
        self.log_trade.assert_not_called()
        self.alert.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
