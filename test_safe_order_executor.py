"""
Unit tests for utils/order_executor.py
Verifies pre-flight validation, network resilience, delayed settlement,
and auto-cancellation/recovery without causing bot freezing or crashes.
"""

import unittest
from unittest.mock import MagicMock, patch
import time

from utils.order_executor import (
    preflight_check_buy,
    preflight_check_sell,
    execute_market_order_resilient,
    poll_order_settlement,
    execute_safe_market_buy,
    execute_safe_market_sell,
    MIN_ORDER_KRW
)


class FakeMockUpbitClient:
    """Mock Upbit API Client for testing edge cases."""

    def __init__(self):
        self.orders = {}
        self.buy_call_count = 0
        self.sell_call_count = 0
        self.cancel_call_count = 0
        self.get_order_call_count = 0
        self.transient_fail_count = 0

    def buy_market_order(self, market: str, amount: int):
        self.buy_call_count += 1
        if self.transient_fail_count > 0:
            self.transient_fail_count -= 1
            raise ConnectionResetError("Upbit 502 Bad Gateway (Simulated)")
        uuid = f"mock-buy-{self.buy_call_count}"
        self.orders[uuid] = {
            "uuid": uuid,
            "market": market,
            "side": "bid",
            "state": "done",
            "executed_volume": "4.9138",
            "trades": [{"funds": str(amount)}],
            "paid_fee": str(amount * 0.0005)
        }
        return {"uuid": uuid}

    def sell_market_order(self, market: str, volume: float):
        self.sell_call_count += 1
        uuid = f"mock-sell-{self.sell_call_count}"
        self.orders[uuid] = {
            "uuid": uuid,
            "market": market,
            "side": "ask",
            "state": "done",
            "executed_volume": str(volume),
            "trades": [{"funds": str(volume * 2050.0)}],
            "paid_fee": "5.0"
        }
        return {"uuid": uuid}

    def get_order(self, uuid: str):
        self.get_order_call_count += 1
        return self.orders.get(uuid)

    def cancel_order(self, uuid: str):
        self.cancel_call_count += 1
        if uuid in self.orders:
            self.orders[uuid]["state"] = "cancel"
        return {"uuid": uuid}


class TestSafeOrderExecutor(unittest.TestCase):

    def test_preflight_check_buy_under_minimum(self):
        """4,999 KRW buy must be rejected before reaching the exchange."""
        ok, clean_amt, err = preflight_check_buy("KRW-XRP", 4999, available_krw=100_000)
        self.assertFalse(ok)
        self.assertIn("최소 주문액", err)

    def test_preflight_check_buy_insufficient_krw_including_fee(self):
        """10,000 KRW buy requires 10,005 KRW with fee; 10,000 KRW balance must fail pre-flight."""
        ok, clean_amt, err = preflight_check_buy("KRW-XRP", 10000, available_krw=10000)
        self.assertFalse(ok)
        self.assertIn("예수금 부족", err)

    def test_preflight_check_buy_valid(self):
        """Valid buy returns integer amount and True."""
        ok, clean_amt, err = preflight_check_buy("KRW-XRP", 10000.8, available_krw=10050)
        self.assertTrue(ok)
        self.assertEqual(clean_amt, 10000)
        self.assertEqual(err, "")

    def test_preflight_check_sell_under_minimum(self):
        """Selling volume worth only 3,000 KRW must be rejected before exchange error."""
        # 1.5 XRP * 2000 KRW = 3,000 KRW (< 5,000 KRW)
        ok, clean_vol, err = preflight_check_sell("KRW-XRP", 1.5, current_price=2000.0, available_volume=5.0)
        self.assertFalse(ok)
        self.assertIn("최소 주문액", err)

    def test_preflight_check_sell_insufficient_volume(self):
        """Selling more than available volume must fail preflight."""
        ok, clean_vol, err = preflight_check_sell("KRW-XRP", 10.0, current_price=2000.0, available_volume=5.0)
        self.assertFalse(ok)
        self.assertIn("가용 수량 부족", err)

    def test_preflight_check_sell_float_auto_clipping(self):
        """Slight float imprecision (e.g. 5.000000001 vs 5.0 avail) auto-clips to available."""
        ok, clean_vol, err = preflight_check_sell("KRW-XRP", 5.000000001, current_price=2000.0, available_volume=5.0)
        self.assertTrue(ok)
        self.assertEqual(clean_vol, 5.0)

    def test_network_resilience_recovers_after_502(self):
        """Temporary 502/Connection error is retried and succeeds."""
        mock_client = FakeMockUpbitClient()
        mock_client.transient_fail_count = 1  # Fails 1st attempt, succeeds 2nd attempt

        with patch("time.sleep", return_value=None):
            resp, err = execute_market_order_resilient(mock_client, "KRW-XRP", "bid", 10000)

        self.assertIsNotNone(resp)
        self.assertEqual(resp.get("uuid"), "mock-buy-2")
        self.assertEqual(err, "")
        self.assertEqual(mock_client.buy_call_count, 2)

    def test_poll_order_delayed_settlement_succeeds(self):
        """Settlement that takes multiple polls succeeds once state becomes 'done'."""
        mock_client = FakeMockUpbitClient()
        uuid = "delayed-order-1"
        # First call wait, second call done
        calls = [
            {"uuid": uuid, "state": "wait", "executed_volume": "0.0"},
            {"uuid": uuid, "state": "done", "executed_volume": "4.9", "trades": [{"funds": "10000"}], "paid_fee": "5.0"}
        ]
        mock_client.get_order = MagicMock(side_effect=calls)

        with patch("time.sleep", return_value=None):
            result = poll_order_settlement(mock_client, uuid, max_wait_sec=10.0)

        self.assertEqual(result["status"], "FILLED")
        self.assertEqual(result["volume"], 4.9)
        self.assertAlmostEqual(result["price"], 10000 / 4.9)

    def test_poll_order_stuck_in_wait_auto_cancels(self):
        """Order stuck in 'wait' beyond timeout triggers auto-cancel and returns CANCELLED_TIMEOUT (no halt)."""
        mock_client = FakeMockUpbitClient()
        uuid = "stuck-order-1"
        mock_client.orders[uuid] = {"uuid": uuid, "state": "wait", "executed_volume": "0.0"}

        # Simulate timeout by letting max_wait_sec expire
        with patch("time.sleep", return_value=None):
            result = poll_order_settlement(mock_client, uuid, max_wait_sec=0.1)

        self.assertEqual(result["status"], "CANCELLED_TIMEOUT")
        self.assertEqual(result["volume"], 0.0)
        self.assertEqual(mock_client.cancel_call_count, 1)

    def test_poll_order_stuck_with_partial_fill_auto_cancels(self):
        """Order stuck with partial execution cancels remaining and returns PARTIAL_CANCELLED."""
        mock_client = FakeMockUpbitClient()
        uuid = "partial-stuck-1"
        mock_client.orders[uuid] = {
            "uuid": uuid,
            "state": "wait",
            "executed_volume": "2.5",
            "trades": [{"funds": "5000"}],
            "paid_fee": "2.5"
        }

        with patch("time.sleep", return_value=None):
            result = poll_order_settlement(mock_client, uuid, max_wait_sec=0.1)

        self.assertEqual(result["status"], "PARTIAL_CANCELLED")
        self.assertEqual(result["volume"], 2.5)
        self.assertAlmostEqual(result["price"], 2000.0)
        self.assertEqual(mock_client.cancel_call_count, 1)

    def test_execute_safe_market_buy_end_to_end(self):
        """End-to-end safe market buy integration test."""
        mock_client = FakeMockUpbitClient()
        accepted_uuids = []

        with patch("time.sleep", return_value=None):
            res = execute_safe_market_buy(
                mock_client,
                market="KRW-XRP",
                amount_krw=10000,
                available_krw=50000,
                on_accepted=lambda u: accepted_uuids.append(u)
            )

        self.assertEqual(res["status"], "FILLED")
        self.assertEqual(len(accepted_uuids), 1)
        self.assertEqual(accepted_uuids[0], res["uuid"])
        self.assertEqual(res["volume"], 4.9138)

    def test_execute_safe_market_sell_end_to_end(self):
        """End-to-end safe market sell integration test."""
        mock_client = FakeMockUpbitClient()
        accepted_uuids = []

        with patch("time.sleep", return_value=None):
            res = execute_safe_market_sell(
                mock_client,
                market="KRW-XRP",
                volume=4.9138,
                current_price=2050.0,
                available_volume=5.0,
                on_accepted=lambda u: accepted_uuids.append(u)
            )

        self.assertEqual(res["status"], "FILLED")
        self.assertEqual(len(accepted_uuids), 1)
        self.assertEqual(res["volume"], 4.9138)


if __name__ == "__main__":
    unittest.main()
