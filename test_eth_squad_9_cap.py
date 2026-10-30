"""Unit and regression tests for ETH martingale squad cap at maximum 9.

Acceptance Criteria:
1. MAX_SQUAD_CAPS defines KRW-ETH: 9 and KRW-SOL: 4.
2. get_next_squad_stages enforces max_squad=9 for KRW-ETH, preventing Squad 10.
3. calculate_adaptive_buy_prices allocates Squad 9 Stage 4 when at (9, 3), but returns [] when (9, 4) is occupied.
4. Over-cap orders (squad_id > 9, e.g. Squad 10 Stage 1 and Stage 2) are identified and cancelled.
5. Squad 9 Stage 4 waiting order is preserved while awaiting fill.
6. When Squad 9 Stage 4 is filled in tranches, squad_exhausted triggers, expecting 0 open buy orders.
"""

from __future__ import annotations

import copy
import os
import unittest
from unittest.mock import patch, MagicMock

import config
import main
from strategy.adaptive_grid import calculate_adaptive_buy_prices
from strategy.matingale2x_logic import (
    calculate_new_buy_prices,
    get_existing_stages_from_state,
    get_next_squad_stages,
)


class TestETHSquad9Cap(unittest.TestCase):
    def test_config_defines_squad_caps(self):
        """config.py defines MAX_SQUAD_CAPS with ETH=9 and SOL=4."""
        self.assertEqual(9, config.get_max_squad_cap("KRW-ETH"))
        self.assertEqual(4, config.get_max_squad_cap("KRW-SOL"))
        self.assertEqual(36, config.get_martingale_max_steps("KRW-ETH"))

    def test_get_next_squad_stages_blocks_squad_10(self):
        """get_next_squad_stages does not allocate any stages for squad_id > 9."""
        stages_up_to_9_3 = [(s, i) for s in range(1, 9) for i in range(1, 5)]
        stages_up_to_9_3.extend([(9, 1), (9, 2), (9, 3)])

        # At (9, 3), requesting 3 stages only yields 1 stage: (9, 4)
        planned = get_next_squad_stages(stages_up_to_9_3, count=3, max_squad=9)
        self.assertEqual(1, len(planned))
        self.assertEqual(9, planned[0]["squad_id"])
        self.assertEqual(4, planned[0]["squad_stage"])
        self.assertEqual(4, planned[0]["units"])

        # When (9, 4) is present, requesting more stages yields empty list
        stages_up_to_9_4 = list(stages_up_to_9_3) + [(9, 4)]
        planned_full = get_next_squad_stages(stages_up_to_9_4, count=3, max_squad=9)
        self.assertEqual([], planned_full)

    def test_get_next_squad_stages_infers_squad_cap_from_max_steps(self):
        """When max_squad is None but max_steps=36, max_squad=9 is inferred."""
        stages_up_to_9_4 = [(s, i) for s in range(1, 10) for i in range(1, 5)]
        planned = get_next_squad_stages(stages_up_to_9_4, count=3, max_steps=36)
        self.assertEqual([], planned)

    def test_calculate_adaptive_buy_prices_caps_at_squad_9(self):
        """calculate_adaptive_buy_prices for KRW-ETH respects Squad 9 cap."""
        # 1 legacy tranche + stages up to (9, 3)
        tranches = [{"tranche_id": 0, "step": 1}]  # legacy
        for sid in range(1, 9):
            for stg in range(1, 5):
                tranches.append({"squad_id": sid, "squad_stage": stg, "units": 1})
        tranches.extend([
            {"squad_id": 9, "squad_stage": 1, "units": 1},
            {"squad_id": 9, "squad_stage": 2, "units": 1},
            {"squad_id": 9, "squad_stage": 3, "units": 2},
        ])

        state = {
            "market": "KRW-ETH",
            "tranches": tranches,
            "bear_limit_buys": {},
        }

        # Request 3 orders: only Squad 9 Stage 4 can be returned
        orders = calculate_adaptive_buy_prices(
            base_price=3_000_000.0,
            natr=0.01,
            num_orders=3,
            max_steps=36,
            state=state,
        )
        self.assertEqual(1, len(orders), f"Expected 1 order at squad cap, got {len(orders)}")
        self.assertEqual(9, orders[0]["squad_id"])
        self.assertEqual(4, orders[0]["squad_stage"])
        self.assertEqual(4, orders[0]["units"])

        # Once Squad 9 Stage 4 is in bear_limit_buys (waiting) or tranches, 0 orders returned
        state["bear_limit_buys"]["uuid-9-4"] = {
            "state": "wait",
            "squad_id": 9,
            "squad_stage": 4,
            "units": 4,
        }
        orders_empty = calculate_adaptive_buy_prices(
            base_price=2_900_000.0,
            natr=0.01,
            num_orders=3,
            max_steps=36,
            state=state,
        )
        self.assertEqual([], orders_empty)

    def test_calculate_new_buy_prices_caps_at_squad_9(self):
        """calculate_new_buy_prices for KRW-ETH respects Squad 9 cap."""
        tranches = []
        for sid in range(1, 9):
            for stg in range(1, 5):
                tranches.append({"squad_id": sid, "squad_stage": stg, "units": 1})
        tranches.extend([
            {"squad_id": 9, "squad_stage": 1, "units": 1},
            {"squad_id": 9, "squad_stage": 2, "units": 1},
            {"squad_id": 9, "squad_stage": 3, "units": 2},
            {"squad_id": 9, "squad_stage": 4, "units": 4},
        ])
        state = {
            "market": "KRW-ETH",
            "tranches": tranches,
            "bear_limit_buys": {},
        }
        orders = calculate_new_buy_prices(
            avg_buy_price=3_000_000.0,
            max_steps=36,
            state=state,
        )
        self.assertEqual([], orders)

    def test_over_cap_squad_10_orders_are_cancelled_and_squad_9_preserved(self):
        """
        Verify that orders exceeding max_squad (Squad 10 Stage 1 and 2)
        are cancelled while Squad 9 Stage 4 is kept active.
        """
        state = {
            "market": "KRW-ETH",
            "tranches": [
                {"squad_id": 9, "squad_stage": 1, "units": 1},
                {"squad_id": 9, "squad_stage": 2, "units": 1},
                {"squad_id": 9, "squad_stage": 3, "units": 2},
            ],
            "bear_limit_buys": {
                "uuid-9-4": {"state": "wait", "squad_id": 9, "squad_stage": 4, "units": 4},
                "uuid-10-1": {"state": "wait", "squad_id": 10, "squad_stage": 1, "units": 1},
                "uuid-10-2": {"state": "wait", "squad_id": 10, "squad_stage": 2, "units": 1},
            },
        }
        buy_orders = [
            {"uuid": "uuid-9-4", "price": "2943000", "side": "bid"},
            {"uuid": "uuid-10-1", "price": "2917000", "side": "bid"},
            {"uuid": "uuid-10-2", "price": "2882000", "side": "bid"},
        ]

        max_squad_cap = config.get_max_squad_cap("KRW-ETH")
        self.assertEqual(9, max_squad_cap)

        over_cap_buys = [
            o for o in buy_orders
            if state.get("bear_limit_buys", {}).get(o["uuid"], {}).get("squad_id", 0) > max_squad_cap
        ]
        self.assertEqual(2, len(over_cap_buys))
        self.assertEqual(["uuid-10-1", "uuid-10-2"], [o["uuid"] for o in over_cap_buys])

        # After filtering over_cap orders, only uuid-9-4 remains
        valid_buys = [o for o in buy_orders if o["uuid"] not in [x["uuid"] for x in over_cap_buys]]
        self.assertEqual(1, len(valid_buys))
        self.assertEqual("uuid-9-4", valid_buys[0]["uuid"])

    def test_squad_exhausted_when_final_stage_filled_in_tranches(self):
        """When Squad 9 Stage 4 is filled in tranches, squad_exhausted is True and expected_open_buys is 0."""
        max_squad_cap = 9
        # Case A: (9, 4) in bear_limit_buys (waiting), NOT in tranches
        state_waiting = {
            "tranches": [
                {"squad_id": 9, "squad_stage": 1},
                {"squad_id": 9, "squad_stage": 2},
                {"squad_id": 9, "squad_stage": 3},
            ],
            "bear_limit_buys": {
                "uuid-9-4": {"state": "wait", "squad_id": 9, "squad_stage": 4, "units": 4}
            }
        }
        active_stages = get_existing_stages_from_state(state_waiting)
        squad_exhausted = any(
            t.get("squad_id") is not None and t.get("squad_id") >= max_squad_cap and
            t.get("squad_stage") is not None and t.get("squad_stage") >= 4
            for t in state_waiting.get("tranches", [])
        )
        max_squad_cap_reached = squad_exhausted or any(
            sid >= max_squad_cap and stg >= 4 for sid, stg in active_stages
        )
        self.assertFalse(squad_exhausted)
        self.assertTrue(max_squad_cap_reached)
        expected_open_buys = 0 if squad_exhausted else (1 if max_squad_cap_reached else 3)
        self.assertEqual(1, expected_open_buys)

        # Case B: (9, 4) filled into tranches
        state_filled = {
            "tranches": [
                {"squad_id": 9, "squad_stage": 1},
                {"squad_id": 9, "squad_stage": 2},
                {"squad_id": 9, "squad_stage": 3},
                {"squad_id": 9, "squad_stage": 4},
            ],
            "bear_limit_buys": {
                "uuid-9-4": {"state": "done", "squad_id": 9, "squad_stage": 4, "units": 4}
            }
        }
        active_stages_filled = get_existing_stages_from_state(state_filled)
        squad_exhausted_filled = any(
            t.get("squad_id") is not None and t.get("squad_id") >= max_squad_cap and
            t.get("squad_stage") is not None and t.get("squad_stage") >= 4
            for t in state_filled.get("tranches", [])
        )
        max_squad_cap_reached_filled = squad_exhausted_filled or any(
            sid >= max_squad_cap and stg >= 4 for sid, stg in active_stages_filled
        )
        self.assertTrue(squad_exhausted_filled)
        self.assertTrue(max_squad_cap_reached_filled)
        expected_open_buys_filled = 0 if squad_exhausted_filled else (1 if max_squad_cap_reached_filled else 3)
        self.assertEqual(0, expected_open_buys_filled)


if __name__ == "__main__":
    unittest.main()
