"""Offline live-boundary regressions using explicit fake HTTP, never network."""
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
from ma_trader.models import ExitRules, OrderIntent, Position


class FakeHTTP:
    """Mutable deterministic account fixture; records every submitted order."""
    def __init__(self, quantity=10, cash=100000):
        self.quantity, self.cash = quantity, cash
        self.posts = []

    def call(self, method, request):
        path, params = request
        if path == "/v1/accounts":
            return json.dumps([{"currency": "KRW", "balance": self.cash, "locked": 0},
                               {"currency": "XRP", "balance": self.quantity, "locked": 0}])
        if path == "/v1/orders/open":
            return "[]"
        if method == "POST":
            self.posts.append(params)
            return json.dumps({"identifier": params["identifier"], "uuid": "new-ask",
                "market": params["market"], "side": params["side"], "state": "wait",
                "executed_volume": 0, "paid_fee": 0, "trades": []})
        raise BrokerUncertain("unsupported_fake_route")


class TestLiveSafety(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = paper_settings(self.temp.name).model_copy(update={"mode": "live", "live_enabled": True})
        self.addCleanup(patch.stopall)
        patch.dict("os.environ", {"MA_LIVE_ACK": "I_ACCEPT_SPOT_RISK"}).start()
        patch("utils.account_coordination.assert_legacy_processes_coordinated").start()
        patch("utils.account_coordination.account_lock", side_effect=lambda: nullcontext()).start()
        self.rules = ExitRules(stop_pct=.01, take_pct=.02, max_hold_seconds=3600)

    def position(self):
        return Position(market="KRW-XRP", quantity=10, cost_krw=10000, opened_at=1,
                        rules=self.rules, strategy_id="range_reversion_v1", strategy_version="1.0")

    def intent(self):
        return OrderIntent(identifier="ma-test", analysis_id="exit-test", market="KRW-XRP",
            side="ask", quantity=10, price=1000, reserve_krw=0, rules=self.rules,
            strategy_id="range_reversion_v1", strategy_version="1.0", created_at=1)

    def test_manual_inventory_change_after_reconciliation_blocks_sale(self):
        # Given: exact owned inventory was verified, then manual inventory appears.
        http = FakeHTTP()
        broker = LiveBroker(self.settings, http)
        broker.reconcile_holdings((self.position(),))
        http.quantity = 12
        # When: an exit attempts to use stale ownership evidence.
        with self.assertRaises(BrokerUncertain):
            broker.submit(self.intent())
        # Then: no external order was submitted.
        self.assertEqual([], http.posts)

    def test_runtime_owners_are_distinct_for_distinct_allocations(self):
        # Given: separate configured private ledgers.
        first = LiveBroker(self.settings, FakeHTTP())
        other = self.settings.model_copy(update={"runtime_dir": str(Path(self.temp.name) / "other")})
        # When: second owner is constructed without any network.
        second = LiveBroker(other, FakeHTTP())
        # Then: its claim cannot replace the first allocation.
        self.assertNotEqual(first.owner, second.owner)

    def test_cash_claim_failure_does_not_block_verified_position_exit(self):
        # Given: an isolated filled position and a cash allocation deficit.
        paper = self.settings.model_copy(update={"mode": "paper", "live_enabled": False})
        engine = Engine(paper, PaperBroker(Path(self.temp.name) / "exchange.sqlite3", .0005))
        now = 1791795600.
        with redirect_stdout(StringIO()):
            engine.tick((fixture(now),), now)
        owned = engine.ledger.positions()[0]
        http = FakeHTTP(quantity=owned.quantity, cash=0)
        broker = LiveBroker(self.settings, http)
        engine.settings = self.settings
        engine.ledger.settings = self.settings
        engine.broker = broker
        # When: deterministic stop triggers despite failed cash claiming.
        with patch.object(broker, "claim_cash", side_effect=BrokerUncertain("cash_deficit")), redirect_stdout(StringIO()):
            engine.tick((fixture(now + 2, 980),), now + 2)
        # Then: safe owned exit is still submitted; new entries stay disabled.
        self.assertEqual(1, len(http.posts))
        self.assertEqual("ask", http.posts[0]["side"])
        self.assertFalse(engine.entry_enabled)

    def test_live_disabled_rejects_constructor_before_exchange_use(self):
        # Given: default paper/disabled activation settings.
        settings = paper_settings(self.temp.name)
        http = FakeHTTP()
        # When: live adapter construction is requested.
        with self.assertRaises(BrokerUncertain):
            LiveBroker(settings, http)
        # Then: no order is possible.
        self.assertEqual([], http.posts)
