"""Offline outcome feedback, forward validation and execution-boundary tests."""
import sys
import unittest
if sys.version_info < (3, 12):
    raise unittest.SkipTest("Separate multiagent runtime requires Python 3.12")
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
import time
from pydantic import ValidationError

from ma_trader import ledger_store as store
from ma_trader.agents import AgentPipeline
from ma_trader.broker import PaperBroker
from ma_trader.demo import fixture, paper_settings
from ma_trader.engine import Engine
from ma_trader.models import ContractError, OrderStatus
from ma_trader.performance import PerformanceLoop, evaluate
from ma_trader.performance_history import completed
from ma_trader.performance_types import Outcome, Policy
from ma_trader.risk import RiskRejected
from ma_trader.settings import PerformanceSettings
from test_ma_agents import settings, snapshot

KEY = "range_reversion_v1:1.0:range"


def outcomes(returns: tuple[float, ...], start: float = 1000) -> tuple[Outcome, ...]:
    return tuple(Outcome(id=str(i), market="KRW-XRP", strategy_id="range_reversion_v1",
        version="1.0", regime="range", opened_at=start + i * 10,
        closed_at=start + i * 10 + 5, cost=10000, net_pnl=r * 10000) for i, r in enumerate(returns))


class PerformancePolicyTests(unittest.TestCase):
    def test_small_sample_does_not_pause(self):
        # Given: only six losses, below the required sample size.
        evidence = outcomes((-.02,) * 6)
        # When: performance is evaluated.
        decision = evaluate(Policy(key=KEY), (evidence, ()), (PerformanceSettings(), 10000))
        # Then: small samples do not trigger adaptation.
        self.assertFalse(decision.paused)

    def test_negative_train_and_validation_pause(self):
        # Given: thirty completed losses with costs included.
        evidence = outcomes((-.01,) * 30)
        # When: chronological windows are evaluated.
        decision = evaluate(Policy(key=KEY), (evidence, ()), (PerformanceSettings(), 10000))
        # Then: the strategy is paused for new entries.
        self.assertTrue(decision.paused)
        self.assertEqual(10000, decision.paused_at)

    def test_positive_validation_does_not_follow_negative_training_blindly(self):
        # Given: training losses but recent validation profits.
        evidence = outcomes((-.01,) * 20 + (.02,) * 10)
        # When: the two windows are evaluated separately.
        decision = evaluate(Policy(key=KEY), (evidence, ()), (PerformanceSettings(), 10000))
        # Then: pooled losing history alone does not force a pause.
        self.assertFalse(decision.paused)

    def test_old_shadow_profits_cannot_resume_paused_strategy(self):
        # Given: winning shadow trades all entered before the pause.
        evidence = outcomes((.02,) * 30)
        # When: requalification is evaluated after the time minimum.
        decision = evaluate(Policy(key=KEY, paused=True, paused_at=2000), ((), evidence), (PerformanceSettings(), 100000))
        # Then: old in-sample wins cannot unlock new trading.
        self.assertTrue(decision.paused)

    def test_forward_validation_loss_prevents_resume(self):
        # Given: new shadow training profits followed by validation losses.
        evidence = outcomes((.02,) * 20 + (-.01,) * 10, start=3000)
        # When: requalification is evaluated.
        decision = evaluate(Policy(key=KEY, paused=True, paused_at=2000), ((), evidence), (PerformanceSettings(), 100000))
        # Then: a successful training block cannot override failed validation.
        self.assertTrue(decision.paused)

    def test_forward_success_resumes_without_reusing_old_actual_losses(self):
        # Given: a paused strategy and thirty new forward shadow wins.
        actual = outcomes((-.01,) * 30)
        shadow = outcomes((.02,) * 30, start=3000)
        # When: the minimum pause time and both validation blocks pass.
        decision = evaluate(Policy(key=KEY, paused=True, paused_at=2000), (actual, shadow), (PerformanceSettings(), 100000))
        # Then: resume advances the actual evidence floor rather than re-pausing immediately.
        self.assertFalse(decision.paused)
        self.assertEqual(actual[-1].closed_at, decision.actual_floor)
        self.assertFalse(evaluate(decision, (actual, shadow), (PerformanceSettings(), 100001)).paused)

    def test_minimum_pause_duration_prevents_churn(self):
        # Given: enough forward wins but less than24hours since pause.
        shadow = outcomes((.02,) * 30, start=3000)
        # When: requalification runs early.
        decision = evaluate(Policy(key=KEY, paused=True, paused_at=2000), ((), shadow), (PerformanceSettings(), 10000))
        # Then: qualification cannot prematurely toggle entry eligibility.
        self.assertTrue(decision.paused)

    def test_future_shadow_outcomes_are_not_validation_evidence(self):
        # Given: thirty wins whose close times are later than this review.
        shadow = outcomes((.02,) * 30, start=200000)
        # When: the policy is reviewed before those outcomes exist.
        decision = evaluate(Policy(key=KEY, paused=True, paused_at=2000), ((), shadow), (PerformanceSettings(), 100000))
        # Then: there is no look-ahead requalification.
        self.assertTrue(decision.paused)

    def test_pipeline_pauses_only_the_affected_strategy(self):
        # Given: an eligible range signal but a paused range strategy.
        pipeline = AgentPipeline(settings())
        pipeline.performance_blocks = frozenset((KEY,))
        # When: normal selection runs.
        reports, plan = pipeline.analyze((snapshot(),), 1000)
        # Then: no advisory selection bypasses the deterministic veto.
        self.assertIsNone(plan)
        self.assertIn("performance_strategy_paused", reports[-1].rejection_reasons)


class PerformanceExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = time.time()
        self.config = paper_settings(self.tmp.name)
        self.engine = Engine(self.config, PaperBroker(Path(self.tmp.name) / "exchange.db", .0005))

    def test_shadow_progress_has_no_financial_side_effects(self):
        # Given: an isolated empty account and performance-enabled settings.
        enabled = self.config.model_copy(update={"performance": PerformanceSettings(enabled=True)})
        loop = PerformanceLoop(self.engine.ledger.path)
        # When: an eligible signal opens and closes only an observation position.
        loop.update((fixture(self.now),), (enabled, self.now))
        loop.update((fixture(self.now + 2, 1020),), (enabled, self.now + 2))
        # Then: observation outcomes exist but account/orders remain unchanged.
        self.assertEqual(100000, self.engine.ledger.cash())
        with store.transaction(self.engine.ledger.path) as db:
            self.assertEqual((), store.orders(db))
            self.assertEqual(1, db.execute("SELECT count(*) FROM performance_shadow_outcomes").fetchone()[0])

    def test_direct_reservation_cannot_bypass_paused_policy(self):
        # Given: a valid plan and a durable paused policy.
        enabled = self.config.model_copy(update={"performance": PerformanceSettings(enabled=True)})
        self.engine.ledger.settings = enabled
        PerformanceLoop(self.engine.ledger.path).update((), (enabled, self.now))
        with store.transaction(self.engine.ledger.path) as db:
            db.execute("UPDATE performance_policy SET body=? WHERE key=?", (Policy(key=KEY, paused=True, paused_at=self.now).model_dump_json(), KEY))
        _, plan = AgentPipeline(enabled).analyze((fixture(self.now),), self.now)
        # When: a caller attempts to skip the pipeline veto.
        with self.assertRaises(RiskRejected) as caught:
            self.engine.ledger.reserve(plan, self.now)
        # Then: the final admission boundary still rejects the entry.
        self.assertIn("performance_strategy_paused", caught.exception.reasons)

    def test_corrupt_review_does_not_disable_existing_stop(self):
        # Given: an existing position and corrupted advisory policy.
        with redirect_stdout(StringIO()):
            self.engine.tick((fixture(self.now),), self.now)
        enabled = self.config.model_copy(update={"performance": PerformanceSettings(enabled=True)})
        self.engine.settings = self.engine.ledger.settings = self.engine.pipeline.settings = enabled
        self.engine.performance.update((), (enabled, self.now))
        with store.transaction(self.engine.ledger.path) as db:
            db.execute("UPDATE performance_policy SET body='invalid'")
        # When: a stop quote arrives while the performance parser fails.
        with redirect_stdout(StringIO()):
            self.engine.tick((fixture(self.now + 2, 980),), self.now + 2)
        # Then: owned exit completes and new entries are blocked.
        self.assertEqual((), self.engine.ledger.positions())
        self.assertFalse(self.engine.entry_enabled)

    def test_partial_exit_counts_one_completed_episode(self):
        # Given: a position sold in two settlement increments.
        with redirect_stdout(StringIO()):
            self.engine.tick((fixture(self.now),), self.now)
        position = self.engine.ledger.positions()[0]
        intent = self.engine.ledger.approve_sell(position, 1020, self.now + 1)
        self.engine.ledger.record_submission(intent.identifier, "partial-test")
        partial = OrderStatus(identifier=intent.identifier, uuid="partial-test", market=intent.market,
            side="ask", state="wait", executed_volume=intent.quantity / 2,
            executed_funds=intent.quantity * 510, paid_fee=intent.quantity * 510 * .0005)
        self.engine.ledger.settle(partial)
        with store.transaction(self.engine.ledger.path) as db:
            self.assertEqual((), completed(db, time.time() + 10))
        # When: the cumulative final fill is settled, then replayed twice.
        final = partial.model_copy(update={"state": "done", "executed_volume": intent.quantity,
            "executed_funds": intent.quantity * 1020, "paid_fee": intent.quantity * 1020 * .0005})
        self.engine.ledger.settle(final)
        self.engine.ledger.settle(final)
        # Then: exactly one outcome includes both entry and exit fees.
        with store.transaction(self.engine.ledger.path) as db:
            result = completed(db, time.time() + 10)
        self.assertEqual(1, len(result))
        self.assertAlmostEqual(final.executed_funds - final.paid_fee - position.cost_krw, result[0].net_pnl)

    def test_paused_policy_survives_controller_restart(self):
        # Given: a durable paused strategy policy.
        enabled = self.config.model_copy(update={"performance": PerformanceSettings(enabled=True)})
        self.engine.performance.update((), (enabled, self.now))
        with store.transaction(self.engine.ledger.path) as db:
            db.execute("UPDATE performance_policy SET body=? WHERE key=?", (Policy(key=KEY, paused=True, paused_at=self.now).model_dump_json(), KEY))
        # When: a new controller resumes on the same ledger.
        blocked = PerformanceLoop(self.engine.ledger.path).update((), (enabled, self.now + 1))
        # Then: restart cannot reset a pause to sample-insufficient trading.
        self.assertIn(KEY, blocked)

    def test_actual_settlements_trigger_pause_and_preserve_cash(self):
        # Given: thirty real isolated ledger entry/exit settlements, all losing.
        limits = self.config.limits.model_copy(update={"order_cooldown_seconds": 0,
            "market_cooldown_seconds": 0, "strategy_cooldown_seconds": 0,
            "daily_loss_krw": 50000, "max_drawdown_krw": 50000})
        self.config = self.config.model_copy(update={"limits": limits})
        ledger = self.engine.ledger
        ledger.settings = self.config
        pipeline = AgentPipeline(self.config)
        for i in range(30):
            at = self.now + i
            _, plan = pipeline.analyze((fixture(at),), at)
            intent = ledger.reserve(plan, at)
            ledger.record_submission(intent.identifier, "entry-" + str(i))
            funds = intent.quantity * intent.price
            ledger.settle(OrderStatus(identifier=intent.identifier, uuid="entry-" + str(i),
                market=intent.market, side="bid", state="done", executed_volume=intent.quantity,
                executed_funds=funds, paid_fee=funds * .0005))
            exit_intent = ledger.approve_sell(ledger.positions()[0], 980, at + .5)
            ledger.record_submission(exit_intent.identifier, "exit-" + str(i))
            proceeds = exit_intent.quantity * 980
            ledger.settle(OrderStatus(identifier=exit_intent.identifier, uuid="exit-" + str(i),
                market=intent.market, side="ask", state="done", executed_volume=exit_intent.quantity,
                executed_funds=proceeds, paid_fee=proceeds * .0005))
        original_cash = ledger.cash()
        enabled = self.config.model_copy(update={"performance": PerformanceSettings(enabled=True)})
        # When: the feedback loop reviews the atomic settlement journal.
        blocked = self.engine.performance.update((), (enabled, self.now + 100))
        # Then: completed losing episodes pause selection without changing finances.
        self.assertIn(KEY, blocked)
        self.assertEqual(original_cash, ledger.cash())
        with store.transaction(ledger.path) as db:
            self.assertEqual(30, len(completed(db, self.now + 100)))

    def test_missing_policy_blocks_direct_order_admission(self):
        # Given: performance is enabled but its first reconciliation has not run.
        enabled = self.config.model_copy(update={"performance": PerformanceSettings(enabled=True)})
        self.engine.ledger.settings = enabled
        _, plan = AgentPipeline(enabled).analyze((fixture(self.now),), self.now)
        # When: a caller tries to order ahead of performance initialization.
        with self.assertRaises(RiskRejected) as caught:
            self.engine.ledger.reserve(plan, self.now)
        # Then: no missing policy defaults to unrestricted trading.
        self.assertIn("performance_policy_uninitialized", caught.exception.reasons)

    def test_forward_observation_does_not_use_stale_quote(self):
        # Given: a quote outside the freshness limit.
        enabled = self.config.model_copy(update={"performance": PerformanceSettings(enabled=True)})
        # When: observation monitoring runs.
        self.engine.performance.update((fixture(self.now - 1000),), (enabled, self.now))
        # Then: stale data cannot create validating outcomes or positions.
        with store.transaction(self.engine.ledger.path) as db:
            self.assertEqual(0, db.execute("SELECT count(*) FROM performance_shadow_positions").fetchone()[0])

    def test_open_shadow_is_retained_in_market_watch_list(self):
        # Given: a forward observation position whose market may leave the top list.
        enabled = self.config.model_copy(update={"performance": PerformanceSettings(enabled=True)})
        self.engine.performance.update((fixture(self.now),), (enabled, self.now))
        # When: the feed requests independently managed observation markets.
        managed = self.engine.performance.managed_markets()
        # Then: ranking changes do not orphan its stop/timeout observation.
        self.assertEqual(("KRW-XRP",), managed)

    def test_shadow_cap_limits_observation_load_without_using_budget(self):
        # Given: two eligible markets but one allowed shadow slot.
        limits = self.config.limits.model_copy(update={"allowed_markets": ("KRW-XRP", "KRW-ADA")})
        enabled = self.config.model_copy(update={"limits": limits,
            "performance": PerformanceSettings(enabled=True, max_shadow_positions=1)})
        quotes = (fixture(self.now), fixture(self.now).model_copy(update={"market": "KRW-ADA"}))
        # When: both eligible signals are observed.
        self.engine.performance.update(quotes, (enabled, self.now))
        # Then: workload is capped without reserving any trading capital.
        self.assertEqual(1, len(self.engine.performance.managed_markets()))
        self.assertEqual(100000, self.engine.ledger.cash())

    def test_forward_shadow_closes_and_requalifies_durable_policy(self):
        # Given: a paused strategy and a persistent observation monitor.
        enabled = self.config.model_copy(update={"performance": PerformanceSettings(enabled=True)})
        loop = self.engine.performance
        loop.update((), (enabled, self.now))
        with store.transaction(self.engine.ledger.path) as db:
            db.execute("UPDATE performance_policy SET body=? WHERE key=?", (Policy(key=KEY, paused=True, paused_at=self.now).model_dump_json(), KEY))
        # When: thirty registered signals succeed after the pause on fresh quotes.
        for i in range(30):
            at = self.now + 86401 + i * 3601
            loop.update((fixture(at),), (enabled, at))
            blocked = loop.update((fixture(at + 2, 1020),), (enabled, at + 2))
            if i < 29:
                self.assertIn(KEY, blocked)
        # Then: only complete forward qualification unlocks; no account funds move.
        self.assertNotIn(KEY, blocked)
        self.assertEqual(100000, self.engine.ledger.cash())
        with store.transaction(self.engine.ledger.path) as db:
            self.assertEqual((), store.orders(db))
            policy = Policy.model_validate_json(db.execute("SELECT body FROM performance_policy WHERE key=?", (KEY,)).fetchone()[0])
        self.assertFalse(policy.paused)
        self.assertEqual("forward_shadow_train_validation_passed", policy.reason)

    def test_performance_adaptation_cannot_enable_live_mode(self):
        # Given: live mode with performance adaptation requested.
        payload = self.config.model_dump()
        payload.update(mode="live", performance={"enabled": True})
        # When: the configuration boundary parses the request.
        with self.assertRaises(ValidationError):
            type(self.config).model_validate(payload)
        # Then: no runtime API or orders were involved.
