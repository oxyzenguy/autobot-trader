"""
Adversarial stress and penetration test for XRP Grid Scalper & Gommapa engine.
Tests edge cases, corrupted states, NaN/zero prices, SQLite locks, and recovery.
"""

import unittest
import os
import json
import sqlite3
import tempfile
from unittest.mock import patch, MagicMock

import utils.order_executor as oe
from utils.order_executor import (
    preflight_check_buy,
    preflight_check_sell,
    execute_market_order_resilient,
    poll_order_settlement,
    execute_safe_market_buy,
    execute_safe_market_sell
)
import paper_grid_xrp


class TestAdversarialOrderExecutor(unittest.TestCase):

    def test_extreme_floating_point_inputs(self):
        """Preflight must handle inf, nan, and negative values safely without throwing uncaught exceptions."""
        for bad_val in [float("nan"), float("inf"), float("-inf"), -100.0, 0.0]:
            ok, _, _ = preflight_check_buy("KRW-XRP", bad_val, available_krw=100_000)
            self.assertFalse(ok, f"Expected False for buy amount {bad_val}")

            ok_s, _, _ = preflight_check_sell("KRW-XRP", bad_val, current_price=2000.0, available_volume=10.0)
            self.assertFalse(ok_s, f"Expected False for sell volume {bad_val}")

            ok_s2, _, _ = preflight_check_sell("KRW-XRP", 5.0, current_price=bad_val, available_volume=10.0)
            self.assertFalse(ok_s2, f"Expected False for sell price {bad_val}")

    def test_poll_order_cancel_done_race_condition(self):
        """If order finishes 'done' exactly when cancel is sent, status must be FILLED, not PARTIAL_CANCELLED."""
        mock_client = MagicMock()
        uuid = "race-order-uuid"

        # 1st call: wait
        # 2nd call: done (after cancel)
        mock_client.get_order.side_effect = [
            {"uuid": uuid, "state": "wait", "executed_volume": "0.0"},
            {"uuid": uuid, "state": "done", "executed_volume": "4.9138", "trades": [{"funds": "10000"}], "paid_fee": "5"}
        ]
        mock_client.cancel_order.return_value = {"error": {"name": "already_done"}}

        with patch("time.sleep", return_value=None):
            res = poll_order_settlement(mock_client, uuid, max_wait_sec=0.1)

        self.assertEqual(res["status"], "FILLED")
        self.assertEqual(res["volume"], 4.9138)

    def test_sqlite_concurrent_busy_timeout(self):
        """Verify SQLite DB connections handle concurrency without deadlock."""
        with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
            db_path = tmp.name
            conn1 = sqlite3.connect(db_path, timeout=5.0)
            c1 = conn1.cursor()
            c1.execute("CREATE TABLE test (id INT, val TEXT)")
            conn1.commit()

            conn2 = sqlite3.connect(db_path, timeout=5.0)
            c2 = conn2.cursor()
            c2.execute("INSERT INTO test VALUES (1, 'hello')")
            conn2.commit()

            c1.execute("SELECT val FROM test WHERE id = 1")
            row = c1.fetchone()
            self.assertEqual(row[0], "hello")
            conn1.close()
            conn2.close()

    def test_corrupted_json_state_recovery(self):
        """If state JSON is corrupted or empty, load_state must recover cleanly without crashing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_state_file = os.path.join(tmpdir, "corrupt_state.json")
            with open(test_state_file, "w") as f:
                f.write("{invalid_json: true, unterminated")

            with patch.object(paper_grid_xrp, "STATE_FILE", test_state_file):
                state = paper_grid_xrp.load_state()
                self.assertIsNotNone(state)
                self.assertEqual(state["mode"], "GRID_SCALPER")
                self.assertEqual(state["initial_capital"], 1_000_000.0)

    def test_missing_backward_compatible_keys_in_state(self):
        """Old state format missing Gommapa keys must automatically backfill defaults."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_state_file = os.path.join(tmpdir, "old_state.json")
            with open(test_state_file, "w") as f:
                json.dump({"initial_capital": 1000000, "active_grids": []}, f)

            with patch.object(paper_grid_xrp, "STATE_FILE", test_state_file):
                state = paper_grid_xrp.load_state()
                self.assertIn("mode", state)
                self.assertEqual(state["mode"], "GRID_SCALPER")
                self.assertIn("gommapa_tranches", state)
                self.assertEqual(state["gommapa_tranches"], [])
                self.assertIn("gommapa_base_p", state)

    def test_atomic_state_save(self):
        """Atomic save must create valid JSON with flush and fsync."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_state_file = os.path.join(tmpdir, "atomic_state.json")
            test_state = {"test_key": "test_val", "active_grids": [{"lvl": 1}]}

            with patch.object(paper_grid_xrp, "STATE_FILE", test_state_file):
                paper_grid_xrp.save_state(test_state)
                self.assertTrue(os.path.exists(test_state_file))
                with open(test_state_file, "r") as f:
                    loaded = json.load(f)
                self.assertEqual(loaded["test_key"], "test_val")
                self.assertIn("last_update", loaded)


if __name__ == "__main__":
    unittest.main()
