"""Regression coverage for fail-closed account and order checks."""

from __future__ import annotations

import unittest

import main


class UnavailableBalance:
    def get_balance(self, ticker: str, verbose: bool = False):
        raise ConnectionError("exchange unavailable")


class MalformedOpenOrders:
    def get_order(self, market: str, state: str):
        return [{"state": "wait"}]


class TestFailClosedSafety(unittest.TestCase):
    def test_bot_balance_is_zero_when_exchange_balance_is_unavailable(self):
        # Given: a saved bot quantity exists but the exchange balance cannot be read.
        state = {"bot_quantity": 0.1, "bot_avg_price": 100_000.0}

        # When: the live balance guard evaluates the unavailable account.
        average, quantity = main.get_bot_balance(UnavailableBalance(), "KRW-ETH", "ETH", state)

        # Then: no quantity remains available for an automatic sell.
        self.assertEqual(0.0, average)
        self.assertEqual(0.0, quantity)

    def test_cancel_all_orders_rejects_wait_order_without_uuid(self):
        # Given: the exchange returns a malformed open-order entry.
        exchange = MalformedOpenOrders()

        # When: the replacement-order guard attempts cancellation.
        cancelled = main.cancel_all_orders(exchange, "KRW-ETH")

        # Then: the unknown order cannot count as cancelled.
        self.assertFalse(cancelled)
