from __future__ import annotations

import copy
import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import main
import test_ag02_engine_transition_reconcile as ag02
from test_p1_squad_exit import EndTick, SquadFakeExchange


class TestEngineMarkerInitialization(unittest.TestCase):
    def test_missing_marker_save_failure_blocks_all_orders(self) -> None:
        for adaptive in (False, True):
            for empty in (False, True):
                with self.subTest(adaptive=adaptive, empty=empty):
                    fixture = ag02.TestAG02EngineTransitionReconcile()
                    fixture.setUp()
                    try:
                        state = fixture._make_state()
                        state.pop("strategy_engine")
                        if empty:
                            state.update(bot_quantity=0.0, bot_avg_price=0.0,
                                         tranches=[], initial_entry_done=False)
                        exchange = SquadFakeExchange(
                            main.PROTECTED_BALANCES["KRW-ETH"] + state["bot_quantity"],
                            3_700_000.0,
                        )
                        with patch.multiple(
                            main,
                            acquire_market_process_lock=lambda market: True,
                            init_db=lambda: None,
                            get_upbit_client=lambda: exchange,
                            load_strategy_state=lambda market: state,
                            save_strategy_state=lambda market, current: False,
                            load_trailing_exit_recovery=lambda market: None,
                            check_krw_balance_alert=lambda *args, **kwargs: 1_000_000,
                            is_adaptive_grid_enabled=lambda market: adaptive,
                            safe_get_current_price=lambda market: 3_700_000.0,
                            get_hybrid_regime_and_signals=lambda market: {
                                "is_bull": False, "signal": "HOLD", "ma5": 3_700_000,
                            },
                        ), patch.object(main.time, "sleep", side_effect=EndTick()), \
                                redirect_stdout(io.StringIO()):
                            with self.assertRaises(EndTick):
                                main.run_trading_strategy("KRW-ETH")
                        self.assertTrue(state["trading_halted"])
                        self.assertIsNone(state.get("strategy_engine"))
                        self.assertEqual([], exchange.limit_sells)
                        self.assertEqual([], exchange.limit_buys)
                        self.assertEqual([], exchange.market_buys)
                        self.assertEqual([], exchange.market_sells)
                    finally:
                        fixture.doCleanups()

    def test_unmarked_legacy_order_is_settled_before_adaptive_start(self) -> None:
        fixture = ag02.TestAG02EngineTransitionReconcile()
        fixture.setUp()
        try:
            state = fixture._make_state()
            state.pop("strategy_engine")
            state["bear_limit_buys"]["legacy-fill"] = {
                "state": "wait", "units": 1, "squad_id": 1, "squad_stage": 2,
                "executed_volume": 0.0, "executed_funds": 0.0,
            }
            exchange = SquadFakeExchange(main.PROTECTED_BALANCES["KRW-ETH"] + 0.15,
                                         3_700_000.0)
            exchange.orders["legacy-fill"] = {
                "uuid": "legacy-fill", "side": "bid", "state": "done",
                "executed_volume": "0.05",
                "trades": [{"funds": "185000", "volume": "0.05", "price": "3700000"}],
            }
            with patch.multiple(
                main,
                acquire_market_process_lock=lambda market: True,
                init_db=lambda: None,
                get_upbit_client=lambda: exchange,
                load_strategy_state=lambda market: state,
                load_trailing_exit_recovery=lambda market: None,
                check_krw_balance_alert=lambda *args, **kwargs: 1_000_000,
                is_adaptive_grid_enabled=lambda market: True,
                safe_get_current_price=lambda market: (_ for _ in ()).throw(EndTick()),
            ), patch.object(main.time, "sleep", side_effect=EndTick()), \
                    redirect_stdout(io.StringIO()):
                with self.assertRaises(EndTick):
                    main.run_trading_strategy("KRW-ETH")
            self.assertAlmostEqual(0.15, state["bot_quantity"])
            self.assertEqual("done", state["bear_limit_buys"]["legacy-fill"]["state"])
            self.assertEqual("ADAPTIVE_GRID_C_E", state["strategy_engine"])
            self.assertEqual([], exchange.limit_buys)
            self.assertEqual([], exchange.limit_sells)
        finally:
            fixture.doCleanups()

    def test_missing_marker_is_durable_before_trading(self) -> None:
        for adaptive in (False, True):
            for empty in (False, True):
                with self.subTest(adaptive=adaptive, empty=empty):
                    fixture = ag02.TestAG02EngineTransitionReconcile()
                    fixture.setUp()
                    try:
                        state = fixture._make_state()
                        state.pop("strategy_engine")
                        if empty:
                            state.update(bot_quantity=0.0, bot_avg_price=0.0,
                                         tranches=[], initial_entry_done=False)
                        exchange = SquadFakeExchange(
                            main.PROTECTED_BALANCES["KRW-ETH"] + state["bot_quantity"],
                            3_700_000.0,
                        )
                        snapshots = []
                        state_path = Path(fixture.temp_dir) / "marker.json"

                        def persist(market: str, current) -> bool:
                            snapshots.append(copy.deepcopy(current))
                            state_path.write_text(json.dumps(current), encoding="utf-8")
                            return True

                        def before_price(market: str) -> float:
                            raise EndTick()

                        with patch.multiple(
                            main,
                            acquire_market_process_lock=lambda market: True,
                            init_db=lambda: None,
                            get_upbit_client=lambda: exchange,
                            load_strategy_state=lambda market: state,
                            save_strategy_state=persist,
                            load_trailing_exit_recovery=lambda market: None,
                            check_krw_balance_alert=lambda *args, **kwargs: 1_000_000,
                            is_adaptive_grid_enabled=lambda market: adaptive,
                            safe_get_current_price=before_price,
                        ), patch.object(main.time, "sleep", side_effect=EndTick()), \
                                redirect_stdout(io.StringIO()):
                            with self.assertRaises(EndTick):
                                main.run_trading_strategy("KRW-ETH")
                        self.assertTrue(snapshots)
                        saved = json.loads(state_path.read_text(encoding="utf-8"))
                        self.assertEqual(
                            "ADAPTIVE_GRID_C_E" if adaptive else "LEGACY",
                            saved["strategy_engine"],
                        )
                        self.assertEqual([], exchange.limit_sells)
                        self.assertEqual([], exchange.limit_buys)
                        self.assertFalse(state.get("trading_halted", False))
                    finally:
                        fixture.doCleanups()
