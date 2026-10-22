"""Offline behavioural tests: registry, structured pipeline, AI and quota gates."""
import sys
import unittest
if sys.version_info < (3, 12):
    raise unittest.SkipTest("Separate multiagent runtime requires Python 3.12")
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from ma_trader.agents import AgentPipeline, StrategyState
from ma_trader.llm import AIMeter, BoundedProvider, Choice, ChoiceError
from ma_trader.models import ExitRules, Plan, Snapshot
from ma_trader.settings import AISettings, Limits, Settings
from ma_trader.llm_http import OpenAICompatibleProvider


def settings() -> Settings:
    return Settings(limits=Limits(budget_krw=100000, max_order_krw=10000,
          max_market_krw=20000, max_trade_loss_krw=500, daily_loss_krw=2000,
          max_drawdown_krw=5000, allowed_markets=("KRW-XRP",), excluded_markets=()))


def snapshot(now: float = 1000) -> Snapshot:
    return Snapshot(market="KRW-XRP", observed_at=now, bid=990, ask=991,
            prices=tuple(1000 + (i % 2) * 2 for i in range(19)) + (990,),
            turnover_24h=2000000000, expected_slippage=.0001)


class TimeoutProvider:
    def choose(self, candidates: tuple[Plan, ...], now: float) -> Choice:
        raise TimeoutError("offline timeout fixture")


class UnknownProvider:
    def choose(self, candidates: tuple[Plan, ...], now: float) -> Choice:
        return Choice(decision="select", candidate_id="invented", generated_at=now,
                      input_at=candidates[0].input_at, valid_until=candidates[0].valid_until, reasons=("fake",))


class ExpiredProvider:
    def choose(self, candidates: tuple[Plan, ...], now: float) -> Choice:
        return Choice(decision="select", candidate_id=candidates[0].analysis_id,
                      generated_at=now - 10, input_at=now - 20,
                      valid_until=now - 1, reasons=("fake",))


class ConflictProvider:
    def choose(self, candidates: tuple[Plan, ...], now: float) -> Choice:
        return Choice(decision="wait", generated_at=now,
                      input_at=candidates[0].input_at,
                      valid_until=candidates[0].valid_until, reasons=("opposing_evidence",))


class SelectWithObjectionProvider:
    def choose(self, candidates: tuple[Plan, ...], now: float) -> Choice:
        return Choice(decision="select", candidate_id=candidates[0].analysis_id,
                      generated_at=now, input_at=candidates[0].input_at,
                      valid_until=candidates[0].valid_until, reasons=("signal",),
                      objections=("unresolved_execution_cost",))


class AgentTests(unittest.TestCase):
    def test_selection_with_unresolved_opposition_remains_wait(self):
        # Given: an advisory selection accompanied by unresolved opposition.
        configured = settings().model_copy(update={"ai": AISettings(provider="openai-compatible")})
        pipeline = AgentPipeline(configured, SelectWithObjectionProvider())
        # When: the supervisor receives the contradictory recommendation.
        _, plan = pipeline.analyze((snapshot(),), 1001)
        # Then: it has no execution authority.
        self.assertIsNone(plan)

    def test_current_quote_chase_gap_blocks_still_eligible_historical_signal(self):
        # Given
        pipeline = AgentPipeline(settings())
        quote = snapshot().model_copy(update={"bid": 1020., "ask": 1021.})
        # When
        reports, plan = pipeline.analyze((quote,), 1001)
        # Then
        self.assertIsNone(plan)
        self.assertTrue(any("live_price_shock_vs_confirmed_close" in r.rejection_reasons for r in reports))

    def test_current_quote_crash_gap_blocks_historical_range_signal(self):
        # Given
        pipeline = AgentPipeline(settings())
        quote = snapshot().model_copy(update={"bid": 950., "ask": 951.})
        # When
        reports, plan = pipeline.analyze((quote,), 1001)
        # Then
        self.assertIsNone(plan)
        self.assertTrue(any("live_price_shock_vs_confirmed_close" in r.rejection_reasons for r in reports))

    def test_pipeline_plan_is_accepted_by_real_ledger_with_fee_in_order_cap(self):
        # Given
        from ma_trader.ledger import Ledger
        configured = settings()
        _, plan = AgentPipeline(configured).analyze((snapshot(),), 1001)
        with TemporaryDirectory() as directory:
            ledger = Ledger(Path(directory) / "ledger.sqlite", configured)
            # When
            intent = ledger.reserve(plan, 1001)
            # Then
            self.assertLessEqual(intent.reserve_krw, 10000)
            self.assertGreaterEqual(intent.quantity * intent.price, 5000)

    def test_expired_ai_metadata_blocks_entry(self):
        # Given
        configured = settings().model_copy(update={"ai": AISettings(provider="openai-compatible")})
        pipeline = AgentPipeline(configured, ExpiredProvider())
        # When
        _, plan = pipeline.analyze((snapshot(),), 1001)
        # Then
        self.assertIsNone(plan)

    def test_opposing_ai_view_results_in_wait(self):
        # Given
        configured = settings().model_copy(update={"ai": AISettings(provider="openai-compatible")})
        pipeline = AgentPipeline(configured, ConflictProvider())
        # When
        _, plan = pipeline.analyze((snapshot(),), 1001)
        # Then
        self.assertIsNone(plan)

    def test_ai_missing_price_bounds_blocks_before_network(self):
        # Given
        provider = OpenAICompatibleProvider(AISettings(provider="openai-compatible"))
        # When / Then
        with self.assertRaises(ChoiceError):
            provider.choose((), 1001)

    def test_ai_cost_bound_blocks_before_network(self):
        # Given
        provider = OpenAICompatibleProvider(AISettings(provider="openai-compatible",
                    input_usd_per_million=1000, output_usd_per_million=1000,
                    max_cost_per_call_usd=.01))
        # When / Then
        with self.assertRaises(ChoiceError):
            provider.choose((), 1001)

    def test_registered_range_produces_all_eight_roles(self):
        # Given
        pipeline = AgentPipeline(settings())
        # When
        reports, plan = pipeline.analyze((snapshot(),), 1001)
        # Then
        self.assertEqual({r.role for r in reports}, {"market_watch", "market_regime", "asset_selection", "strategy_selection", "critic", "supervisor", "position_manager", "reflection"})
        self.assertEqual(plan.strategy_id, "range_reversion_v1")
        self.assertFalse(plan.risk_passed)

    def test_stale_data_never_produces_order_plan(self):
        # Given
        pipeline = AgentPipeline(settings())
        # When
        reports, plan = pipeline.analyze((snapshot(),), 1300)
        # Then
        self.assertIsNone(plan)
        self.assertTrue(any("stale_or_future_data" in r.rejection_reasons for r in reports))

    def test_unknown_strategy_id_rejected_by_registry(self):
        # Given
        pipeline = AgentPipeline(settings())
        _, plan = pipeline.analyze((snapshot(),), 1001)
        # When
        valid = pipeline.registry.validate(plan.model_copy(update={"strategy_id": "invented"}))
        # Then
        self.assertFalse(valid)

    def test_changed_exit_parameters_rejected_by_registry(self):
        # Given
        pipeline = AgentPipeline(settings())
        _, plan = pipeline.analyze((snapshot(),), 1001)
        altered = plan.model_copy(update={"exits": ExitRules(stop_pct=.09, take_pct=.008, max_hold_seconds=7200)})
        # When
        valid = pipeline.registry.validate(altered)
        # Then
        self.assertFalse(valid)

    def test_ai_timeout_does_not_fall_back_to_rules_entry(self):
        # Given
        configured = settings().model_copy(update={"ai": AISettings(provider="openai-compatible")})
        pipeline = AgentPipeline(configured, TimeoutProvider())
        # When
        reports, plan = pipeline.analyze((snapshot(),), 1001)
        # Then
        self.assertIsNone(plan)
        self.assertTrue(any("ai_failure_fail_closed" in r.rejection_reasons for r in reports))

    def test_ai_invented_candidate_does_not_enter(self):
        # Given
        configured = settings().model_copy(update={"ai": AISettings(provider="openai-compatible")})
        pipeline = AgentPipeline(configured, UnknownProvider())
        # When
        _, plan = pipeline.analyze((snapshot(),), 1001)
        # Then
        self.assertIsNone(plan)

    def test_strategy_minimum_hold_survives_restoration(self):
        # Given
        pipeline = AgentPipeline(settings())
        pipeline.restore(StrategyState("trend_pullback_v1", 999))
        # When
        reports, plan = pipeline.analyze((snapshot(),), 1001)
        # Then
        self.assertIsNone(plan)
        self.assertTrue(any("minimum_strategy_hold" in r.rejection_reasons for r in reports))

    def test_spread_and_fee_critic_blocks_expensive_entry(self):
        # Given
        configured = settings().model_copy(update={"limits": settings().limits.model_copy(update={"fee_rate": .01})})
        pipeline = AgentPipeline(configured)
        # When
        reports, plan = pipeline.analyze((snapshot(),), 1001)
        # Then
        self.assertIsNone(plan)
        self.assertTrue(any("expected_move_does_not_cover_costs" in r.rejection_reasons for r in reports))

    def test_unqualified_live_strategy_never_produces_plan(self):
        # Given
        pipeline = AgentPipeline(settings().model_copy(update={"mode": "live", "live_enabled": True}))
        # When
        _, plan = pipeline.analyze((snapshot(),), 1001)
        # Then
        self.assertIsNone(plan)

    def test_quota_concurrent_reservations_are_atomic(self):
        # Given
        with TemporaryDirectory() as directory:
            meter = AIMeter(Path(directory) / "quota.db", AISettings(max_calls_per_day=2, max_cost_per_day_usd=.1, max_cost_per_call_usd=.05))
            # When
            with ThreadPoolExecutor(max_workers=8) as pool:
                results = tuple(pool.map(meter.reserve, (1000,) * 8))
            # Then
            self.assertEqual(sum(results), 2)

    def test_quota_persists_across_provider_restarts(self):
        # Given
        with TemporaryDirectory() as directory:
            path = Path(directory) / "quota.db"
            limits = AISettings(max_calls_per_day=1)
            AIMeter(path, limits).reserve(1000)
            # When
            approved = AIMeter(path, limits).reserve(1001)
            # Then
            self.assertFalse(approved)

    def test_quota_blocks_network_before_provider_call(self):
        # Given
        with TemporaryDirectory() as directory:
            provider = BoundedProvider(TimeoutProvider(), AIMeter(Path(directory) / "quota.db", AISettings(max_calls_per_day=0)))
            # When / Then
            with self.assertRaises(ChoiceError):
                provider.choose((), 1000)
