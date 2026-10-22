"""Offline complete-cycle and ambiguous exchange regressions; no network access."""
import sys
import unittest
if sys.version_info < (3, 12):
    raise unittest.SkipTest("Separate multiagent runtime requires Python 3.12")
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, patch
import tempfile
import json
import unittest
from ma_trader.agents import AgentPipeline
from ma_trader.broker import BrokerUncertain, PaperBroker
from ma_trader.demo import fixture, paper_settings
from ma_trader.engine import Engine
from ma_trader.models import OrderIntent


class LoseResponse(PaperBroker):
    def __init__(self, path, fee):
        super().__init__(path, fee)
        self.submissions = 0

    def submit(self, intent):
        self.submissions += 1
        super().submit(intent)
        raise BrokerUncertain("accepted but acknowledgement lost")


class TestExecution(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = paper_settings(self.tmp.name)
        self.now = 1791795600.

    def engine(self, broker=None):
        return Engine(self.settings, broker or PaperBroker(Path(self.tmp.name) / "exchange.db", .0005))

    def test_waiting_exit_is_managed_without_duplicate_rejection(self):
        # Given: an owned position whose sell limit remains unfilled.
        engine = self.engine()
        with redirect_stdout(StringIO()):
            engine.tick((fixture(self.now),), self.now)
            engine.tick((fixture(self.now + 2, 980).model_copy(update={"expected_slippage": .0001}),), self.now + 2)
        original = engine.ledger.pending()[0]
        output = StringIO()
        # When: the next loop still sees a stop breach and an unfilled exit.
        with redirect_stdout(output):
            engine.tick((fixture(self.now + 3, 980).model_copy(update={"expected_slippage": .0001}),), self.now + 3)
            engine.tick((fixture(self.now + 4, 980).model_copy(update={"expected_slippage": .0001}),), self.now + 4)
        # Then: the same order is managed; no new exit or error is emitted.
        events = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual((original,), engine.ledger.pending())
        self.assertFalse(any(e["event"] in ("exit_blocked", "position_exit") for e in events))
        waiting = next(e["payload"] for e in events if e["event"] == "position_exit_pending")
        self.assertEqual(1, sum(e["event"] == "position_exit_pending" for e in events))
        self.assertEqual(original.identifier, waiting["identifier"])
        self.assertEqual("paper-" + original.identifier, waiting["uuid"])

    def test_waiting_exit_finishes_after_restart_without_new_order(self):
        # Given: a durable unfilled sell order and its original exit contract.
        engine = self.engine()
        with redirect_stdout(StringIO()):
            engine.tick((fixture(self.now),), self.now)
            engine.tick((fixture(self.now + 2, 980).model_copy(update={"expected_slippage": .0001}),), self.now + 2)
        original = engine.ledger.pending()[0]
        # When: a restarted engine receives an executable quote.
        with redirect_stdout(StringIO()):
            restarted = self.engine()
            restarted.tick((fixture(self.now + 3, 980),), self.now + 3)
        # Then: the original exit settles, leaving no owned or pending quantity.
        self.assertEqual("done", restarted.ledger.order_state(original.identifier))
        self.assertEqual((), restarted.ledger.positions())
        self.assertEqual((), restarted.ledger.pending())

    def test_cli_uses_post_fetch_clock_for_snapshot_validation(self):
        # Given: a public quote arriving after the beginning of a network fetch.
        from ma_trader import __main__ as cli
        from ma_trader.ledger import Ledger
        config_path = Path(self.tmp.name) / "settings.json"
        config_path.write_text(self.settings.model_dump_json())
        clock = Mock()
        clock.time.side_effect = [self.now, self.now + 1]
        feed = Mock(errors=())
        feed.read.return_value = (fixture(self.now + .5),)
        # When: the real one-cycle CLI runs with offline transport and feed doubles.
        with patch.object(cli, "time", clock), patch.object(cli, "UpbitHTTP"), patch.object(cli, "PublicFeed", return_value=feed), redirect_stdout(StringIO()):
            cli.run(config_path, once=True)
        # Then: the legitimate quote is not rejected as coming from the future.
        self.assertEqual(1, len(Ledger(Path(self.tmp.name) / "portfolio.sqlite3", self.settings).positions()))

    def test_bad_advisory_checkpoint_does_not_prevent_owned_stop_exit(self):
        # Given: an owned position and a corrupted non-financial AI selection checkpoint.
        import sqlite3
        from contextlib import closing
        engine = self.engine()
        with redirect_stdout(StringIO()):
            engine.tick((fixture(self.now),), self.now)
        with closing(sqlite3.connect(engine.ledger.path)) as db, db:
            db.execute("UPDATE engine_state SET value=? WHERE key='strategy'", ('{"activated_at":"broken"}',))
        # When: the runner restarts and sees a stop condition.
        with redirect_stdout(StringIO()):
            restarted = self.engine()
            restarted.tick((fixture(self.now + 2, 980),), self.now + 2)
        # Then: new analysis is blocked but immutable existing exit rules still execute.
        self.assertFalse(restarted.selection_valid)
        self.assertEqual((), restarted.ledger.positions())

    def test_waiting_buy_is_cancelled_when_entry_range_invalidated(self):
        # Given: a valid signal whose limit cannot fill through the estimated spread depth.
        engine = self.engine()
        quote = fixture(self.now).model_copy(update={"expected_slippage": .0001})
        with redirect_stdout(StringIO()):
            engine.tick((quote,), self.now)
        self.assertEqual(1, len(engine.ledger.pending()))
        # When: the next quote is outside the persisted entry interval.
        outside = fixture(self.now + 2, 990)
        with redirect_stdout(StringIO()):
            engine.tick((outside,), self.now + 2)
        # Then: the original order is cancelled, not filled at an invalidated entry.
        self.assertEqual((), engine.ledger.pending())
        self.assertEqual((), engine.ledger.positions())
        self.assertEqual(100000, engine.ledger.cash())

    def test_existing_high_ranked_position_does_not_starve_other_asset(self):
        # Given: two allowed markets and sufficient separate exposure limits.
        limits = self.settings.limits.model_copy(update={"allowed_markets": ("KRW-XRP", "KRW-ADA"),
            "max_positions": 2, "order_cooldown_seconds": 0, "strategy_cooldown_seconds": 0})
        self.settings = self.settings.model_copy(update={"limits": limits})
        engine = self.engine()
        with redirect_stdout(StringIO()):
            engine.tick((fixture(self.now),), self.now)
        # When: the owned XRP is still highest ranked but ADA now has a signal.
        ada = fixture(self.now + 2).model_copy(update={"market": "KRW-ADA", "turnover_24h": 2_000_000_000})
        with redirect_stdout(StringIO()):
            engine.tick((fixture(self.now + 2), ada), self.now + 2)
        # Then: duplicate exposure is skipped before selection, not retried indefinitely.
        self.assertEqual({"KRW-XRP", "KRW-ADA"}, {p.market for p in engine.ledger.positions()})

    def test_end_to_end_select_reserve_fill_exit_and_reflection(self):
        # Given: a valid range-reversion signal and isolated paper account.
        engine = self.engine()
        # When: entry then an executable take-profit quote are observed.
        with redirect_stdout(StringIO()):
            engine.tick((fixture(self.now),), self.now)
            self.assertEqual(1, len(engine.ledger.positions()))
            engine.tick((fixture(self.now + 2, 1020),), self.now + 2)
        # Then: the owned position is gone and net cash includes both fees.
        self.assertEqual((), engine.ledger.positions())
        self.assertGreater(engine.ledger.cash(), 100000)
        self.assertEqual((), engine.ledger.pending())

    def test_lost_acknowledgement_restart_does_not_resubmit(self):
        # Given: exchange acceptance followed by a missing response.
        broker = LoseResponse(Path(self.tmp.name) / "exchange.db", .0005)
        engine = self.engine(broker)
        with redirect_stdout(StringIO()):
            engine.tick((fixture(self.now),), self.now)
        self.assertEqual(1, broker.submissions)
        self.assertEqual(1, len(engine.ledger.pending()))
        # When: a new runner reconciles the same durable identifier.
        restarted = self.engine(broker)
        with redirect_stdout(StringIO()):
            restarted.tick((fixture(self.now + 1),), self.now + 1)
        # Then: one credited position and only the original submission exist.
        self.assertEqual(1, len(restarted.ledger.positions()))
        self.assertEqual(1, broker.submissions)

    def test_ai_unavailable_does_not_disable_position_exit(self):
        # Given: an owned position created before switching to unavailable AI.
        engine = self.engine()
        with redirect_stdout(StringIO()):
            engine.tick((fixture(self.now),), self.now)
        old = engine.ledger.positions()[0].rules
        updated = self.settings.model_copy(update={"ai": self.settings.ai.model_copy(update={"provider": "openai-compatible"})})
        engine.settings = updated
        engine.ledger.settings = updated
        engine.pipeline = AgentPipeline(updated, provider=None)
        # When: a stop-loss quote arrives while no AI provider is available.
        with redirect_stdout(StringIO()):
            engine.tick((fixture(self.now + 2, 980),), self.now + 2)
        # Then: deterministic exit runs first; AI cannot widen the stored stop.
        self.assertEqual(.01, old.stop_pct)
        self.assertEqual((), engine.ledger.positions())
        self.assertLess(engine.ledger.cash(), 100000)

    def test_stale_snapshot_and_missing_budget_block_entry(self):
        # Given: no configured budgets and an old observation.
        self.settings = self.settings.model_copy(update={"limits": self.settings.limits.model_copy(update={"budget_krw": None})})
        engine = self.engine()
        # When: the runner processes the unusable signal.
        with redirect_stdout(StringIO()):
            engine.tick((fixture(self.now - 1000),), self.now)
        # Then: no order is even reserved or submitted.
        self.assertEqual((), engine.ledger.pending())
        self.assertEqual((), engine.ledger.positions())

    def test_same_identifier_with_different_paper_order_is_rejected(self):
        # Given: a confirmed simulated order in persistent exchange storage.
        engine = self.engine()
        with redirect_stdout(StringIO()):
            engine.tick((fixture(self.now),), self.now)
        from ma_trader import ledger_store as store
        with store.transaction(engine.ledger.path) as db:
            original = store.orders(db)[0].intent
        changed = original.model_copy(update={"quantity": original.quantity + 1})
        # When: the identifier is reused for a different request.
        with self.assertRaises(BrokerUncertain):
            engine.broker.lookup(changed)
        # Then: the original fill remains authoritative, not overwritten.
        self.assertEqual(original.quantity, engine.ledger.positions()[0].quantity)


if __name__ == "__main__":
    unittest.main()
