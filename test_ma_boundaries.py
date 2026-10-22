"""Offline adversarial boundaries: fresh quotes, expiry and verified exchange fills."""
import sys
import unittest
if sys.version_info < (3, 12):
    raise unittest.SkipTest("Separate multiagent runtime requires Python 3.12")
from contextlib import nullcontext, redirect_stdout
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ma_trader.broker import BrokerUncertain, PaperBroker
from ma_trader.demo import fixture, paper_settings
from ma_trader.engine import Engine
from ma_trader.live_broker import LiveBroker
from ma_trader.models import OrderIntent
from ma_trader.upbit_http import UpbitHTTP
from test_ma_ledger import plan, settings


class CountingPaper(PaperBroker):
    """Tracks the observable submission boundary, without replacing accounting."""

    def __init__(self, path: Path):
        super().__init__(path, .0005)
        self.submissions = 0

    def submit(self, intent: OrderIntent):
        self.submissions += 1
        return super().submit(intent)


class FakeHTTP(UpbitHTTP):
    """In-memory wire fixture; deliberately never initializes a network session."""

    def __init__(self, text: str):
        self.text = text
        self.calls: list[str] = []

    def call(self, method: str, request: tuple[str, dict[str, str]]) -> str:
        self.calls.append(method)
        return self.text


class BoundaryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.now = 1791795600.
        self.settings = paper_settings(self.directory.name)
        self.broker = CountingPaper(Path(self.directory.name) / "exchange.sqlite")
        self.engine = Engine(self.settings, self.broker)

    def waiting_entry(self) -> None:
        snapshot = fixture(self.now).model_copy(update={"expected_slippage": .001})
        with redirect_stdout(StringIO()):
            self.engine.tick((snapshot,), self.now)
        self.assertEqual(1, len(self.engine.ledger.pending()))
        self.assertEqual((), self.engine.ledger.positions())

    def test_stale_quote_cannot_fill_a_pending_paper_order(self):
        # Given a valid but not-yet-marketable pending entry.
        self.waiting_entry()
        # When a cheap quote predating the freshness window arrives.
        with redirect_stdout(StringIO()):
            self.engine.tick((fixture(self.now - 1000, 900),), self.now + 1)
        # Then no ownership is credited from the stale observation.
        self.assertEqual((), self.engine.ledger.positions())
        self.assertEqual(1, len(self.engine.ledger.pending()))

    def test_future_quote_cannot_fill_a_pending_paper_order(self):
        # Given a valid but not-yet-marketable pending entry.
        self.waiting_entry()
        # When an apparent future quote crosses the entry price.
        with redirect_stdout(StringIO()):
            self.engine.tick((fixture(self.now + 1000, 900),), self.now + 1)
        # Then no fill may be inferred from future data.
        self.assertEqual((), self.engine.ledger.positions())

    def test_expiry_after_reservation_blocks_submission_boundary(self):
        # Given a reserved, once-valid plan and an advancing injected monotonic clock.
        _, candidate = self.engine.pipeline.analyze((fixture(self.now),), self.now)
        self.assertIsNotNone(candidate)
        intent = self.engine.ledger.reserve(candidate, self.now)
        self.engine.clock_anchor = (self.now, 0.)
        self.engine.monotonic = lambda: 121.
        # When execution reaches the submission boundary after expiry.
        with redirect_stdout(StringIO()):
            self.engine._submit(intent)
        # Then no broker submission occurs and only the provably local reservation is released.
        self.assertEqual(0, self.broker.submissions)
        self.assertEqual("cancel", self.engine.ledger.order_state(intent.identifier))
        self.assertEqual((), self.engine.ledger.pending())

    def test_cash_sync_error_does_not_reopen_a_terminal_fill(self):
        # Given an owned entry after a confirmed exchange fill.
        with redirect_stdout(StringIO()):
            self.engine.tick((fixture(self.now),), self.now)
        position = self.engine.ledger.positions()[0]
        sell = self.engine.ledger.approve_sell(position, 1020, self.now + 1)
        self.broker.update((fixture(self.now + 1, 1020),))
        self.engine.clock_anchor = (self.now + 1, 0.)
        self.engine.monotonic = lambda: 0.
        # When cash-claim synchronization fails after successful exit settlement.
        with patch.object(self.engine, "_cash_claim", side_effect=BrokerUncertain("cash unavailable")), redirect_stdout(StringIO()):
            self.engine._submit(sell)
        # Then the exit stays terminal and only new-entry permission is revoked.
        self.assertEqual("done", self.engine.ledger.order_state(sell.identifier))
        self.assertEqual((), self.engine.ledger.positions())
        self.assertFalse(self.engine.entry_enabled)


class LiveParsingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        candidate = plan()
        self.intent = OrderIntent(identifier="owned-entry", analysis_id="analysis", market="KRW-XRP",
            side="bid", price=100, quantity=200, reserve_krw=20010, rules=candidate.exits,
            strategy_id=candidate.strategy_id, strategy_version=candidate.strategy_version,
            created_at=1760000000, valid_until=1760000060)

    def response(self, **changes: str | float | list[dict[str, float]]) -> str:
        body = {"identifier": self.intent.identifier, "uuid": "owned-uuid", "market": "KRW-XRP",
            "side": "bid", "state": "wait", "executed_volume": 80., "paid_fee": 4.,
            "trades": [{"volume": 80., "funds": 8000.}]}
        body.update(changes)
        return json.dumps(body)

    def broker(self, wire: str) -> tuple[LiveBroker, FakeHTTP]:
        configured = settings().model_copy(update={"mode": "live", "live_enabled": True,
            "runtime_dir": self.directory.name})
        http = FakeHTTP(wire)
        with patch.dict("os.environ", {"MA_LIVE_ACK": "I_ACCEPT_SPOT_RISK"}), patch(
            "utils.account_coordination.assert_legacy_processes_coordinated"):
            return LiveBroker(configured, http), http

    def test_verified_partial_uses_cumulative_trade_funds(self):
        # Given an exchange partial with matching identity and trade totals.
        broker, _ = self.broker(self.response())
        # When transport JSON crosses the parser boundary.
        status = broker.parse(self.response(), self.intent)
        # Then actual cumulative quantities/funds/fees remain partial, not requested totals.
        self.assertEqual((80., 8000., 4., "wait"),
            (status.executed_volume, status.executed_funds, status.paid_fee, status.state))

    def test_unowned_identifier_cannot_credit_a_fill(self):
        # Given trade data belonging to a different client identifier.
        broker, _ = self.broker(self.response(identifier="manual-order"))
        # When attempting to bind it to an owned intent, then the parser rejects.
        with self.assertRaises(BrokerUncertain):
            broker.parse(self.response(identifier="manual-order"), self.intent)

    def test_trade_volume_mismatch_cannot_credit_a_fill(self):
        # Given advertised fills that exceed the provided trade evidence.
        broker, _ = self.broker(self.response(executed_volume=100.))
        # When parsing the evidence, then cumulative volume is rejected.
        with self.assertRaises(BrokerUncertain):
            broker.parse(self.response(executed_volume=100.), self.intent)

    def test_cancel_ack_without_terminal_lookup_retains_uncertainty(self):
        # Given cancellation acknowledgement but a still-open subsequent GET.
        broker, http = self.broker(self.response())
        # When cancelling, then the DELETE alone cannot establish cancellation.
        with patch("utils.account_coordination.account_lock", return_value=nullcontext()):
            with self.assertRaises(BrokerUncertain):
                broker.cancel(self.intent)
        self.assertEqual(["DELETE", "GET"], http.calls)

    def test_expired_live_entry_is_not_posted(self):
        # Given expired entry authorization and no live credentials/session.
        broker, http = self.broker(self.response())
        # When the wall clock reaches the POST boundary, then HTTP sees no order request.
        with patch("utils.account_coordination.account_lock", return_value=nullcontext()), patch(
            "utils.account_coordination.assert_legacy_processes_coordinated"), patch(
            "ma_trader.live_broker.time.time", return_value=1760000060.):
            with self.assertRaises(BrokerUncertain):
                broker.submit(self.intent)
        self.assertEqual([], http.calls)
