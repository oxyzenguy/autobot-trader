"""Tests for relaxed BULL entry logic: 5/20 MA aligned entry and fresh cross gating."""

from __future__ import annotations

import copy
import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import main


class EndTick(BaseException):
    """Stop the loop after the decision."""


class FakeExchange:
    def __init__(self, holdings: float, price: float):
        self.holdings = holdings
        self.price = price
        self.market_buys: list[float] = []

    def get_balance(self, currency: str, verbose: bool = False):
        if currency == "KRW":
            return 100_000.0
        if currency == "SOL" and verbose:
            return {"balance": self.holdings, "locked": 0.0}
        if currency == "SOL":
            return self.holdings
        raise AssertionError(f"Unexpected currency: {currency}")

    def get_order(self, identifier: str, state: str | None = None):
        if identifier == "fake-bull-buy":
            vol = 10_000.0 / self.price
            return {
                "uuid": identifier,
                "state": "done",
                "executed_volume": str(vol),
                "trades": [{"funds": "10000.0"}],
                "paid_fee": "0",
            }
        return []

    def buy_market_order(self, market: str, price: float):
        self.market_buys.append(price)
        vol = price / self.price
        self.holdings += vol
        return {
            "uuid": "fake-bull-buy",
            "state": "done",
            "executed_volume": str(vol),
            "trades": [{"funds": str(price)}],
            "paid_fee": "0",
        }


class TestP1BullEntry(unittest.TestCase):
    def run_tick(self, exchange: FakeExchange, state: dict, price: float, is_bull: bool, signal: str):
        saved = []
        logged = []
        output = io.StringIO()

        price_queries = 0

        def get_price(market: str):
            nonlocal price_queries
            price_queries += 1
            if price_queries > 1:
                raise EndTick()
            return price

        def stop_after_decision(seconds: float):
            if state.get("trading_halted"):
                raise EndTick()
            return

        def save_state(market: str, value: dict):
            saved.append(copy.deepcopy(value))

        with (
            patch.multiple(
                main,
                acquire_market_process_lock=lambda market: object(),
                init_db=lambda: None,
                get_upbit_client=lambda: exchange,
                load_strategy_state=lambda market: state,
                save_strategy_state=save_state,
                check_krw_balance_alert=lambda *args, **kwargs: 100_000.0,
                send_telegram_alert=lambda message: None,
                safe_get_current_price=get_price,
                get_hybrid_regime_and_signals=lambda market: {
                    "candles_valid": True,
                    "is_valid": True,
                    "is_bull": is_bull,
                    "signal": signal,
                    "curr_price": price,
                    "ma5": price + 500,
                    "ma20": price - 1000,
                    "ma200": price - 3000,
                    "regime_korean": "상승 국면 (Bull)" if is_bull else "하락 국면 (Bear)",
                },
                log_real_trade=lambda **trade: logged.append(trade),
            ),
            patch.object(main.time, "sleep", side_effect=stop_after_decision),
            patch("requests.sessions.Session.request", side_effect=AssertionError("Real HTTP forbidden")),
            redirect_stdout(output),
        ):
            with self.assertRaises(EndTick):
                main.run_trading_strategy("KRW-SOL")

        return saved, logged

    def test_bull_aligned_enters_when_no_fresh_gc_needed(self):
        """When in BULL regime with 0 quantity and 5/20 MA aligned, enter 1 Unit."""
        prot = main.PROTECTED_BALANCES["KRW-SOL"]
        exchange = FakeExchange(prot, 165_000.0)
        state = {
            "market": "KRW-SOL",
            "current_regime": "BULL",
            "active_mode": "TREND",
            "bot_quantity": 0.0,
            "bot_avg_price": 0.0,
            "protected_quantity": prot,
            "initial_entry_done": True,
            "trailing_stop_active": False,
            "tranches": [],
            "completed_cycles": 2,
            "need_fresh_golden_cross": False,
        }

        saved, logged = self.run_tick(exchange, state, 165_000.0, is_bull=True, signal="BUY_ALIGNED")

        self.assertEqual(len(exchange.market_buys), 1)
        self.assertEqual(exchange.market_buys[0], 10_000.0)
        self.assertEqual(len(logged), 1)
        self.assertEqual(logged[0]["action"], "BULL_REGIME_ALIGNED_ENTRY")
        self.assertGreater(saved[-1]["bot_quantity"], 0.0)
        self.assertEqual(saved[-1]["need_fresh_golden_cross"], False)

    def test_bull_aligned_blocked_when_need_fresh_gc(self):
        """After trailing stop exit (need_fresh_golden_cross=True), BUY_ALIGNED does not re-enter."""
        prot = main.PROTECTED_BALANCES["KRW-SOL"]
        exchange = FakeExchange(prot, 165_000.0)
        state = {
            "market": "KRW-SOL",
            "current_regime": "BULL",
            "active_mode": "TREND",
            "bot_quantity": 0.0,
            "bot_avg_price": 0.0,
            "protected_quantity": prot,
            "initial_entry_done": True,
            "trailing_stop_active": False,
            "tranches": [],
            "completed_cycles": 2,
            "need_fresh_golden_cross": True,
        }

        saved, logged = self.run_tick(exchange, state, 165_000.0, is_bull=True, signal="BUY_ALIGNED")

        self.assertEqual(len(exchange.market_buys), 0)
        self.assertEqual(len(logged), 0)

    def test_bull_fresh_golden_cross_resets_need_fresh_gc_and_buys(self):
        """When fresh BUY signal arrives, need_fresh_golden_cross is cleared and buy executes."""
        prot = main.PROTECTED_BALANCES["KRW-SOL"]
        exchange = FakeExchange(prot, 165_000.0)
        state = {
            "market": "KRW-SOL",
            "current_regime": "BULL",
            "active_mode": "TREND",
            "bot_quantity": 0.0,
            "bot_avg_price": 0.0,
            "protected_quantity": prot,
            "initial_entry_done": True,
            "trailing_stop_active": False,
            "tranches": [],
            "completed_cycles": 2,
            "need_fresh_golden_cross": True,
        }

        saved, logged = self.run_tick(exchange, state, 165_000.0, is_bull=True, signal="BUY")

        self.assertEqual(len(exchange.market_buys), 1)
        self.assertEqual(exchange.market_buys[0], 10_000.0)
        self.assertEqual(len(logged), 1)
        self.assertEqual(logged[0]["action"], "TREND_GOLDEN_CROSS_BUY")
        self.assertEqual(saved[-1]["need_fresh_golden_cross"], False)

    def test_exit_only_state_blocks_new_buys(self):
        """When state has exit_only=True, new buys are strictly blocked even in BULL BUY signal."""
        prot = main.PROTECTED_BALANCES["KRW-SOL"]
        exchange = FakeExchange(prot, 165_000.0)
        state = {
            "market": "KRW-SOL",
            "current_regime": "BULL",
            "active_mode": "TREND",
            "bot_quantity": 0.0,
            "bot_avg_price": 0.0,
            "protected_quantity": prot,
            "initial_entry_done": True,
            "trailing_stop_active": False,
            "tranches": [],
            "completed_cycles": 2,
            "need_fresh_golden_cross": False,
            "exit_only": True,
        }

        saved, logged = self.run_tick(exchange, state, 165_000.0, is_bull=True, signal="BUY")

        # Zero buys must have occurred
        self.assertEqual(len(exchange.market_buys), 0)
        self.assertEqual(len(logged), 0)


if __name__ == "__main__":
    unittest.main()
