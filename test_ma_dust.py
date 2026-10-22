"""Paper quantity precision and auditable sub-quantum inventory regressions."""
import sys
import unittest
if sys.version_info < (3, 12):
    raise unittest.SkipTest("Separate multiagent runtime requires Python 3.12")
from tempfile import TemporaryDirectory
from pathlib import Path
from contextlib import redirect_stdout
from io import StringIO
import time

from ma_trader import ledger_store as store
from ma_trader.demo import paper_settings
from ma_trader.ledger import Ledger
from ma_trader.models import Position, ExitRules, OrderIntent, OrderStatus, Snapshot
from ma_trader.paper_dust import quarantine
from ma_trader.performance_history import completed
from ma_trader.quantity import floor_quantity


class QuantityPrecisionTests(unittest.TestCase):
    def test_wal_eight_decimal_position_is_not_reduced_by_one_quantum(self):
        # Given: the exact historical WAL holding that triggered floating floor loss.
        with TemporaryDirectory() as root:
            ledger = Ledger(Path(root) / "portfolio.sqlite3", paper_settings(root))
            pos = Position(market="KRW-WAL", quantity=4277.46183969, cost_krw=300000,
                opened_at=time.time(), rules=ExitRules(stop_pct=.01, take_pct=.015, max_hold_seconds=21600),
                strategy_id="trend_pullback_v1", strategy_version="1.0")
            with store.transaction(ledger.path) as db:
                store.save_position(db, pos)
            # When: a full liquidation is reserved.
            sell = ledger.approve_sell(pos, 72, time.time())
            # Then: a canonical eight-decimal quantity is preserved exactly.
            self.assertEqual(4277.46183969, sell.quantity)

    def test_substep_quantity_is_never_rounded_up(self):
        # Given: legitimate holdings with precision beyond the order step.
        quantity = 1.123456789
        # When: an orderable quantity is calculated.
        result = floor_quantity(quantity)
        # Then: it never exceeds the holding.
        self.assertEqual(1.12345678, result)
        self.assertLessEqual(result, quantity)


class PaperDustTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = time.time()
        base = paper_settings(self.tmp.name)
        self.settings = base.model_copy(update={"limits": base.limits.model_copy(update={"budget_krw": 1000000})})
        self.ledger = Ledger(Path(self.tmp.name) / "portfolio.sqlite3", self.settings)
        self.quote = Snapshot(market="KRW-WAL", observed_at=self.now, bid=71.7, ask=71.8,
            prices=(71.7,) * 30, turnover_24h=1e10, expected_slippage=0)

    def legacy_residual(self) -> None:
        """Replay real ledger fills with the historical one-step-short exit."""
        rules = ExitRules(stop_pct=.01, take_pct=.015, max_hold_seconds=21600)
        buy = OrderIntent(identifier="buy", analysis_id="buy", market="KRW-WAL", side="bid",
            price=70.1, quantity=4277.46183969, reserve_krw=300000, rules=rules,
            strategy_id="trend_pullback_v1", strategy_version="1.0", created_at=self.now - 2)
        sell = buy.model_copy(update={"identifier": "sell", "analysis_id": "sell", "side": "ask",
            "price": 71.7, "quantity": 4277.46183968, "reserve_krw": 0, "created_at": self.now - 1})
        for intent in (buy, sell):
            with store.transaction(self.ledger.path) as db:
                db.execute("INSERT INTO orders VALUES(?,?,?)", (intent.identifier, intent.analysis_id,
                    store.StoredOrder(intent=intent, remaining=intent.reserve_krw).model_dump_json()))
            self.ledger.record_submission(intent.identifier, "paper-" + intent.identifier)
            funds = intent.quantity * intent.price
            self.ledger.settle(OrderStatus(identifier=intent.identifier, uuid="paper-" + intent.identifier,
                market=intent.market, side=intent.side, state="done", executed_volume=intent.quantity,
                executed_funds=funds, paid_fee=funds * .0005))

    def test_residual_is_archived_without_cash_or_order_changes(self):
        # Given: a verified terminal paper exit leaving one quantity step.
        self.legacy_residual()
        pos = self.ledger.positions()[0]
        cash = self.ledger.cash()
        # When: the dust policy transfers inventory out of active slots.
        transfers = quarantine(self.ledger.path, (self.quote,), (self.settings, self.now))
        # Then: inventory and cost remain auditable without fabricated cash or fills.
        self.assertEqual(pos, transfers[0].position)
        self.assertEqual(cash, self.ledger.cash())
        self.assertEqual((), self.ledger.positions())
        with store.transaction(self.ledger.path) as db:
            self.assertEqual(2, len(store.orders(db)))
            self.assertEqual(1, db.execute("SELECT count(*) FROM paper_dust_inventory").fetchone()[0])

    def test_dust_replay_completes_one_episode_without_double_count(self):
        # Given: an archived residual whose previous fills and costs are intact.
        self.legacy_residual()
        quarantine(self.ledger.path, (self.quote,), (self.settings, self.now))
        # When: cleanup runs again after restart and the history is rebuilt.
        second = quarantine(self.ledger.path, (self.quote,), (self.settings, self.now + 1))
        with store.transaction(self.ledger.path) as db:
            episodes = completed(db, time.time() + 10)
        # Then: one completed outcome includes the entire original cost.
        self.assertEqual((), second)
        self.assertEqual(1, len(episodes))
        expected = 4277.46183968 * 71.7 * .9995 - 4277.46183969 * 70.1 * 1.0005
        self.assertAlmostEqual(expected, episodes[0].net_pnl)

    def test_dust_retirement_is_never_applied_in_live_mode(self):
        # Given: the same residual under a live-mode execution context.
        self.legacy_residual()
        config = self.settings.model_copy(update={"mode": "live"})
        # When: the paper-only policy is called.
        result = quarantine(self.ledger.path, (self.quote,), (config, self.now))
        # Then: live holdings are untouched.
        self.assertEqual((), result)
        self.assertEqual(1, len(self.ledger.positions()))

    def test_stale_quote_cannot_authorize_inventory_retirement(self):
        # Given: a terminal exit but only a stale price.
        self.legacy_residual()
        old = self.quote.model_copy(update={"observed_at": self.now - 1000})
        # When: cleanup evaluates the position value.
        result = quarantine(self.ledger.path, (old,), (self.settings, self.now))
        # Then: no inventory is classified without a fresh price.
        self.assertEqual((), result)
        self.assertEqual(1, len(self.ledger.positions()))

    def test_unresolved_order_prevents_dust_transfer(self):
        # Given: a residual and an uncertain same-market order.
        self.legacy_residual()
        with store.transaction(self.ledger.path) as db:
            prior = store.order(db, "sell")
            store.save_order(db, prior.model_copy(update={"state": "unknown"}))
        # When: cleanup encounters an unresolved ownership checkpoint.
        result = quarantine(self.ledger.path, (self.quote,), (self.settings, self.now))
        # Then: active inventory is retained until reconciliation.
        self.assertEqual((), result)
        self.assertEqual(1, len(self.ledger.positions()))

    def test_small_quantity_with_material_cost_is_not_retired(self):
        # Given: a small volume with accounting cost above the dust ceiling.
        self.legacy_residual()
        with store.transaction(self.ledger.path) as db:
            pos = store.positions(db)[0]
            store.save_position(db, pos.model_copy(update={"cost_krw": 10.}))
        # When: classification evaluates quantity and value independently.
        result = quarantine(self.ledger.path, (self.quote,), (self.settings, self.now))
        # Then: small quantity alone never erases a material cost basis.
        self.assertEqual((), result)
        self.assertEqual(10, self.ledger.positions()[0].cost_krw)
