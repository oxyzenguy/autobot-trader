"""Fake-fill reproduction of a swallowed atomic state-save failure."""

from __future__ import annotations

import io
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import main
from utils import db_logger
from test_order_safety_regressions import FakeExchange, StopAfterOneIteration


class TestStateSaveFailure(unittest.TestCase):
    def test_failed_exit_intent_persistence_prevents_sell(self):
        protected = main.PROTECTED_BALANCES["KRW-ETH"]
        original_state = {
            "market": "KRW-ETH",
            "current_regime": "BULL",
            "trailing_stop_active": True,
            "trend_peak_price": 120_000.0,
            "bot_quantity": 0.1,
            "bot_avg_price": 100_000.0,
            "tranches": [{"step": 1, "volume": 0.1, "buy_price": 100_000.0}],
        }
        exchange = FakeExchange(protected + 0.1, 115_000.0)
        loaded: list[dict] = []
        logged: list[dict] = []
        sleeps: list[float] = []
        output = io.StringIO()
        real_load = main.load_strategy_state

        def load_state(market: str):
            state = real_load(market)
            loaded.append(state)
            return state

        def stop_after_sell(seconds: float):
            sleeps.append(seconds)
            raise StopAfterOneIteration()

        with tempfile.TemporaryDirectory(prefix="autotrade-p1-state-") as directory:
            state_path = os.path.join(directory, "state.json")
            with open(state_path, "w", encoding="utf-8") as fixture:
                json.dump(original_state, fixture)

            with (
                patch.multiple(
                    main,
                    BASE_DIR=directory,
                    acquire_market_process_lock=lambda market: object(),
                    init_db=lambda: None,
                    get_upbit_client=lambda: exchange,
                    get_state_file_path=lambda market: state_path,
                    load_strategy_state=load_state,
                    check_krw_balance_alert=lambda *args, **kwargs: 100_000.0,
                    send_telegram_alert=lambda message: None,
                    safe_get_current_price=lambda market: 115_000.0,
                    get_hybrid_regime_and_signals=lambda market: {
                        "is_bull": True,
                        "signal": "HOLD",
                        "ma5": 115_000.0,
                    },
                    log_real_trade=lambda **trade: logged.append(trade),
                ),
                patch.dict(main._last_saved_state_strings, {}, clear=True),
                patch.object(main.os, "replace", side_effect=OSError("fake disk failure")) as replace,
                patch.object(main.time, "sleep", side_effect=stop_after_sell),
                redirect_stdout(output),
            ):
                with self.assertRaises(StopAfterOneIteration):
                    main.run_trading_strategy("KRW-ETH")
                persisted = real_load("KRW-ETH")

            self.assertEqual([], exchange.sells)
            self.assertEqual([], logged)
            self.assertEqual([10], sleeps)
            self.assertGreaterEqual(replace.call_count, 1)
            self.assertEqual(original_state["bot_quantity"], persisted["bot_quantity"])
            self.assertTrue(persisted["trailing_stop_active"])


class RecoveryExchange(FakeExchange):
    def get_order(self, identifier: str, state: str | None = None):
        order = super().get_order(identifier, state)
        if isinstance(order, dict):
            return {"uuid": identifier, **order}
        return order


class TestTrailingExitRecovery(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="autotrade-p1-recovery-")
        self.addCleanup(self.temp.cleanup)
        self.state_path = os.path.join(self.temp.name, "state.json")
        self.db_path = os.path.join(self.temp.name, "trades.db")
        self.exchange = RecoveryExchange(main.PROTECTED_BALANCES["KRW-ETH"] + 0.1, 115_000.0)
        with open(self.state_path, "w", encoding="utf-8") as handle:
            json.dump({
                "market": "KRW-ETH",
                "current_regime": "BULL",
                "trailing_stop_active": True,
                "trend_peak_price": 120_000.0,
                "bot_quantity": 0.1,
                "bot_avg_price": 100_000.0,
                "tranches": [{"step": 1, "volume": 0.1, "buy_price": 100_000.0}],
            }, handle)

    def run_ticks(self, count: int = 1, json_failure: bool = False, sqlite_failure: bool = False):
        actual_replace = os.replace
        sleeps = []

        def replace(source, destination):
            if json_failure and destination == self.state_path:
                raise OSError("fake final JSON failure")
            return actual_replace(source, destination)

        def tick_sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) >= count:
                raise StopAfterOneIteration()

        output = io.StringIO()
        with (
            patch.multiple(
                main,
                BASE_DIR=self.temp.name,
                acquire_market_process_lock=lambda market: object(),
                init_db=db_logger.init_db,
                get_upbit_client=lambda: self.exchange,
                get_state_file_path=lambda market: self.state_path,
                check_krw_balance_alert=lambda *args, **kwargs: 100_000.0,
                send_telegram_alert=lambda message: None,
                safe_get_current_price=lambda market: 115_000.0,
                get_hybrid_regime_and_signals=lambda market: {
                    "is_bull": True, "signal": "HOLD", "ma5": 115_000.0,
                },
            ),
            patch.object(db_logger, "DB_PATH", self.db_path),
            patch.object(main.os, "replace", side_effect=replace),
            patch.object(main.time, "sleep", side_effect=tick_sleep),
            patch.dict(main._last_saved_state_strings, {}, clear=True),
            patch("utils.sync_manager.trigger_snapshot_sync", lambda: None),
            redirect_stdout(output),
        ):
            if sqlite_failure:
                with patch.object(main, "log_real_trade", side_effect=sqlite3.OperationalError("fake SQLite failure")):
                    with self.assertRaises(StopAfterOneIteration):
                        main.run_trading_strategy("KRW-ETH")
            else:
                with self.assertRaises(StopAfterOneIteration):
                    main.run_trading_strategy("KRW-ETH")
        return output.getvalue()

    def recovery(self):
        with open(self.state_path + ".trailing-exit.json", encoding="utf-8") as handle:
            return json.load(handle)

    def state(self):
        with open(self.state_path, encoding="utf-8") as handle:
            return json.load(handle)

    def trade_uuids(self):
        with sqlite3.connect(self.db_path) as connection:
            return [row[0] for row in connection.execute(
                "SELECT order_uuid FROM trades WHERE action = 'TRAILING_STOP_EXIT'"
            )]

    def test_failed_final_json_keeps_recovery_and_blocks_same_process(self):
        self.run_ticks(count=2, json_failure=True)
        self.assertEqual([0.1], self.exchange.sells)
        self.assertEqual("fake-sell-1", self.recovery()["uuid"])
        self.assertEqual(["fake-sell-1"], self.trade_uuids())
        self.assertEqual(0.1, self.state()["bot_quantity"])

    def test_restart_with_new_excess_balance_never_resells_stale_position(self):
        self.run_ticks(json_failure=True)
        self.exchange.available_eth += 0.1
        self.run_ticks(json_failure=True)
        self.assertEqual([0.1], self.exchange.sells)
        self.assertEqual(["fake-sell-1"], self.trade_uuids())
        self.assertEqual(0.1, self.state()["bot_quantity"])
        self.assertEqual("fake-sell-1", self.recovery()["uuid"])

    def test_sqlite_failure_retains_recovery_and_never_reorders(self):
        self.run_ticks(count=2, sqlite_failure=True)
        self.assertEqual([0.1], self.exchange.sells)
        self.assertEqual([], self.trade_uuids())
        self.assertEqual(0.1, self.state()["bot_quantity"])
        self.assertEqual("fake-sell-1", self.recovery()["uuid"])
        self.run_ticks()
        self.assertEqual([0.1], self.exchange.sells)
        self.assertEqual(["fake-sell-1"], self.trade_uuids())
        self.assertEqual(0.0, self.state()["bot_quantity"])
        self.assertFalse(os.path.exists(self.state_path + ".trailing-exit.json"))

    def test_success_records_one_uuid_and_clears_recovery(self):
        self.run_ticks()
        self.assertEqual([0.1], self.exchange.sells)
        self.assertEqual(["fake-sell-1"], self.trade_uuids())
        self.assertEqual(0.0, self.state()["bot_quantity"])
        self.assertEqual(1, self.state()["completed_cycles"])
        self.assertFalse(self.state()["trailing_stop_active"])
        self.assertFalse(os.path.exists(self.state_path + ".trailing-exit.json"))

    def test_recovery_after_restart_reconciles_without_reselling(self):
        self.run_ticks(json_failure=True)
        self.exchange.available_eth += 0.1
        self.run_ticks()
        self.assertEqual([0.1], self.exchange.sells)
        self.assertEqual(["fake-sell-1"], self.trade_uuids())
        self.assertEqual(0.0, self.state()["bot_quantity"])
        self.assertFalse(os.path.exists(self.state_path + ".trailing-exit.json"))

    def test_corrupt_recovery_halts_all_automatic_orders(self):
        with open(self.state_path + ".trailing-exit.json", "w", encoding="utf-8") as handle:
            handle.write('{"kind":"TRAILING_STOP_EXIT","uuid":')
        output = self.run_ticks()
        self.assertEqual([], self.exchange.sells)
        self.assertIn("recovery 실패", output)
        self.assertTrue(os.path.exists(self.state_path + ".trailing-exit.json"))

    def test_nonfinite_recovery_quantity_halts_all_automatic_orders(self):
        with open(self.state_path + ".trailing-exit.json", "w", encoding="utf-8") as handle:
            json.dump({
                "kind": "TRAILING_STOP_EXIT", "market": "KRW-ETH",
                "uuid": "fake-sell-1", "volume": float("nan"),
                "avg_price": 100_000.0, "cycles": 0,
            }, handle)
        output = self.run_ticks()
        self.assertEqual([], self.exchange.sells)
        self.assertIn("recovery 실패", output)
        self.assertTrue(os.path.exists(self.state_path + ".trailing-exit.json"))

    def test_uuid_checkpoint_failure_remains_halted_after_restart(self):
        real_save = main.save_trailing_exit_recovery
        writes = []

        def fail_after_submission(market, recovery):
            writes.append(recovery["uuid"])
            if len(writes) == 2:
                raise OSError("fake UUID checkpoint failure")
            return real_save(market, recovery)

        with patch.object(main, "save_trailing_exit_recovery", side_effect=fail_after_submission):
            self.run_ticks()
        self.assertEqual([None, "fake-sell-1"], writes)
        self.assertEqual([0.1], self.exchange.sells)
        self.assertIsNone(self.recovery()["uuid"])
        self.run_ticks()
        self.assertEqual([0.1], self.exchange.sells)
        self.assertEqual([], self.trade_uuids())

    def test_mismatched_exchange_uuid_remains_halted_until_verified(self):
        actual_get_order = self.exchange.get_order

        def wrong_order(identifier, state=None):
            order = actual_get_order(identifier, state)
            if identifier == "fake-sell-1":
                return {**order, "uuid": "another-order"}
            return order

        with patch.object(self.exchange, "get_order", side_effect=wrong_order):
            self.run_ticks(count=2)
        self.assertEqual([0.1], self.exchange.sells)
        self.assertEqual([], self.trade_uuids())
        self.assertEqual("fake-sell-1", self.recovery()["uuid"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
