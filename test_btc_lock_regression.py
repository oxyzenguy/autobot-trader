from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import TracebackType
import types
from typing import Protocol
import unittest
from unittest.mock import patch

from strategy import btc_accumulator as btc
import test_p1_btc_fill as fill_tests
from utils import db_logger


class LockProtocol(Protocol):
    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool: ...
    def release(self) -> None: ...


class TimeoutBoundRealLock:
    def __init__(self, inner: LockProtocol):
        self.inner = inner

    def __enter__(self) -> None:
        if not self.inner.acquire(timeout=0.2):
            raise TimeoutError("BTC nested lock acquisition timed out")

    def __exit__(self, kind: type[BaseException] | None,
                 error: BaseException | None, traceback: TracebackType | None) -> None:
        self.inner.release()


class TestBtcLockRegression(unittest.TestCase):
    def test_immediate_new_order_fill_completes_under_nested_lock(self):
        fixture = fill_tests.TestP1BtcFillReproduction()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        fixture.set_fill("done")
        with patch.object(btc, "_btc_buy_lock", TimeoutBoundRealLock(btc._btc_buy_lock)):
            result = fixture.execute()
        fixture.assert_fill(result, 0.00012, 9_999.0)
        self.assertEqual(1, len(fixture.exchange.orders))

    def test_another_thread_cannot_enter_while_owner_holds_lock(self):
        lock = btc._btc_buy_lock
        with lock, ThreadPoolExecutor(max_workers=1) as pool:
            acquired = pool.submit(lock.acquire, timeout=0.05).result(timeout=2)
            self.assertFalse(acquired)


class TestBtcUnlockedSettlement(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.state_path = str(Path(folder.name) / "state.json")
        self.db_path = str(Path(folder.name) / "trades.db")
        for patcher in (
            patch.multiple(btc, BTC_STATE_FILE=self.state_path, DB_PATH=self.db_path,
                           datetime=fill_tests.DueDatetime),
            patch.object(db_logger, "DB_PATH", self.db_path),
            patch.object(btc, "_btc_buy_lock", TimeoutBoundRealLock(btc._btc_buy_lock)),
            patch.dict(btc._btc_unpersisted_by_file, clear=True),
            patch.object(btc, "check_btc_tiered_status", return_value={
                "tier": 1, "buy_krw": 10000, "tier_reason": "test", "is_valid": True}),
            patch.dict(sys.modules, {"main": types.SimpleNamespace(send_telegram_alert=lambda text: None)}),
            patch("utils.sync_manager.trigger_snapshot_sync"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(btc._clear_btc_fallback_checkpoint)
        self.exchange = fill_tests.FakeExchange()

    def assert_single_fill(self):
        with sqlite3.connect(self.db_path) as connection:
            count, volume, funds = connection.execute(
                "SELECT COUNT(*), SUM(volume), SUM(cost_or_revenue) FROM trades"
            ).fetchone()
        state = btc.load_btc_state()
        self.assertAlmostEqual(0.0001, volume)
        self.assertEqual(10000.0, funds)
        self.assertAlmostEqual(0.0001, state["total_dca_volume"])
        self.assertEqual(10000.0, state["total_dca_invested_krw"])
        self.assertEqual(1, state["total_dca_buys_count"])
        self.assertNotIn("pending_buy_order", state)
        self.assertEqual(1, len(self.exchange.orders))

    def test_immediate_done_fill_updates_real_db_and_json_once(self):
        self.exchange.order.update(state="done", executed_volume="0.0001", trades=[{"funds": "10000"}])
        result = btc.execute_btc_tiered_buy_if_due(self.exchange)
        self.assertEqual("BOUGHT", result["action"])
        self.assert_single_fill()
        disk = json.loads(Path(self.state_path).read_text())
        self.assertEqual(10000.0, disk["total_dca_invested_krw"])
        self.assertEqual(1, disk["total_dca_buys_count"])

    def test_partial_then_terminal_json_failure_recovers_without_double_credit(self):
        btc.execute_btc_tiered_buy_if_due(self.exchange)
        self.exchange.order.update(state="wait", executed_volume="0.00004", trades=[{"funds": "3200"}])
        btc.execute_btc_tiered_buy_if_due(self.exchange)
        self.exchange.order.update(state="done", executed_volume="0.0001", trades=[{"funds": "10000"}])
        replace = os.replace

        def fail_primary(src, dst) -> None:
            if str(dst) == self.state_path:
                raise OSError("terminal state replacement failure")
            replace(src, dst)

        with patch.object(btc.os, "replace", side_effect=fail_primary):
            btc.execute_btc_tiered_buy_if_due(self.exchange)
        btc._btc_unpersisted_by_file.clear()
        btc.execute_btc_tiered_buy_if_due(self.exchange)
        self.assert_single_fill()


if __name__ == "__main__":
    unittest.main()
