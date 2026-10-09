"""Unit and regression tests for AG-04-R1 post-deployment ETH recovery defects.

Validates:
1. R1: Balance lookup failure stops new buys, halts, and prevents risk exposure
   in Case 3, Case 4, and exit-skip basket restoration paths.
2. R2: Decimal round-down (ROUND_DOWN to 8 decimals) strictly bounds safe sell
   quantities to <= avail, <= ledger, <= req, <= max(0, total - protected),
   eliminating IEEE 754 precision overshoot and half-unit upward rounding.
3. R3: Basket ask cancellation preserves canonical verified cumulative execution
   funds (including price-improvement fills) and never overwrites with limit price.
4. Normal squad exit when manual orders lock protected holdings.
5. Real main loop execution for Case 2, Case 3, Case 4, and fail-closed halt on save/API errors.
"""

from __future__ import annotations

import copy
import io
import math
import os
import sys
import tempfile
import time
import types
import unittest
from decimal import Decimal
from unittest.mock import patch, MagicMock

import numpy as np
import pandas as pd

import config
import main
from strategy.hybrid_regime import _CANDLE_CACHE
from utils import db_logger


class EndTick(BaseException):
    """Signal to terminate a bounded single-tick main loop execution."""


def make_sample_candle_df(
    num_bars: int = 220,
    base_price: float = 3_700_000.0,
) -> pd.DataFrame:
    """Generate 220 sequential 1-hour candles for main loop tests."""
    now = time.time()
    dts = [
        pd.Timestamp(now - (num_bars - i) * 3600, unit="s", tz="Asia/Seoul").strftime("%Y-%m-%d %H:%M:%S")
        for i in range(num_bars)
    ]
    closes = [base_price]
    for _ in range(num_bars - 1):
        closes.append(closes[-1] * 1.0005)
    closes = np.array(closes)

    return pd.DataFrame({
        "candle_date_time_kst": dts,
        "open": closes * 0.999,
        "high": closes * 1.002,
        "low": closes * 0.998,
        "close": closes,
        "volume": np.full(num_bars, 50.0),
    })


class RealisticRecoveryExchange:
    """Realistic fake exchange modeling available/locked balance mechanics and trade settlement."""

    def __init__(self, avail: float, locked: float, price: float, market: str = "KRW-ETH"):
        self.market = market
        self.currency = market.split("-")[1]
        self.avail = float(avail)
        self.locked = float(locked)
        self.price = float(price)
        self.krw = 10_000_000.0
        self.open_orders: list[dict] = []
        self.orders: dict[str, dict] = {}
        self.cancelled: list[str] = []
        self.market_sells: list[dict] = []
        self.limit_sells: list[dict] = []
        self.limit_buys: list[dict] = []

        # Failure injection flags
        self.inject_balance_query_fail = False
        self.inject_sell_limit_fail = False
        self.inject_buy_limit_fail = False

    def get_balance(self, currency: str, verbose: bool = False):
        if currency == "KRW":
            return self.krw
        if currency == self.currency:
            if self.inject_balance_query_fail:
                return None
            if verbose:
                return {"balance": self.avail, "locked": self.locked}
            return self.avail
        raise ValueError(f"Unknown currency: {currency}")

    def get_order(self, identifier: str, state: str | None = None):
        if identifier == self.market and state == "wait":
            return copy.deepcopy(self.open_orders)
        if identifier in self.orders:
            return copy.deepcopy(self.orders[identifier])
        return {"uuid": identifier, "state": "cancel", "executed_volume": "0.0", "trades": []}

    def cancel_order(self, identifier: str):
        self.cancelled.append(identifier)
        matched = [o for o in self.open_orders if o.get("uuid") == identifier]
        self.open_orders = [o for o in self.open_orders if o.get("uuid") != identifier]
        if identifier in self.orders:
            ord_entry = self.orders[identifier]
            ord_entry["state"] = "cancel"
            # Unlock remaining unsold volume if it was a limit ask
            if ord_entry.get("side") == "ask":
                total_vol = float(ord_entry.get("volume", 0.0) or 0.0)
                exec_vol = float(ord_entry.get("executed_volume", 0.0) or 0.0)
                rem_vol = max(0.0, total_vol - exec_vol)
                self.locked = max(0.0, round(self.locked - rem_vol, 8))
                self.avail = round(self.avail + rem_vol, 8)
        return {"uuid": identifier}

    def sell_limit_order(self, market: str, price: float, volume: float):
        if self.inject_sell_limit_fail:
            return None
        vol = float(volume)
        self.avail = max(0.0, round(self.avail - vol, 8))
        self.locked = round(self.locked + vol, 8)
        uuid = f"limit-sell-{len(self.limit_sells) + 1}"
        record = {
            "uuid": uuid,
            "side": "ask",
            "ord_type": "limit",
            "price": str(price),
            "volume": str(vol),
            "executed_volume": "0.0",
            "trades": [],
            "state": "wait",
        }
        self.limit_sells.append(record)
        self.open_orders.append(record.copy())
        self.orders[uuid] = record
        return {"uuid": uuid}

    def buy_limit_order(self, market: str, price: float, volume: float):
        if self.inject_buy_limit_fail:
            return None
        vol = float(volume)
        uuid = f"limit-buy-{len(self.limit_buys) + 1}"
        record = {
            "uuid": uuid,
            "side": "bid",
            "ord_type": "limit",
            "price": str(price),
            "volume": str(vol),
            "executed_volume": "0.0",
            "trades": [],
            "state": "wait",
        }
        self.limit_buys.append(record)
        self.open_orders.append(record.copy())
        self.orders[uuid] = record
        return {"uuid": uuid}

    def sell_market_order(self, market: str, volume: float):
        vol = float(volume)
        self.avail = max(0.0, round(self.avail - vol, 8))
        uuid = f"mkt-sell-{len(self.market_sells) + 1}"
        exec_funds = round(vol * self.price, 4)
        record = {
            "uuid": uuid,
            "side": "ask",
            "ord_type": "market",
            "price": str(self.price),
            "volume": str(vol),
            "executed_volume": str(vol),
            "trades": [{"funds": str(exec_funds), "volume": str(vol), "price": str(self.price)}],
            "state": "done",
            "paid_fee": "0",
        }
        self.market_sells.append(record)
        self.orders[uuid] = record
        return {"uuid": uuid}


class TestETHRecoveryDefectFixes(unittest.TestCase):
    def setUp(self):
        _CANDLE_CACHE.clear()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.temp_dir = tmp.name
        self.db_path = os.path.join(self.temp_dir, "test_safety.db")

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

        p_alert = patch.object(main, "send_telegram_alert", lambda msg: None)
        p_alert.start()
        self.addCleanup(p_alert.stop)

        p_req = patch("requests.post", side_effect=AssertionError("Live HTTP prohibited"))
        p_req.start()
        self.addCleanup(p_req.stop)

        self.market = "KRW-ETH"
        self.ticker = "ETH"
        self.protected_eth = config.PROTECTED_BALANCES.get(self.market, 0.40756151)

    def tearDown(self):
        _CANDLE_CACHE.clear()

    def run_single_tick(self, market: str, exchange: RealisticRecoveryExchange, state: dict, price: float):
        """Execute one bounded main loop tick via run_trading_strategy."""
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
            get_upbit_client=lambda: exchange,
            load_strategy_state=lambda m: state,
            save_strategy_state=lambda m, s: True,
            load_trailing_exit_recovery=lambda m: None,
            check_krw_balance_alert=lambda *args, **kwargs: 10_000_000.0,
            safe_get_current_price=next_price,
        ), patch.object(main.time, "sleep", side_effect=bounded_sleep):
            try:
                main.run_trading_strategy(market)
            except EndTick:
                pass

    # =========================================================================
    # R2 / P2: Decimal Round-Down & Strict Sell Cap Tests
    # =========================================================================
    def test_r2_decimal_round_down_half_unit_boundary(self):
        """Half-unit boundary: free ETH 0.100000005 must be floored to 0.10000000, never 0.10000001."""
        state = {"bot_quantity": 0.20}
        client = MagicMock()
        client.get_balance.return_value = {
            "balance": 0.100000005,
            "locked": self.protected_eth
        }

        qty = main.get_safe_sell_quantity(client, self.market, self.ticker, 0.20, state)
        self.assertEqual(0.10000000, qty)
        self.assertLessEqual(qty, 0.100000005)

        # Another half-unit boundary
        client.get_balance.return_value = {
            "balance": 0.100000009,
            "locked": self.protected_eth
        }
        qty9 = main.get_safe_sell_quantity(client, self.market, self.ticker, 0.20, state)
        self.assertEqual(0.10000000, qty9)
        self.assertLessEqual(qty9, 0.100000009)

    def test_r2_decimal_subtraction_representation(self):
        """IEEE 754 precision float subtraction (0.55409789 - 0.40756151) is handled with Decimal."""
        state = {"bot_quantity": 0.20}
        client = MagicMock()
        client.get_balance.return_value = {
            "balance": 0.47768011,
            "locked": 0.07641778  # total = 0.55409789
        }
        qty = main.get_safe_sell_quantity(client, self.market, self.ticker, 0.20, state)
        expected = float((Decimal("0.55409789") - Decimal("0.40756151")).quantize(Decimal("1e-8")))
        self.assertEqual(expected, qty)
        self.assertEqual(0.14653638, qty)

    def test_r2_get_bot_balance_decimal_floor(self):
        """get_bot_balance floors to 8 decimal places and does not round upward."""
        state = {"bot_quantity": 0.20, "bot_avg_price": 3_500_000.0}
        client = MagicMock()
        # total_held - protected = 0.100000005
        client.get_balance.return_value = {
            "balance": 0.100000005,
            "locked": self.protected_eth
        }
        avg_p, safe_q = main.get_bot_balance(client, self.market, self.ticker, state)
        self.assertEqual(0.10000000, safe_q)
        self.assertLessEqual(safe_q, 0.100000005)

    def test_r2_safe_sell_quantity_strict_four_way_bound(self):
        """min(req, ledger, avail, max(0, total - protected)) is strictly observed."""
        state = {"bot_quantity": 0.15}
        client = MagicMock()

        # avail is bottleneck
        client.get_balance.return_value = {"balance": 0.05, "locked": 0.50}
        self.assertEqual(0.05, main.get_safe_sell_quantity(client, self.market, self.ticker, 0.10, state))

        # req is bottleneck
        client.get_balance.return_value = {"balance": 0.45, "locked": 0.10}
        self.assertEqual(0.03, main.get_safe_sell_quantity(client, self.market, self.ticker, 0.03, state))

        # ledger is bottleneck
        state["bot_quantity"] = 0.02
        self.assertEqual(0.02, main.get_safe_sell_quantity(client, self.market, self.ticker, 0.05, state))

        # total <= protected -> 0.0
        client.get_balance.return_value = {"balance": 0.30, "locked": 0.10}
        self.assertEqual(0.0, main.get_safe_sell_quantity(client, self.market, self.ticker, 0.10, state))

    def test_r2_fail_closed_on_invalid_responses(self):
        """Fail-closed on None, negative, NaN, Inf, bool, or exceptions."""
        invalid_cases = [
            None, False, True, "invalid",
            {"balance": None, "locked": 0.0},
            {"balance": -0.1, "locked": 0.0},
            {"balance": 0.5, "locked": -0.1},
            {"balance": float("nan"), "locked": 0.0},
            {"balance": float("inf"), "locked": 0.0},
            {"balance": 0.5, "locked": float("nan")},
            {"balance": False, "locked": 0.0},
            [],
        ]
        for val in invalid_cases:
            state = {"bot_quantity": 0.10}
            client = MagicMock()
            client.get_balance.return_value = val
            qty = main.get_safe_sell_quantity(client, self.market, self.ticker, 0.05, state)
            self.assertEqual(-1.0, qty)
            self.assertTrue(state.get("balance_query_failed"))

    # =========================================================================
    # R3 / P2: Cancellation Canonical Checkpoint Preservation Tests
    # =========================================================================
    def test_r3_cancel_preserves_verified_execution_funds_price_improvement(self):
        """Cancellation with partial fill and price improvement preserves actual trade funds (70,000 KRW, not 68,000 KRW)."""
        db_logger.init_db()
        exchange = RealisticRecoveryExchange(0.10, 0.40756151, 3_500_000.0, self.market)
        ask_uuid = "ask-price-imp-uuid"
        order = {
            "uuid": ask_uuid,
            "side": "ask",
            "price": "3400000",
            "volume": "0.05",
            "executed_volume": "0.02",
            "trades": [
                {"funds": "70000.0", "volume": "0.02", "price": "3500000.0"}
            ],
            "state": "cancel"
        }
        exchange.orders[ask_uuid] = order

        state = {
            "bot_quantity": 0.05,
            "bot_avg_price": 3_200_000.0,
            "basket_limit_ask": {"uuid": ask_uuid, "price": 3400000.0, "volume": 0.05},
            "tranches": [
                {"squad_id": 1, "squad_stage": 1, "volume": 0.05, "buy_price": 3_200_000.0}
            ]
        }

        ok = main.cancel_order_and_wait(exchange, ask_uuid, market=self.market, state=state)
        self.assertTrue(ok)

        # Canonical execution funds must be preserved as 70,000.0 (NOT 68,000.0!)
        rec = state.get("_reconciled_asks", {}).get(ask_uuid)
        self.assertIsNotNone(rec)
        self.assertEqual(0.02, rec["volume"])
        self.assertEqual(70000.0, rec["funds"])

        # Remaining ledger volume is 0.03
        self.assertEqual(0.03, state["bot_quantity"])
        self.assertIsNone(state.get("basket_limit_ask"))

    def test_r3_cancel_zero_fill_stores_zero_funds(self):
        """Zero executed volume cancellation stores volume=0.0, funds=0.0 and clears basket."""
        exchange = RealisticRecoveryExchange(0.10, 0.40756151, 3_400_000.0, self.market)
        ask_uuid = "ask-zero-fill-uuid"
        order = {
            "uuid": ask_uuid,
            "side": "ask",
            "price": "3400000",
            "volume": "0.05",
            "executed_volume": "0.0",
            "trades": [],
            "state": "cancel"
        }
        exchange.orders[ask_uuid] = order

        state = {
            "bot_quantity": 0.05,
            "bot_avg_price": 3_200_000.0,
            "basket_limit_ask": {"uuid": ask_uuid, "price": 3400000.0, "volume": 0.05},
            "tranches": [
                {"squad_id": 1, "squad_stage": 1, "volume": 0.05, "buy_price": 3_200_000.0}
            ]
        }

        ok = main.cancel_order_and_wait(exchange, ask_uuid, market=self.market, state=state)
        self.assertTrue(ok)
        rec = state.get("_reconciled_asks", {}).get(ask_uuid)
        self.assertEqual({"volume": 0.0, "funds": 0.0}, rec)
        self.assertIsNone(state.get("basket_limit_ask"))

    # =========================================================================
    # R1 / P1: Main Loop Real Surface Tests (Case 3, Case 4, Exit Skip)
    # =========================================================================
    def test_r1_case3_balance_lookup_failure_stops_new_buys_and_halts_main_loop(self):
        """Codex R1 Reproduction: Case 3에서 바스켓 매도 잔고 조회 실패 시 신규 매수 0건 및 trading_halted=True."""
        price = 3_700_000.0
        good_df = make_sample_candle_df(220, base_price=price)
        _CANDLE_CACHE[self.market] = {"df": good_df, "timestamp": time.time()}

        exchange = RealisticRecoveryExchange(
            avail=0.50756151,  # 0.1 ETH free + 0.40756151 protected
            locked=0.0,
            price=price,
            market=self.market
        )

        state = {
            "market": self.market,
            "current_regime": "BEAR",
            "initial_entry_done": True,
            "strategy_engine": "ADAPTIVE_GRID_C_E",
            "bot_quantity": 0.10,
            "bot_avg_price": 4_000_000.0,
            "trading_halted": False,
            "tranches": [
                {
                    "tranche_id": 1, "step": 1, "squad_id": 1, "squad_stage": 1,
                    "units": 1, "volume": 0.10, "buy_price": 4_000_000.0,
                }
            ],
            "bear_limit_buys": {},
            "basket_limit_ask": None,
        }

        # Inject balance failure specifically when get_safe_sell_quantity queries balance
        original_get_balance = exchange.get_balance
        call_count = 0

        def failing_balance_query(currency, verbose=False):
            nonlocal call_count
            call_count += 1
            # First call is get_bot_balance in Case 3 (succeeds)
            # Second call is get_safe_sell_quantity in Case 3 (fails!)
            if call_count >= 2 and currency == "ETH":
                return None
            return original_get_balance(currency, verbose=verbose)

        exchange.get_balance = failing_balance_query

        self.run_single_tick(self.market, exchange, state, price)

        # MUST halt, and MUST NOT place any new bids or asks!
        self.assertTrue(state.get("trading_halted"), "Bot must halt on balance query failure")
        self.assertIn("잔고 조회 미확정", state.get("halt_reason", ""))
        self.assertEqual(0, len(exchange.limit_buys), "Must place 0 new bids")
        self.assertEqual(0, len(exchange.limit_sells), "Must place 0 basket asks")

    def test_r1_case3_basket_order_submission_failure_stops_new_buys_and_halts_main_loop(self):
        """Case 3에서 바스켓 매도 주문 등록 실패 시 추가 매수 없이 즉시 중단."""
        price = 3_700_000.0
        good_df = make_sample_candle_df(220, base_price=price)
        _CANDLE_CACHE[self.market] = {"df": good_df, "timestamp": time.time()}

        exchange = RealisticRecoveryExchange(
            avail=0.50756151,
            locked=0.0,
            price=price,
            market=self.market
        )
        exchange.inject_sell_limit_fail = True  # Basket ask returns None

        state = {
            "market": self.market,
            "current_regime": "BEAR",
            "initial_entry_done": True,
            "strategy_engine": "ADAPTIVE_GRID_C_E",
            "bot_quantity": 0.10,
            "bot_avg_price": 4_000_000.0,
            "trading_halted": False,
            "tranches": [
                {
                    "tranche_id": 1, "step": 1, "squad_id": 1, "squad_stage": 1,
                    "units": 1, "volume": 0.10, "buy_price": 4_000_000.0,
                }
            ],
            "bear_limit_buys": {},
            "basket_limit_ask": None,
        }

        self.run_single_tick(self.market, exchange, state, price)

        self.assertTrue(state.get("trading_halted"))
        self.assertIn("바스켓 매도 주문 등록 실패", state.get("halt_reason", ""))
        self.assertEqual(0, len(exchange.limit_buys), "Must place 0 new bids when basket ask fails")

    def test_r1_case4_balance_lookup_failure_stops_additional_buys_and_halts_main_loop(self):
        """Case 4에서 물타기 매수 체결 후 새 바스켓 매도 잔고 조회 실패 시 추가 물타기 매수 차단 및 중단."""
        price = 3_500_000.0
        good_df = make_sample_candle_df(220, base_price=price)
        _CANDLE_CACHE[self.market] = {"df": good_df, "timestamp": time.time()}

        exchange = RealisticRecoveryExchange(
            avail=0.10,
            locked=0.40756151 + 0.10,
            price=price,
            market=self.market
        )

        old_basket_uuid = "case4-old-basket"
        basket_order = {
            "uuid": old_basket_uuid,
            "side": "ask",
            "price": "3800000",
            "volume": "0.10",
            "executed_volume": "0.0",
            "trades": [],
            "state": "wait"
        }
        exchange.open_orders = [basket_order.copy()]
        exchange.orders = {old_basket_uuid: basket_order.copy()}

        state = {
            "market": self.market,
            "current_regime": "BEAR",
            "initial_entry_done": True,
            "strategy_engine": "ADAPTIVE_GRID_C_E",
            "bot_quantity": 0.20,
            "bot_avg_price": 3_800_000.0,
            "trading_halted": False,
            "bear_buy_rebalance_pending": True,
            "basket_limit_ask": {"uuid": old_basket_uuid, "price": 3800000.0, "volume": 0.10},
            "tranches": [
                {"squad_id": 1, "squad_stage": 1, "volume": 0.10, "buy_price": 4_000_000.0},
                {"squad_id": 1, "squad_stage": 2, "volume": 0.10, "buy_price": 3_600_000.0},
            ],
            "bear_limit_buys": {},
        }

        # Inject balance failure during get_safe_sell_quantity in Case 4
        original_get_balance = exchange.get_balance
        call_count = 0

        def failing_balance_query(currency, verbose=False):
            nonlocal call_count
            call_count += 1
            if call_count >= 3 and currency == "ETH":
                return None
            return original_get_balance(currency, verbose=verbose)

        exchange.get_balance = failing_balance_query

        self.run_single_tick(self.market, exchange, state, price)

        self.assertTrue(state.get("trading_halted"))
        self.assertEqual(0, len(exchange.limit_buys), "Must place 0 additional bids")

    def test_r1_squad_exit_skip_basket_restore_balance_query_failure_halts_main_loop(self):
        """스쿼드 익절 스킵 후 바스켓 복구 시 잔고 조회 실패하면 추가 매수 없이 즉시 중단."""
        price = 3_450_000.0  # Squad 1 buy_price 3,300,000 -> +4.5% gain triggers SQUAD exit
        good_df = make_sample_candle_df(220, base_price=price)
        _CANDLE_CACHE[self.market] = {"df": good_df, "timestamp": time.time()}

        exchange = RealisticRecoveryExchange(
            avail=0.10,
            locked=self.protected_eth,
            price=price,
            market=self.market
        )

        basket_uuid = "squad-skip-basket"
        basket_order = {
            "uuid": basket_uuid,
            "side": "ask",
            "price": "3900000",
            "volume": "0.10",
            "executed_volume": "0.0",
            "trades": [],
            "state": "wait"
        }
        exchange.open_orders = [basket_order.copy()]
        exchange.orders = {basket_uuid: basket_order.copy()}

        state = {
            "market": self.market,
            "current_regime": "BEAR",
            "initial_entry_done": True,
            "strategy_engine": "ADAPTIVE_GRID_C_E",
            "bot_quantity": 0.10,
            "bot_avg_price": 3_800_000.0,
            "trading_halted": False,
            "basket_limit_ask": {"uuid": basket_uuid, "price": 3900000.0, "volume": 0.10},
            "tranches": [
                {"squad_id": 1, "squad_stage": 1, "volume": 0.02, "buy_price": 3_300_000.0},
                {"squad_id": 2, "squad_stage": 1, "volume": 0.08, "buy_price": 3_805_000.0},
            ],
            "bear_limit_buys": {},
        }

        # Sequence of get_balance calls:
        # 1. line 1795: get_bot_balance (normal)
        # 2. line 1910: get_bot_balance after cancellations (normal)
        # 3. line 1913: get_safe_sell_quantity before market sell (returns 0.0 avail so tot_sell_vol=0)
        # 4. line 1928: get_safe_sell_quantity during basket restore (returns None -> lookup failure!)
        original_get_balance = exchange.get_balance
        call_count = 0

        def controlled_get_balance(currency, verbose=False):
            nonlocal call_count
            if currency == "ETH":
                call_count += 1
                if call_count == 3:
                    if verbose:
                        return {"balance": 0.0, "locked": self.protected_eth}
                    return 0.0
                elif call_count >= 4:
                    return None
            return original_get_balance(currency, verbose=verbose)

        exchange.get_balance = controlled_get_balance

        self.run_single_tick(self.market, exchange, state, price)

        self.assertTrue(state.get("trading_halted"))
        self.assertIn("스쿼드 익절 스킵 후 바스켓 복구 전 잔고 조회 미확정", state.get("halt_reason", ""))
        self.assertEqual(0, len(exchange.limit_buys))

    def test_r1_squad_exit_skip_basket_restore_order_submission_failure_halts_main_loop(self):
        """스쿼드 익절 스킵 후 바스켓 복구 주문 등록 실패 시 즉시 중단."""
        price = 3_450_000.0
        good_df = make_sample_candle_df(220, base_price=price)
        _CANDLE_CACHE[self.market] = {"df": good_df, "timestamp": time.time()}

        exchange = RealisticRecoveryExchange(
            avail=0.10,
            locked=self.protected_eth,
            price=price,
            market=self.market
        )
        exchange.inject_sell_limit_fail = True

        basket_uuid = "squad-skip-order-fail"
        basket_order = {
            "uuid": basket_uuid,
            "side": "ask",
            "price": "3900000",
            "volume": "0.10",
            "executed_volume": "0.0",
            "trades": [],
            "state": "wait"
        }
        exchange.open_orders = [basket_order.copy()]
        exchange.orders = {basket_uuid: basket_order.copy()}

        state = {
            "market": self.market,
            "current_regime": "BEAR",
            "initial_entry_done": True,
            "strategy_engine": "ADAPTIVE_GRID_C_E",
            "bot_quantity": 0.10,
            "bot_avg_price": 3_800_000.0,
            "trading_halted": False,
            "basket_limit_ask": {"uuid": basket_uuid, "price": 3900000.0, "volume": 0.10},
            "tranches": [
                {"squad_id": 1, "squad_stage": 1, "volume": 0.02, "buy_price": 3_300_000.0},
                {"squad_id": 2, "squad_stage": 1, "volume": 0.08, "buy_price": 3_805_000.0},
            ],
            "bear_limit_buys": {},
        }

        original_get_balance = exchange.get_balance
        call_count = 0

        def controlled_get_balance(currency, verbose=False):
            nonlocal call_count
            if currency == "ETH":
                call_count += 1
                if call_count == 3:
                    if verbose:
                        return {"balance": 0.0, "locked": self.protected_eth}
                    return 0.0
            return original_get_balance(currency, verbose=verbose)

        exchange.get_balance = controlled_get_balance

        self.run_single_tick(self.market, exchange, state, price)

        self.assertTrue(state.get("trading_halted"))
        self.assertIn("스쿼드 익절 스킵 후 바스켓 복구 주문 등록 실패", state.get("halt_reason", ""))
        self.assertEqual(0, len(exchange.limit_buys))

    # =========================================================================
    # Case 2 Main Loop Tests (Executing lines 2198-2237 in run_trading_strategy)
    # =========================================================================
    def test_case2_cancelled_basket_ask_partial_fill_reconciled_in_main_loop(self):
        """Case 2: 거래소에 매도 주문이 없고 basket_limit_ask가 cancel+부분체결 상태일 때 main loop 정산."""
        price = 3_400_000.0
        good_df = make_sample_candle_df(220, base_price=price)
        _CANDLE_CACHE[self.market] = {"df": good_df, "timestamp": time.time()}

        exchange = RealisticRecoveryExchange(
            avail=0.03,
            locked=self.protected_eth,
            price=price,
            market=self.market
        )

        ask_uuid = "case2-cancel-part-uuid"
        order = {
            "uuid": ask_uuid,
            "side": "ask",
            "price": "3400000",
            "volume": "0.05",
            "executed_volume": "0.02",
            "trades": [{"funds": "70000.0", "volume": "0.02", "price": "3500000.0"}],
            "state": "cancel"
        }
        exchange.orders[ask_uuid] = order
        # No open sell orders on exchange (num_sell == 0)
        exchange.open_orders = []

        state = {
            "market": self.market,
            "current_regime": "BEAR",
            "initial_entry_done": True,
            "strategy_engine": "ADAPTIVE_GRID_C_E",
            "bot_quantity": 0.05,
            "bot_avg_price": 3_800_000.0,
            "completed_cycles": 0,
            "basket_limit_ask": {"uuid": ask_uuid, "price": 4000000.0, "volume": 0.05},
            "tranches": [
                {"squad_id": 1, "squad_stage": 1, "volume": 0.05, "buy_price": 3_800_000.0}
            ],
            "bear_limit_buys": {},
        }

        self.run_single_tick(self.market, exchange, state, price)

        # Case 2 reconciles the partial fill and pops basket_limit_ask
        self.assertIsNone(state.get("basket_limit_ask"))
        rec = state.get("_reconciled_asks", {}).get(ask_uuid)
        self.assertIsNotNone(rec)
        self.assertEqual(0.02, rec["volume"])
        self.assertEqual(70000.0, rec["funds"])
        self.assertEqual(0.03, state["bot_quantity"])

    def test_case2_cancelled_basket_ask_zero_fill_in_main_loop(self):
        """Case 2: cancel+체결수량 0일 때 main loop에서 정산 기록 후 basket_limit_ask 제거."""
        price = 3_400_000.0
        good_df = make_sample_candle_df(220, base_price=price)
        _CANDLE_CACHE[self.market] = {"df": good_df, "timestamp": time.time()}

        exchange = RealisticRecoveryExchange(
            avail=0.05,
            locked=self.protected_eth,
            price=price,
            market=self.market
        )

        ask_uuid = "case2-cancel-zero-uuid"
        order = {
            "uuid": ask_uuid,
            "side": "ask",
            "price": "3400000",
            "volume": "0.05",
            "executed_volume": "0.0",
            "trades": [],
            "state": "cancel"
        }
        exchange.orders[ask_uuid] = order
        exchange.open_orders = []

        state = {
            "market": self.market,
            "current_regime": "BEAR",
            "initial_entry_done": True,
            "strategy_engine": "ADAPTIVE_GRID_C_E",
            "bot_quantity": 0.05,
            "bot_avg_price": 3_800_000.0,
            "basket_limit_ask": {"uuid": ask_uuid, "price": 4000000.0, "volume": 0.05},
            "tranches": [
                {"squad_id": 1, "squad_stage": 1, "volume": 0.05, "buy_price": 3_800_000.0}
            ],
            "bear_limit_buys": {},
        }

        self.run_single_tick(self.market, exchange, state, price)

        self.assertIsNone(state.get("basket_limit_ask"))
        self.assertEqual({"volume": 0.0, "funds": 0.0}, state["_reconciled_asks"][ask_uuid])

    # =========================================================================
    # Squad Exit with Locked Manual Orders & State Save Failure Tests
    # =========================================================================
    def test_squad_exit_with_protected_locked_orders(self):
        """보호 물량 일부(0.07641778)가 수동 주문에 locked여도 가용량으로 정상 스쿼드 매도."""
        exchange = RealisticRecoveryExchange(
            avail=0.47768011,
            locked=0.07641778,
            price=3_450_000.0,
            market=self.market
        )

        manual_ask = {"uuid": "manual-ask-1", "side": "ask", "price": "5000000", "volume": "0.07641778", "state": "wait"}
        bot_basket_ask = {"uuid": "bot-basket-1", "side": "ask", "price": "3400000", "volume": "0.14653638", "state": "wait"}
        bot_bid_1 = {"uuid": "bot-bid-1", "side": "bid", "price": "3100000", "volume": "0.03", "state": "wait"}

        exchange.open_orders = [manual_ask.copy(), bot_basket_ask.copy(), bot_bid_1.copy()]
        exchange.orders = {
            "manual-ask-1": manual_ask.copy(),
            "bot-basket-1": bot_basket_ask.copy(),
            "bot-bid-1": bot_bid_1.copy(),
        }

        state = {
            "bot_quantity": 0.14653638,
            "bot_avg_price": 3_250_000.0,
            "basket_limit_ask": {"uuid": "bot-basket-1", "price": 3400000.0, "volume": 0.14653638},
            "bear_limit_buys": {
                "bot-bid-1": {"units": 1, "squad_id": 6, "squad_stage": 2, "state": "wait", "executed_volume": 0.0, "executed_funds": 0.0}
            },
            "tranches": [
                {"squad_id": 1, "squad_stage": 1, "volume": 0.02653638, "buy_price": 3_200_000.0},
                {"squad_id": 2, "squad_stage": 1, "volume": 0.02, "buy_price": 3_220_000.0},
                {"squad_id": 3, "squad_stage": 1, "volume": 0.02, "buy_price": 3_240_000.0},
                {"squad_id": 4, "squad_stage": 1, "volume": 0.02, "buy_price": 3_260_000.0},
                {"squad_id": 5, "squad_stage": 1, "volume": 0.02, "buy_price": 3_280_000.0},
                {"squad_id": 6, "squad_stage": 1, "volume": 0.02, "buy_price": 3_300_000.0},
            ]
        }

        safe_qty = main.get_safe_sell_quantity(exchange, self.market, self.ticker, 0.02, state)
        self.assertEqual(0.02, safe_qty)

        self.assertTrue(main.cancel_order_and_wait(exchange, "bot-basket-1", market=self.market, state=state))
        self.assertIsNone(state.get("basket_limit_ask"))

        sell_res = main.submit_market_order_and_wait(exchange, self.market, "ask", safe_qty)
        self.assertEqual("FILLED", sell_res.get("status"))
        self.assertEqual(0.02, float(sell_res.get("volume")))

        self.assertIn("manual-ask-1", [o["uuid"] for o in exchange.open_orders])
        self.assertNotIn("manual-ask-1", exchange.cancelled)

    def test_cancel_order_fails_closed_when_state_save_fails(self):
        """cancel_order_and_wait에서 상태 저장이 실패하면 False를 반환하여 fail-closed 유지."""
        exchange = RealisticRecoveryExchange(0.10, 0.40756151, 3_300_000.0, self.market)
        order_uuid = "ask-save-fail"
        order = {"uuid": order_uuid, "side": "ask", "price": "3400000", "volume": "0.05", "state": "wait"}
        exchange.open_orders = [order.copy()]
        exchange.orders = {order_uuid: order.copy()}

        state = {
            "basket_limit_ask": {"uuid": order_uuid, "price": 3400000.0, "volume": 0.05}
        }

        with patch.object(main, "save_strategy_state", return_value=False):
            ok = main.cancel_order_and_wait(exchange, order_uuid, market=self.market, state=state)
            self.assertFalse(ok)

    def test_case2_partial_fill_reconciliation_failure_halts_main_loop(self):
        """Case 2에서 부분체결 정산 실패 시 halt_for_order_reconciliation 및 거래 중단."""
        price = 3_400_000.0
        good_df = make_sample_candle_df(220, base_price=price)
        _CANDLE_CACHE[self.market] = {"df": good_df, "timestamp": time.time()}

        exchange = RealisticRecoveryExchange(0.05, self.protected_eth, price, self.market)
        ask_uuid = "fail-part-ask"
        order = {
            "uuid": ask_uuid,
            "side": "ask",
            "price": "3400000",
            "volume": "0.05",
            "executed_volume": "0.02",
            "trades": [{"funds": "68000.0", "volume": "0.02", "price": "3400000.0"}],
            "state": "cancel"
        }
        exchange.orders[ask_uuid] = order
        exchange.open_orders = []

        state = {
            "market": self.market,
            "current_regime": "BEAR",
            "initial_entry_done": True,
            "strategy_engine": "ADAPTIVE_GRID_C_E",
            "bot_quantity": 0.05,
            "bot_avg_price": 3_800_000.0,
            "basket_limit_ask": {"uuid": ask_uuid, "price": 4000000.0, "volume": 0.05},
            "tranches": [{"squad_id": 1, "squad_stage": 1, "volume": 0.05, "buy_price": 3_800_000.0}],
            "bear_limit_buys": {},
        }

        with patch.object(main, "_reconcile_ask_order_fill", return_value=False):
            self.run_single_tick(self.market, exchange, state, price)

        self.assertTrue(state.get("trading_halted"))
        self.assertIn("취소 매도 주문 부분체결 정산 실패", state.get("halt_reason", ""))

    def test_reconcile_ask_fill_prevents_duplicate_deduction(self):
        """동일 ask 주문에 대해 _reconcile_ask_order_fill 반복 호출 시 중복 차감 방지."""
        exchange = RealisticRecoveryExchange(0.10, self.protected_eth, 3_300_000.0, self.market)
        order = {
            "uuid": "ask-dup-test",
            "state": "cancel",
            "price": "3400000",
            "executed_volume": "0.02",
            "trades": [{"funds": "68000.0", "volume": "0.02", "price": "3400000.0"}]
        }

        state = {
            "bot_quantity": 0.05,
            "bot_avg_price": 3_200_000.0,
            "tranches": [
                {"squad_id": 1, "squad_stage": 1, "volume": 0.05, "buy_price": 3_200_000.0}
            ]
        }

        with patch.object(main, "_get_db_cumulative_trades", return_value=(0.0, 0.0)), \
             patch.object(main, "log_real_trade", return_value=None), \
             patch.object(main, "save_strategy_state", return_value=True):
            ok1 = main._reconcile_ask_order_fill(exchange, self.market, state, order, 0.02, action="LIMIT_ASK_CANCEL_FILL")
            self.assertTrue(ok1)
            self.assertEqual(0.03, state["bot_quantity"])
            self.assertEqual(0.03, state["tranches"][0]["volume"])

        with patch.object(main, "_get_db_cumulative_trades", return_value=(0.02, 68000.0)), \
             patch.object(main, "log_real_trade", return_value=None), \
             patch.object(main, "save_strategy_state", return_value=True):
            ok2 = main._reconcile_ask_order_fill(exchange, self.market, state, order, 0.02, action="LIMIT_ASK_CANCEL_FILL")
            self.assertTrue(ok2)
            self.assertEqual(0.03, state["bot_quantity"])
            self.assertEqual(0.03, state["tranches"][0]["volume"])


if __name__ == "__main__":
    unittest.main()
