"""
Adversarial stress and penetration test for XRP Grid Scalper & Gommapa engine.
Tests edge cases, persistent corruption blockage (P1-1), critical accounting validation (P1-2),
isolated 1-tick engine PnL invariance (P2-3), flash crash gap pricing policy (P2-4),
Gommapa 3rd stage execution, process lock, and concurrency collisions.
"""

import unittest
import os
import json
import sqlite3
import tempfile
import concurrent.futures
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

    def test_corrupted_json_state_persistent_blockage(self):
        """
        P1-1: Corrupted JSON state must persistently block subsequent restarts.
        Without explicit operator recovery, it MUST NOT auto-create a brand new 1M KRW state.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            test_state_file = os.path.join(tmpdir, "corrupt_state.json")
            halt_flag_file = os.path.join(tmpdir, ".paper_grid_KRW_XRP.corrupted_halt")
            with open(test_state_file, "w") as f:
                f.write("{invalid_json: true, unterminated")

            with patch.object(paper_grid_xrp, "STATE_FILE", test_state_file), \
                 patch.object(paper_grid_xrp, "BASE_DIR", tmpdir):
                # 1st run: must fail and create halt flag + backup
                with self.assertRaises(RuntimeError) as ctx1:
                    paper_grid_xrp.load_state()
                self.assertIn("안전 차단", str(ctx1.exception))
                self.assertTrue(os.path.exists(halt_flag_file))

                # 2nd run (restart without manual fix): must STILL fail and remain blocked
                with self.assertRaises(RuntimeError) as ctx2:
                    paper_grid_xrp.load_state()
                self.assertIn("안전 차단", str(ctx2.exception))

                # 3rd run: even if user unlinks test_state_file, backup and halt flag prevent 1M auto-create
                os.unlink(test_state_file)
                with self.assertRaises(RuntimeError) as ctx3:
                    paper_grid_xrp.load_state()
                self.assertIn("안전 차단", str(ctx3.exception))

                # 4th run: only after explicit cleanup of halt flag and backups can fresh state be created
                os.unlink(halt_flag_file)
                for f in os.listdir(tmpdir):
                    if "corrupted" in f:
                        os.unlink(os.path.join(tmpdir, f))

                fresh_state = paper_grid_xrp.load_state()
                self.assertEqual(fresh_state["available_krw"], 1_000_000.0)

    def test_missing_active_grids_or_tranches_halts(self):
        """
        P1: Missing active_grids or gommapa_tranches must NEVER be defaulted to empty [].
        Doing so would erase existing positions and allow dangerous re-entries.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            test_state_file = os.path.join(tmpdir, "missing_positions_state.json")
            halt_flag = os.path.join(tmpdir, ".paper_grid_KRW_XRP.corrupted_halt")

            # Case 1: active_grids missing
            state_no_grids = {
                "available_krw": 980_000.0,
                "mode": "GRID_SCALPER",
                "base_price": 2000.0,
                "gommapa_base_p": 2000.0,
                "gommapa_tranches": []
            }
            with open(test_state_file, "w") as f:
                json.dump(state_no_grids, f)

            with patch.object(paper_grid_xrp, "STATE_FILE", test_state_file), \
                 patch.object(paper_grid_xrp, "BASE_DIR", tmpdir):
                with self.assertRaises(RuntimeError) as ctx:
                    paper_grid_xrp.load_state()
                self.assertIn("active_grids", str(ctx.exception))
                self.assertTrue(os.path.exists(halt_flag))

            if os.path.exists(halt_flag):
                os.unlink(halt_flag)

            # Case 2: gommapa_tranches missing
            state_no_tranches = {
                "available_krw": 980_000.0,
                "mode": "GRID_SCALPER",
                "base_price": 2000.0,
                "gommapa_base_p": 2000.0,
                "active_grids": []
            }
            with open(test_state_file, "w") as f:
                json.dump(state_no_tranches, f)

            with patch.object(paper_grid_xrp, "STATE_FILE", test_state_file), \
                 patch.object(paper_grid_xrp, "BASE_DIR", tmpdir):
                with self.assertRaises(RuntimeError) as ctx:
                    paper_grid_xrp.load_state()
                self.assertIn("gommapa_tranches", str(ctx.exception))

    def test_missing_mode_halts_and_tranches_preserved_on_transition(self):
        """
        P1: Missing mode must NOT default to GRID_SCALPER (must halt).
        Also, entering 50 grids in GOMMAPA_ESCAPE must NOT wipe existing gommapa_tranches.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            test_state_file = os.path.join(tmpdir, "missing_mode_state.json")
            halt_flag = os.path.join(tmpdir, ".paper_grid_KRW_XRP.corrupted_halt")

            # Case 1: mode missing
            state_no_mode = {
                "available_krw": 400_000.0,
                "base_price": 2000.0,
                "gommapa_base_p": 1500.0,
                "active_grids": [],
                "gommapa_tranches": [{"stage": 1, "buy_p": 1350.0, "vol": 74.0, "cost": 100_000.0}]
            }
            with open(test_state_file, "w") as f:
                json.dump(state_no_mode, f)

            with patch.object(paper_grid_xrp, "STATE_FILE", test_state_file), \
                 patch.object(paper_grid_xrp, "BASE_DIR", tmpdir):
                with self.assertRaises(RuntimeError) as ctx:
                    paper_grid_xrp.load_state()
                self.assertIn("mode", str(ctx.exception))

            if os.path.exists(halt_flag):
                os.unlink(halt_flag)

            # Case 2: In process_paper_tick, 50-grid transition must preserve existing gommapa_tranches
            existing_tranche = {"stage": 1, "buy_p": 1350.0, "vol": 74.0, "cost": 100_000.0}
            active_grids_50 = [
                {"lvl": i, "buy_p": 2000.0 * (1 - 0.005 * i), "vol": 5.0, "cost": 10000.0, "tp_p": 2000.0 * (1 - 0.005 * i) * 1.012}
                for i in range(1, 51)
            ]
            state_50 = {
                "market": "KRW-XRP",
                "strategy": "GRID_SCALPER",
                "mode": "GRID_SCALPER",
                "available_krw": 400_000.0,
                "base_price": 2000.0,
                "gommapa_base_p": 1500.0,
                "active_grids": active_grids_50,
                "gommapa_tranches": [existing_tranche]
            }
            # Run 1 tick in GRID_SCALPER mode with 50 grids without notifying Telegram (testing Gommapa transition when enabled).
            with patch.multiple(paper_grid_xrp, USE_GOMMAPA=True, MAX_LEVELS=50), \
                 patch.object(paper_grid_xrp, "send_telegram_alert", return_value=None):
                updated_state, _ = paper_grid_xrp.process_paper_tick(state_50, curr_p=1400.0)
            self.assertEqual(updated_state["mode"], "GOMMAPA_ESCAPE")
            # The existing 100,000 KRW tranche must NOT be erased!
            self.assertEqual(len(updated_state["gommapa_tranches"]), 1)
            self.assertEqual(updated_state["gommapa_tranches"][0]["stage"], 1)

    def test_tp_p_validation_and_duplicate_levels_halts(self):
        """
        P2: Missing tp_p, invalid tp_p <= buy_p, duplicate grid levels, or duplicate gommapa stages
        must all fail validation with a halt flag.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            test_state_file = os.path.join(tmpdir, "validation_state.json")
            halt_flag = os.path.join(tmpdir, ".paper_grid_KRW_XRP.corrupted_halt")

            def check_state_raises(s_dict, expected_substr):
                if os.path.exists(halt_flag):
                    os.unlink(halt_flag)
                with open(test_state_file, "w") as f:
                    json.dump(s_dict, f)
                with patch.object(paper_grid_xrp, "STATE_FILE", test_state_file), \
                     patch.object(paper_grid_xrp, "BASE_DIR", tmpdir):
                    with self.assertRaises(RuntimeError) as ctx:
                        paper_grid_xrp.load_state()
                    self.assertIn(expected_substr, str(ctx.exception))
                    self.assertTrue(os.path.exists(halt_flag))

            base_valid = {
                "initial_capital": 1_000_000.0,
                "available_krw": 990_000.0,
                "realized_pnl": 0.0,
                "mode": "GRID_SCALPER",
                "base_price": 2000.0,
                "gommapa_base_p": 0.0,
                "active_grids": [],
                "gommapa_tranches": []
            }

            # 1. Missing tp_p in active grid
            s1 = dict(base_valid)
            s1["active_grids"] = [{"lvl": 1, "buy_p": 2000.0, "vol": 5.0, "cost": 10000.0}] # no tp_p
            check_state_raises(s1, "tp_p")

            # 2. tp_p <= buy_p
            s2 = dict(base_valid)
            s2["active_grids"] = [{"lvl": 1, "buy_p": 2000.0, "vol": 5.0, "cost": 10000.0, "tp_p": 1900.0}]
            check_state_raises(s2, "익절 목표가")

            # 3. Duplicate grid level
            s3 = dict(base_valid)
            s3["available_krw"] = 980_000.0
            s3["active_grids"] = [
                {"lvl": 1, "buy_p": 2000.0, "vol": 5.0, "cost": 10000.0, "tp_p": 2024.0},
                {"lvl": 1, "buy_p": 2000.0, "vol": 5.0, "cost": 10000.0, "tp_p": 2024.0}
            ]
            check_state_raises(s3, "중복 감지")

            # 4. Duplicate gommapa stage
            s4 = dict(base_valid)
            s4["mode"] = "GOMMAPA_ESCAPE"
            s4["gommapa_base_p"] = 1800.0
            s4["available_krw"] = 800_000.0
            s4["gommapa_tranches"] = [
                {"stage": 1, "buy_p": 1800.0, "vol": 55.0, "cost": 100_000.0},
                {"stage": 1, "buy_p": 1800.0, "vol": 55.0, "cost": 100_000.0}
            ]
            check_state_raises(s4, "중복 감지")

            # 5. Out of range grid level (lvl=0 or lvl=51)
            s5 = dict(base_valid)
            s5["active_grids"] = [{"lvl": 0, "buy_p": 2000.0, "vol": 5.0, "cost": 10000.0, "tp_p": 2024.0}]
            check_state_raises(s5, "범위 오류")

            # 6. Out of range gommapa stage (stage=4)
            s6 = dict(base_valid)
            s6["mode"] = "GOMMAPA_ESCAPE"
            s6["gommapa_base_p"] = 1800.0
            s6["available_krw"] = 900_000.0
            s6["gommapa_tranches"] = [{"stage": 4, "buy_p": 1800.0, "vol": 55.0, "cost": 100_000.0}]
            check_state_raises(s6, "범위 오류")

    def test_accounting_critical_fields_strict_validation(self):
        """
        Verify available_krw, base_price, gommapa_base_p are strictly validated.
        Display/statistical defaults are safely filled only when critical fields are valid.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            test_state_file = os.path.join(tmpdir, "critical_fields_state.json")
            halt_flag = os.path.join(tmpdir, ".paper_grid_KRW_XRP.corrupted_halt")

            # Case A: Missing available_krw
            bad_state = {
                "mode": "GRID_SCALPER",
                "base_price": 2000.0,
                "gommapa_base_p": 0.0,
                "active_grids": [{"lvl": 1, "buy_p": 2000.0, "vol": 5.0, "cost": 10000.0, "tp_p": 2024.0}],
                "gommapa_tranches": []
            }
            with open(test_state_file, "w") as f:
                json.dump(bad_state, f)

            with patch.object(paper_grid_xrp, "STATE_FILE", test_state_file), \
                 patch.object(paper_grid_xrp, "BASE_DIR", tmpdir):
                with self.assertRaises(RuntimeError) as ctx:
                    paper_grid_xrp.load_state()
                self.assertIn("available_krw", str(ctx.exception))
                self.assertTrue(os.path.exists(halt_flag))

            if os.path.exists(halt_flag):
                os.unlink(halt_flag)

            # Case B: Valid critical fields, missing display/stats metadata
            valid_minimal = {
                "initial_capital": 1_000_000.0,
                "available_krw": 990_000.0,
                "mode": "GRID_SCALPER",
                "base_price": 2034.0,
                "gommapa_base_p": 0.0,
                "active_grids": [{"lvl": 1, "buy_p": 2034.0, "vol": 4.91, "cost": 10000.0, "tp_p": 2058.0}],
                "gommapa_tranches": []
            }
            with open(test_state_file, "w") as f:
                json.dump(valid_minimal, f)

            with patch.object(paper_grid_xrp, "STATE_FILE", test_state_file), \
                 patch.object(paper_grid_xrp, "BASE_DIR", tmpdir):
                loaded = paper_grid_xrp.load_state()
                self.assertEqual(loaded["available_krw"], 990_000.0)
                self.assertEqual(loaded["mode"], "GRID_SCALPER")
                self.assertEqual(len(loaded["active_grids"]), 1)
                # Display/metadata fields filled with defaults
                self.assertEqual(loaded["market"], "KRW-XRP")
                self.assertEqual(loaded["win_count"], 0)
                self.assertGreater(loaded["total_equity"], 980_000.0)

    def test_atomic_state_save(self):
        """Atomic save must create valid JSON with flush and fsync."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_state_file = os.path.join(tmpdir, "atomic_state.json")
            test_state = {
                "initial_capital": 1_000_000.0,
                "available_krw": 1_000_000.0,
                "realized_pnl": 0.0,
                "mode": "GRID_SCALPER",
                "base_price": 0.0,
                "gommapa_base_p": 0.0,
                "active_grids": [],
                "gommapa_tranches": [],
                "test_key": "test_val"
            }

            with patch.object(paper_grid_xrp, "STATE_FILE", test_state_file), \
                 patch.object(paper_grid_xrp, "BASE_DIR", tmpdir):
                paper_grid_xrp.save_state(test_state)
                self.assertTrue(os.path.exists(test_state_file))
                with open(test_state_file, "r") as f:
                    loaded = json.load(f)
                self.assertEqual(loaded["test_key"], "test_val")
                self.assertIn("last_update", loaded)

    def test_gommapa_stage3_execution_exact_cash(self):
        """
        Verify Gommapa Stage 3 (250,000 KRW) executes when available cash is exactly 250,000 KRW.
        In the old code, requiring an extra fee buffer (250,125 KRW) caused Stage 3 to be rejected.
        With fee_inclusive=True, Stage 3 preflight passes cleanly.
        """
        initial_krw = 1_000_000.0
        # 50 grids consumed: 50 * 10,000 = 500,000 KRW
        krw_after_grids = initial_krw - 50 * 10_000.0
        self.assertEqual(krw_after_grids, 500_000.0)

        # Stage 1: 100,000 KRW
        ok1, amt1, err1 = preflight_check_buy("KRW-XRP", 100_000.0, available_krw=krw_after_grids, fee_inclusive=True)
        self.assertTrue(ok1, f"Stage 1 failed: {err1}")
        krw_after_stage1 = krw_after_grids - amt1
        self.assertEqual(krw_after_stage1, 400_000.0)

        # Stage 2: 150,000 KRW
        ok2, amt2, err2 = preflight_check_buy("KRW-XRP", 150_000.0, available_krw=krw_after_stage1, fee_inclusive=True)
        self.assertTrue(ok2, f"Stage 2 failed: {err2}")
        krw_after_stage2 = krw_after_stage1 - amt2
        self.assertEqual(krw_after_stage2, 250_000.0)

        # Stage 3: 250,000 KRW with exact remaining balance 250,000 KRW
        ok3_buggy, _, err3_buggy = preflight_check_buy("KRW-XRP", 250_000.0, available_krw=krw_after_stage2, fee_inclusive=False)
        self.assertFalse(ok3_buggy)
        self.assertIn("예수금 부족", err3_buggy)

        ok3, amt3, err3 = preflight_check_buy("KRW-XRP", 250_000.0, available_krw=krw_after_stage2, fee_inclusive=True)
        self.assertTrue(ok3, f"Stage 3 failed unexpectedly: {err3}")
        self.assertEqual(amt3, 250_000.0)
        krw_after_stage3 = krw_after_stage2 - amt3
        self.assertEqual(krw_after_stage3, 0.0)

    def test_engine_tick_isolated_realized_pnl_invariance(self):
        """
        P2-3: End-to-end 1-tick engine execution test via process_paper_tick.
        Verifies that actual cash change strictly equals net realized PnL across buy and TP cycles.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            test_db = os.path.join(tmpdir, "test_paper.db")
            test_state_file = os.path.join(tmpdir, "test_paper.json")
            with patch.multiple(paper_grid_xrp, STATE_FILE=test_state_file, DB_FILE=test_db), \
                 patch.object(paper_grid_xrp, "send_telegram_alert", return_value=None):
                paper_grid_xrp.init_db()

                state = {
                    "market": "KRW-XRP",
                    "strategy": "GRID_SCALPER",
                    "mode": "GRID_SCALPER",
                    "initial_capital": 1_000_000.0,
                    "available_krw": 1_000_000.0,
                    "total_equity": 1_000_000.0,
                    "peak_equity": 1_000_000.0,
                    "max_drawdown_pct": 0.0,
                    "realized_pnl": 0.0,
                    "win_count": 0,
                    "total_trades": 0,
                    "base_price": 0.0,
                    "gommapa_base_p": 0.0,
                    "active_grids": [],
                    "gommapa_tranches": []
                }
                paper_grid_xrp.save_state(state)

                # Tick 1: Initial entry at 2,000 KRW
                state, events1 = paper_grid_xrp.process_paper_tick(state, curr_p=2000.0)
                paper_grid_xrp.save_state(state)
                paper_grid_xrp.commit_tick_events(events1, state=state)
                self.assertEqual(state["available_krw"], 990_000.0)
                self.assertEqual(len(state["active_grids"]), 1)
                grid1 = state["active_grids"][0]
                tp_target = grid1["tp_p"]

                cash_before_tp = state["available_krw"]
                pnl_before_tp = state["realized_pnl"]

                # Tick 2: Price hits TP target (+1.2%)
                # Upon TP, Grid 1 is sold, realizing profit ~107.86 KRW.
                # In the same tick, since active_grids becomes empty, Step 2 starts the next cycle
                # by buying a fresh Level 1 grid (10,000 KRW).
                # Therefore, available cash increases by exactly the net realized PnL!
                state, events2 = paper_grid_xrp.process_paper_tick(state, curr_p=tp_target)
                paper_grid_xrp.save_state(state)
                paper_grid_xrp.commit_tick_events(events2, state=state)

                cash_after_tp = state["available_krw"]
                pnl_after_tp = state["realized_pnl"]

                cash_delta = cash_after_tp - cash_before_tp
                pnl_gain = pnl_after_tp - pnl_before_tp

                # Strict invariance test: cash delta must match realized_pnl exactly (no 5 KRW fee leak)
                self.assertAlmostEqual(cash_delta, pnl_gain, places=4)
                self.assertGreater(pnl_gain, 100.0)  # ~107.86 KRW net profit
                self.assertEqual(state["win_count"], 1)
                # Fresh Level 1 grid of next cycle
                self.assertEqual(len(state["active_grids"]), 1)
                self.assertEqual(state["active_grids"][0]["lvl"], 1)

    def test_execution_realism_ladder_and_gap_throttling(self):
        """
        P2: Unified Execution Model & Gommapa Reserve Preservation Verification:
        1. When price drops past multiple grid levels:
           - All reached levels fill sequentially without artificial 3-per-tick truncation (unified with backtest).
           - Buy price is bounded by actual observed market price (never buys higher than curr_p * (1 + slippage)).
        2. When price reaches Level 50:
           - All 50 levels are filled (500,000 KRW deployed to grid).
           - Available cash is EXACTLY 500,000 KRW for the Gommapa 3-stage emergency reserve.
           - Gommapa base price correctly anchors to Level 50, not an intermediate unbought level.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            test_db = os.path.join(tmpdir, "test_paper.db")
            test_state_file = os.path.join(tmpdir, "test_paper.json")
            with patch.multiple(paper_grid_xrp, STATE_FILE=test_state_file, DB_FILE=test_db), \
                 patch.object(paper_grid_xrp, "send_telegram_alert", return_value=None):
                paper_grid_xrp.init_db()

                base_price = 2000.0
                state = {
                    "market": "KRW-XRP",
                    "strategy": "GRID_SCALPER",
                    "mode": "GRID_SCALPER",
                    "initial_capital": 1_000_000.0,
                    "available_krw": 990_000.0,
                    "total_equity": 1_000_000.0,
                    "peak_equity": 1_000_000.0,
                    "max_drawdown_pct": 0.0,
                    "realized_pnl": 0.0,
                    "win_count": 0,
                    "total_trades": 1,
                    "base_price": base_price,
                    "gommapa_base_p": 0.0,
                    "active_grids": [{
                        "lvl": 1,
                        "buy_p": base_price * (1.0 + paper_grid_xrp.SLIPPAGE),
                        "vol": 4.95,
                        "cost": 10000.0,
                        "tp_p": base_price * 1.012
                    }],
                    "gommapa_tranches": []
                }
                paper_grid_xrp.save_state(state)

                # 1. Price drops from 2,000 KRW to 1,900 KRW (crosses 10 grid levels: L2 ~ L11)
                plunge_price = 1900.0
                state, events = paper_grid_xrp.process_paper_tick(state, curr_p=plunge_price)

                # All 10 crossed levels fill: L1 + 10 = 11 levels
                self.assertEqual(len(state["active_grids"]), 11)

                # Execution price must NOT be higher than observed market price (1,900 * 1.0002)
                max_allowed_buy_p = plunge_price * (1.0 + paper_grid_xrp.SLIPPAGE) + 0.01
                for g in state["active_grids"][1:]:
                    self.assertLessEqual(g["buy_p"], max_allowed_buy_p)

                # 2. Price plunges to Level 50 target (1,510 KRW):
                lvl50_p = base_price * (1.0 - paper_grid_xrp.STEP_PCT * 49)
                with patch.multiple(paper_grid_xrp, USE_GOMMAPA=True, MAX_LEVELS=50, GRID_BUDGET=500_000.0):
                    state, events50 = paper_grid_xrp.process_paper_tick(state, curr_p=lvl50_p)

                # All 50 levels must be filled!
                self.assertEqual(len(state["active_grids"]), 50)
                # Available cash must be EXACTLY 500,000 KRW (reserving the 500,000 KRW for Gommapa!)
                self.assertAlmostEqual(state["available_krw"], 500_000.0, places=2)

                # Mode must transition to GOMMAPA_ESCAPE
                self.assertEqual(state["mode"], "GOMMAPA_ESCAPE")
                # gommapa_base_p must anchor to Level 50's buy price (~1,510 KRW)
                self.assertLessEqual(state["gommapa_base_p"], lvl50_p * 1.01)

                with patch.multiple(paper_grid_xrp, USE_GOMMAPA=True, MAX_LEVELS=50):
                    paper_grid_xrp.validate_state_invariants(state)

    def test_tick_atomicity_side_effects_suppressed_on_save_failure(self):
        """
        P1: Tick Atomicity Verification:
        If saving state fails (IOError, disk full, etc.), DB trade logs and Telegram alerts
        must NOT be emitted. Upon successful save, events are committed without duplication.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            test_db = os.path.join(tmpdir, "test_paper.db")
            test_state_file = os.path.join(tmpdir, "atomic_tick_state.json")
            with patch.object(paper_grid_xrp, "DB_FILE", test_db), \
                 patch.object(paper_grid_xrp, "STATE_FILE", test_state_file), \
                 patch.object(paper_grid_xrp, "BASE_DIR", tmpdir):
                paper_grid_xrp.init_db()

                initial_state = {
                    "market": "KRW-XRP",
                    "strategy": "GRID_SCALPER",
                    "mode": "GRID_SCALPER",
                    "initial_capital": 1_000_000.0,
                    "available_krw": 1_000_000.0,
                    "total_equity": 1_000_000.0,
                    "peak_equity": 1_000_000.0,
                    "max_drawdown_pct": 0.0,
                    "realized_pnl": 0.0,
                    "win_count": 0,
                    "total_trades": 0,
                    "base_price": 0.0,
                    "gommapa_base_p": 0.0,
                    "active_grids": [],
                    "gommapa_tranches": []
                }
                paper_grid_xrp.save_state(initial_state)

                # Process 1 tick that would trigger Level 1 Buy
                new_state, events = paper_grid_xrp.process_paper_tick(initial_state, curr_p=2000.0)
                self.assertGreater(len(events), 0)

                # Scenario A: save_state fails!
                with patch("tempfile.mkstemp", side_effect=IOError("Simulated disk full")):
                    with self.assertRaises(IOError):
                        paper_grid_xrp.save_state(new_state)
                    # Because save failed, commit_tick_events is NOT called!

                # Verify DB has 0 rows recorded!
                conn = sqlite3.connect(test_db)
                c = conn.cursor()
                c.execute("SELECT count(*) FROM paper_trades")
                count = c.fetchone()[0]
                conn.close()
                self.assertEqual(count, 0, "DB recorded a trade even though save_state failed!")

                # Scenario B: save_state succeeds -> now commit_tick_events is called!
                with patch.object(paper_grid_xrp, "send_telegram_alert") as mock_tg:
                    paper_grid_xrp.save_state(new_state)
                    paper_grid_xrp.commit_tick_events(events)
                    self.assertTrue(mock_tg.called)

                # Verify DB now has exactly 1 row (no duplicates!)
                conn = sqlite3.connect(test_db)
                c = conn.cursor()
                c.execute("SELECT count(*) FROM paper_trades")
                count = c.fetchone()[0]
                conn.close()
                self.assertEqual(count, 1)

    def test_cross_field_contradictions_halt(self):
        """
        P1: Cross-Field Contradictions Verification:
        1. GRID_SCALPER mode with gommapa_tranches or gommapa_base_p > 0 -> Halt!
        2. GOMMAPA_ESCAPE mode with gommapa_base_p <= 0 or base_price <= 0 -> Halt!
        3. GOMMAPA_ESCAPE mode with 0 positions -> Halt!
        4. Active grids present but base_price <= 0 -> Halt!
        5. Capital conservation law violation -> Halt!
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            test_state_file = os.path.join(tmpdir, "contradiction_state.json")
            halt_flag = os.path.join(tmpdir, ".paper_grid_KRW_XRP.corrupted_halt")

            def assert_halts(s_dict, expected_reason):
                if os.path.exists(halt_flag):
                    os.unlink(halt_flag)
                with open(test_state_file, "w") as f:
                    json.dump(s_dict, f)
                with patch.object(paper_grid_xrp, "STATE_FILE", test_state_file), \
                     patch.object(paper_grid_xrp, "BASE_DIR", tmpdir):
                    with self.assertRaises(RuntimeError) as ctx:
                        paper_grid_xrp.load_state()
                    self.assertIn(expected_reason, str(ctx.exception))
                    self.assertTrue(os.path.exists(halt_flag))

            valid_base = {
                "initial_capital": 1_000_000.0,
                "available_krw": 990_000.0,
                "realized_pnl": 0.0,
                "mode": "GRID_SCALPER",
                "base_price": 2000.0,
                "gommapa_base_p": 0.0,
                "active_grids": [{"lvl": 1, "buy_p": 2000.0, "vol": 4.95, "cost": 10000.0, "tp_p": 2024.0}],
                "gommapa_tranches": []
            }

            # 1. GRID_SCALPER with gommapa_tranches remaining
            c1 = dict(valid_base)
            c1["gommapa_tranches"] = [{"stage": 1, "buy_p": 1800.0, "vol": 50.0, "cost": 100_000.0}]
            c1["available_krw"] = 890_000.0
            assert_halts(c1, "상태 모순 감지: GRID_SCALPER 모드이나 곰마파 트랜치가")

            # 2. GRID_SCALPER with gommapa_base_p > 0
            c2 = dict(valid_base)
            c2["gommapa_base_p"] = 1500.0
            assert_halts(c2, "상태 모순 감지: GRID_SCALPER 모드이나 gommapa_base_p")

            # 3. GOMMAPA_ESCAPE with gommapa_base_p <= 0
            c3 = dict(valid_base)
            c3["mode"] = "GOMMAPA_ESCAPE"
            c3["gommapa_base_p"] = 0.0
            assert_halts(c3, "상태 모순 감지: GOMMAPA_ESCAPE 모드이나 gommapa_base_p")

            # 4. Active grids present but base_price <= 0
            c4 = dict(valid_base)
            c4["base_price"] = 0.0
            assert_halts(c4, "상태 모순 감지: 보유 그리드가")

            # 5. GOMMAPA_ESCAPE with 0 positions
            c5 = dict(valid_base)
            c5["mode"] = "GOMMAPA_ESCAPE"
            c5["gommapa_base_p"] = 1800.0
            c5["active_grids"] = []
            c5["available_krw"] = 1_000_000.0
            assert_halts(c5, "상태 모순 감지: GOMMAPA_ESCAPE 모드이나 보유 중인 그리드 및 곰마파 포지션이")

            # 6. Capital conservation law violation: +50,000 KRW phantom money
            c6 = dict(valid_base)
            c6["available_krw"] = 1_040_000.0  # 1,040,000 + 10,000 = 1,050,000 != 1,000,000
            assert_halts(c6, "자본 보존 법칙 위배")

    def test_paper_process_lock_exclusivity(self):
        """Verify process lock prevents double execution and releases cleanly."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_lock_file = os.path.join(tmpdir, "test_paper.lock")
            with patch.object(paper_grid_xrp, "LOCK_FILE", test_lock_file):
                # First acquire should succeed
                self.assertTrue(paper_grid_xrp.acquire_paper_lock())

                # Second acquire within the same or another context must fail
                try:
                    import fcntl
                    with open(test_lock_file, "w") as f2:
                        with self.assertRaises((IOError, OSError)):
                            fcntl.flock(f2, fcntl.LOCK_EX | fcntl.LOCK_NB)
                finally:
                    paper_grid_xrp.release_paper_lock()

                # After release, acquire should succeed again
                self.assertTrue(paper_grid_xrp.acquire_paper_lock())
                paper_grid_xrp.release_paper_lock()

    def test_atomic_save_concurrent_writers(self):
        """Verify 50 concurrent saves using unique temp files do not crash with FileNotFoundError."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_state_file = os.path.join(tmpdir, "concurrent_state.json")
            errors = []

            with patch.object(paper_grid_xrp, "STATE_FILE", test_state_file), \
                 patch.object(paper_grid_xrp, "BASE_DIR", tmpdir):
                def worker(idx):
                    try:
                        state = {
                            "initial_capital": 1_000_000.0,
                            "available_krw": 1_000_000.0,
                            "realized_pnl": 0.0,
                            "mode": "GRID_SCALPER",
                            "base_price": 0.0,
                            "gommapa_base_p": 0.0,
                            "active_grids": [],
                            "gommapa_tranches": [],
                            "writer_id": idx,
                            "timestamp": idx * 10
                        }
                        paper_grid_xrp.save_state(state)
                    except Exception as e:
                        errors.append(e)

                with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
                    futures = [executor.submit(worker, i) for i in range(50)]
                    concurrent.futures.wait(futures)

            self.assertEqual(len(errors), 0, f"Concurrent saves failed: {errors}")
            self.assertTrue(os.path.exists(test_state_file))
            with open(test_state_file, "r") as f:
                loaded = json.load(f)
            self.assertIn("writer_id", loaded)
            self.assertIn("last_update", loaded)

    def test_paper_idle_ticks_memory_outbox_ack(self):
        """커밋 완료 후 메모리 및 디스크 outbox가 모두 비워져 다음 틱에서 중복 알림이 발생하지 않아야 합니다."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_state_file = os.path.join(tmpdir, "paper.json")
            test_db = os.path.join(tmpdir, "paper.db")

            with patch.multiple(paper_grid_xrp, STATE_FILE=test_state_file, DB_FILE=test_db), \
                 patch.object(paper_grid_xrp, "send_telegram_alert") as mock_tg:
                paper_grid_xrp.init_db()
                state = paper_grid_xrp.load_state()

                for i in range(3):
                    state, events = paper_grid_xrp.process_paper_tick(state, 2000.0, now_str=f"2026-10-05 12:00:{i:02d}")
                    paper_grid_xrp.save_state(state)
                    paper_grid_xrp.commit_tick_events(events)

                with sqlite3.connect(test_db) as conn:
                    rows = conn.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0]

                self.assertEqual(rows, 1)
                self.assertEqual(mock_tg.call_count, 1)
                self.assertEqual(len(state.get("pending_events", [])), 0)
                self.assertEqual(len(paper_grid_xrp.load_state().get("pending_events", [])), 0)

    def test_squad_basket_exit_and_capital_conservation(self):
        """
        Verify 10-step Squad Basket exit:
        When multiple tranches in a squad exist, if price hits squad VWAP + 1.2%,
        the entire squad exits together even if Level 1 has not hit its individual TP.
        Also verifies capital conservation invariant holds strictly.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            test_db = os.path.join(tmpdir, "test_paper_squad.db")
            test_state_file = os.path.join(tmpdir, "test_paper_squad.json")

            with patch.multiple(paper_grid_xrp, STATE_FILE=test_state_file, DB_FILE=test_db), \
                 patch.object(paper_grid_xrp, "send_telegram_alert") as mock_tg:
                paper_grid_xrp.init_db()

                # Given 3 active grids in Squad 1 (Levels 1, 2, 3)
                # Level 1: bought at 2000, individual TP = 2024
                # Level 2: bought at 1990, individual TP = 2013.88
                # Level 3: bought at 1980, individual TP = 2003.76
                g1 = {"lvl": 1, "squad": 1, "buy_p": 2000.0, "vol": 4.9975, "cost": 10000.0, "tp_p": 2024.0}
                g2 = {"lvl": 2, "squad": 1, "buy_p": 1990.0, "vol": 5.0226, "cost": 10000.0, "tp_p": 2013.88}
                g3 = {"lvl": 3, "squad": 1, "buy_p": 1980.0, "vol": 5.0480, "cost": 10000.0, "tp_p": 2003.76}

                initial_cap = 1_000_000.0
                state = {
                    "market": "KRW-XRP",
                    "strategy": "GRID_SCALPER",
                    "mode": "GRID_SCALPER",
                    "initial_capital": initial_cap,
                    "available_krw": 970_000.0,
                    "realized_pnl": 0.0,
                    "win_count": 0,
                    "total_trades": 3,
                    "base_price": 2000.0,
                    "gommapa_base_p": 0.0,
                    "active_grids": [g1, g2, g3],
                    "gommapa_tranches": []
                }
                paper_grid_xrp.save_state(state)

                # Total cost = 30,000, total vol = 15.0681
                # Squad VWAP = 30000 / 15.0681 = 1990.96 KRW
                # Squad target (+1.2%) = 1990.96 * 1.012 = 2014.85 KRW
                # When current price is 2016 KRW:
                # - 2016 >= 2014.85 (Squad target hit!)
                # - Note that Level 1 individual TP is 2024 KRW (not hit individually!)
                new_state, events = paper_grid_xrp.process_paper_tick(state, curr_p=2016.0)
                paper_grid_xrp.save_state(new_state)
                paper_grid_xrp.commit_tick_events(events, state=new_state)

                # All 3 tranches in Squad 1 must have exited!
                # And since active_grids became empty, a fresh Level 1 grid was opened at 2016 KRW
                squad_exits = [e for e in events if e.get("type") == "trade" and "SQUAD_1_TP" in e["payload"]["action"]]
                self.assertEqual(len(squad_exits), 3)

                # Verify capital conservation invariant holds
                paper_grid_xrp.validate_state_invariants(new_state)
                self.assertGreater(new_state["realized_pnl"], 0.0)

    def test_5step_squad_partition_and_isolated_exit(self):
        """Verify 5-step squad grouping: Squad 2 (L6, L7) exits independently while Squad 1 (L1~5) stays held."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_db = os.path.join(tmpdir, "test_paper_squad5.db")
            test_state_file = os.path.join(tmpdir, "test_paper_squad5.json")

            with patch.multiple(paper_grid_xrp, STATE_FILE=test_state_file, DB_FILE=test_db), \
                 patch.object(paper_grid_xrp, "send_telegram_alert") as mock_tg:
                paper_grid_xrp.init_db()

                # Squad 1: L1~5 (at ~2000 KRW, target ~2024 KRW)
                grids = []
                for lvl in range(1, 6):
                    buy_p = 2000.0 - (lvl - 1) * 10.0
                    vol = round(10000.0 / buy_p, 4)
                    grids.append({"lvl": lvl, "squad": 1, "buy_p": buy_p, "vol": vol, "cost": 10000.0, "tp_p": buy_p * 1.012})

                # Squad 2: L6, L7 (at 1950, 1940 KRW)
                # Squad 2 VWAP = 20000 / (10000/1950 + 10000/1940) = 1944.98 KRW
                # Squad 2 Target (+1.2%) = 1944.98 * 1.012 = 1968.32 KRW
                grids.append({"lvl": 6, "squad": 2, "buy_p": 1950.0, "vol": 5.1282, "cost": 10000.0, "tp_p": 1950.0 * 1.012})
                grids.append({"lvl": 7, "squad": 2, "buy_p": 1940.0, "vol": 5.1546, "cost": 10000.0, "tp_p": 1940.0 * 1.012})

                initial_cap = 1_000_000.0
                state = {
                    "market": "KRW-XRP",
                    "strategy": "GRID_SCALPER",
                    "mode": "GRID_SCALPER",
                    "initial_capital": initial_cap,
                    "available_krw": initial_cap - 70000.0,
                    "realized_pnl": 0.0,
                    "win_count": 0,
                    "total_trades": 7,
                    "base_price": 2000.0,
                    "gommapa_base_p": 0.0,
                    "active_grids": grids,
                    "gommapa_tranches": []
                }
                paper_grid_xrp.save_state(state)

                # Current price = 1970 KRW:
                # - Reaches Squad 2 target (1968.32)
                # - Below Squad 1 target (> 2000 KRW)
                # - Below L6 individual TP (1973.4 KRW)
                new_state, events = paper_grid_xrp.process_paper_tick(state, curr_p=1970.0)
                paper_grid_xrp.save_state(new_state)
                paper_grid_xrp.commit_tick_events(events, state=new_state)

                # Squad 2 must have exited!
                squad2_exits = [e for e in events if e.get("type") == "trade" and "SQUAD_2_TP" in e["payload"]["action"]]
                self.assertEqual(len(squad2_exits), 2)

                # Squad 1 (5 grids) must still be held!
                remaining_levels = {g["lvl"] for g in new_state["active_grids"]}
                self.assertEqual(remaining_levels, {1, 2, 3, 4, 5})
                for g in new_state["active_grids"]:
                    self.assertEqual(g["squad"], 1)

                paper_grid_xrp.validate_state_invariants(new_state)
                self.assertGreater(new_state["realized_pnl"], 0.0)

    def test_squad_basket_individual_loss_never_increments_win_count(self):
        """
        Verify P1 fix: When a 5-step squad basket exits at basket VWAP + 1.2%,
        upper tranches sold at a loss do NOT increment win_count.
        Only profitable tranches increment win_count.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            test_db = os.path.join(tmpdir, "test_paper_win_count.db")
            test_state_file = os.path.join(tmpdir, "test_paper_win_count.json")

            with patch.multiple(paper_grid_xrp, STATE_FILE=test_state_file, DB_FILE=test_db), \
                 patch.object(paper_grid_xrp, "send_telegram_alert"):
                paper_grid_xrp.init_db()

                # Squad 1 with wide spread: L1 at 2000, L2 at 1950, L3 at 1900, L4 at 1850, L5 at 1800
                grids = [
                    {"lvl": 1, "squad": 1, "buy_p": 2000.0, "vol": 5.0, "cost": 10000.0, "tp_p": 2024.0},
                    {"lvl": 2, "squad": 1, "buy_p": 1950.0, "vol": 5.1282, "cost": 10000.0, "tp_p": 1973.4},
                    {"lvl": 3, "squad": 1, "buy_p": 1900.0, "vol": 5.2631, "cost": 10000.0, "tp_p": 1922.8},
                    {"lvl": 4, "squad": 1, "buy_p": 1850.0, "vol": 5.4054, "cost": 10000.0, "tp_p": 1872.2},
                    {"lvl": 5, "squad": 1, "buy_p": 1800.0, "vol": 5.5555, "cost": 10000.0, "tp_p": 1821.6},
                ]
                # Squad VWAP = 50000 / 26.3522 = 1897.37 KRW
                # Target TP (+1.2%) = 1897.37 * 1.012 = 1920.14 KRW
                initial_cap = 1_000_000.0
                state = {
                    "market": "KRW-XRP",
                    "strategy": "GRID_SCALPER",
                    "mode": "GRID_SCALPER",
                    "initial_capital": initial_cap,
                    "available_krw": initial_cap - 50000.0,
                    "realized_pnl": 0.0,
                    "win_count": 0,
                    "total_trades": 5,
                    "base_price": 2000.0,
                    "gommapa_base_p": 0.0,
                    "active_grids": grids,
                    "gommapa_tranches": []
                }
                paper_grid_xrp.save_state(state)

                # At curr_p = 1925.0 KRW:
                # Basket TP triggers!
                # L1 (cost 10000, sold at ~1925) -> LOSS
                # L2 (cost 10000, sold at ~1925) -> LOSS
                # L3 (cost 10000, sold at ~1925) -> PROFIT
                # L4 (cost 10000, sold at ~1925) -> PROFIT
                # L5 (cost 10000, sold at ~1925) -> PROFIT
                new_state, events = paper_grid_xrp.process_paper_tick(state, curr_p=1925.0)
                paper_grid_xrp.save_state(new_state)
                paper_grid_xrp.commit_tick_events(events, state=new_state)

                # Exactly 5 trade exit events
                squad_exits = [e for e in events if e.get("type") == "trade" and "SQUAD_1_TP" in e["payload"]["action"]]
                self.assertEqual(len(squad_exits), 5)

                # Net overall PnL of basket is positive
                self.assertGreater(new_state["realized_pnl"], 0.0)

                # Win count must ONLY be 3 (for the 3 profitable levels), NOT 5!
                self.assertEqual(new_state["win_count"], 3)
                self.assertEqual(new_state["total_trades"], 10)  # 5 buys + 5 exits

    def test_gommapa_triggers_when_lvl50_filled_even_if_len_below_50(self):
        """
        Verify P0 fix: Gommapa mode triggers if Level 50 is filled or price drops
        below Level 50 target price, even if len(active_grids) < 50 because upper levels took profit.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            test_db = os.path.join(tmpdir, "test_paper_lvl50.db")
            test_state_file = os.path.join(tmpdir, "test_paper_lvl50.json")

            with patch.multiple(paper_grid_xrp, STATE_FILE=test_state_file, DB_FILE=test_db, USE_GOMMAPA=True, MAX_LEVELS=50), \
                 patch.object(paper_grid_xrp, "send_telegram_alert"):
                paper_grid_xrp.init_db()

                base_p = 2000.0
                step_pct = paper_grid_xrp.STEP_PCT
                lvl50_p = base_p * (1.0 - step_pct * 49)

                # Only 5 grids held (levels 46~50), len is 5 << 50
                grids = []
                for lvl in range(46, 51):
                    p = base_p * (1.0 - step_pct * (lvl - 1))
                    grids.append({
                        "lvl": lvl,
                        "squad": 10,
                        "buy_p": p,
                        "vol": round(10000.0 / p, 4),
                        "cost": 10000.0,
                        "tp_p": p * 1.012
                    })

                state = {
                    "market": "KRW-XRP",
                    "strategy": "GRID_SCALPER",
                    "mode": "GRID_SCALPER",
                    "initial_capital": 1_000_000.0,
                    "available_krw": 950_000.0,
                    "realized_pnl": 0.0,
                    "win_count": 45,
                    "total_trades": 90,
                    "base_price": base_p,
                    "gommapa_base_p": 0.0,
                    "active_grids": grids,
                    "gommapa_tranches": []
                }
                paper_grid_xrp.save_state(state)

                # Tick at lvl50_p:
                new_state, events = paper_grid_xrp.process_paper_tick(state, curr_p=lvl50_p)
                # Must transition to GOMMAPA_ESCAPE!
                self.assertEqual(new_state["mode"], "GOMMAPA_ESCAPE")
                self.assertGreater(new_state["gommapa_base_p"], 0.0)

    def test_commit_tick_events_preserves_memory_events_when_disk_save_fails(self):
        """
        Verify that commit_tick_events does not drop memory pending_events if disk save_state fails,
        and properly propagates the save failure exception.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            test_db = os.path.join(tmpdir, "test_paper_save_fail.db")
            test_state_file = os.path.join(tmpdir, "test_paper_save_fail.json")

            with patch.multiple(paper_grid_xrp, STATE_FILE=test_state_file, DB_FILE=test_db), \
                 patch.object(paper_grid_xrp, "send_telegram_alert"):
                paper_grid_xrp.init_db()

                state = {
                    "market": "KRW-XRP",
                    "strategy": "GRID_SCALPER",
                    "mode": "GRID_SCALPER",
                    "initial_capital": 1_000_000.0,
                    "available_krw": 1_000_000.0,
                    "realized_pnl": 0.0,
                    "win_count": 0,
                    "total_trades": 0,
                    "base_price": 0.0,
                    "gommapa_base_p": 0.0,
                    "active_grids": [],
                    "gommapa_tranches": []
                }
                paper_grid_xrp.save_state(state)

                new_state, events = paper_grid_xrp.process_paper_tick(state, curr_p=2000.0)
                paper_grid_xrp.save_state(new_state)

                # Simulate save_state failure during commit_tick_events
                def fail_save(*args, **kwargs):
                    raise IOError("Simulated disk error during outbox ack")

                with patch.object(paper_grid_xrp, "save_state", side_effect=fail_save):
                    with self.assertRaises(IOError):
                        paper_grid_xrp.commit_tick_events(events, state=new_state)

                # Memory pending_events MUST NOT be wiped! It must still contain the pending events
                self.assertGreater(len(new_state.get("pending_events", [])), 0)

    def test_gommapa_escape_exit_strictly_uses_preflight_clean_vol_without_forced_override(self):
        """
        Verify P0 fix: In Gommapa escape exit, preflight clean_vol is strictly adhered to,
        and never forcibly modified or enlarged.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            test_db = os.path.join(tmpdir, "test_paper_gom_clean.db")
            test_state_file = os.path.join(tmpdir, "test_paper_gom_clean.json")

            with patch.multiple(paper_grid_xrp, STATE_FILE=test_state_file, DB_FILE=test_db), \
                 patch.object(paper_grid_xrp, "send_telegram_alert"):
                paper_grid_xrp.init_db()

                # Setup state with 1 grid and 1 gommapa tranche
                # grid: 10000 KRW at 2000 KRW -> 4.9975 XRP
                # gommapa: 100000 KRW at 1800 KRW -> 55.52777777 XRP
                g = {"lvl": 50, "squad": 10, "buy_p": 2000.0, "vol": 4.9975, "cost": 10000.0, "tp_p": 2024.0}
                t = {"stage": 1, "buy_p": 1800.0, "vol": 55.52777777, "cost": 100000.0}

                initial_cap = 1_000_000.0
                state = {
                    "market": "KRW-XRP",
                    "strategy": "GRID_SCALPER",
                    "mode": "GOMMAPA_ESCAPE",
                    "initial_capital": initial_cap,
                    "available_krw": initial_cap - 110000.0,
                    "realized_pnl": 0.0,
                    "win_count": 0,
                    "total_trades": 2,
                    "base_price": 2000.0,
                    "gommapa_base_p": 1800.0,
                    "active_grids": [g],
                    "gommapa_tranches": [t]
                }
                paper_grid_xrp.save_state(state)

                # Total cost = 110,000 KRW
                # Total vol = 60.52527777 XRP
                # VWAP = 110000 / 60.52527777 = 1817.42 KRW
                # Target escape (+3.5%) = 1817.42 * 1.035 = 1881.03 KRW
                target_p = 1885.0
                new_state, events = paper_grid_xrp.process_paper_tick(state, curr_p=target_p)
                paper_grid_xrp.save_state(new_state)
                paper_grid_xrp.commit_tick_events(events, state=new_state)

                # Must have GOMMAPA_ESCAPE_EXIT event
                escape_events = [e for e in events if e.get("type") == "trade" and e["payload"]["action"] == "GOMMAPA_ESCAPE_EXIT"]
                self.assertEqual(len(escape_events), 1)

                clean_vol = escape_events[0]["payload"]["volume"]
                # Must strictly equal round(total_vols, 8)
                self.assertEqual(clean_vol, round(60.52527777, 8))

                # Capital conservation invariant holds
                paper_grid_xrp.validate_state_invariants(new_state)
                self.assertEqual(len(new_state["active_grids"]), 0)
                self.assertEqual(len(new_state["gommapa_tranches"]), 0)
                self.assertEqual(new_state["mode"], "GRID_SCALPER")

    def test_model_g60_pure_squad_specifications(self):
        """
        Verify Model G60:
        1. MAX_LEVELS = 60, USE_GOMMAPA = False, GRID_BUDGET = 600_000 KRW, SAFETY_CASH_BUFFER = 400_000 KRW.
        2. Plunge fills all 60 levels without transitioning to GOMMAPA_ESCAPE.
        3. Available cash strictly preserves the 400,000 KRW safe cash buffer.
        4. Prices below level 60 do not buy level 61 or exceed 60 levels.
        5. Rebound triggers Squad 12 (levels 56~60) basket TP.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            test_db = os.path.join(tmpdir, "test_g60.db")
            test_state_file = os.path.join(tmpdir, "test_g60.json")

            with patch.multiple(paper_grid_xrp, STATE_FILE=test_state_file, DB_FILE=test_db), \
                 patch.object(paper_grid_xrp, "send_telegram_alert"):
                paper_grid_xrp.init_db()

                # Verify default constants match Model G60
                self.assertEqual(paper_grid_xrp.MAX_LEVELS, 60)
                self.assertFalse(paper_grid_xrp.USE_GOMMAPA)
                self.assertEqual(paper_grid_xrp.GRID_BUDGET, 600_000.0)
                self.assertEqual(paper_grid_xrp.SAFETY_CASH_BUFFER, 400_000.0)

                base_p = 2000.0
                state = {
                    "market": "KRW-XRP",
                    "strategy": "GRID_SCALPER",
                    "mode": "GRID_SCALPER",
                    "created_at": "2026-10-14 12:00:00",
                    "initial_capital": 1_000_000.0,
                    "available_krw": 1_000_000.0,
                    "total_equity": 1_000_000.0,
                    "peak_equity": 1_000_000.0,
                    "max_drawdown_pct": 0.0,
                    "realized_pnl": 0.0,
                    "win_count": 0,
                    "total_trades": 0,
                    "base_price": base_p,
                    "active_grids": [],
                    "gommapa_tranches": [],
                    "gommapa_base_p": 0.0,
                    "last_update": "2026-10-14 12:00:00"
                }

                # 1. Level 1 buy at base_p
                state, events1 = paper_grid_xrp.process_paper_tick(state, curr_p=base_p)
                paper_grid_xrp.save_state(state)
                self.assertEqual(len(state["active_grids"]), 1)
                self.assertEqual(state["active_grids"][0]["squad"], 1)

                # 2. Plunge crossing all 60 levels (Level 60 is at base_p * (1 - 0.005 * 59) = 1,410 KRW)
                lvl60_p = base_p * (1.0 - 0.005 * 59)
                state, events60 = paper_grid_xrp.process_paper_tick(state, curr_p=lvl60_p)
                paper_grid_xrp.save_state(state)

                # Exactly 60 levels filled
                self.assertEqual(len(state["active_grids"]), 60)
                # Cash must be exactly 400,000 KRW (40% safe cash preserved)
                self.assertAlmostEqual(state["available_krw"], 400_000.0, places=2)
                # Mode MUST remain GRID_SCALPER (never GOMMAPA_ESCAPE)
                self.assertEqual(state["mode"], "GRID_SCALPER")
                self.assertEqual(len(state["gommapa_tranches"]), 0)

                # Verify Squad 12 assignment for levels 56~60
                sq12_grids = [g for g in state["active_grids"] if g["squad"] == 12]
                self.assertEqual(len(sq12_grids), 5)
                self.assertEqual([g["lvl"] for g in sq12_grids], [56, 57, 58, 59, 60])

                # 3. Further drop to 1,000 KRW (extreme crash): no more levels bought!
                state, events_crash = paper_grid_xrp.process_paper_tick(state, curr_p=1000.0)
                paper_grid_xrp.save_state(state)
                self.assertEqual(len(state["active_grids"]), 60)
                self.assertAlmostEqual(state["available_krw"], 400_000.0, places=2)
                self.assertEqual(state["mode"], "GRID_SCALPER")

                # Invariants must strictly pass
                paper_grid_xrp.validate_state_invariants(state)

                # 4. Rebound: price bounces above Squad 12 to exit (sq12_tp ~1427 KRW, rebound to 1460 KRW)
                sq12_cost = sum(g["cost"] for g in sq12_grids)
                sq12_vol = sum(g["vol"] for g in sq12_grids)
                sq12_vwap = sq12_cost / sq12_vol
                sq12_tp = sq12_vwap * 1.012

                rebound_p = max(sq12_tp + 20.0, 1460.0)
                state, events_exit = paper_grid_xrp.process_paper_tick(state, curr_p=rebound_p)
                paper_grid_xrp.save_state(state)
                paper_grid_xrp.commit_tick_events(events_exit, state=state)

                # Squad 12 must have been cleanly cleared (60 - 5 = 55 grids remaining)
                self.assertEqual(len(state["active_grids"]), 55)
                self.assertTrue(all(g["squad"] != 12 for g in state["active_grids"]))
                # Cash must increase by 50,000 KRW + profit
                self.assertGreater(state["available_krw"], 450_000.0)
                self.assertGreater(state["realized_pnl"], 0.0)
                paper_grid_xrp.validate_state_invariants(state)


if __name__ == "__main__":
    unittest.main()

