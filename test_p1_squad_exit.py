"""Unit and regression tests for Squad-based Exit (스쿼드별 탈출) in ETH and SOL."""

from __future__ import annotations

import copy
import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import main
from strategy.hybrid_regime import check_magic_split_exits
from strategy.matingale2x_logic import (
    calculate_new_buy_prices,
    get_existing_stages_from_state,
    get_next_squad_stages,
)


class EndTick(BaseException):
    """Signal to stop the single-tick test loop."""


class SquadFakeExchange:
    def __init__(self, holdings: float, price: float, market: str = "KRW-ETH"):
        self.market = market
        self.currency = market.split("-")[1]
        self.holdings = holdings
        self.price = price
        self.open_orders: list[dict] = []
        self.orders: dict[str, dict] = {}
        self.market_buys: list[float] = []
        self.market_sells: list[float] = []
        self.limit_buys: list[dict] = []
        self.limit_sells: list[dict] = []
        self.cancelled: list[str] = []

    def get_balance(self, currency: str, verbose: bool = False):
        if currency == "KRW":
            return 1_000_000.0
        if currency == self.currency:
            if verbose:
                return {"balance": self.holdings, "locked": 0.0}
            return self.holdings
        raise AssertionError(f"Unexpected balance currency: {currency}")

    def get_order(self, identifier: str, state: str | None = None):
        if identifier == self.market and state == "wait":
            return copy.deepcopy(self.open_orders)
        if identifier in self.orders:
            return copy.deepcopy(self.orders[identifier])
        return {"state": "cancel"}

    def cancel_order(self, identifier: str):
        self.cancelled.append(identifier)
        self.open_orders = [o for o in self.open_orders if o.get("uuid") != identifier]
        if identifier in self.orders:
            self.orders[identifier]["state"] = "cancel"
        return {"uuid": identifier}

    def buy_market_order(self, market: str, amount: float):
        self.market_buys.append(amount)
        vol = round(amount / self.price, 8)
        self.holdings += vol
        identifier = f"mkt-buy-{len(self.market_buys)}"
        order = {
            "uuid": identifier,
            "state": "done",
            "executed_volume": str(vol),
            "trades": [{"funds": str(amount)}],
            "paid_fee": "0",
        }
        self.orders[identifier] = order
        return {"uuid": identifier}

    def sell_market_order(self, market: str, volume: float):
        self.market_sells.append(volume)
        self.holdings = max(0.0, self.holdings - volume)
        identifier = f"mkt-sell-{len(self.market_sells)}"
        order = {
            "uuid": identifier,
            "state": "done",
            "executed_volume": str(volume),
            "trades": [{"funds": str(volume * self.price)}],
            "paid_fee": "0",
        }
        self.orders[identifier] = order
        return {"uuid": identifier}

    def sell_limit_order(self, market: str, price: float, volume: float):
        self.limit_sells.append(volume)
        identifier = f"limit-sell-{len(self.limit_sells)}"
        order = {"uuid": identifier, "side": "ask", "price": str(price), "volume": str(volume), "state": "wait"}
        self.open_orders.append(order.copy())
        self.orders[identifier] = order
        return {"uuid": identifier}

    def buy_limit_order(self, market: str, price: float, volume: float):
        identifier = f"limit-buy-{len(self.limit_buys) + 1}"
        order = {
            "uuid": identifier, "side": "bid", "price": str(price), "volume": str(volume),
            "state": "wait", "executed_volume": "0", "trades": [],
        }
        self.limit_buys.append(order.copy())
        self.open_orders.append(order.copy())
        self.orders[identifier] = order
        return {"uuid": identifier}

    def settle_buy(self, identifier: str, status: str, trades: list[tuple[float, float]]):
        order = self.orders[identifier]
        executed = sum(volume for volume, _ in trades)
        self.holdings += executed - float(order["executed_volume"])
        order.update(
            state=status,
            executed_volume=str(executed),
            trades=[{"volume": str(v), "price": str(p), "funds": str(v * p)} for v, p in trades],
        )
        if status != "wait":
            self.open_orders = [o for o in self.open_orders if o.get("uuid") != identifier]


class TestSquadExit(unittest.TestCase):
    def run_strategy_tick(
        self,
        exchange: SquadFakeExchange,
        state: dict,
        price: float,
        market: str = "KRW-ETH",
        is_bull: bool = False,
        signal: str = "HOLD"
    ):
        saved = []
        logged = []
        output = io.StringIO()
        ticks = 0

        def get_price(m):
            nonlocal ticks
            ticks += 1
            if ticks > 1:
                raise EndTick()
            return price

        def stop_after_decision(sec):
            raise EndTick()

        def save_state(m, val):
            saved.append(copy.deepcopy(val))

        with (
            patch.multiple(
                main,
                acquire_market_process_lock=lambda m: object(),
                init_db=lambda: None,
                get_upbit_client=lambda: exchange,
                load_strategy_state=lambda m: state,
                save_strategy_state=save_state,
                check_krw_balance_alert=lambda *args, **kwargs: 1_000_000.0,
                send_telegram_alert=lambda msg: None,
                safe_get_current_price=get_price,
                get_hybrid_regime_and_signals=lambda m: {
                    "is_bull": is_bull,
                    "signal": signal,
                    "ma5": price,
                },
                log_real_trade=lambda **trade: logged.append(trade),
            ),
            patch.object(main.time, "sleep", side_effect=stop_after_decision),
            redirect_stdout(output),
        ):
            try:
                main.run_trading_strategy(market)
            except EndTick:
                pass

        return saved, logged, output.getvalue()

    def test_eth_two_squads_progression_multipliers_are_exact_1_1_2_4_1_1_2_4(self):
        """ETH multiple squads generate exact sequential multipliers: 1-1-2-4-1-1-2-4."""
        # Start with empty state: 1st market entry is Squad 1 Stage 1 (1 unit)
        state = {
            "market": "KRW-ETH",
            "current_regime": "BEAR",
            "tranches": [
                {"tranche_id": 1, "step": 1, "step_num": 1, "squad_id": 1, "squad_stage": 1, "units": 1, "buy_price": 100_000.0, "volume": 0.1}
            ],
            "bear_limit_buys": {},
            "bot_quantity": 0.1,
            "bot_avg_price": 100_000.0,
        }

        # 1. First ladder creation: should plan Squad 1 Stages 2, 3, 4 (Units 1, 2, 4)
        orders_1 = calculate_new_buy_prices(avg_buy_price=100_000.0, max_steps=9999, state=state)
        self.assertEqual(3, len(orders_1))
        self.assertEqual([(1, 2, 1), (1, 3, 2), (1, 4, 4)],
                         [(o["squad_id"], o["squad_stage"], o["units"]) for o in orders_1])

        # Register these 3 limit buy orders in state
        for i, o in enumerate(orders_1):
            state.setdefault("bear_limit_buys", {})[f"order-sq1-{o['squad_stage']}"] = {
                "units": o["units"],
                "squad_id": o["squad_id"],
                "squad_stage": o["squad_stage"],
                "state": "wait",
                "executed_volume": 0.0,
                "executed_funds": 0.0,
            }

        # 2. Simulate Squad 1 Stage 2 filling:
        state["bear_limit_buys"]["order-sq1-2"]["state"] = "done"
        state["tranches"].append({
            "tranche_id": 2, "step": 2, "step_num": 2, "squad_id": 1, "squad_stage": 2, "units": 1, "buy_price": 96_000.0, "volume": 0.104
        })
        # Active open buys now: Stage 3 (2U) and Stage 4 (4U). Existing open buys = 2.
        # Next order planned should be Squad 2 Stage 1 (Unit 1)!
        open_orders = [
            {"price": 92_160.0, "units": 2, "squad_id": 1, "squad_stage": 3},
            {"price": 88_473.0, "units": 4, "squad_id": 1, "squad_stage": 4},
        ]
        orders_2 = calculate_new_buy_prices(
            avg_buy_price=98_000.0,
            existing_orders=open_orders,
            max_steps=9999,
            state=state
        )
        self.assertEqual(1, len(orders_2))
        self.assertEqual(2, orders_2[0]["squad_id"])
        self.assertEqual(1, orders_2[0]["squad_stage"])
        self.assertEqual(1, orders_2[0]["units"])

        # Register Squad 2 Stage 1 in state
        state["bear_limit_buys"]["order-sq2-1"] = {
            "units": 1, "squad_id": 2, "squad_stage": 1, "state": "wait", "executed_volume": 0.0, "executed_funds": 0.0
        }

        # 3. Simulate Squad 1 Stage 3 filling:
        state["bear_limit_buys"]["order-sq1-3"]["state"] = "done"
        state["tranches"].append({
            "tranche_id": 3, "step": 3, "step_num": 3, "squad_id": 1, "squad_stage": 3, "units": 2, "buy_price": 92_160.0, "volume": 0.217
        })
        open_orders = [
            {"price": 88_473.0, "units": 4, "squad_id": 1, "squad_stage": 4},
            {"price": 84_934.0, "units": 1, "squad_id": 2, "squad_stage": 1},
        ]
        orders_3 = calculate_new_buy_prices(
            avg_buy_price=95_000.0, existing_orders=open_orders, max_steps=9999, state=state
        )
        self.assertEqual(1, len(orders_3))
        self.assertEqual(2, orders_3[0]["squad_id"])
        self.assertEqual(2, orders_3[0]["squad_stage"])
        self.assertEqual(1, orders_3[0]["units"])

        # Register Squad 2 Stage 2
        state["bear_limit_buys"]["order-sq2-2"] = {
            "units": 1, "squad_id": 2, "squad_stage": 2, "state": "wait", "executed_volume": 0.0, "executed_funds": 0.0
        }

        # 4. Simulate Squad 1 Stage 4 filling:
        state["bear_limit_buys"]["order-sq1-4"]["state"] = "done"
        state["tranches"].append({
            "tranche_id": 4, "step": 4, "step_num": 4, "squad_id": 1, "squad_stage": 4, "units": 4, "buy_price": 88_473.0, "volume": 0.452
        })
        open_orders = [
            {"price": 84_934.0, "units": 1, "squad_id": 2, "squad_stage": 1},
            {"price": 81_536.0, "units": 1, "squad_id": 2, "squad_stage": 2},
        ]
        orders_4 = calculate_new_buy_prices(
            avg_buy_price=92_000.0, existing_orders=open_orders, max_steps=9999, state=state
        )
        self.assertEqual(1, len(orders_4))
        self.assertEqual(2, orders_4[0]["squad_id"])
        self.assertEqual(3, orders_4[0]["squad_stage"])
        self.assertEqual(2, orders_4[0]["units"])

        # Register Squad 2 Stage 3
        state["bear_limit_buys"]["order-sq2-3"] = {
            "units": 2, "squad_id": 2, "squad_stage": 3, "state": "wait", "executed_volume": 0.0, "executed_funds": 0.0
        }

        # 5. Check next order after Squad 2 Stage 3:
        open_orders.append({"price": 78_275.0, "units": 2, "squad_id": 2, "squad_stage": 3})
        # If open orders drops to 2 (e.g. Squad 2 Stage 1 fills):
        open_orders = open_orders[1:]
        orders_5 = calculate_new_buy_prices(
            avg_buy_price=90_000.0, existing_orders=open_orders, max_steps=9999, state=state
        )
        self.assertEqual(1, len(orders_5))
        self.assertEqual(2, orders_5[0]["squad_id"])
        self.assertEqual(4, orders_5[0]["squad_stage"])
        self.assertEqual(4, orders_5[0]["units"])

        # Verify full multiplier sequence across Squad 1 and Squad 2:
        # Squad 1: 1, 1, 2, 4. Squad 2: 1, 1, 2, 4.
        all_planned = get_next_squad_stages([], count=8, max_steps=9999)
        multipliers = [s["units"] for s in all_planned]
        self.assertEqual([1, 1, 2, 4, 1, 1, 2, 4], multipliers)

    def test_squad_one_exit_leaves_squad_two_holdings_and_orders_intact(self):
        """When Squad 1 hits +3% VWAP, sell only Squad 1 tranches and cancel only Squad 1 buys."""
        protected = main.PROTECTED_BALANCES["KRW-ETH"]
        # Squad 1: 2 tranches, total vol 0.2 at 100,000. VWAP = 100,000. Target +3% = 103,000.
        # Squad 2: 1 tranche, vol 0.1 at 120,000. VWAP = 120,000. Target +3% = 123,600.
        # Total bot quantity = 0.3. Avg price = (0.2*100k + 0.1*120k)/0.3 = 106,666.67.
        # Current price = 104,000.
        # Basket target = 106,666.67 * 1.008 = 107,520 (Not reached!).
        # Squad 1 target = 103,000 (REACHED: 104,000 >= 103,000!).
        # Squad 2 target = 123,600 (NOT reached).
        exchange = SquadFakeExchange(protected + 0.3, 104_000.0)
        exchange.orders["basket-old"] = {"uuid": "basket-old", "state": "wait"}
        exchange.orders["sq1-open-buy"] = {"uuid": "sq1-open-buy", "state": "wait", "executed_volume": "0", "trades": []}
        exchange.orders["sq2-open-buy"] = {"uuid": "sq2-open-buy", "state": "wait", "executed_volume": "0", "trades": []}
        exchange.open_orders = [
            {"uuid": "basket-old", "side": "ask", "price": "107520"},
            {"uuid": "sq1-open-buy", "side": "bid", "price": "90000"},
            {"uuid": "sq2-open-buy", "side": "bid", "price": "80000"},
        ]

        state = {
            "market": "KRW-ETH",
            "current_regime": "BEAR",
            "initial_entry_done": True,
            "bot_quantity": 0.3,
            "bot_avg_price": 106_666.67,
            "tranches": [
                {"tranche_id": 1, "step": 1, "squad_id": 1, "squad_stage": 1, "volume": 0.1, "buy_price": 100_000.0},
                {"tranche_id": 2, "step": 2, "squad_id": 1, "squad_stage": 2, "volume": 0.1, "buy_price": 100_000.0},
                {"tranche_id": 3, "step": 3, "squad_id": 2, "squad_stage": 1, "volume": 0.1, "buy_price": 120_000.0},
            ],
            "bear_limit_buys": {
                "sq1-open-buy": {"units": 2, "squad_id": 1, "squad_stage": 3, "state": "wait", "executed_volume": 0.0, "executed_funds": 0.0},
                "sq2-open-buy": {"units": 1, "squad_id": 2, "squad_stage": 2, "state": "wait", "executed_volume": 0.0, "executed_funds": 0.0},
            },
        }

        saved, logged, output = self.run_strategy_tick(exchange, state, 104_000.0)

        # 1. Exchange sales: only 0.2 ETH (Squad 1 volume) sold via market order
        self.assertEqual(1, len(exchange.market_sells))
        self.assertAlmostEqual(0.2, exchange.market_sells[0])

        # 2. Cancelled orders: basket sell and Squad 1 buy cancelled. Squad 2 buy is NOT cancelled!
        self.assertIn("basket-old", exchange.cancelled)
        self.assertIn("sq1-open-buy", exchange.cancelled)
        self.assertNotIn("sq2-open-buy", exchange.cancelled)
        self.assertEqual("cancel", state["bear_limit_buys"]["sq1-open-buy"]["state"])
        self.assertEqual("wait", state["bear_limit_buys"]["sq2-open-buy"]["state"])

        # 3. Squad 2 tranches remain in state
        remaining_tranches = state["tranches"]
        self.assertEqual(1, len(remaining_tranches))
        self.assertEqual(3, remaining_tranches[0]["tranche_id"])
        self.assertEqual(2, remaining_tranches[0]["squad_id"])
        self.assertAlmostEqual(0.1, remaining_tranches[0]["volume"])

        # 4. Remaining position updated to Squad 2 alone
        self.assertAlmostEqual(0.1, state["bot_quantity"])
        self.assertAlmostEqual(120_000.0, state["bot_avg_price"])

        # 5. Trade log records SQUAD_1_TAKE_PROFIT
        self.assertTrue(any("SQUAD_1_TAKE_PROFIT" in t.get("action", "") for t in logged))

    def test_basket_exit_clears_all_squads_completely(self):
        """When basket target is reached, all squads are sold and all orders cancelled."""
        protected = main.PROTECTED_BALANCES["KRW-ETH"]
        exchange = SquadFakeExchange(protected + 0.3, 110_000.0)
        exchange.orders["basket-old"] = {"uuid": "basket-old", "state": "wait"}
        exchange.orders["sq1-open-buy"] = {"uuid": "sq1-open-buy", "state": "wait", "executed_volume": "0", "trades": []}
        exchange.orders["sq2-open-buy"] = {"uuid": "sq2-open-buy", "state": "wait", "executed_volume": "0", "trades": []}
        exchange.open_orders = [
            {"uuid": "basket-old", "side": "ask", "price": "107520"},
            {"uuid": "sq1-open-buy", "side": "bid", "price": "90000"},
            {"uuid": "sq2-open-buy", "side": "bid", "price": "80000"},
        ]

        state = {
            "market": "KRW-ETH",
            "current_regime": "BEAR",
            "initial_entry_done": True,
            "bot_quantity": 0.3,
            "bot_avg_price": 100_000.0,  # Basket target: 100,000 * 1.008 = 100,800. Price is 110,000!
            "tranches": [
                {"tranche_id": 1, "step": 1, "squad_id": 1, "squad_stage": 1, "volume": 0.1, "buy_price": 95_000.0},
                {"tranche_id": 2, "step": 2, "squad_id": 1, "squad_stage": 2, "volume": 0.1, "buy_price": 100_000.0},
                {"tranche_id": 3, "step": 3, "squad_id": 2, "squad_stage": 1, "volume": 0.1, "buy_price": 105_000.0},
            ],
            "bear_limit_buys": {
                "sq1-open-buy": {"units": 2, "squad_id": 1, "squad_stage": 3, "state": "wait", "executed_volume": 0.0, "executed_funds": 0.0},
                "sq2-open-buy": {"units": 1, "squad_id": 2, "squad_stage": 2, "state": "wait", "executed_volume": 0.0, "executed_funds": 0.0},
            },
        }

        saved, logged, output = self.run_strategy_tick(exchange, state, 110_000.0)

        # 1. Total volume (0.3) sold
        self.assertEqual(1, len(exchange.market_sells))
        self.assertAlmostEqual(0.3, exchange.market_sells[0])

        # 2. Both open buys cancelled
        self.assertIn("sq1-open-buy", exchange.cancelled)
        self.assertIn("sq2-open-buy", exchange.cancelled)

        # 3. State position completely reset
        self.assertAlmostEqual(0.0, state["bot_quantity"])
        self.assertEqual([], state["tranches"])
        self.assertTrue(any(t.get("action") == "BASKET_TAKE_PROFIT" for t in logged))

    def test_partial_fill_and_restart_idempotency_preserves_squad_tags(self):
        """UUID cumulative execution credits exact delta and does not double-credit on restart."""
        client = SquadFakeExchange(10.0, 100_000.0)
        state = {
            "market": "KRW-ETH",
            "bot_quantity": 0.1,
            "bot_avg_price": 100_000.0,
            "tranches": [
                {"tranche_id": 1, "step": 1, "squad_id": 1, "squad_stage": 1, "units": 1, "volume": 0.1, "buy_price": 100_000.0}
            ],
            "bear_limit_buys": {
                "order-sq2-stage1": {
                    "units": 1,
                    "squad_id": 2,
                    "squad_stage": 1,
                    "state": "wait",
                    "executed_volume": 0.0,
                    "executed_funds": 0.0,
                }
            }
        }
        client.orders["order-sq2-stage1"] = {
            "uuid": "order-sq2-stage1",
            "state": "wait",
            "executed_volume": "0.05",
            "trades": [{"funds": "4750.0"}],  # 0.05 @ 95,000
        }

        # First reconcile: partial fill of 0.05
        ok = main.reconcile_bear_limit_buys(client, "KRW-ETH", state)
        self.assertTrue(ok)
        self.assertEqual(2, len(state["tranches"]))
        new_tr = state["tranches"][-1]
        self.assertEqual(2, new_tr["squad_id"])
        self.assertEqual(1, new_tr["squad_stage"])
        self.assertAlmostEqual(0.05, new_tr["volume"])
        self.assertAlmostEqual(95_000.0, new_tr["buy_price"])
        self.assertAlmostEqual(0.15, state["bot_quantity"])

        # Second reconcile with same state (restart / no new trades): must NOT double credit
        ok2 = main.reconcile_bear_limit_buys(client, "KRW-ETH", state)
        self.assertTrue(ok2)
        self.assertEqual(2, len(state["tranches"]))
        self.assertAlmostEqual(0.15, state["bot_quantity"])

        # Third reconcile: final fill of remaining 0.05 (total 0.10)
        client.orders["order-sq2-stage1"] = {
            "uuid": "order-sq2-stage1",
            "state": "done",
            "executed_volume": "0.10",
            "trades": [{"funds": "9500.0"}],
        }
        ok3 = main.reconcile_bear_limit_buys(client, "KRW-ETH", state)
        self.assertTrue(ok3)
        self.assertEqual(2, len(state["tranches"]))
        self.assertAlmostEqual(0.10, state["tranches"][-1]["volume"])
        self.assertAlmostEqual(0.20, state["bot_quantity"])
        self.assertEqual("done", state["bear_limit_buys"]["order-sq2-stage1"]["state"])

    def test_sol_strictly_capped_at_four_squads_and_16_stages(self):
        """SOL does not generate any orders beyond 4 squads (16 stages)."""
        # 16 stages (4 squads * 4 stages):
        sol_stages = []
        for sid in range(1, 5):
            for stg in range(1, 5):
                sol_stages.append((sid, stg))
        self.assertEqual(16, len(sol_stages))

        # Check get_next_squad_stages with max_steps=16:
        next_stages = get_next_squad_stages(sol_stages, count=3, max_steps=16)
        self.assertEqual([], next_stages)

        # Check calculate_new_buy_prices for KRW-SOL with 16 stages in state:
        state_sol = {
            "market": "KRW-SOL",
            "current_regime": "BEAR",
            "tranches": [
                {"tranche_id": i + 1, "step": i + 1, "squad_id": sid, "squad_stage": stg, "volume": 0.1, "buy_price": 100_000.0}
                for i, (sid, stg) in enumerate(sol_stages)
            ],
            "bear_limit_buys": {},
        }
        orders = calculate_new_buy_prices(avg_buy_price=100_000.0, max_steps=16, state=state_sol)
        self.assertEqual([], orders)

    def test_legacy_tranches_without_squad_id_are_quarantined_from_squad_exit(self):
        """Legacy tranches without squad_id do not trigger squad exit; they wait for basket exit."""
        legacy_tranches = [
            {"step": 1, "volume": 0.1, "buy_price": 100_000.0},
            {"step": 2, "volume": 0.1, "buy_price": 100_000.0},
        ]
        # Current price 110,000 (+10% > +3%)
        # Overall basket target: 100,000 * 1.008 = 100,800 (let's set avg_buy_price=120,000 so basket is not met)
        decision = check_magic_split_exits(
            tranches=legacy_tranches,
            current_price=110_000.0,
            avg_buy_price=120_000.0,  # Basket target 120,960 not met
            profit_margin=1.008,
            tranche_profit_pct=0.03
        )
        # Because tranches lack squad_id, they are quarantined and no SQUAD exit occurs
        self.assertEqual("NONE", decision["exit_type"])

        # When basket target is met, basket exit triggers:
        decision_basket = check_magic_split_exits(
            tranches=legacy_tranches,
            current_price=122_000.0,
            avg_buy_price=120_000.0,  # 122,000 >= 120,000 * 1.008 = 120,960
            profit_margin=1.008,
            tranche_profit_pct=0.03
        )
        self.assertEqual("BASKET", decision_basket["exit_type"])


if __name__ == "__main__":
    unittest.main()
