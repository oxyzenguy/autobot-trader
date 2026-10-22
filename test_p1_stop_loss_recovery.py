"""Fake-exchange restart regressions for the BULL stop-loss exit."""

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
from test_order_safety_regressions import StopAfterOneIteration
from test_p1_state_persistence import RecoveryExchange
from utils import db_logger


class TestStopLossRecovery(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="autotrade-stop-loss-recovery-")
        self.addCleanup(self.temp.cleanup)
        self.state_path = os.path.join(self.temp.name, "state.json")
        self.db_path = os.path.join(self.temp.name, "trades.db")
        self.exchange = RecoveryExchange(main.PROTECTED_BALANCES["KRW-ETH"] + 0.1, 89_000.0)
        with open(self.state_path, "w", encoding="utf-8") as handle:
            json.dump({
                "market": "KRW-ETH",
                "strategy_engine": "LEGACY",
                "current_regime": "BULL",
                "initial_entry_done": True,
                "bot_quantity": 0.1,
                "bot_avg_price": 100_000.0,
                "tranches": [{"step": 1, "volume": 0.1, "buy_price": 100_000.0}],
            }, handle)

    def run_ticks(self, *, count=1, json_failure=False, sqlite_failure=False, signal="HOLD"):
        original_replace = os.replace
        sleeps = 0

        def replace(source, destination):
            if json_failure and destination == self.state_path:
                raise OSError("fake final JSON failure")
            return original_replace(source, destination)

        def tick_sleep(seconds):
            nonlocal sleeps
            if seconds == main.ORDER_SETTLEMENT_DELAY_SEC:
                return
            sleeps += 1
            if sleeps >= count:
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
                safe_get_current_price=lambda market: self.exchange.price,
                get_hybrid_regime_and_signals=lambda market: {
                    "is_bull": True, "signal": signal, "ma5": self.exchange.price,
                },
                get_market_strategy=lambda market: "HYBRID",
                is_adaptive_grid_enabled=lambda market: False,
            ),
            patch.object(db_logger, "DB_PATH", self.db_path),
            patch.object(main.os, "replace", side_effect=replace),
            patch.object(main.time, "sleep", side_effect=tick_sleep),
            patch.dict(main._last_saved_state_strings, {}, clear=True),
            patch("utils.sync_manager.trigger_snapshot_sync", lambda: None),
            patch("requests.sessions.Session.request", side_effect=AssertionError("live HTTP forbidden")) as http,
            redirect_stdout(output),
        ):
            if sqlite_failure:
                with patch.object(main, "log_real_trade", side_effect=sqlite3.OperationalError("fake SQLite failure")):
                    with self.assertRaises(StopAfterOneIteration):
                        main.run_trading_strategy("KRW-ETH")
            else:
                with self.assertRaises(StopAfterOneIteration):
                    main.run_trading_strategy("KRW-ETH")
            http.assert_not_called()
        return output.getvalue()

    def state(self):
        with open(self.state_path, encoding="utf-8") as handle:
            return json.load(handle)

    def recovery(self):
        with open(self.state_path + ".trailing-exit.json", encoding="utf-8") as handle:
            return json.load(handle)

    def trade_uuids(self):
        with sqlite3.connect(self.db_path) as connection:
            return [row[0] for row in connection.execute(
                "SELECT order_uuid FROM trades WHERE action = 'BULL_STOP_LOSS_10PCT'"
            )]

    def test_intent_persistence_failure_prevents_sale(self):
        with patch.object(main, "save_trailing_exit_recovery", side_effect=OSError("fake intent failure")):
            self.run_ticks()
        self.assertEqual([], self.exchange.sells)
        self.assertEqual(0.1, self.state()["bot_quantity"])
        self.assertFalse(os.path.exists(self.state_path + ".trailing-exit.json"))

    def test_json_failure_restart_with_new_balance_does_not_resell(self):
        self.run_ticks(json_failure=True)
        self.assertEqual([0.1], self.exchange.sells)
        self.assertEqual(["fake-sell-1"], self.trade_uuids())
        self.assertEqual(0.1, self.state()["bot_quantity"])
        self.assertEqual("fake-sell-1", self.recovery()["uuid"])

        self.exchange.available_eth += 0.1
        self.run_ticks(json_failure=True)
        self.assertEqual([0.1], self.exchange.sells)
        self.assertEqual(["fake-sell-1"], self.trade_uuids())
        self.assertEqual(0.1, self.state()["bot_quantity"])
        self.assertEqual("fake-sell-1", self.recovery()["uuid"])

        self.run_ticks()
        self.assertEqual([0.1], self.exchange.sells)
        self.assertEqual(["fake-sell-1"], self.trade_uuids())
        self.assertEqual(0.0, self.state()["bot_quantity"])
        self.assertEqual([], self.state()["tranches"])
        self.assertFalse(os.path.exists(self.state_path + ".trailing-exit.json"))

    def test_sqlite_failure_keeps_recovery_without_second_sale(self):
        self.run_ticks(sqlite_failure=True)
        self.assertEqual([0.1], self.exchange.sells)
        self.assertEqual([], self.trade_uuids())
        self.assertEqual(0.1, self.state()["bot_quantity"])
        self.assertEqual("fake-sell-1", self.recovery()["uuid"])
        self.run_ticks()
        self.assertEqual([0.1], self.exchange.sells)
        self.assertEqual(["fake-sell-1"], self.trade_uuids())
        self.assertEqual(0.0, self.state()["bot_quantity"])
        self.assertFalse(os.path.exists(self.state_path + ".trailing-exit.json"))

    def test_normal_sale_is_recorded_once_and_preserves_protected_balance(self):
        self.run_ticks()
        self.assertEqual([0.1], self.exchange.sells)
        self.assertAlmostEqual(main.PROTECTED_BALANCES["KRW-ETH"], self.exchange.available_eth)
        self.assertEqual(["fake-sell-1"], self.trade_uuids())
        self.assertEqual(0.0, self.state()["bot_quantity"])
        self.assertEqual([], self.state()["tranches"])
        self.assertGreater(self.state()["stop_loss_cooldown_until"], 0)
        self.assertFalse(os.path.exists(self.state_path + ".trailing-exit.json"))
        self.run_ticks()
        self.assertEqual([0.1], self.exchange.sells)
        self.assertEqual(["fake-sell-1"], self.trade_uuids())

    def test_wait_partial_and_cancelled_partial_remain_unresolved(self):
        actual_get_order = self.exchange.get_order
        order_status = {"state": "wait", "volume": 0.04}

        def partial_sell(market, volume):
            self.exchange.sells.append(volume)
            self.exchange.available_eth -= order_status["volume"]
            return {"uuid": "fake-sell-1"}

        def uncertain_order(identifier, state=None):
            if identifier == "fake-sell-1":
                return {
                    "uuid": identifier, "state": order_status["state"],
                    "executed_volume": str(order_status["volume"]),
                    "trades": [{"funds": str(order_status["volume"] * self.exchange.price)}],
                }
            return actual_get_order(identifier, state)

        with (patch.object(self.exchange, "sell_market_order", side_effect=partial_sell),
              patch.object(self.exchange, "get_order", side_effect=uncertain_order)):
            for status in ("wait", "done", "cancel"):
                with self.subTest(status=status):
                    order_status["state"] = status
                    self.run_ticks()
                    self.assertEqual([0.1], self.exchange.sells)
                    self.assertAlmostEqual(
                        main.PROTECTED_BALANCES["KRW-ETH"] + 0.06, self.exchange.available_eth
                    )
                    self.assertEqual([], self.trade_uuids())
                    self.assertEqual(0.1, self.state()["bot_quantity"])
                    self.assertEqual("fake-sell-1", self.recovery()["uuid"])

    def test_unconfirmed_uuid_remains_closed_after_restart(self):
        real_save = main.save_trailing_exit_recovery

        def fail_uuid_checkpoint(market, recovery):
            if recovery["uuid"] is not None:
                raise OSError("fake UUID checkpoint failure")
            return real_save(market, recovery)

        with patch.object(main, "save_trailing_exit_recovery", side_effect=fail_uuid_checkpoint):
            self.run_ticks()
        self.assertEqual([0.1], self.exchange.sells)
        self.assertIsNone(self.recovery()["uuid"])
        with patch.object(self.exchange, "buy_market_order") as buy:
            self.run_ticks(signal="BUY")
            buy.assert_not_called()
        self.assertEqual([0.1], self.exchange.sells)
        self.assertEqual([], self.trade_uuids())

    def test_mismatched_exchange_uuid_remains_unresolved(self):
        self.run_ticks(sqlite_failure=True)
        actual_get_order = self.exchange.get_order

        def wrong_order(identifier, state=None):
            order = actual_get_order(identifier, state)
            if identifier == "fake-sell-1":
                return {**order, "uuid": "another-order"}
            return order

        with patch.object(self.exchange, "get_order", side_effect=wrong_order):
            self.run_ticks()
        self.assertEqual([0.1], self.exchange.sells)
        self.assertEqual([], self.trade_uuids())
        self.assertEqual(0.1, self.state()["bot_quantity"])
        self.assertEqual("fake-sell-1", self.recovery()["uuid"])

    def test_cancel_without_execution_remains_closed(self):
        actual_get_order = self.exchange.get_order

        def accepted_without_fill(market, volume):
            self.exchange.sells.append(volume)
            return {"uuid": "fake-sell-1"}

        def canceled_order(identifier, state=None):
            if identifier == "fake-sell-1":
                return {"uuid": identifier, "state": "cancel", "executed_volume": "0", "trades": []}
            return actual_get_order(identifier, state)

        with (patch.object(self.exchange, "sell_market_order", side_effect=accepted_without_fill),
              patch.object(self.exchange, "get_order", side_effect=canceled_order)):
            self.run_ticks()
        self.assertEqual([0.1], self.exchange.sells)
        self.assertEqual([], self.trade_uuids())
        self.assertEqual(0.1, self.state()["bot_quantity"])
        self.assertEqual("fake-sell-1", self.recovery()["uuid"])

    def test_uncertain_recovery_blocks_buy_and_sell_signals(self):
        recovery = {
            "kind": "STOP_LOSS_EXIT", "market": "KRW-ETH", "uuid": None,
            "volume": 0.1, "avg_price": 100_000.0, "cycles": 0,
            "cooldown_until": 1_800_000_000.0,
        }
        with patch.object(main, "get_state_file_path", return_value=self.state_path):
            main.save_trailing_exit_recovery("KRW-ETH", recovery)
        self.run_ticks(signal="HOLD")  # The original position would trigger a SELL.
        self.assertEqual([], self.exchange.sells)

        state = self.state()
        state["bot_quantity"] = 0.03
        state["tranches"][0]["volume"] = 0.03
        with open(self.state_path, "w", encoding="utf-8") as handle:
            json.dump(state, handle)
        self.exchange.available_eth = main.PROTECTED_BALANCES["KRW-ETH"] + 0.03
        self.exchange.price = 100_000.0
        with patch.object(self.exchange, "buy_market_order") as buy:
            self.run_ticks(signal="BUY")
            buy.assert_not_called()
        self.assertEqual([], self.exchange.sells)
        self.assertIsNone(self.recovery()["uuid"])
