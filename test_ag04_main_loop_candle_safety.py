"""AG-04-R1 Main Loop Candle Safety and Fail-Closed Integration Regression Tests.

Verifies:
1. When candle cache timestamps are corrupted or unparseable, main loop places 0 new buy orders (re-registration).
2. When direct/fallback candle fetch fails (no cache, API failure), main loop places 0 new buy orders.
3. When candles are invalid, initial entry (1st unit market buy) is completely blocked (0 market buys).
4. When candles are invalid, replenishment path (Case 4) places 0 additional limit bids and preserves pending flag.
5. When candles are valid, PURE_MARTINGALE cycles normally and places all 3 adaptive limit bids.
6. When candles are valid, replenishment path (Case 4) places the missing limit bid below the lowest bid.
7. Existing fill reconciliation (reconcile_bear_limit_buys) and position ledgers remain preserved
   even when candle validation fails.
"""

from __future__ import annotations

import copy
import io
import os
import sys
import tempfile
import time
import types
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import numpy as np
import pandas as pd

import main
from strategy.hybrid_regime import _CANDLE_CACHE
from strategy.adaptive_grid import DEFAULT_STEP_SCALE
from test_p1_squad_exit import EndTick, SquadFakeExchange
from utils import db_logger


def make_sample_candle_df(
    num_bars: int = 220,
    base_price: float = 3_700_000.0,
    corrupt_last: bool = False
) -> pd.DataFrame:
    """Generate 220 sequential 1-hour candles with candle_date_time_kst column."""
    now = time.time()
    # 220 hours ago up to now (each hour)
    dts = [
        pd.Timestamp(now - (num_bars - i) * 3600, unit="s", tz="Asia/Seoul").strftime("%Y-%m-%d %H:%M:%S")
        for i in range(num_bars)
    ]
    if corrupt_last:
        dts[-1] = "invalid_timestamp_corrupted"

    closes = [base_price]
    for _ in range(num_bars - 1):
        closes.append(closes[-1] * 1.0005)
    closes = np.array(closes)

    df = pd.DataFrame({
        "candle_date_time_kst": dts,
        "open": closes * 0.999,
        "high": closes * 1.002,
        "low": closes * 0.998,
        "close": closes,
        "volume": np.full(num_bars, 50.0)
    })
    return df


class TestAG04MainLoopCandleSafety(unittest.TestCase):
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

        p_req = patch("requests.post", side_effect=AssertionError("live HTTP forbidden"))
        p_req.start()
        self.addCleanup(p_req.stop)

    def tearDown(self):
        _CANDLE_CACHE.clear()

    def run_single_tick(self, market: str, exchange: SquadFakeExchange, state: dict, price: float):
        """Execute exactly one main loop tick and raise EndTick to cleanly exit."""
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
            check_krw_balance_alert=lambda *args, **kwargs: 1_000_000.0,
            safe_get_current_price=next_price,
        ), patch.object(main.time, "sleep", side_effect=bounded_sleep), redirect_stdout(output):
            try:
                main.run_trading_strategy(market)
            except EndTick:
                pass
        return output.getvalue()

    def test_corrupted_cache_timestamp_results_in_zero_new_buys_re_registration(self):
        """Codex P1 Reproduction: 잘못된 캐시 timestamp가 있으면 재등록 경로에서 신규 매수 0건이어야 합니다."""
        market = "KRW-ETH"
        price = 3_700_000.0
        prot = main.PROTECTED_BALANCES[market]

        # Populate cache with corrupted timestamps
        bad_df = make_sample_candle_df(220, base_price=price, corrupt_last=True)
        _CANDLE_CACHE[market] = {"df": bad_df, "timestamp": time.time()}

        state = {
            "market": market,
            "current_regime": "BEAR",
            "initial_entry_done": True,
            "strategy_engine": "ADAPTIVE_GRID_C_E",
            "bot_quantity": 0.10,
            "bot_avg_price": 4_000_000.0,
            "tranches": [
                {
                    "tranche_id": 1, "step": 1, "squad_id": 1, "squad_stage": 1,
                    "units": 1, "volume": 0.10, "buy_price": 4_000_000.0,
                }
            ],
            "bear_limit_buys": {},
            "basket_limit_ask": None,
        }

        ex = SquadFakeExchange(prot + 0.10, price, market=market)
        self.run_single_tick(market, ex, state, price)

        # 1. 신규 매수 주문 0건 보장
        self.assertEqual(0, len(ex.limit_buys), f"Expected 0 limit buys on corrupted cache, got {len(ex.limit_buys)}")
        self.assertEqual(0, len(ex.market_buys), f"Expected 0 market buys on corrupted cache, got {len(ex.market_buys)}")
        # 2. 바스켓 익절 매도는 정상 등록되어 기존 보유분 청산 감시 유지
        self.assertEqual(1, len(ex.limit_sells))
        # 3. 캔들 검증 실패로 매수 재등록 대기 플래그가 보존되어 캔들 정상 복구 시 재시도 가능
        self.assertTrue(state.get("bear_buy_rebalance_pending", False))

    def test_direct_or_fallback_fetch_failure_results_in_zero_new_buys(self):
        """직접/fallback 조회 실패 시(캐시 부재 및 API 오류) 신규 매수 0건이어야 합니다."""
        market = "KRW-ETH"
        price = 3_700_000.0
        prot = main.PROTECTED_BALANCES[market]

        # No cache
        _CANDLE_CACHE.clear()

        state = {
            "market": market,
            "current_regime": "BEAR",
            "initial_entry_done": True,
            "strategy_engine": "ADAPTIVE_GRID_C_E",
            "bot_quantity": 0.10,
            "bot_avg_price": 4_000_000.0,
            "tranches": [
                {
                    "tranche_id": 1, "step": 1, "squad_id": 1, "squad_stage": 1,
                    "units": 1, "volume": 0.10, "buy_price": 4_000_000.0,
                }
            ],
            "bear_limit_buys": {},
            "basket_limit_ask": None,
        }

        ex = SquadFakeExchange(prot + 0.10, price, market=market)
        # Mock pyupbit.get_ohlcv to simulate network failure (returns None)
        with patch("pyupbit.get_ohlcv", return_value=None):
            self.run_single_tick(market, ex, state, price)

        self.assertEqual(0, len(ex.limit_buys), f"Expected 0 limit buys on fetch failure, got {len(ex.limit_buys)}")
        self.assertEqual(0, len(ex.market_buys), f"Expected 0 market buys on fetch failure, got {len(ex.market_buys)}")
        self.assertTrue(state.get("bear_buy_rebalance_pending", False))

    def test_invalid_candles_prevent_initial_market_entry(self):
        """최초 진입 경로: 캔들 검증 실패 시 PURE_MARTINGALE 1차수 시장가 매수가 차단(0건)되어야 합니다."""
        market = "KRW-ETH"
        price = 3_700_000.0
        prot = main.PROTECTED_BALANCES[market]

        bad_df = make_sample_candle_df(220, base_price=price, corrupt_last=True)
        _CANDLE_CACHE[market] = {"df": bad_df, "timestamp": time.time()}

        state = {
            "market": market,
            "current_regime": "BEAR",
            "initial_entry_done": False,
            "strategy_engine": "ADAPTIVE_GRID_C_E",
            "bot_quantity": 0.0,
            "bot_avg_price": 0.0,
            "tranches": [],
            "bear_limit_buys": {},
            "basket_limit_ask": None,
        }

        # 0 bot quantity (only protected holdings)
        ex = SquadFakeExchange(prot, price, market=market)
        self.run_single_tick(market, ex, state, price)

        self.assertEqual(0, len(ex.market_buys), f"Initial entry must be blocked (0 buys), got {len(ex.market_buys)}")
        self.assertEqual(0, len(ex.limit_buys))

    def test_invalid_candles_prevent_replenishment_limit_buys(self):
        """보충 경로(Case 4): 캔들 검증 실패 시 추가 물타기 매수 주문 0건 및 pending 유지."""
        market = "KRW-ETH"
        price = 3_700_000.0
        prot = main.PROTECTED_BALANCES[market]

        bad_df = make_sample_candle_df(220, base_price=price, corrupt_last=True)
        _CANDLE_CACHE[market] = {"df": bad_df, "timestamp": time.time()}

        state = {
            "market": market,
            "current_regime": "BEAR",
            "initial_entry_done": True,
            "strategy_engine": "ADAPTIVE_GRID_C_E",
            "bot_quantity": 0.15,
            "bot_avg_price": 3_900_000.0,
            "bear_buy_rebalance_pending": True,
            "tranches": [
                {"tranche_id": 1, "step": 1, "squad_id": 1, "squad_stage": 1, "units": 1, "volume": 0.10, "buy_price": 4_000_000.0},
                {"tranche_id": 2, "step": 2, "squad_id": 1, "squad_stage": 2, "units": 1, "volume": 0.05, "buy_price": 3_700_000.0},
            ],
            "bear_limit_buys": {
                "bid-stg3": {"state": "wait", "units": 2, "squad_id": 1, "squad_stage": 3, "executed_volume": 0.0, "executed_funds": 0.0},
                "bid-stg4": {"state": "wait", "units": 4, "squad_id": 1, "squad_stage": 4, "executed_volume": 0.0, "executed_funds": 0.0},
            },
            "basket_limit_ask": {"uuid": "old-ask", "price": 4_000_000.0, "volume": 0.15},
        }

        ex = SquadFakeExchange(prot + 0.15, price, market=market)
        # 1 sell and 2 buys open on exchange
        ex.orders["old-ask"] = {"uuid": "old-ask", "side": "ask", "price": "4000000", "volume": "0.15", "state": "wait"}
        ex.open_orders.append(ex.orders["old-ask"].copy())
        for u in ("bid-stg3", "bid-stg4"):
            o = {"uuid": u, "side": "bid", "price": "3500000", "volume": "0.01", "state": "wait", "executed_volume": "0"}
            ex.orders[u] = o
            ex.open_orders.append(o.copy())

        self.run_single_tick(market, ex, state, price)

        # Additional buys must be 0
        self.assertEqual(0, len(ex.limit_buys), f"Expected 0 limit buys in replenishment, got {len(ex.limit_buys)}")
        self.assertTrue(state.get("bear_buy_rebalance_pending", False))

    def test_valid_candles_execute_normal_pure_martingale_bids(self):
        """정상 캔들 데이터가 제공되면 PURE_MARTINGALE 순환 매수가 정상적으로 3개 호가를 등록해야 합니다."""
        market = "KRW-ETH"
        price = 3_700_000.0
        prot = main.PROTECTED_BALANCES[market]

        good_df = make_sample_candle_df(220, base_price=price, corrupt_last=False)
        _CANDLE_CACHE[market] = {"df": good_df, "timestamp": time.time()}

        state = {
            "market": market,
            "current_regime": "BEAR",
            "initial_entry_done": True,
            "strategy_engine": "ADAPTIVE_GRID_C_E",
            "bot_quantity": 0.10,
            "bot_avg_price": 4_000_000.0,
            "tranches": [
                {
                    "tranche_id": 1, "step": 1, "squad_id": 1, "squad_stage": 1,
                    "units": 1, "volume": 0.10, "buy_price": 4_000_000.0,
                }
            ],
            "bear_limit_buys": {},
            "basket_limit_ask": None,
        }

        ex = SquadFakeExchange(prot + 0.10, price, market=market)
        self.run_single_tick(market, ex, state, price)

        # 1. 3개 지정가 매수 등록 정상 완료
        self.assertEqual(3, len(ex.limit_buys), f"Expected 3 limit buys on valid candles, got {len(ex.limit_buys)}")
        # 2. Squad 1 Stage 2, 3, 4 배정 확인
        units = [o["units"] for o in state["bear_limit_buys"].values()]
        self.assertEqual([1, 2, 4], units)
        # 3. 호가 내림차순 확인
        prices = [float(b["price"]) for b in ex.limit_buys]
        self.assertLess(prices[0], 4_000_000.0)
        self.assertLess(prices[1], prices[0])
        self.assertLess(prices[2], prices[1])
        # 4. 재조정 대기 플래그 완료 (False)
        self.assertFalse(state.get("bear_buy_rebalance_pending", True))

    def test_valid_candles_execute_normal_replenishment_bid(self):
        """정상 캔들 데이터에서 보충 경로(Case 4): 1개 부족한 매수를 최저 호가 아래로 정상 배치해야 합니다."""
        market = "KRW-ETH"
        price = 3_700_000.0
        prot = main.PROTECTED_BALANCES[market]

        good_df = make_sample_candle_df(220, base_price=price, corrupt_last=False)
        _CANDLE_CACHE[market] = {"df": good_df, "timestamp": time.time()}

        state = {
            "market": market,
            "current_regime": "BEAR",
            "initial_entry_done": True,
            "strategy_engine": "ADAPTIVE_GRID_C_E",
            "bot_quantity": 0.15,
            "bot_avg_price": 3_900_000.0,
            "bear_buy_rebalance_pending": True,
            "tranches": [
                {"tranche_id": 1, "step": 1, "squad_id": 1, "squad_stage": 1, "units": 1, "volume": 0.10, "buy_price": 4_000_000.0},
                {"tranche_id": 2, "step": 2, "squad_id": 1, "squad_stage": 2, "units": 1, "volume": 0.05, "buy_price": 3_700_000.0},
            ],
            "bear_limit_buys": {
                "bid-stg3": {"state": "wait", "units": 2, "squad_id": 1, "squad_stage": 3, "executed_volume": 0.0, "executed_funds": 0.0},
                "bid-stg4": {"state": "wait", "units": 4, "squad_id": 1, "squad_stage": 4, "executed_volume": 0.0, "executed_funds": 0.0},
            },
            "basket_limit_ask": {"uuid": "old-ask", "price": 4_000_000.0, "volume": 0.15},
        }

        ex = SquadFakeExchange(prot + 0.15, price, market=market)
        ex.orders["old-ask"] = {"uuid": "old-ask", "side": "ask", "price": "4000000", "volume": "0.15", "state": "wait"}
        ex.open_orders.append(ex.orders["old-ask"].copy())
        for u in ("bid-stg3", "bid-stg4"):
            o = {"uuid": u, "side": "bid", "price": "3500000", "volume": "0.01", "state": "wait", "executed_volume": "0"}
            ex.orders[u] = o
            ex.open_orders.append(o.copy())

        self.run_single_tick(market, ex, state, price)

        # Exactly 1 new buy order
        self.assertEqual(1, len(ex.limit_buys), f"Expected 1 replenishment buy, got {len(ex.limit_buys)}")
        new_bid = ex.limit_buys[0]
        # Must be below lowest existing bid (3,500,000)
        self.assertLess(float(new_bid["price"]), 3_500_000.0)
        # Squad 2 Stage 1 (1 unit)
        new_uuid = new_bid["uuid"]
        rec = state["bear_limit_buys"][new_uuid]
        self.assertEqual(2, rec["squad_id"])
        self.assertEqual(1, rec["squad_stage"])
        self.assertFalse(state.get("bear_buy_rebalance_pending", True))

    def test_existing_fill_reconciliation_preserved_despite_invalid_candles(self):
        """캔들 검증이 실패한 상황에서도 기존 체결 정산(reconcile_bear_limit_buys)은 정상 동작해야 합니다."""
        market = "KRW-ETH"
        price = 3_700_000.0
        prot = main.PROTECTED_BALANCES[market]

        bad_df = make_sample_candle_df(220, base_price=price, corrupt_last=True)
        _CANDLE_CACHE[market] = {"df": bad_df, "timestamp": time.time()}

        state = {
            "market": market,
            "current_regime": "BEAR",
            "initial_entry_done": True,
            "strategy_engine": "ADAPTIVE_GRID_C_E",
            "bot_quantity": 0.10,
            "bot_avg_price": 4_000_000.0,
            "tranches": [
                {"tranche_id": 1, "step": 1, "squad_id": 1, "squad_stage": 1, "units": 1, "volume": 0.10, "buy_price": 4_000_000.0}
            ],
            "bear_limit_buys": {
                "bid-fill-uuid": {
                    "state": "wait", "units": 1, "squad_id": 1, "squad_stage": 2,
                    "executed_volume": 0.0, "executed_funds": 0.0,
                }
            },
            "basket_limit_ask": {"uuid": "old-ask", "price": 4_020_000.0, "volume": 0.10},
        }

        # Exchange has completed the fill for bid-fill-uuid!
        ex = SquadFakeExchange(prot + 0.15, price, market=market)
        ex.orders["bid-fill-uuid"] = {
            "uuid": "bid-fill-uuid", "side": "bid", "state": "done",
            "executed_volume": "0.05", "trades": [{"funds": "180000"}]
        }
        ex.orders["old-ask"] = {"uuid": "old-ask", "side": "ask", "price": "4020000", "volume": "0.10", "state": "wait"}
        ex.open_orders.append(ex.orders["old-ask"].copy())

        self.run_single_tick(market, ex, state, price)

        # 1. Fill is reconciled: tranches length increases to 2
        self.assertEqual(2, len(state["tranches"]))
        # 2. bot_quantity updated
        self.assertAlmostEqual(0.15, state["bot_quantity"], places=5)
        # 3. New buys are 0 due to invalid candles
        self.assertEqual(0, len(ex.limit_buys))

    def test_nan_high_low_candles_results_in_zero_new_buys(self):
        """Codex 3rd Audit P1: 정상 타임스탬프라도 high/low가 NaN인 결측 캔들은 신규 매수 0건이어야 합니다."""
        market = "KRW-ETH"
        price = 3_700_000.0
        prot = main.PROTECTED_BALANCES[market]

        nan_df = make_sample_candle_df(220, base_price=price)
        nan_df.loc[:, ["high", "low"]] = float("nan")
        _CANDLE_CACHE[market] = {"df": nan_df, "timestamp": time.time()}

        state = {
            "market": market,
            "current_regime": "BEAR",
            "initial_entry_done": True,
            "strategy_engine": "ADAPTIVE_GRID_C_E",
            "bot_quantity": 0.10,
            "bot_avg_price": 4_000_000.0,
            "tranches": [
                {"tranche_id": 1, "step": 1, "squad_id": 1, "squad_stage": 1, "units": 1, "volume": 0.10, "buy_price": 4_000_000.0}
            ],
            "bear_limit_buys": {},
            "basket_limit_ask": None,
        }

        ex = SquadFakeExchange(prot + 0.10, price, market=market)
        self.run_single_tick(market, ex, state, price)

        # 신규 매수 0건, 바스켓 매도 1건, 재등록 대기 플래그 유지
        self.assertEqual(0, len(ex.limit_buys), f"Expected 0 limit buys on NaN high/low, got {len(ex.limit_buys)}")
        self.assertEqual(0, len(ex.market_buys), f"Expected 0 market buys on NaN high/low, got {len(ex.market_buys)}")
        self.assertEqual(1, len(ex.limit_sells))
        self.assertTrue(state.get("bear_buy_rebalance_pending", False))

    def test_missing_validity_field_results_in_zero_new_buys(self):
        """Codex 3rd Audit P1: 신호 응답에 candles_valid 필드가 누락되고 Adaptive 조회가 실패하면 신규 매수 0건이어야 합니다."""
        market = "KRW-ETH"
        price = 3_700_000.0
        prot = main.PROTECTED_BALANCES[market]

        # No cache, Adaptive returns None
        _CANDLE_CACHE.clear()

        state = {
            "market": market,
            "current_regime": "BEAR",
            "initial_entry_done": True,
            "strategy_engine": "ADAPTIVE_GRID_C_E",
            "bot_quantity": 0.10,
            "bot_avg_price": 4_000_000.0,
            "tranches": [
                {"tranche_id": 1, "step": 1, "squad_id": 1, "squad_stage": 1, "units": 1, "volume": 0.10, "buy_price": 4_000_000.0}
            ],
            "bear_limit_buys": {},
            "basket_limit_ask": None,
        }

        ex = SquadFakeExchange(prot + 0.10, price, market=market)

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

        # Mock hybrid regime omitting candles_valid field
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
            get_hybrid_regime_and_signals=lambda m: {"is_bull": False, "signal": "HOLD", "ma5": price},
        ), patch.object(main.time, "sleep", side_effect=bounded_sleep), redirect_stdout(io.StringIO()):
            try:
                main.run_trading_strategy(market)
            except EndTick:
                pass

        # candles_valid 필드 누락 시 fail-closed로 매수 0건 및 pending 유지
        self.assertEqual(0, len(ex.limit_buys), f"Expected 0 limit buys on missing validity field, got {len(ex.limit_buys)}")
        self.assertEqual(0, len(ex.market_buys), f"Expected 0 market buys on missing validity field, got {len(ex.market_buys)}")
        self.assertEqual(1, len(ex.limit_sells))
        self.assertTrue(state.get("bear_buy_rebalance_pending", False))

    def test_inf_or_negative_price_candles_results_in_zero_new_buys(self):
        """무한대(inf) 또는 음수/역전 가격 캔들은 유효성 검증 실패로 신규 매수 0건이어야 합니다."""
        market = "KRW-ETH"
        price = 3_700_000.0
        prot = main.PROTECTED_BALANCES[market]

        bad_df = make_sample_candle_df(220, base_price=price)
        bad_df.loc[bad_df.index[-1], "high"] = float("inf")
        _CANDLE_CACHE[market] = {"df": bad_df, "timestamp": time.time()}

        state = {
            "market": market,
            "current_regime": "BEAR",
            "initial_entry_done": True,
            "strategy_engine": "ADAPTIVE_GRID_C_E",
            "bot_quantity": 0.10,
            "bot_avg_price": 4_000_000.0,
            "tranches": [
                {"tranche_id": 1, "step": 1, "squad_id": 1, "squad_stage": 1, "units": 1, "volume": 0.10, "buy_price": 4_000_000.0}
            ],
            "bear_limit_buys": {},
            "basket_limit_ask": None,
        }

        ex = SquadFakeExchange(prot + 0.10, price, market=market)
        self.run_single_tick(market, ex, state, price)

        self.assertEqual(0, len(ex.limit_buys))
        self.assertEqual(0, len(ex.market_buys))
        self.assertEqual(1, len(ex.limit_sells))
        self.assertTrue(state.get("bear_buy_rebalance_pending", False))


if __name__ == "__main__":
    unittest.main()
