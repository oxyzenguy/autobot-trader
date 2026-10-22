"""Adversarial stress and fault-injection regressions for the multi-agent runtime."""
from __future__ import annotations

import json
import math
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
from io import StringIO
from contextlib import redirect_stdout
from unittest.mock import patch

if sys.version_info < (3, 12):
    raise unittest.SkipTest("Separate multiagent runtime requires Python 3.12")

from ma_trader import ledger_store as store
from ma_trader.demo import fixture, paper_settings
from ma_trader.engine import Engine
from ma_trader.ledger import Ledger
from ma_trader.ledger_marks import check, equity
from ma_trader.models import ContractError, ExitRules, OrderIntent, OrderStatus, Plan, Position, Snapshot
from ma_trader.risk import RiskRejected, entry_reasons
from ma_trader.settings import Limits, Settings
from ma_trader.ticks import tick_price


class AdversarialRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.settings = paper_settings(str(self.root))
        self.now = 1791795600.0  # Stable baseline timestamp

    def test_tick_price_rejects_negative_zero_nan_and_inf(self):
        """tick_price must fail closed with ValueError on non-positive or non-finite inputs."""
        for invalid in (-100.0, -0.0001, 0.0, float("nan"), float("inf"), float("-inf")):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError) as ctx:
                    tick_price(invalid)
                self.assertIn("must be positive and finite", str(ctx.exception))

    def test_extreme_slippage_does_not_crash_engine_tick(self):
        """Extreme expected slippage (>= 1.0) must emit exit_blocked instead of StopIteration crash."""
        from ma_trader.broker import PaperBroker

        broker = PaperBroker(self.root / "exchange.sqlite3", 0.0005)
        engine = Engine(self.settings, broker)

        # 1. Establish an owned position
        with redirect_stdout(StringIO()):
            engine.tick((fixture(self.now),), self.now)
        positions = engine.ledger.positions()
        self.assertEqual(1, len(positions))

        # 2. Advance time so stop loss triggers, but quote has slippage >= 1.0 (bid * (1 - 1.1) < 0)
        adverse_quote = fixture(self.now + 10, 950.0).model_copy(
            update={"expected_slippage": 1.1}
        )

        output = StringIO()
        with redirect_stdout(output):
            # Must not crash with StopIteration or ValueError!
            engine.tick((adverse_quote,), self.now + 10)

        events = [json.loads(line) for line in output.getvalue().splitlines()]
        exit_blocked = [e for e in events if e.get("event") == "exit_blocked"]
        self.assertTrue(len(exit_blocked) >= 1)
        self.assertIn("invalid_exit_price", exit_blocked[0]["payload"]["reasons"])
        # Position must remain safe and retained in ledger
        self.assertEqual(1, len(engine.ledger.positions()))

    def test_transaction_rolls_back_on_value_error_and_contract_error(self):
        """Transaction context manager must cleanly rollback on ValueError, ContractError, and generic exceptions."""
        db_path = self.root / "tx_test.sqlite3"
        store.initialize(db_path, 100000.0)

        # Trigger ValueError inside transaction
        try:
            with store.transaction(db_path) as conn:
                conn.execute("UPDATE account SET body='{\"cash\":0}' WHERE id = 1")
                raise ValueError("arbitrary_value_error")
        except ValueError:
            pass

        with store.transaction(db_path) as conn:
            acct = store.account(conn)
            self.assertEqual(100000.0, acct.cash, "Cash must be restored after ValueError")

        # Trigger ContractError inside transaction
        try:
            with store.transaction(db_path) as conn:
                conn.execute("UPDATE account SET body='{\"cash\":500}' WHERE id = 1")
                raise ContractError("contract_violation")
        except ContractError:
            pass

        with store.transaction(db_path) as conn:
            acct = store.account(conn)
            self.assertEqual(100000.0, acct.cash, "Cash must be restored after ContractError")

    def test_daily_loss_halt_resets_at_midnight_kst_while_drawdown_latches(self):
        """Daily loss limit pauses trading for that day only; midnight KST rolls over while drawdown latches."""
        db_path = self.root / "marks_test.sqlite3"
        store.initialize(db_path, 100000.0)

        # Configure limits: daily loss = 2,000 KRW, drawdown limit = 5,000 KRW
        limits = Limits(
            budget_krw=100000.0,
            max_order_krw=10000.0,
            max_market_krw=20000.0,
            max_trade_loss_krw=1000.0,
            daily_loss_krw=2000.0,
            max_drawdown_krw=5000.0,
            allowed_markets=("KRW-XRP",),
            excluded_markets=(),
        )
        custom_settings = self.settings.model_copy(update={"limits": limits})

        # 12:00:00 KST Day 1 (timestamp in KST day 1)
        t_day1 = 1760000000.0  # 2025-10-09 approx

        # Create position: 100 XRP @ 100 KRW = 10,000 KRW cost
        rules = ExitRules(stop_pct=0.05, take_pct=0.05, max_hold_seconds=3600)
        pos = Position(
            market="KRW-XRP",
            quantity=100.0,
            cost_krw=10000.0,
            opened_at=t_day1,
            rules=rules,
            strategy_id="range_reversion_v1",
            strategy_version="1.0",
        )
        with store.transaction(db_path) as conn:
            store.save_position(conn, pos)
            # Cash: 90,000 KRW, Position: 100 XRP
            store.save_account(conn, store.account(conn).model_copy(update={"cash": 90000.0}))

        # Step 1: Initial mark at 100 KRW -> equity = 90000 + 100 * 100 * (1 - 0.0005) = 99995 KRW
        with store.transaction(db_path) as conn:
            halt = check(conn, {"KRW-XRP": 100.0}, (custom_settings, t_day1))
            self.assertFalse(halt)

        # Step 2: Price drops to 70 KRW -> loss = ~3,000 KRW (> daily loss 2000 KRW, but < drawdown 5000 KRW)
        with store.transaction(db_path) as conn:
            halt = check(conn, {"KRW-XRP": 70.0}, (custom_settings, t_day1 + 60))
            self.assertTrue(halt, "Daily loss breached -> must halt")

        # Step 3: Advance 24 hours to Day 2 (rollover past midnight KST)
        t_day2 = t_day1 + 86400.0
        with store.transaction(db_path) as conn:
            halt = check(conn, {"KRW-XRP": 70.0}, (custom_settings, t_day2))
            self.assertFalse(
                halt,
                "New calendar day in KST: daily base resets and daily loss halt clears if drawdown not breached",
            )
            acct = store.account(conn)
            self.assertFalse(acct.halt)

        # Step 4: On Day 2, price drops further to 40 KRW -> total loss = 6,000 KRW (> drawdown limit 5000 KRW)
        with store.transaction(db_path) as conn:
            halt = check(conn, {"KRW-XRP": 40.0}, (custom_settings, t_day2 + 60))
            self.assertTrue(halt, "Drawdown breached -> must halt")

        # Step 5: Advance to Day 3 -> Drawdown is still breached (peak - value >= 5000)
        t_day3 = t_day2 + 86400.0
        with store.transaction(db_path) as conn:
            halt = check(conn, {"KRW-XRP": 40.0}, (custom_settings, t_day3))
            self.assertTrue(halt, "Drawdown limit must stay latched across days until equity recovers")

    def test_ledger_clear_halt(self):
        """clear_halt method must reset account.halt to False."""
        db_path = self.root / "clear_halt_test.sqlite3"
        ledger = Ledger(db_path, self.settings)

        # Manually halt account
        with store.transaction(db_path) as conn:
            store.save_account(conn, store.account(conn).model_copy(update={"halt": True}))
        self.assertTrue(ledger.halted())

        # Clear halt
        ledger.clear_halt()
        self.assertFalse(ledger.halted())

    def test_minor_fee_rounding_slack_prevents_false_halt(self):
        """Exchange fee rounding up to 1 KRW must not trigger fill_exceeds_reserved_cash."""
        from test_ma_ledger import plan as make_plan

        db_path = self.root / "fee_test.sqlite3"
        ledger = Ledger(db_path, self.settings)

        p = make_plan("fee-test", amount=8000.0, now=self.now)
        intent = ledger.reserve(p, self.now)
        ledger.record_submission(intent.identifier, "uuid-test-fee")

        # Exchange rounded fee up by 0.5 KRW: paid_fee = intent.reserve_krw - intent.price * intent.quantity + 0.5
        executed_funds = intent.price * intent.quantity
        exact_fee = executed_funds * 0.0005
        rounded_fee = exact_fee + 0.5  # 0.5 KRW rounding above reserve

        status_with_fee_rounding = OrderStatus(
            identifier=intent.identifier,
            uuid="uuid-test-fee",
            market="KRW-XRP",
            side="bid",
            state="done",
            executed_volume=intent.quantity,
            executed_funds=executed_funds,
            paid_fee=rounded_fee,
        )

        # Must succeed without raising fill_exceeds_reserved_cash
        ledger.settle(status_with_fee_rounding)
        pos = ledger.positions()
        self.assertEqual(1, len(pos))
        self.assertEqual(intent.quantity, pos[0].quantity)

    def test_repeated_settlement_idempotency_buy_and_sell(self):
        """Re-settling identical order status must never duplicate positions, cash, or PnL."""
        from test_ma_ledger import plan as make_plan, status as make_status

        db_path = self.root / "idempotent_test.sqlite3"
        ledger = Ledger(db_path, self.settings)

        # 1. Buy Order
        p = make_plan("idem-buy", amount=8000.0, now=self.now)
        buy_intent = ledger.reserve(p, self.now)
        buy_uuid = "exchange-" + buy_intent.identifier
        ledger.record_submission(buy_intent.identifier, buy_uuid)

        # Partial 50%
        partial_buy = make_status(buy_intent, fraction=0.5, state="wait")
        ledger.settle(partial_buy)
        cash_after_partial1 = ledger.cash()
        qty_after_partial1 = ledger.positions()[0].quantity

        # Re-settle identical partial
        ledger.settle(partial_buy)
        self.assertEqual(cash_after_partial1, ledger.cash(), "Cash must not change on duplicate partial buy")
        self.assertEqual(qty_after_partial1, ledger.positions()[0].quantity, "Quantity must not change on duplicate partial buy")

        # Complete 100% buy
        done_buy = make_status(buy_intent, fraction=1.0, state="done")
        ledger.settle(done_buy)
        cash_after_buy_done = ledger.cash()
        qty_after_buy_done = ledger.positions()[0].quantity

        # Re-settle identical done buy
        ledger.settle(done_buy)
        self.assertEqual(cash_after_buy_done, ledger.cash(), "Cash must not change on duplicate done buy")
        self.assertEqual(qty_after_buy_done, ledger.positions()[0].quantity, "Quantity must not change on duplicate done buy")

        # 2. Sell Order
        sell_intent = ledger.approve_sell(ledger.positions()[0], 105.0, self.now + 10)
        sell_uuid = "exchange-" + sell_intent.identifier
        ledger.record_submission(sell_intent.identifier, sell_uuid)

        # Partial 40% sell
        partial_sell = make_status(sell_intent, fraction=0.4, state="wait")
        ledger.settle(partial_sell)
        cash_after_sell_p1 = ledger.cash()
        qty_after_sell_p1 = ledger.positions()[0].quantity

        # Re-settle identical partial sell
        ledger.settle(partial_sell)
        self.assertEqual(cash_after_sell_p1, ledger.cash(), "Cash must not change on duplicate partial sell")
        self.assertEqual(qty_after_sell_p1, ledger.positions()[0].quantity, "Quantity must not change on duplicate partial sell")

        # Complete 100% sell
        done_sell = make_status(sell_intent, fraction=1.0, state="done")
        ledger.settle(done_sell)
        self.assertEqual((), ledger.positions(), "Position must be deleted after 100% sell")
        final_cash = ledger.cash()

        # Re-settle identical done sell
        ledger.settle(done_sell)
        self.assertEqual(final_cash, ledger.cash(), "Cash must not change on duplicate done sell")
        self.assertEqual((), ledger.positions())

    def test_spread_calculation_consistency(self):
        """Spread in risk.py must use (ask - bid) / bid matching agents.py."""
        limits = Limits(
            budget_krw=100000.0,
            max_order_krw=10000.0,
            max_market_krw=20000.0,
            max_trade_loss_krw=1000.0,
            daily_loss_krw=2000.0,
            max_drawdown_krw=5000.0,
            max_spread=0.002,  # 0.2% max spread
            allowed_markets=("KRW-XRP",),
            excluded_markets=(),
        )
        custom_settings = self.settings.model_copy(update={"limits": limits})

        # bid = 1000.0, ask = 1002.5 -> spread = 2.5 / 1000 = 0.0025 > 0.002 (exceeds spread)
        snap = Snapshot(
            market="KRW-XRP",
            observed_at=self.now,
            bid=1000.0,
            ask=1002.5,
            prices=(1000.0,) * 30,
            turnover_24h=1e10,
            expected_slippage=0.0,
        )
        rules = ExitRules(stop_pct=0.01, take_pct=0.01, max_hold_seconds=3600)
        plan = Plan(
            analysis_id="spread-test",
            role="supervisor",
            generated_at=self.now,
            input_at=self.now,
            valid_until=self.now + 60,
            market="KRW-XRP",
            regime="range",
            decision="enter",
            strategy_id="range_reversion_v1",
            strategy_version="1.0",
            reasons=("test",),
            objections=(),
            requested_krw=5000.0,
            approved_krw=5000.0,
            risk_passed=True,
            rejection_reasons=(),
            entry_min=1000.0,
            entry_max=1002.5,
            cancel_conditions=(),
            exits=rules,
            snapshot=snap,
        )

        reasons = entry_reasons(plan, custom_settings, self.now)
        self.assertIn("spread_limit", reasons)


if __name__ == "__main__":
    unittest.main()
