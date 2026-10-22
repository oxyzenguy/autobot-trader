"""Offline private-ledger safety regressions with isolated actual SQLite files."""
import sys
import unittest
if sys.version_info < (3, 12):
    raise unittest.SkipTest("Separate multiagent runtime requires Python 3.12")
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import sqlite3
import unittest

from ma_trader.ledger import Ledger
from ma_trader.models import OrderIntent, OrderStatus, Plan, Snapshot
from ma_trader.risk import RiskRejected
from ma_trader.settings import Limits, Settings
from ma_trader.strategies import STRATEGIES


def settings(**changes: float | int | bool) -> Settings:
    limits = Limits(budget_krw=100000, max_order_krw=80000, max_market_krw=90000,
        max_trade_loss_krw=5000, daily_loss_krw=10000, max_drawdown_krw=20000,
        min_cash_ratio=0, order_cooldown_seconds=0, market_cooldown_seconds=0,
        strategy_cooldown_seconds=0)
    return Settings(limits=limits.model_copy(update=changes), min_turnover_krw=0)


def plan(identifier: str = "entry", amount: float = 20000, now: float = 1760000000) -> Plan:
    snapshot = Snapshot(market="KRW-XRP", observed_at=now, bid=99.99, ask=100,
        prices=(100,) * 19 + (99.8,), turnover_24h=1e10, expected_slippage=.0001)
    return Plan(analysis_id=identifier, role="coordinator", generated_at=now, input_at=now,
        valid_until=now + 60, market=snapshot.market, regime="range", decision="enter",
        strategy_id=STRATEGIES[1].id, strategy_version=STRATEGIES[1].version,
        reasons=("signal",), objections=(), requested_krw=amount, approved_krw=1e12,
        risk_passed=True, rejection_reasons=(), entry_min=99, entry_max=101,
        cancel_conditions=("stale",), exits=STRATEGIES[1].exits, snapshot=snapshot)


def status(intent: OrderIntent, fraction: float = 1, state: str = "done") -> OrderStatus:
    return OrderStatus(identifier=intent.identifier, uuid="exchange-" + intent.identifier,
        market=intent.market, side=intent.side, state=state,
        executed_volume=intent.quantity * fraction,
        executed_funds=intent.price * intent.quantity * fraction,
        paid_fee=intent.price * intent.quantity * fraction * .0005)


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "private.sqlite"
        self.ledger = Ledger(self.path, settings())
        self.now = 1760000000

    def test_partial_fill_is_idempotent_across_restart(self):
        # Given a committed cumulative partial.
        intent = self.ledger.reserve(plan(), self.now)
        partial = status(intent, .4, "wait")
        self.ledger.settle(partial)
        reopened = Ledger(self.path, settings())
        # When the same exchange cumulative is replayed, then quantities/cash do not duplicate.
        reopened.settle(partial)
        self.assertAlmostEqual(80, reopened.positions()[0].quantity)
        self.assertAlmostEqual(91996, reopened.cash())
        reopened.settle(status(intent))
        self.assertAlmostEqual(200, reopened.positions()[0].quantity)
        self.assertAlmostEqual(79990, reopened.cash())

    def test_response_loss_blocks_duplicate_and_new_orders(self):
        # Given a durable intent followed by ambiguous submission.
        intent = self.ledger.reserve(plan(), self.now)
        self.ledger.mark_unknown(intent.identifier, "timeout")
        # When resumed, then new and duplicate entries remain blocked.
        reopened = Ledger(self.path, settings())
        for candidate in (plan(), plan("other")):
            with self.assertRaises(RiskRejected):
                reopened.reserve(candidate, self.now)
        self.assertEqual(1, len(reopened.pending()))

    def test_concurrent_reservation_cannot_double_spend(self):
        # Given two writers sharing one real SQLite ledger.
        def attempt(index: int) -> bool:
            try:
                self.ledger.reserve(plan(str(index), 60000), self.now)
                return True
            except RiskRejected:
                return False
        # When both enter concurrently, then at most one commits.
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = tuple(executor.map(attempt, (1, 2)))
        self.assertEqual(1, sum(results))
        self.assertEqual(1, len(self.ledger.pending()))

    def test_protected_or_unowned_position_cannot_sell(self):
        # Given no privately owned position, even an external position cannot grant ownership.
        intent = self.ledger.reserve(plan(), self.now)
        self.ledger.settle(status(intent))
        external = self.ledger.positions()[0].model_copy(update={"quantity": 10000})
        # When attempting to sell external holdings, then admission rejects.
        with self.assertRaises(RiskRejected):
            self.ledger.approve_sell(external, 100, self.now)

    def test_daily_unrealized_loss_and_fees_halt(self):
        # Given a newly entered position and KST day baseline.
        intent = self.ledger.reserve(plan(amount=60000), self.now)
        self.ledger.settle(status(intent))
        # When fee-inclusive equity drops beyond daily loss, then entries halt.
        self.assertTrue(self.ledger.check_limits({"KRW-XRP": 80}, self.now + 1))
        with self.assertRaises(RiskRejected):
            self.ledger.reserve(plan("second", now=self.now + 1), self.now + 1)

    def test_cashflow_does_not_create_false_loss(self):
        # Given unchanged investment marks after entering.
        intent = self.ledger.reserve(plan(), self.now)
        self.ledger.settle(status(intent))
        self.ledger.cashflows(-20000, self.now)
        # When measuring equity after withdrawal, then it is not classified as loss.
        self.assertFalse(self.ledger.check_limits({"KRW-XRP": 100}, self.now + 1))

    def test_immutable_exit_contract_and_full_exit_accounting(self):
        # Given position exits bound at first fill.
        intent = self.ledger.reserve(plan(), self.now)
        self.ledger.settle(status(intent))
        position = self.ledger.positions()[0]
        # When approval changes the strategy contract, then ledger refuses.
        altered = position.model_copy(update={"rules": position.rules.model_copy(update={"stop_pct": .03})})
        with self.assertRaises(RiskRejected):
            self.ledger.approve_sell(altered, 102, self.now)
        sell = self.ledger.approve_sell(position, 102, self.now)
        self.ledger.settle(status(sell))
        self.assertEqual((), self.ledger.positions())
        self.assertAlmostEqual(100379.8, self.ledger.cash())

    def test_budget_ignores_ai_approved_amount(self):
        # Given an AI report claiming arbitrarily large approval.
        candidate = plan(amount=90000)
        # When deterministic budget gates run, then request is refused by order cap.
        with self.assertRaises(RiskRejected) as caught:
            self.ledger.reserve(candidate, self.now)
        self.assertIn("order_limit", caught.exception.reasons)

    def test_ai_preapproval_flags_do_not_grant_or_prevent_authority(self):
        # Given an eligible report that has not passed deterministic admission yet.
        candidate = plan().model_copy(update={"risk_passed": False, "approved_krw": 0})
        # When deterministic limits approve, then the durable intent has the requested bounded amount.
        intent = self.ledger.reserve(candidate, self.now)
        self.assertAlmostEqual(20010, intent.reserve_krw)

    def test_stale_analysis_is_not_ordered(self):
        # Given an expired report.
        candidate = plan()
        # When used after expiry, then zero intents are committed.
        with self.assertRaises(RiskRejected):
            self.ledger.reserve(candidate, self.now + 121)
        self.assertEqual((), self.ledger.pending())

    def test_unconfigured_limits_fail_closed(self):
        # Given a separate unfunded ledger with missing constraints.
        ledger = Ledger(Path(self.directory.name) / "empty.sqlite", Settings())
        # When a valid signal requests entry, then funding is denied.
        with self.assertRaises(RiskRejected) as caught:
            ledger.reserve(plan(), self.now)
        self.assertIn("limits_unconfigured", caught.exception.reasons)

    def test_confirmed_reserves_still_enforce_concurrent_budget(self):
        # Given three permitted markets and one confirmed open limit bid.
        configured = settings().model_copy(update={"limits": settings().limits.model_copy(update={
            "allowed_markets": ("KRW-XRP", "KRW-ADA", "KRW-DOGE"), "max_positions": 3})})
        ledger = Ledger(Path(self.directory.name) / "concurrent.sqlite", configured)
        first = ledger.reserve(plan("first"), self.now)
        ledger.record_submission(first.identifier, "confirmed")
        def attempt(market: str) -> bool:
            candidate = plan(market, 60000)
            candidate = candidate.model_copy(update={"market": market,
                "snapshot": candidate.snapshot.model_copy(update={"market": market})})
            try:
                ledger.reserve(candidate, self.now)
                return True
            except RiskRejected:
                return False
        # When competing writers request the remaining cash, then only one commits.
        with ThreadPoolExecutor(max_workers=2) as executor:
            result = tuple(executor.map(attempt, ("KRW-ADA", "KRW-DOGE")))
        self.assertEqual(1, sum(result))
        self.assertEqual(2, len(ledger.pending()))

    def test_cancelled_partial_releases_only_unfilled_reserve(self):
        # Given a partially filled cancelled order.
        intent = self.ledger.reserve(plan(), self.now)
        # When cancellation settles, then cash includes only executed cost and ownership.
        self.ledger.settle(status(intent, .4, "cancel"))
        self.assertEqual((), self.ledger.pending())
        self.assertAlmostEqual(91996, self.ledger.cash())
        self.assertAlmostEqual(80, self.ledger.positions()[0].quantity)

    def test_uuid_mismatch_halts_reconciliation(self):
        # Given a confirmed submitted UUID.
        intent = self.ledger.reserve(plan(), self.now)
        self.ledger.record_submission(intent.identifier, "correct")
        # When another order's fills are supplied, then no position or cash is credited.
        with self.assertRaises(RiskRejected):
            self.ledger.settle(status(intent))
        self.assertEqual((), self.ledger.positions())
        self.assertEqual(100000, self.ledger.cash())
        self.assertEqual("unknown", self.ledger.order_state(intent.identifier))

    def test_profitable_mark_does_not_consume_committed_budget(self):
        # Given a position whose marked value grows beyond the budget.
        intent = self.ledger.reserve(plan(), self.now)
        self.ledger.settle(status(intent))
        # When marking gains, then appreciation alone is not a budget violation.
        self.assertFalse(self.ledger.check_limits({"KRW-XRP": 1000}, self.now + 1))
        self.assertGreater(self.ledger.equity({"KRW-XRP": 1000}), 100000)

    def test_complete_fill_replay_is_idempotent(self):
        # Given a settled full purchase.
        intent = self.ledger.reserve(plan(), self.now)
        self.ledger.settle(status(intent))
        # When replaying terminal exchange data, then the position/cash remain unchanged.
        self.ledger.settle(status(intent))
        self.assertAlmostEqual(79990, self.ledger.cash())
        self.assertAlmostEqual(200, self.ledger.positions()[0].quantity)

    def test_recovery_cannot_clear_unconfirmed_reservation(self):
        # Given a reservation persisted before an unknown crash boundary.
        self.ledger.reserve(plan(), self.now)
        reopened = Ledger(self.path, settings())
        # When a caller attempts recovery without remote proof, then entry gate stays closed.
        with self.assertRaises(RiskRejected):
            reopened.clear_recovery()

    def test_local_preflight_rejection_releases_reserve_without_cash_change(self):
        # Given a durable intent that provably never reached the submission boundary.
        intent = self.ledger.reserve(plan(), self.now)
        # When local admission fails, then only the reservation is released.
        self.ledger.reject_unsubmitted(intent.identifier, "expired_before_post")
        self.assertEqual((), self.ledger.pending())
        self.assertEqual(100000, self.ledger.cash())
        self.assertEqual("cancel", self.ledger.order_state(intent.identifier))

    def test_ambiguous_submission_cannot_be_released_as_local_rejection(self):
        # Given a possibly accepted order with a lost acknowledgement.
        intent = self.ledger.reserve(plan(), self.now)
        self.ledger.mark_unknown(intent.identifier, "timeout")
        # When a caller labels it never-submitted, then uncertainty still retains its reserve.
        with self.assertRaises(RiskRejected):
            self.ledger.reject_unsubmitted(intent.identifier, "assumed_rejected")
        self.assertEqual(1, len(self.ledger.pending()))

    def test_sell_reflection_counts_net_partial_pnl_once(self):
        # Given purchased inventory with its buy fee included in basis.
        buy = self.ledger.reserve(plan(), self.now)
        self.ledger.settle(status(buy))
        sell = self.ledger.approve_sell(self.ledger.positions()[0], 102, self.now + 1)
        # When cumulative partial and final sale evidence repeat, only newly settled PnL counts.
        partial = status(sell, .4, "wait")
        self.ledger.settle(partial)
        self.ledger.settle(partial)
        self.ledger.settle(status(sell))
        self.ledger.settle(status(sell))
        # Then the journal's PnL deltas include both sides' fees, without duplicate proceeds.
        with sqlite3.connect(self.path) as connection:
            events = tuple(json.loads(row[0]) for row in connection.execute(
                "SELECT payload FROM events WHERE kind='fill'"))
        deltas = tuple(event["realized_pnl_delta"] for event in events if event["side"] == "ask")
        self.assertAlmostEqual(151.92, deltas[0])
        self.assertAlmostEqual(0, deltas[1])
        self.assertAlmostEqual(227.88, deltas[2])
        self.assertEqual(3, len(deltas))
        self.assertAlmostEqual(379.8, sum(deltas))
