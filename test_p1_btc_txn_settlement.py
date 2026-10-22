"""Transactional BTC settlement: DB baseline, delta, trade row and checkpoint in one transaction.

Every test uses its own temp SQLite and temp state; the operational DB is never touched.
"""

from __future__ import annotations

import copy
import os
import sqlite3
import sys
import tempfile
import threading
import types
import unittest
from datetime import datetime
from unittest.mock import Mock, patch

from strategy import btc_accumulator
from utils import db_logger

UUID = "txn-btc-order"


class _Exchange:
    def __init__(self):
        self.order = {"uuid": UUID, "state": "wait", "executed_volume": "0", "trades": []}

    def get_order(self, uuid):
        return copy.deepcopy(self.order)

    def get_balance(self, currency, verbose=False):
        return {"balance": "0.01", "locked": "0", "avg_buy_price": "80000000"}

    def fill(self, state, volume, funds):
        self.order.update(state=state, executed_volume=str(volume),
                          trades=[{"volume": str(volume), "price": str(funds / volume), "funds": str(funds)}])


class IsolatedDbCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = os.path.join(tmp.name, "iso.db")
        self.state_path = os.path.join(tmp.name, "state.json")
        p = patch.object(btc_accumulator, "BTC_STATE_FILE", self.state_path)
        p.start()
        self.addCleanup(p.stop)
        for target in (db_logger, btc_accumulator):
            p = patch.object(target, "DB_PATH", self.db)
            p.start()
            self.addCleanup(p.stop)
        # snapshot sync is best-effort and imported lazily; make it inert
        sync = types.SimpleNamespace(trigger_snapshot_sync=lambda: None)
        p = patch.dict(sys.modules, {"utils.sync_manager": sync})
        p.start()
        self.addCleanup(p.stop)

    def totals(self):
        c = sqlite3.connect(self.db)
        try:
            return c.execute(
                "SELECT COUNT(*), COALESCE(SUM(volume),0), COALESCE(SUM(cost_or_revenue),0) "
                "FROM trades WHERE order_uuid=?", (UUID,)).fetchone()
        finally:
            c.close()

    def settle(self, vol, funds, terminal=False):
        return db_logger.log_real_trade(
            market="KRW-BTC", ticker="BTC", side="bid", action="T", price=0.0, volume=vol,
            cost_or_revenue=funds, order_uuid=UUID, cumulative_volume=vol,
            cumulative_funds=funds, terminal=terminal, idempotent=True)


class TestTransactionalSettlement(IsolatedDbCase):
    def test_repeat_same_cumulative_records_once(self):
        first = self.settle(0.0001, 10_000)
        second = self.settle(0.0001, 10_000, terminal=True)
        self.assertTrue(first["recorded"])
        self.assertFalse(second["recorded"])
        n, vol, funds = self.totals()
        self.assertEqual(1, n)
        self.assertAlmostEqual(0.0001, vol)
        self.assertAlmostEqual(10_000, funds)

    def test_partial_repeat_more_partial_then_done_records_only_increments(self):
        self.settle(0.00004, 3_200)
        self.settle(0.00004, 3_200)
        self.settle(0.00012, 9_999)
        self.settle(0.00012, 9_999, terminal=True)
        n, vol, funds = self.totals()
        self.assertEqual(2, n)
        self.assertAlmostEqual(0.00012, vol)
        self.assertAlmostEqual(9_999, funds)

    def test_concurrent_reprocessing_has_no_duplicates(self):
        barrier = threading.Barrier(8)
        results, errors = [], []

        def worker():
            try:
                barrier.wait()
                results.append(self.settle(0.0001, 10_000, terminal=True))
            except Exception as e:  # pragma: no cover
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual([], errors)
        self.assertEqual(1, sum(1 for r in results if r["recorded"]))
        n, vol, funds = self.totals()
        self.assertEqual(1, n)
        self.assertAlmostEqual(0.0001, vol)
        self.assertAlmostEqual(10_000, funds)

    def test_failed_transaction_rolls_back_trade_and_checkpoint(self):
        with patch.object(db_logger, "datetime") as dt:
            dt.now.side_effect = RuntimeError("boom")
            with self.assertRaises(RuntimeError):
                self.settle(0.0001, 10_000)
        self.assertEqual(0, self.totals()[0])
        # retry afterwards recovers without loss or duplication
        self.settle(0.0001, 10_000, terminal=True)
        self.assertEqual(1, self.totals()[0])

    def test_checkpoint_never_decreases(self):
        self.settle(0.0001, 10_000, terminal=True)
        db_logger.record_order_terminal_checkpoint(UUID, "KRW-BTC", 0.00001, 100)
        got = db_logger.get_order_settled_totals(UUID)
        self.assertAlmostEqual(0.0001, got["credited_volume"])
        self.assertAlmostEqual(10_000, got["credited_funds"])


class TestBtcSettleWithStaleJson(IsolatedDbCase):
    def setUp(self):
        super().setUp()
        self.state = {"total_dca_buys_count": 0, "total_dca_invested_krw": 0.0,
                      "pending_buy_order": {"uuid": UUID, "tier": 1, "reason": "r",
                                            "credited_volume": 0.0, "credited_funds": 0.0}}
        btc_accumulator.save_btc_state(self.state)
        self.alert = Mock()
        for p in (
            patch.object(btc_accumulator, "save_btc_state", return_value=True),
            patch.object(btc_accumulator, "_clear_btc_fallback_checkpoint"),
            patch.object(btc_accumulator, "_save_btc_fallback_checkpoint"),
            patch.dict(sys.modules, {"main": types.SimpleNamespace(send_telegram_alert=self.alert)}),
        ):
            p.start()
            self.addCleanup(p.stop)
        self.ex = _Exchange()
        self.now = datetime(2026, 9, 28, 8, 55, 0)

    def run_settle(self, state):
        return btc_accumulator._settle_btc_tiered_buy(self.ex, state, self.now)

    def test_db_commit_then_json_loss_restart_does_not_duplicate(self):
        self.ex.fill("done", 0.0001, 10_000)
        stale = copy.deepcopy(self.state)  # JSON that never got saved
        self.assertEqual("BOUGHT", self.run_settle(self.state)["action"])
        self.assertEqual(1, self.totals()[0])
        self.run_settle(stale)  # restart with the stale pending
        n, vol, funds = self.totals()
        self.assertEqual(1, n)
        self.assertAlmostEqual(0.0001, vol)
        self.assertAlmostEqual(10_000, funds)
        self.assertNotIn("pending_buy_order", stale)

    def test_partial_then_done_across_restart(self):
        self.ex.fill("wait", 0.00004, 3_200)
        self.run_settle(self.state)
        stale = copy.deepcopy(self.state)
        stale["pending_buy_order"]["credited_volume"] = 0.0
        stale["pending_buy_order"]["credited_funds"] = 0.0
        self.run_settle(stale)
        self.ex.fill("done", 0.0001, 10_000)
        self.run_settle(stale)
        n, vol, funds = self.totals()
        self.assertEqual(2, n)
        self.assertAlmostEqual(0.0001, vol)
        self.assertAlmostEqual(10_000, funds)

    def test_cancelled_after_partial_fill_recorded_once(self):
        self.ex.fill("cancel", 0.00005, 5_000)
        self.run_settle(copy.deepcopy(self.state))
        self.run_settle(copy.deepcopy(self.state))
        n, vol, _ = self.totals()
        self.assertEqual(1, n)
        self.assertAlmostEqual(0.00005, vol)


if __name__ == "__main__":
    unittest.main()
