"""Read-only regression scenarios for owned orders and worker heartbeats."""

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

from utils import system_auditor as auditor


class FakeOrderReader:
    """In-memory exchange responses; no order mutation methods exist."""

    def __init__(self, open_orders, details=None):
        self.open_orders = open_orders
        self.details = details or {}
        self.queried = []

    def get_balances(self):
        return [{"currency": "KRW", "balance": "100000", "locked": "0"}] + [
            {"currency": market[4:], "balance": str(amount + (0.1 if market == "KRW-ETH" else 0)), "locked": "0"}
            for market, amount in auditor.PROTECTED_BALANCES.items()
        ]

    def get_order(self, key, state="wait"):
        self.queried.append(key)
        return self.open_orders if key == "KRW-ETH" else ([] if key.startswith("KRW-") else self.details.get(key))


class TestAuditorOrderMonitoring(unittest.TestCase):
    def inspect(self, exchange):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = {"market": "KRW-ETH", "bot_quantity": 0.1, "basket_limit_ask": {"uuid": "bot-ask"},
                     "bear_limit_buys": {"bot-bid": {"state": "wait"}, "old": {"state": "cancel"}}}
            (root / "real_strategy_state_KRW_ETH.json").write_text(json.dumps(state))
            with patch.object(auditor, "BASE_DIR", root), patch("pyupbit.Upbit", return_value=exchange):
                return auditor.audit_exchange_and_ledgers()

    def test_manual_order_cannot_mask_missing_owned_uuid(self):
        # Given: correct holdings but only an unrelated manual ask.
        exchange = FakeOrderReader([{"uuid": "manual", "side": "ask"}])
        # When: the auditor reads exchange orders.
        result = self.inspect(exchange)
        # Then: both missing owned UUIDs are reported, not a green count.
        self.assertTrue(any("bot-ask" in issue for issue in result["issues"]))
        self.assertTrue(any("bot-bid" in issue for issue in result["issues"]))

    def test_terminal_order_is_distinguished_from_unknown_order(self):
        # Given: a filled ask missing from the open list and an unresolved bid.
        exchange = FakeOrderReader([], {"bot-ask": {"uuid": "bot-ask", "market": "KRW-ETH", "side": "ask", "state": "done"}})
        # When: missing UUIDs are queried individually.
        result = self.inspect(exchange)
        # Then: terminal settlement and lookup uncertainty remain distinct.
        statuses = result["markets"]["KRW-ETH"]["owned_order_states"]
        self.assertEqual("done", statuses["bot-ask"])
        self.assertEqual("unconfirmed", statuses["bot-bid"])

    def test_owned_orders_and_manual_orders_are_counted_separately(self):
        # Given: both expected orders plus a manual order.
        exchange = FakeOrderReader([{"uuid": "bot-ask", "side": "ask"}, {"uuid": "bot-bid", "side": "bid"}, {"uuid": "manual", "side": "ask"}])
        # When: ownership is audited.
        result = self.inspect(exchange)
        # Then: manual order is preserved and no UUID reconciliation is needed.
        market = result["markets"]["KRW-ETH"]
        self.assertEqual((1, 1, 1), (market["bot_open_asks"], market["bot_open_bids"], market["manual_open_orders"]))
        self.assertEqual([], market["missing_owned_order_uuids"])
        self.assertNotIn("old", exchange.queried)

    def test_owned_order_wrong_side_is_reported(self):
        # Given: an owned ask UUID appears as a bid.
        exchange = FakeOrderReader([{"uuid": "bot-ask", "side": "bid"}, {"uuid": "bot-bid", "side": "bid"}])
        # When: orders are checked against their checkpoints.
        result = self.inspect(exchange)
        # Then: wrong ownership metadata cannot pass silently.
        self.assertTrue(any("bot-ask" in issue for issue in result["issues"]))

    def test_lookup_with_wrong_uuid_or_market_remains_unconfirmed(self):
        # Given: an API response that cannot confirm the requested owned order.
        for uuid, market in (("other-order", "KRW-ETH"), ("bot-ask", "KRW-SOL")):
            with self.subTest(uuid=uuid, market=market):
                exchange = FakeOrderReader([], {"bot-ask": {"uuid": uuid, "market": market, "side": "ask", "state": "done"}})
                # When: the auditor looks up the missing ask.
                result = self.inspect(exchange)
                # Then: no terminal settlement is claimed from a mismatched response.
                self.assertEqual("unconfirmed", result["markets"]["KRW-ETH"]["owned_order_states"]["bot-ask"])

    def test_open_query_failure_does_not_invent_zero_owned_orders(self):
        # Given: open order status is unavailable, not an empty confirmed list.
        exchange = FakeOrderReader(None)
        # When: the auditor reads exchange orders.
        result = self.inspect(exchange)
        # Then: ownership counts remain explicitly unknown with an issue recorded.
        self.assertIsNone(result["markets"]["KRW-ETH"]["bot_open_asks"])
        self.assertTrue(result["issues"])


class TestAuditorWorkerHeartbeats(unittest.TestCase):
    def inspect(self, log, paper_log="[2026-10-10 01:00:00] 💓 [가상 봇 하트비트] XRP: 1900원\n"):
        timestamp = datetime(2026, 10, 10, 1, 0, tzinfo=timezone(timedelta(hours=9))).timestamp()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, content in (("bot.log", log), ("paper_grid_xrp.log", paper_log)):
                path = root / name
                path.write_text(content)
                os.utime(path, (timestamp, timestamp))
            with patch.object(auditor, "BASE_DIR", root), patch.object(auditor.time, "time", return_value=timestamp):
                return auditor.check_heartbeat_and_log_health()

    def test_fresh_eth_and_log_mtime_cannot_hide_stale_sol(self):
        # Given: new ETH output but SOL last reported an hour ago.
        log = "[2026-10-10 00:00:00] [HEARTBEAT] SOL: 150000원\n[2026-10-10 01:00:00] [HEARTBEAT] ETH: 3400000원\n[2026-10-10 01:00:00] [KRW-BTC] 💓 하트비트\n"
        # When: the fresh shared log is inspected.
        result = self.inspect(log)
        # Then: the silent SOL worker is stalled despite recent writes.
        self.assertTrue(result["is_stalled"])
        self.assertTrue(result["market_heartbeats"]["KRW-SOL"]["is_stalled"])
        self.assertFalse(result["market_heartbeats"]["KRW-ETH"]["is_stalled"])

    def test_warnings_after_heartbeat_do_not_hide_fresh_workers(self):
        # Given: warning noise displaces heartbeats beyond the former 40-line tail.
        log = "[00:00:00] [KRW-BTC] 💓 하트비트\n[00:40:00] [HEARTBEAT] ETH: 3400000원\n[00:40:00] [HEARTBEAT] SOL: 150000원\n" + "warning\n" * 100
        # When: logs are inspected with per-worker cadence.
        result = self.inspect(log)
        # Then: all workers remain healthy, including the hourly BTC loop.
        self.assertFalse(result["is_stalled"])

    def test_midnight_time_only_heartbeat_uses_previous_day(self):
        # Given: SOL's time-only heartbeat was just before midnight.
        log = "[23:50:00] [HEARTBEAT] SOL: 150000원\n[00:40:00] [HEARTBEAT] ETH: 3400000원\n[01:00:00] [KRW-BTC] 💓 하트비트\n"
        # When: current time is 01:00 KST.
        result = self.inspect(log)
        # Then: its age is 70 minutes, not a future or fresh heartbeat.
        self.assertEqual(4200, result["market_heartbeats"]["KRW-SOL"]["age_seconds"])
        self.assertTrue(result["market_heartbeats"]["KRW-SOL"]["is_stalled"])

    def test_hourly_paper_worker_has_its_own_stall_threshold(self):
        # Given: all live loops are current but XRP has been silent for two hours.
        log = "[01:00:00] [HEARTBEAT] ETH: 3400000원\n[01:00:00] [HEARTBEAT] SOL: 150000원\n[01:00:00] [KRW-BTC] 💓 하트비트\n"
        # When: its separate log is inspected.
        result = self.inspect(log, "[2026-10-09 23:00:00] 💓 [가상 봇 하트비트] XRP: 1900원\n")
        # Then: only the XRP worker is stalled.
        self.assertTrue(result["market_heartbeats"]["KRW-XRP"]["is_stalled"])
        self.assertFalse(result["market_heartbeats"]["KRW-BTC"]["is_stalled"])

    def test_missing_or_future_heartbeat_cannot_pass_as_fresh(self):
        # Given: no legitimate ETH/SOL heartbeat despite a newly written log.
        log = "[2026-10-10 03:00:00] [HEARTBEAT] ETH: 3400000원\n[01:00:00] warning\n"
        # When: 01:00 KST is the current time.
        result = self.inspect(log)
        # Then: future data and absent worker data both remain unconfirmed/stalled.
        self.assertTrue(result["market_heartbeats"]["KRW-ETH"]["is_stalled"])
        self.assertIsNone(result["market_heartbeats"]["KRW-ETH"]["age_seconds"])
        self.assertTrue(result["market_heartbeats"]["KRW-SOL"]["is_stalled"])


if __name__ == "__main__":
    unittest.main()
