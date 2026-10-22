"""Small exchange-clock skew must not defeat safety gates or extend TTL."""
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
from ma_trader.llm import Choice
from ma_trader.risk import entry_reasons


class FutureAI:
    def __init__(self):
        self.calls = 0

    def choose(self, candidates, now):
        self.calls += 1
        plan = candidates[0]
        return Choice(decision="select", candidate_id=plan.analysis_id,
                      generated_at=now + .1, input_at=plan.input_at,
                      valid_until=plan.valid_until, reasons=("signal",))


class ClockSkewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = 1791909000.
        self.settings = paper_settings(self.tmp.name)

    def engine(self, suffix):
        settings = self.settings.model_copy(update={"runtime_dir": str(Path(self.tmp.name) / suffix)})
        return Engine(settings, PaperBroker(Path(self.tmp.name) / (suffix + ".sqlite3"), .0005))

    def test_observed_small_skew_passes_all_gates_and_paper_fill(self):
        # Given: valid entry signals with the measured exchange-clock offsets.
        for offset in (.085, 1.977, 3.):
            with self.subTest(offset=offset):
                engine = self.engine(str(offset))
                # When: a complete daemon tick analyzes, reserves and settles the entry.
                with redirect_stdout(StringIO()):
                    engine.tick((fixture(self.now + offset),), self.now)
                # Then: all three admission/quote gates permit a single legitimate fill.
                self.assertEqual(1, len(engine.ledger.positions()))

    def test_large_future_and_expired_quotes_still_block_entries(self):
        # Given: apparent quotes beyond the permitted clock error or age.
        for offset in (3.001, 1000., -120., -120.001):
            engine = self.engine(str(offset))
            # When: a full daemon tick processes an otherwise valid signal.
            with redirect_stdout(StringIO()):
                engine.tick((fixture(self.now + offset),), self.now)
            # Then: no entry can be reserved, submitted or credited.
            self.assertEqual((), engine.ledger.positions())
            self.assertEqual((), engine.ledger.pending())

    def test_original_input_timestamp_retained_but_ttl_not_extended(self):
        # Given: a valid observation two seconds ahead of the local clock.
        observed = self.now + 2
        # When: role reports produce the bound plan.
        _, plan = AgentPipeline(self.settings).analyze((fixture(observed),), self.now)
        # Then: provenance is retained and expiry is capped by the local analysis time.
        self.assertIsNotNone(plan)
        self.assertEqual(observed, plan.input_at)
        self.assertEqual(self.now + 120, plan.valid_until)
        self.assertIn("stale_analysis_or_data", entry_reasons(plan, self.settings, self.now + 120))

    def test_owned_position_stop_works_with_small_clock_skew(self):
        # Given: an owned position created with an ordinary quote.
        engine = self.engine("owned")
        with redirect_stdout(StringIO()):
            engine.tick((fixture(self.now),), self.now)
        self.assertEqual(1, len(engine.ledger.positions()))
        # When: a stop quote has a two-second exchange-clock lead.
        with redirect_stdout(StringIO()):
            engine.tick((fixture(self.now + 3, 980),), self.now + 1)
        # Then: deterministic management is not disabled by harmless clock skew.
        self.assertEqual((), engine.ledger.positions())

    def test_pending_buy_fills_from_small_skew_but_not_large_future(self):
        # Given: an accepted, not-yet-executable limit entry.
        for offset, expected in ((2., 1), (3.001, 0)):
            engine = self.engine("pending" + str(offset))
            with redirect_stdout(StringIO()):
                engine.tick((fixture(self.now).model_copy(update={"expected_slippage": .001}),), self.now)
            self.assertEqual(1, len(engine.ledger.pending()))
            # When: an executable quote arrives, within or beyond clock tolerance.
            with redirect_stdout(StringIO()):
                engine.tick((fixture(self.now + 1 + offset),), self.now + 1)
            # Then: only the bounded skew quote may cause a simulated fill.
            self.assertEqual(expected, len(engine.ledger.positions()))

    def test_ai_future_timestamp_is_not_relaxed(self):
        # Given: valid market data but a provider output from the future.
        provider = FutureAI()
        settings = self.settings.model_copy(update={"ai": self.settings.ai.model_copy(update={"provider": "openai-compatible"})})
        # When: the AI tries to select an eligible candidate with a future generated_at.
        reports, plan = AgentPipeline(settings, provider).analyze((fixture(self.now + 1),), self.now)
        # Then: the new market tolerance cannot authorize future AI output.
        self.assertEqual(1, provider.calls)
        self.assertIsNone(plan)
        self.assertIn("ai_failure_fail_closed", reports[-1].rejection_reasons)


if __name__ == "__main__":
    unittest.main()
