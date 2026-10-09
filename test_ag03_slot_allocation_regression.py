"""AG-03 Regression and Unit Tests: Shared squad/stage allocator and replenishment anchoring.

Acceptance Criteria:
1. Shared squad/stage allocator used in both initial/re-registration and replenishment paths.
2. Deduplicate occupied identities for planning: partial fill of one UUID owns one slot and does not consume two cap slots.
3. 1-1-2-4 unit multiplier sequence maintained; next squad stage progression continues sequentially.
4. Replenishment prices anchor strictly below the lowest existing owned bid.
5. Reproduction 1: Stage 3 & 4 waiting, Stage 2 completes -> allocates Squad 2 Stage 1 (1 unit), price below existing bids.
6. Reproduction 2: Surviving slots (1, 4) and (2, 4) -> allocates (3, 1), (3, 2), (3, 3) with 1, 1, 2 units (no collision).
7. Several fills between ticks, sparse survivors, restart/engine switch, and SOL 16-stage cap preserved.
"""

from __future__ import annotations

import copy
import io
import os
import sqlite3
import sys
import tempfile
import time
import types
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import main
from strategy.adaptive_grid import (
    calculate_adaptive_buy_prices,
    DEFAULT_STEP_SCALE,
    DEFAULT_MIN_DROP_PCT,
    DEFAULT_MAX_DROP_PCT,
)
from strategy.matingale2x_logic import (
    calculate_new_buy_prices,
    get_existing_stages_from_state,
    get_next_squad_stages,
    adjust_price_to_tick,
    get_upbit_tick_size,
)
from utils import db_logger
from test_p1_squad_exit import EndTick, SquadFakeExchange


class TestAG03SlotAllocationRegression(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.temp_dir = tmp.name
        self.db_path = os.path.join(self.temp_dir, "test_ag03.db")

        p_db = patch.object(db_logger, "DB_PATH", self.db_path)
        p_db.start()
        self.addCleanup(p_db.stop)

        sync = types.SimpleNamespace(trigger_snapshot_sync=lambda: None)
        p_sync = patch.dict(sys.modules, {"utils.sync_manager": sync})
        p_sync.start()
        self.addCleanup(p_sync.stop)

        p_base = patch.object(main, "BASE_DIR", self.temp_dir)
        p_base.start()
        self.addCleanup(p_base.stop)

        p_state_path = patch.object(
            main, "get_state_file_path",
            lambda m: os.path.join(self.temp_dir, f"real_strategy_state_{m.replace('-', '_')}.json")
        )
        p_state_path.start()
        self.addCleanup(p_state_path.stop)

        p_save = patch.object(main, "save_strategy_state", lambda m, s: True)
        p_save.start()
        self.addCleanup(p_save.stop)

        p_alert = patch.object(main, "send_telegram_alert", lambda msg: None)
        p_alert.start()
        self.addCleanup(p_alert.stop)

        p_req = patch("requests.post", side_effect=AssertionError("live HTTP request forbidden in unit tests"))
        p_req.start()
        self.addCleanup(p_req.stop)

        db_logger.init_db()

        # Seed candle cache with valid confirmed 220-bar candle data so test fixtures reflect valid production input
        from strategy.hybrid_regime import _CANDLE_CACHE
        from test_ag04_main_loop_candle_safety import make_sample_candle_df
        _CANDLE_CACHE.clear()
        _CANDLE_CACHE["KRW-ETH"] = {"df": make_sample_candle_df(220, base_price=3_700_000.0), "timestamp": time.time()}

    def tearDown(self):
        from strategy.hybrid_regime import _CANDLE_CACHE
        _CANDLE_CACHE.clear()

    # =========================================================================
    # REPRODUCTION 1: Partial Ladder Completion Replenishment
    # =========================================================================
    def test_reproduction_1_replenishment_allocates_next_squad_below_lowest_bid(self):
        """
        Reproduction 1:
        Holding Squad 1 Stage 1 (0.10 ETH).
        Tracked Stage 2 completes 0.05 ETH.
        Stages 3 & 4 remain waiting at 3,500,000 KRW.
        Case 4 replenishment must allocate Squad 2 Stage 1 (1 unit), NOT duplicate Stage 3 (2 units).
        New order price must be strictly below 3,500,000 KRW and below spot (3,700,000 KRW).
        """
        market = "KRW-ETH"
        price = 3_700_000.0
        prot = main.PROTECTED_BALANCES[market]
        ex = SquadFakeExchange(prot + 0.15, price, market=market)

        state = {
            "market": market,
            "current_regime": "BEAR",
            "initial_entry_done": True,
            "strategy_engine": "ADAPTIVE_GRID_C_E",
            "bot_quantity": 0.10,
            "bot_avg_price": 4_000_000.0,
            "tranches": [
                {
                    "tranche_id": 1,
                    "step": 1,
                    "squad_id": 1,
                    "squad_stage": 1,
                    "units": 1,
                    "volume": 0.10,
                    "buy_price": 4_000_000.0,
                }
            ],
            "bear_limit_buys": {
                "bid-stage2": {
                    "state": "wait",
                    "units": 1,
                    "squad_id": 1,
                    "squad_stage": 2,
                    "executed_volume": 0.0,
                    "executed_funds": 0.0,
                },
                "bid-stage3": {
                    "state": "wait",
                    "units": 2,
                    "squad_id": 1,
                    "squad_stage": 3,
                    "executed_volume": 0.0,
                    "executed_funds": 0.0,
                },
                "bid-stage4": {
                    "state": "wait",
                    "units": 4,
                    "squad_id": 1,
                    "squad_stage": 4,
                    "executed_volume": 0.0,
                    "executed_funds": 0.0,
                },
            },
            "basket_limit_ask": {"uuid": "old-ask", "price": 4_020_000.0, "volume": 0.10},
        }

        # Stage 2 completes on exchange
        ex.orders["bid-stage2"] = {
            "uuid": "bid-stage2", "side": "bid", "state": "done",
            "executed_volume": "0.05", "trades": [{"funds": "180000"}]
        }
        # Old basket ask is waiting on exchange
        ex.orders["old-ask"] = {
            "uuid": "old-ask", "side": "ask", "state": "wait", "price": "4020000",
            "volume": "0.10", "executed_volume": "0", "trades": []
        }
        ex.open_orders.append(ex.orders["old-ask"].copy())

        # Stages 3 & 4 remain waiting on exchange at 3,500,000 KRW
        for stg in (3, 4):
            u = f"bid-stage{stg}"
            order = {
                "uuid": u, "side": "bid", "state": "wait", "price": "3500000",
                "volume": "0.01", "executed_volume": "0", "trades": []
            }
            ex.orders[u] = order
            ex.open_orders.append(order.copy())

        calls = 0
        def next_price(m):
            nonlocal calls
            calls += 1
            if calls > 1:
                raise EndTick()
            return price

        def bounded_sleep(sec):
            if sec >= 2:
                raise EndTick()

        output = io.StringIO()
        with patch.multiple(
            main,
            acquire_market_process_lock=lambda m: True,
            init_db=lambda: None,
            get_upbit_client=lambda: ex,
            load_strategy_state=lambda m: state,
            save_strategy_state=lambda m, s: True,
            load_trailing_exit_recovery=lambda m: None,
            check_krw_balance_alert=lambda *args, **kwargs: 1_000_000.0,
            send_telegram_alert=lambda msg: None,
            safe_get_current_price=next_price,
            get_hybrid_regime_and_signals=lambda m: {"candles_valid": True, "is_valid": True, "is_bull": False, "signal": "HOLD", "ma5": price, "curr_price": price},
            is_adaptive_grid_enabled=lambda m: True,
        ), patch.object(main.time, "sleep", side_effect=bounded_sleep), redirect_stdout(output):
            try:
                main.run_trading_strategy(market)
            except EndTick:
                pass

        # 1. Exactly 1 new buy order submitted (replenishing 3 - 2 = 1)
        self.assertEqual(1, len(ex.limit_buys), f"Expected 1 new limit buy, got {len(ex.limit_buys)}")
        new_bid = ex.limit_buys[0]

        # 2. Find record of the new order in state["bear_limit_buys"]
        new_uuid = new_bid["uuid"]
        new_rec = state["bear_limit_buys"][new_uuid]

        # 3. CRITICAL: New order must be Squad 2 Stage 1 with 1 unit (NOT Squad 1 Stage 3 with 2 units)
        self.assertEqual(2, new_rec["squad_id"], f"Expected squad_id 2, got {new_rec['squad_id']}")
        self.assertEqual(1, new_rec["squad_stage"], f"Expected squad_stage 1, got {new_rec['squad_stage']}")
        self.assertEqual(1, new_rec["units"], f"Expected 1 unit, got {new_rec['units']}")

        # 4. CRITICAL: Price must be strictly below lowest existing bid (3,500,000 KRW) and spot (3,700,000 KRW)
        self.assertLess(
            float(new_bid["price"]), 3_500_000.0,
            f"New price {new_bid['price']} must be strictly below lowest existing bid 3,500,000"
        )
        self.assertLess(
            float(new_bid["price"]), price,
            f"New price {new_bid['price']} must be below spot price {price}"
        )

        # 5. Check all open wait stages on exchange: must be exactly [(1, 3), (1, 4), (2, 1)] with no duplicates
        wait_stages = [
            (rec["squad_id"], rec["squad_stage"])
            for rec in state["bear_limit_buys"].values()
            if rec.get("state") == "wait"
        ]
        self.assertEqual(3, len(wait_stages))
        self.assertEqual(len(wait_stages), len(set(wait_stages)), f"Duplicate wait stages found: {wait_stages}")
        self.assertIn((1, 3), wait_stages)
        self.assertIn((1, 4), wait_stages)
        self.assertIn((2, 1), wait_stages)

    # =========================================================================
    # REPRODUCTION 2: Survivors After Squad Exits, New Ladder
    # =========================================================================
    def test_reproduction_2_survivors_allocates_squad_3_no_collision(self):
        """
        Reproduction 2:
        Position 0.10 ETH @ 4,000,000 with only surviving slots (1, 4) and (2, 4), 0.05 each.
        No pending orders, spot 3,700,000.
        Adaptive must allocate (3, 1), (3, 2), (3, 3) with units 1, 1, 2.
        Must have 0 collision with held tranches ((1, 4), (2, 4)).
        Legacy control on same state must produce the exact same stage sequence.
        """
        market = "KRW-ETH"
        price = 3_700_000.0
        prot = main.PROTECTED_BALANCES[market]

        surviving_tranches = [
            {
                "tranche_id": 1, "step": 4, "squad_id": 1, "squad_stage": 4,
                "units": 4, "volume": 0.05, "buy_price": 4_000_000.0,
            },
            {
                "tranche_id": 2, "step": 8, "squad_id": 2, "squad_stage": 4,
                "units": 4, "volume": 0.05, "buy_price": 4_000_000.0,
            },
        ]

        state_adaptive = {
            "market": market,
            "current_regime": "BEAR",
            "initial_entry_done": True,
            "strategy_engine": "ADAPTIVE_GRID_C_E",
            "bot_quantity": 0.10,
            "bot_avg_price": 4_000_000.0,
            "tranches": copy.deepcopy(surviving_tranches),
            "bear_limit_buys": {},
            "basket_limit_ask": None,
        }

        ex_adaptive = SquadFakeExchange(prot + 0.10, price, market=market)

        calls = 0
        def next_price(m):
            nonlocal calls
            calls += 1
            if calls > 1:
                raise EndTick()
            return price

        def bounded_sleep(sec):
            if sec >= 2:
                raise EndTick()

        with patch.multiple(
            main,
            acquire_market_process_lock=lambda m: True,
            init_db=lambda: None,
            get_upbit_client=lambda: ex_adaptive,
            load_strategy_state=lambda m: state_adaptive,
            save_strategy_state=lambda m, s: True,
            load_trailing_exit_recovery=lambda m: None,
            check_krw_balance_alert=lambda *args, **kwargs: 1_000_000.0,
            send_telegram_alert=lambda msg: None,
            safe_get_current_price=next_price,
            get_hybrid_regime_and_signals=lambda m: {"candles_valid": True, "is_valid": True, "is_bull": False, "signal": "HOLD", "ma5": price, "curr_price": price},
            is_adaptive_grid_enabled=lambda m: True,
        ), patch.object(main.time, "sleep", side_effect=bounded_sleep), redirect_stdout(io.StringIO()):
            try:
                main.run_trading_strategy(market)
            except EndTick:
                pass

        # 1. Exactly 3 new buy orders submitted
        self.assertEqual(3, len(ex_adaptive.limit_buys), f"Expected 3 bids, got {len(ex_adaptive.limit_buys)}")

        # 2. Collect allocated stages and units
        adaptive_stages = [
            (rec["squad_id"], rec["squad_stage"])
            for rec in state_adaptive["bear_limit_buys"].values()
        ]
        adaptive_units = [
            rec["units"]
            for rec in state_adaptive["bear_limit_buys"].values()
        ]

        # 3. CRITICAL: Expected sequence is (3, 1), (3, 2), (3, 3) with units 1, 1, 2
        self.assertEqual([(3, 1), (3, 2), (3, 3)], adaptive_stages)
        self.assertEqual([1, 1, 2], adaptive_units)

        # 4. CRITICAL: ZERO collision with held tranches ((1, 4), (2, 4))
        held_stages = {(t["squad_id"], t["squad_stage"]) for t in surviving_tranches}
        overlap = held_stages & set(adaptive_stages)
        self.assertEqual(set(), overlap, f"Collision detected with held tranches: {overlap}")

        # 5. Prices must be strictly decreasing and below avg_price
        prices = [float(b["price"]) for b in ex_adaptive.limit_buys]
        self.assertLess(prices[0], 4_000_000.0)
        self.assertLess(prices[1], prices[0])
        self.assertLess(prices[2], prices[1])

        # 6. Legacy control check on the same state produces identical stages & units
        state_legacy = {
            "market": market,
            "current_regime": "BEAR",
            "initial_entry_done": True,
            "strategy_engine": "LEGACY",
            "bot_quantity": 0.10,
            "bot_avg_price": 4_000_000.0,
            "tranches": copy.deepcopy(surviving_tranches),
            "bear_limit_buys": {},
            "basket_limit_ask": None,
        }
        legacy_orders = calculate_new_buy_prices(
            avg_buy_price=4_000_000.0,
            existing_orders=None,
            max_steps=9999,
            current_step=len(state_legacy["tranches"]),
            state=state_legacy,
        )
        legacy_stages = [(o["squad_id"], o["squad_stage"]) for o in legacy_orders]
        legacy_units = [o["units"] for o in legacy_orders]
        self.assertEqual(legacy_stages, adaptive_stages)
        self.assertEqual(legacy_units, adaptive_units)

    # =========================================================================
    # PARTIAL FILL DEDUPLICATION & SINGLE SLOT OWNERSHIP
    # =========================================================================
    def test_partial_fill_deduplication_owns_single_slot(self):
        """
        Partial fill of UUID u1 exists in state['tranches'] AND in state['bear_limit_buys']['u1'] (wait).
        get_existing_stages_from_state must return (1, 2) exactly once, consuming 1 slot against cap.
        Subsequent fill updates the tranche and maintains single slot ownership.
        """
        state = {
            "tranches": [
                {"tranche_id": 1, "step": 1, "squad_id": 1, "squad_stage": 1, "units": 1, "volume": 0.1},
                {
                    "tranche_id": 2, "step": 2, "squad_id": 1, "squad_stage": 2, "units": 1,
                    "volume": 0.02, "buy_price": 3_800_000.0, "order_uuid": "partial-bid-1"
                },
            ],
            "bear_limit_buys": {
                "partial-bid-1": {
                    "squad_id": 1, "squad_stage": 2, "units": 1, "state": "wait",
                    "executed_volume": 0.02, "executed_funds": 76000.0
                },
                "open-bid-2": {
                    "squad_id": 1, "squad_stage": 3, "units": 2, "state": "wait",
                    "executed_volume": 0.0, "executed_funds": 0.0
                },
            }
        }

        # Stage extraction must deduplicate partial-bid-1
        stages = get_existing_stages_from_state(state)
        self.assertEqual([(1, 1), (1, 2), (1, 3)], stages)
        self.assertEqual(3, len(stages), f"Expected exactly 3 unique stages, got {len(stages)}: {stages}")

        # SOL cap check: max_steps 16 - len(stages) must be 13, NOT 12
        allowed_new = 16 - len(stages)
        self.assertEqual(13, allowed_new)

        # Next stage allocation continues from max (1, 3) -> (1, 4)
        next_stages = get_next_squad_stages(stages, count=1)
        self.assertEqual([{"squad_id": 1, "squad_stage": 4, "units": 4}], next_stages)

    def test_partial_fill_reconciliation_maintains_single_slot(self):
        """
        reconcile_bear_limit_buys credits new volume to existing tranche with order_uuid,
        preserving exactly 1 slot across multiple ticks.
        """
        market = "KRW-ETH"
        state = {
            "bot_quantity": 0.12,
            "bot_avg_price": 3_900_000.0,
            "tranches": [
                {"tranche_id": 1, "step": 1, "squad_id": 1, "squad_stage": 1, "units": 1, "volume": 0.1, "buy_price": 4_000_000.0},
                {
                    "tranche_id": 2, "step": 2, "squad_id": 1, "squad_stage": 2, "units": 1,
                    "volume": 0.02, "buy_price": 3_800_000.0, "order_uuid": "partial-u1"
                },
            ],
            "bear_limit_buys": {
                "partial-u1": {
                    "squad_id": 1, "squad_stage": 2, "units": 1, "state": "wait",
                    "executed_volume": 0.02, "executed_funds": 76000.0
                }
            }
        }

        # Client reports further partial execution (0.04 total executed)
        client = SquadFakeExchange(1.0, 3_800_000.0, market=market)
        client.orders["partial-u1"] = {
            "uuid": "partial-u1", "side": "bid", "state": "wait",
            "executed_volume": "0.04", "trades": [{"funds": "152000"}]
        }

        ok = main.reconcile_bear_limit_buys(client, market, state)
        self.assertTrue(ok)

        # Tranches must still have length 2 (updated in place, not added as a 3rd tranche)
        self.assertEqual(2, len(state["tranches"]))
        self.assertAlmostEqual(0.04, state["tranches"][1]["volume"])
        self.assertEqual("partial-u1", state["tranches"][1]["order_uuid"])

        # Occupied stages must still be exactly 2
        stages = get_existing_stages_from_state(state)
        self.assertEqual([(1, 1), (1, 2)], stages)
        self.assertEqual(2, len(stages))

    # =========================================================================
    # MULTIPLE FILLS BETWEEN TICKS REPLENISHMENT
    # =========================================================================
    def test_several_fills_between_ticks_sequential_replenishment(self):
        """
        When two orders fill between ticks (e.g. Stage 2 and Stage 3),
        Case 4 needs 2 orders. Allocates (2, 1) and (2, 2) with units 1 and 1.
        Prices must be monotonically decreasing below the lowest remaining bid (Stage 4).
        """
        natr = 0.009
        lowest_bid_price = 3_400_000.0
        state = {
            "tranches": [
                {"squad_id": 1, "squad_stage": 1, "units": 1},
                {"squad_id": 1, "squad_stage": 2, "units": 1},
                {"squad_id": 1, "squad_stage": 3, "units": 2},
            ],
            "bear_limit_buys": {
                "bid-stage4": {
                    "squad_id": 1, "squad_stage": 4, "units": 4, "state": "wait",
                }
            }
        }
        existing_orders = [{"uuid": "bid-stage4", "price": lowest_bid_price}]

        orders = calculate_adaptive_buy_prices(
            base_price=lowest_bid_price,
            natr=natr,
            num_orders=2,
            max_steps=9999,
            step_scale=DEFAULT_STEP_SCALE,
            unit_krw=10_000.0,
            state=state,
            existing_orders=existing_orders
        )

        self.assertEqual(2, len(orders))
        # Stages must be (2, 1) and (2, 2)
        self.assertEqual(2, orders[0]["squad_id"])
        self.assertEqual(1, orders[0]["squad_stage"])
        self.assertEqual(1, orders[0]["units"])

        self.assertEqual(2, orders[1]["squad_id"])
        self.assertEqual(2, orders[1]["squad_stage"])
        self.assertEqual(1, orders[1]["units"])

        # Prices must be strictly decreasing below 3,400,000 KRW
        self.assertLess(orders[0]["price"], lowest_bid_price)
        self.assertLess(orders[1]["price"], orders[0]["price"])

    # =========================================================================
    # SPARSE SURVIVORS AFTER SQUAD EXITS
    # =========================================================================
    def test_sparse_survivors_after_squad_exits_progression(self):
        """
        Surviving slots are (1, 3) and (2, 1).
        New ladder of 3 orders must allocate (2, 2), (2, 3), (2, 4) with units 1, 2, 4.
        """
        state = {
            "tranches": [
                {"squad_id": 1, "squad_stage": 3, "units": 2},
                {"squad_id": 2, "squad_stage": 1, "units": 1},
            ],
            "bear_limit_buys": {}
        }
        orders = calculate_adaptive_buy_prices(
            base_price=3_700_000.0,
            natr=0.008,
            num_orders=3,
            max_steps=9999,
            state=state
        )
        self.assertEqual(3, len(orders))
        self.assertEqual([(2, 2), (2, 3), (2, 4)], [(o["squad_id"], o["squad_stage"]) for o in orders])
        self.assertEqual([1, 2, 4], [o["units"] for o in orders])

    # =========================================================================
    # SOL 16-STAGE CAP (4 SQUADS) PRESERVATION
    # =========================================================================
    def test_sol_strictly_capped_at_four_squads_16_stages_adaptive(self):
        """
        Adaptive Grid enforces SOL max_steps=16 (4 squads * 4 stages).
        Cannot allocate into Squad 5.
        """
        # Populate state with 15 stages (Squad 1, 2, 3 complete, Squad 4 stages 1, 2, 3)
        sol_stages = []
        for sid in range(1, 4):
            for stg in range(1, 5):
                sol_stages.append((sid, stg))
        sol_stages.extend([(4, 1), (4, 2), (4, 3)])
        self.assertEqual(15, len(sol_stages))

        state = {
            "market": "KRW-SOL",
            "tranches": [{"squad_id": s[0], "squad_stage": s[1], "units": 1} for s in sol_stages],
            "bear_limit_buys": {}
        }

        # Request 3 orders: only 1 slot remains (Squad 4 Stage 4) before hitting 16
        orders = calculate_adaptive_buy_prices(
            base_price=160_000.0,
            natr=0.01,
            num_orders=3,
            max_steps=16,
            state=state
        )
        self.assertEqual(1, len(orders), f"Expected 1 order at cap boundary, got {len(orders)}")
        self.assertEqual(4, orders[0]["squad_id"])
        self.assertEqual(4, orders[0]["squad_stage"])
        self.assertEqual(4, orders[0]["units"])

        # When all 16 stages are occupied, calculate_adaptive_buy_prices must return []
        state["tranches"].append({"squad_id": 4, "squad_stage": 4, "units": 4})
        capped_orders = calculate_adaptive_buy_prices(
            base_price=160_000.0,
            natr=0.01,
            num_orders=3,
            max_steps=16,
            state=state
        )
        self.assertEqual([], capped_orders)

    # =========================================================================
    # REPRODUCTION 3: Untagged Legacy Partial-Fill Tranche with Known Pending Order
    # =========================================================================
    def test_legacy_untagged_tranche_with_validated_pending_order_canonical_slot(self):
        """
        Codex P1 Reproduction:
        Tagged holding (1, 1) @ 4,000,000 KRW (0.10 ETH).
        Untagged legacy partial-fill tranche: UUID 'partial', step 2, volume 0.02 ETH @ 3,500,000 KRW.
        Matching pending checkpoint 'partial': state 'wait', actual slot (1, 3), units 2, executed 0.02 ETH.
        Exchange fake confirms same UUID wait/partial with executed 0.02; basket ask waiting; spot 3,700,000 KRW.

        Canonical slot resolution must determine occupancy [(1, 1), (1, 3)] BEFORE deduplication.
        One Adaptive strategy-loop decision tick must:
        - Submit exactly 2 new bids (replenishing 3 - 1 = 2)
        - Allocate Squad 1 Stage 4 (4 units) and Squad 2 Stage 1 (1 unit), NOT duplicate Stage 3 (1, 3)
        - Anchor prices strictly decreasing below 3,500,000 KRW
        - Result in active wait slots [(1, 3), (1, 4), (2, 1)] on exchange/state with zero duplicate (1, 3)
        - Not halt for reconciliation
        """
        market = "KRW-ETH"
        price = 3_700_000.0
        prot = main.PROTECTED_BALANCES[market]
        ex = SquadFakeExchange(prot + 0.12, price, market=market)

        state = {
            "market": market,
            "current_regime": "BEAR",
            "initial_entry_done": True,
            "strategy_engine": "ADAPTIVE_GRID_C_E",
            "bot_quantity": 0.12,
            "bot_avg_price": 3_916_667.0,
            "bear_buy_rebalance_pending": True,
            "tranches": [
                {
                    "tranche_id": 1,
                    "step": 1,
                    "squad_id": 1,
                    "squad_stage": 1,
                    "units": 1,
                    "volume": 0.10,
                    "buy_price": 4_000_000.0,
                },
                {
                    "tranche_id": 2,
                    "step": 2,
                    # No squad_id or squad_stage tags (legacy tranche)
                    "volume": 0.02,
                    "buy_price": 3_500_000.0,
                    "order_uuid": "partial",
                },
            ],
            "bear_limit_buys": {
                "partial": {
                    "state": "wait",
                    "price": 3_500_000.0,
                    "volume": 0.05,
                    "units": 2,
                    "squad_id": 1,
                    "squad_stage": 3,
                    "executed_volume": 0.02,
                    "executed_funds": 70_000.0,
                },
            },
            "basket_limit_ask": {"uuid": "old-ask", "price": 4_020_000.0, "volume": 0.12},
        }

        # Verify canonical occupancy BEFORE live decision tick
        occupied_before = get_existing_stages_from_state(state)
        self.assertEqual([(1, 1), (1, 3)], occupied_before, f"Expected canonical [(1, 1), (1, 3)], got {occupied_before}")

        # Exchange orders
        ex.orders["partial"] = {
            "uuid": "partial", "side": "bid", "state": "wait", "price": "3500000",
            "volume": "0.05", "executed_volume": "0.02", "trades": [{"funds": "70000"}]
        }
        ex.open_orders.append(ex.orders["partial"].copy())

        ex.orders["old-ask"] = {
            "uuid": "old-ask", "side": "ask", "state": "wait", "price": "4020000",
            "volume": "0.12", "executed_volume": "0", "trades": []
        }
        ex.open_orders.append(ex.orders["old-ask"].copy())

        calls = 0
        def next_price(m):
            nonlocal calls
            calls += 1
            if calls > 1:
                raise EndTick()
            return price

        def bounded_sleep(sec):
            if sec >= 2:
                raise EndTick()

        output = io.StringIO()
        with patch.multiple(
            main,
            acquire_market_process_lock=lambda m: True,
            init_db=lambda: None,
            get_upbit_client=lambda: ex,
            load_strategy_state=lambda m: state,
            save_strategy_state=lambda m, s: True,
            load_trailing_exit_recovery=lambda m: None,
            check_krw_balance_alert=lambda *args, **kwargs: 1_000_000.0,
            send_telegram_alert=lambda msg: None,
            safe_get_current_price=next_price,
            get_hybrid_regime_and_signals=lambda m: {"candles_valid": True, "is_valid": True, "is_bull": False, "signal": "HOLD", "ma5": price, "curr_price": price},
            is_adaptive_grid_enabled=lambda m: True,
        ), patch.object(main.time, "sleep", side_effect=bounded_sleep), redirect_stdout(output):
            try:
                main.run_trading_strategy(market)
            except EndTick:
                pass

        # 1. Exactly 2 new buy orders submitted
        self.assertEqual(2, len(ex.limit_buys), f"Expected 2 new limit buys, got {len(ex.limit_buys)}")

        # 2. Check submitted orders: Squad 1 Stage 4 (4U) and Squad 2 Stage 1 (1U)
        bid0_uuid = ex.limit_buys[0]["uuid"]
        bid1_uuid = ex.limit_buys[1]["uuid"]
        rec0 = state["bear_limit_buys"][bid0_uuid]
        rec1 = state["bear_limit_buys"][bid1_uuid]

        self.assertEqual((1, 4), (rec0["squad_id"], rec0["squad_stage"]))
        self.assertEqual(4, rec0["units"])
        self.assertEqual((2, 1), (rec1["squad_id"], rec1["squad_stage"]))
        self.assertEqual(1, rec1["units"])

        # 3. Prices strictly below 3,500,000 KRW and decreasing
        p0 = float(ex.limit_buys[0]["price"])
        p1 = float(ex.limit_buys[1]["price"])
        self.assertLess(p0, 3_500_000.0, f"Bid 0 price {p0} must be strictly below 3,500,000")
        self.assertLess(p1, p0, f"Bid 1 price {p1} must be strictly below Bid 0 price {p0}")

        # 4. Wait slots after tick: exactly [(1, 3), (1, 4), (2, 1)] with NO duplicate (1, 3)
        wait_slots = [
            (rec["squad_id"], rec["squad_stage"])
            for rec in state["bear_limit_buys"].values()
            if rec.get("state") == "wait"
        ]
        self.assertEqual(3, len(wait_slots))
        self.assertEqual(len(wait_slots), len(set(wait_slots)), f"Duplicate wait slots found: {wait_slots}")
        self.assertEqual([(1, 3), (1, 4), (2, 1)], sorted(wait_slots))

        # 5. Not halted
        self.assertFalse(state.get("trading_halted", False))

    # =========================================================================
    # EXPLICIT METADATA CONFLICT BLOCKS BUYS AND HALTS
    # =========================================================================
    def test_explicit_metadata_conflict_blocks_buys_and_halts(self):
        """
        If tranche has explicit squad tags differing from order checkpoint tags,
        get_existing_stages_from_state returns None, allocators return [],
        and live tick halts for order reconciliation with 0 new bids.
        """
        market = "KRW-ETH"
        price = 3_700_000.0
        prot = main.PROTECTED_BALANCES[market]
        ex = SquadFakeExchange(prot + 0.10, price, market=market)

        state = {
            "market": market,
            "current_regime": "BEAR",
            "initial_entry_done": True,
            "strategy_engine": "ADAPTIVE_GRID_C_E",
            "bot_quantity": 0.10,
            "bot_avg_price": 4_000_000.0,
            "bear_buy_rebalance_pending": True,
            "tranches": [
                {
                    "tranche_id": 1,
                    "squad_id": 1,
                    "squad_stage": 2,  # Tranche says (1, 2)
                    "units": 1,
                    "volume": 0.10,
                    "buy_price": 4_000_000.0,
                    "order_uuid": "conflict-uuid",
                }
            ],
            "bear_limit_buys": {
                "conflict-uuid": {
                    "state": "wait",
                    "price": 3_800_000.0,
                    "volume": 0.05,
                    "units": 2,
                    "squad_id": 1,
                    "squad_stage": 3,  # Checkpoint says (1, 3) -> CONFLICT!
                    "executed_volume": 0.0,
                    "executed_funds": 0.0,
                }
            },
            "basket_limit_ask": None,
        }

        # 1. State extraction must return None
        self.assertIsNone(get_existing_stages_from_state(state))

        # 2. Both allocators must return []
        self.assertEqual([], calculate_adaptive_buy_prices(base_price=3_800_000.0, natr=0.01, state=state))
        self.assertEqual([], calculate_new_buy_prices(avg_buy_price=4_000_000.0, state=state))

        # 3. Live strategy tick must halt and submit 0 orders
        ex.orders["conflict-uuid"] = {
            "uuid": "conflict-uuid", "side": "bid", "state": "wait", "price": "3800000",
            "volume": "0.05", "executed_volume": "0", "trades": []
        }
        ex.open_orders.append(ex.orders["conflict-uuid"].copy())

        calls = 0
        def next_price(m):
            nonlocal calls
            calls += 1
            if calls > 1:
                raise EndTick()
            return price

        def bounded_sleep(sec):
            if sec >= 2:
                raise EndTick()

        with patch.multiple(
            main,
            acquire_market_process_lock=lambda m: True,
            init_db=lambda: None,
            get_upbit_client=lambda: ex,
            load_strategy_state=lambda m: state,
            save_strategy_state=lambda m, s: True,
            load_trailing_exit_recovery=lambda m: None,
            check_krw_balance_alert=lambda *args, **kwargs: 1_000_000.0,
            send_telegram_alert=lambda msg: None,
            safe_get_current_price=next_price,
            get_hybrid_regime_and_signals=lambda m: {"candles_valid": True, "is_valid": True, "is_bull": False, "signal": "HOLD", "ma5": price, "curr_price": price},
            is_adaptive_grid_enabled=lambda m: True,
        ), patch.object(main.time, "sleep", side_effect=bounded_sleep), redirect_stdout(io.StringIO()):
            try:
                main.run_trading_strategy(market)
            except EndTick:
                pass

        self.assertTrue(state.get("trading_halted", False))
        self.assertEqual(0, len(ex.limit_buys))

    # =========================================================================
    # UNKNOWN METADATA NOT GUESSED / UNRESOLVED ACTIVE ORDER BLOCKS BUYS
    # =========================================================================
    def test_unknown_order_metadata_not_guessed(self):
        """
        Orders in bear_limit_buys lacking squad_id/squad_stage are not guessed from price or volume.
        An active waiting order with unknown metadata blocks downstream allocation and halts.
        """
        # Case A: Completed order without tags does not block active stages
        state_done = {
            "tranches": [{"squad_id": 1, "squad_stage": 1, "units": 1}],
            "bear_limit_buys": {
                "old-done-order": {
                    "state": "done",
                    "price": 3_500_000.0,
                    "units": 2,
                }
            }
        }
        self.assertEqual([(1, 1)], get_existing_stages_from_state(state_done))

        # Case B: Active waiting order with unknown metadata returns None and blocks new buys
        state_wait = {
            "tranches": [{"squad_id": 1, "squad_stage": 1, "units": 1}],
            "bear_limit_buys": {
                "mystery-order": {
                    "state": "wait",
                    "price": 3_500_000.0,
                    "units": 2,
                    # squad_id and squad_stage absent
                }
            }
        }
        self.assertIsNone(get_existing_stages_from_state(state_wait))
        self.assertEqual([], calculate_adaptive_buy_prices(base_price=3_500_000.0, natr=0.01, state=state_wait))
        self.assertEqual([], calculate_new_buy_prices(avg_buy_price=4_000_000.0, state=state_wait))

    def test_unresolvable_active_waiting_order_blocks_buys_and_halts(self):
        """
        If an active waiting order lacks squad/stage metadata and cannot be resolved,
        get_existing_stages_from_state returns None, allocators return [],
        and live tick halts for order reconciliation with 0 new bids.
        """
        market = "KRW-ETH"
        price = 3_700_000.0
        prot = main.PROTECTED_BALANCES[market]
        ex = SquadFakeExchange(prot + 0.10, price, market=market)

        state_wait = {
            "market": market,
            "current_regime": "BEAR",
            "initial_entry_done": True,
            "strategy_engine": "ADAPTIVE_GRID_C_E",
            "bot_quantity": 0.10,
            "bot_avg_price": 4_000_000.0,
            "bear_buy_rebalance_pending": True,
            "tranches": [{"squad_id": 1, "squad_stage": 1, "units": 1, "volume": 0.10}],
            "bear_limit_buys": {
                "mystery-wait-order": {
                    "state": "wait",
                    "price": 3_500_000.0,
                    "volume": 0.05,
                    "units": 2,
                    # squad_id and squad_stage absent
                }
            },
            "basket_limit_ask": None,
        }

        # 1. State extraction must return None
        self.assertIsNone(get_existing_stages_from_state(state_wait))

        # 2. Both allocators must return []
        self.assertEqual([], calculate_adaptive_buy_prices(base_price=3_500_000.0, natr=0.01, state=state_wait))
        self.assertEqual([], calculate_new_buy_prices(avg_buy_price=4_000_000.0, state=state_wait))

        # 3. Live tick must halt with 0 new orders
        ex.orders["mystery-wait-order"] = {
            "uuid": "mystery-wait-order", "side": "bid", "state": "wait", "price": "3500000",
            "volume": "0.05", "executed_volume": "0", "trades": []
        }
        ex.open_orders.append(ex.orders["mystery-wait-order"].copy())

        calls = 0
        def next_price(m):
            nonlocal calls
            calls += 1
            if calls > 1:
                raise EndTick()
            return price

        def bounded_sleep(sec):
            if sec >= 2:
                raise EndTick()

        with patch.multiple(
            main,
            acquire_market_process_lock=lambda m: True,
            init_db=lambda: None,
            get_upbit_client=lambda: ex,
            load_strategy_state=lambda m: state_wait,
            save_strategy_state=lambda m, s: True,
            load_trailing_exit_recovery=lambda m: None,
            check_krw_balance_alert=lambda *args, **kwargs: 1_000_000.0,
            send_telegram_alert=lambda msg: None,
            safe_get_current_price=next_price,
            get_hybrid_regime_and_signals=lambda m: {"candles_valid": True, "is_valid": True, "is_bull": False, "signal": "HOLD", "ma5": price, "curr_price": price},
            is_adaptive_grid_enabled=lambda m: True,
        ), patch.object(main.time, "sleep", side_effect=bounded_sleep), redirect_stdout(io.StringIO()):
            try:
                main.run_trading_strategy(market)
            except EndTick:
                pass

        self.assertTrue(state_wait.get("trading_halted", False))
        self.assertEqual(0, len(ex.limit_buys))

    # =========================================================================
    # TICK ROUNDING AND STRICT MONOTONIC PRICE DECREASING
    # =========================================================================
    def test_tick_rounding_and_monotonic_prices(self):
        """All calculated prices must conform to Upbit tick sizes and be strictly monotonically decreasing."""
        base_p = 3_750_000.0
        orders = calculate_adaptive_buy_prices(
            base_price=base_p,
            natr=0.008,
            current_step=0,
            num_orders=4,
            max_steps=16,
            step_scale=1.25
        )
        self.assertEqual(4, len(orders))
        prev_p = base_p
        for o in orders:
            p = o["price"]
            tick = get_upbit_tick_size(p)
            self.assertEqual(0.0, p % tick, f"Price {p} not aligned to tick size {tick}")
            self.assertLess(p, prev_p, f"Price {p} not strictly less than previous {prev_p}")
            prev_p = p


if __name__ == "__main__":
    unittest.main()
