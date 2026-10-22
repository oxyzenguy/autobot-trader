"""Offline bearish routing and immutable position-exit regressions."""
import sys
import unittest

if sys.version_info < (3, 12):
    raise unittest.SkipTest("Separate multiagent runtime requires Python 3.12")

from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

from ma_trader.agents import AgentPipeline
from ma_trader.broker import PaperBroker
from ma_trader.demo import fixture, paper_settings
from ma_trader.engine import Engine
from ma_trader.models import Report, Snapshot
from ma_trader.strategies import Registry, regime_of
from test_ma_agents import settings, snapshot


def declining(now: float = 1000) -> Snapshot:
    return snapshot(now).model_copy(update={"prices": tuple(1000 - i for i in range(30)),
                                           "bid": 971., "ask": 972., "expected_slippage": 0.})


class BearRegimeTests(unittest.TestCase):
    def test_decline_classified_separately_from_unknown(self):
        # Given: a gradual fall without a sudden latest-bar shock.
        quote = declining()
        # When: the classifier processes confirmed closes.
        regime = regime_of(quote)
        # Then: the taxonomy explicitly identifies a bearish market.
        self.assertEqual("bear", regime)

    def test_shock_overrides_direction(self):
        # Given: a decline ending in an abrupt fall.
        quote = declining().model_copy(update={"prices": (1000.,) * 29 + (970.,)})
        # When: the classifier processes confirmed closes.
        regime = regime_of(quote)
        # Then: shock protection takes precedence over directional classification.
        self.assertEqual("shock", regime)

    def test_range_boundary_and_uptrend_remain_compatible(self):
        # Given: exact negative one-percent drift and gradual positive drift.
        quotes = (snapshot(), snapshot().model_copy(update={"prices": tuple(1000 + i for i in range(30))}))
        # When: both are classified.
        regimes = tuple(regime_of(quote) for quote in quotes)
        # Then: the existing range boundary and upward-trend policy remain intact.
        self.assertEqual(("range", "trend"), regimes)

    def test_bear_watch_reports_survive_schema_roundtrip(self):
        # Given: no registered strategy is qualified for declining conditions.
        pipeline = AgentPipeline(settings())
        # When: a bearish candidate is analyzed.
        reports, plan = pipeline.analyze((declining(),), 1000)
        # Then: no entry is selected and the persisted report explains why.
        self.assertIsNone(plan)
        supervisor = Report.model_validate_json(next(r for r in reports if r.role == "supervisor").model_dump_json())
        self.assertEqual("bear", supervisor.regime)
        self.assertEqual("wait", supervisor.decision)
        self.assertIn("bear_no_qualified_strategy", supervisor.rejection_reasons)

    def test_bear_candidate_does_not_block_eligible_other_market(self):
        # Given: a highly ranked bearish asset and an eligible range asset.
        config = settings().model_copy(update={"limits": settings().limits.model_copy(
            update={"allowed_markets": ("KRW-XRP", "KRW-ADA")})})
        other = snapshot().model_copy(update={"market": "KRW-ADA", "turnover_24h": 1_000_000_000})
        # When: the pipeline selects among both candidates.
        _, plan = AgentPipeline(config).analyze((declining(), other), 1000)
        # Then: bearish watch is local to the asset, not a global shutdown.
        self.assertIsNotNone(plan)
        self.assertEqual("KRW-ADA", plan.market)

    def test_range_strategy_cannot_be_relabelled_for_bear_entry(self):
        # Given: a valid range plan changed by an advisory caller to a bearish asset.
        _, plan = AgentPipeline(settings()).analyze((snapshot(),), 1000)
        self.assertIsNotNone(plan)
        forged = plan.model_copy(update={"regime": "bear", "snapshot": declining()})
        # When: the independent registry validates execution eligibility.
        accepted = Registry().validate(forged)
        # Then: the advisory caller cannot repurpose an unqualified strategy.
        self.assertFalse(accepted)

    def test_existing_stop_exit_continues_in_bear_regime(self):
        # Given: an isolated paper position with its original range exit rules.
        with TemporaryDirectory() as root, redirect_stdout(StringIO()):
            now = 1791795600.
            config = paper_settings(root)
            engine = Engine(config, PaperBroker(Path(root) / "exchange.db", .0005))
            engine.tick((fixture(now),), now)
            rules = engine.ledger.positions()[0].rules
            quote = declining(now + 2)
            # When: bearish prices breach the original stop.
            engine.tick((quote,), now + 2)
            engine.tick((declining(now + 3),), now + 3)
            # Then: the position exits without a new bearish entry or widened stop.
            self.assertEqual(.01, rules.stop_pct)
            self.assertEqual((), engine.ledger.positions())
            self.assertEqual((), engine.ledger.pending())
            self.assertLess(engine.ledger.cash(), 100000)
