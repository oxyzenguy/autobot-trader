"""Isolated read-only paper dashboard regressions on the legacy UI runtime."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from utils.ma_dashboard_reader import read_paper, ViewError, Service


class TestPaperDashboard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = Path(self.tmp.name)
        self.path = self.app / "portfolio.sqlite3"
        (self.app / "settings.json").write_text(json.dumps({"mode": "paper", "live_enabled": False,
            "runtime_dir": str(self.app), "limits": {"budget_krw": 3000000, "fee_rate": .0005}}))
        with closing(sqlite3.connect(self.path)) as db, db:
            db.executescript("CREATE TABLE account(id INTEGER PRIMARY KEY,body TEXT); CREATE TABLE positions(market TEXT,body TEXT); CREATE TABLE orders(body TEXT); CREATE TABLE marks(market TEXT,price REAL,at REAL); CREATE TABLE events(id INTEGER PRIMARY KEY,at REAL,kind TEXT,payload TEXT);")
            db.execute("INSERT INTO account VALUES(1,?)", (json.dumps({"cash": 3000000, "halt": False, "recovery": False}),))

    def test_empty_account_is_visible_without_writing_database(self):
        # Given: an initialized, empty paper account.
        before = self.path.read_bytes()
        # When: the view is read.
        result = read_paper(self.app, 1000)
        # Then: full cash is shown and storage remains byte-identical.
        self.assertEqual((3000000, 3000000, (), ()), (result.cash, result.equity, result.positions, result.orders))
        self.assertEqual(before, self.path.read_bytes())

    def test_missing_database_is_not_created(self):
        # Given: settings referring to a missing ledger.
        self.path.unlink()
        # When: the view is requested.
        with self.assertRaises(ViewError):
            read_paper(self.app, 1000)
        # Then: no replacement empty ledger is created.
        self.assertFalse(self.path.exists())

    def add_position(self, mark_at):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("INSERT INTO positions VALUES('KRW-XRP',?)", (json.dumps({"market": "KRW-XRP", "quantity": 100, "cost_krw": 99000,
                "opened_at": 900, "strategy_id": "range_reversion_v1", "rules": {"stop_pct": .01, "take_pct": .008}}),))
            db.execute("INSERT INTO marks VALUES('KRW-XRP',1000,?)", (mark_at,))

    def test_fee_inclusive_equity_and_pending_cash_reservation(self):
        # Given: a marked position and one partially filled buy with remaining reservation.
        self.add_position(999)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("INSERT INTO orders VALUES(?)", (json.dumps({"state": "wait", "remaining": 5000, "volume": 1, "intent": {"side": "bid", "market": "KRW-XRP", "price": 1000}}),))
        # When: the account view is calculated.
        result = read_paper(self.app, 1000)
        # Then: held equity includes estimated exit fees and reservations stay separate from cash.
        self.assertEqual((3099950, 3000000, 5000), (result.equity, result.cash, result.reserved))

    def test_stale_position_price_does_not_fake_zero_profit(self):
        # Given: a position whose quote is too old.
        self.add_position(800)
        # When: the account is viewed at 1000.
        result = read_paper(self.app, 1000)
        # Then: evaluation is explicitly unavailable, not zero or stale-marked.
        self.assertIsNone(result.equity)

    def test_nan_cash_fails_closed(self):
        # Given: corrupted nonfinite cash in the ledger.
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("UPDATE account SET body=?", ('{"cash":NaN,"halt":false,"recovery":false}',))
        # When/Then: the reader refuses a seemingly valid financial display.
        with self.assertRaises(ViewError):
            read_paper(self.app, 1000)

    def test_partial_fill_realized_deltas_are_summed_once(self):
        # Given: two sell-fill accounting deltas and a zero-volume terminal checkpoint.
        with closing(sqlite3.connect(self.path)) as db, db:
            for volume, pnl in ((2, 50), (3, 75), (0, 0)):
                db.execute("INSERT INTO events(at,kind,payload) VALUES(1000,'fill',?)", (json.dumps({
                    "market": "KRW-XRP", "side": "ask", "delta_volume": volume,
                    "delta_funds": 1000 * volume, "delta_fee": volume * .5, "realized_pnl_delta": pnl}),))
        # When: the view is read.
        view = read_paper(self.app, 1000)
        # Then: cumulative volume is not counted again and the zero-fill event is not a trade.
        self.assertEqual((125, 2), (view.realized, len(view.fills)))

    def test_missing_ledger_panel_shows_warning_not_fake_metrics(self):
        # Given: a missing ledger behind the real panel.
        from streamlit.testing.v1 import AppTest
        self.path.unlink()
        with patch("utils.ma_dashboard_panel.DEFAULT_APP", self.app):
            # When: the panel renders.
            app = AppTest.from_string("from utils.ma_dashboard_panel import render_ma_paper\nrender_ma_paper()").run()
        # Then: no invented balances or uncaught exceptions are shown.
        self.assertEqual((0, 0, 1), (len(app.exception), len(app.metric), len(app.warning)))

    def test_panel_renders_empty_running_and_missing_ledger_states(self):
        # Given: the actual Streamlit panel, isolated data and an observed process.
        from streamlit.testing.v1 import AppTest
        source = "from utils.ma_dashboard_panel import render_ma_paper\nrender_ma_paper()"
        with patch("utils.ma_dashboard_panel.DEFAULT_APP", self.app), patch("utils.ma_dashboard_panel.service_status", return_value=Service(True, "1", True)):
            # When: the real panel is rendered.
            app = AppTest.from_string(source).run()
        # Then: metrics and empty states render without an exception.
        self.assertEqual(0, len(app.exception))
        self.assertEqual("3,000,000원", app.metric[0].value)
        self.assertEqual(3, len(app.info))


if __name__ == "__main__":
    unittest.main()
