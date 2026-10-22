"""
Unit tests for the Decision Settlement & Reflection Engine (strategy/settlement_reflection.py)
"""

import os
import shutil
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from strategy import settlement_reflection as engine


class TestSettlementReflection(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.decision_db = Path(self.test_dir) / "test_decision_history.db"
        self.trade_db = Path(self.test_dir) / "test_trade_history.db"
        self._init_fake_trade_db()

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def _init_fake_trade_db(self):
        conn = sqlite3.connect(str(self.trade_db))
        with conn:
            conn.execute("""
                CREATE TABLE trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT,
                    ticker TEXT,
                    side TEXT,
                    volume REAL,
                    price REAL,
                    strategy TEXT,
                    market TEXT,
                    action TEXT,
                    cost_or_revenue REAL,
                    pnl REAL DEFAULT 0.0,
                    cycle INTEGER DEFAULT 0,
                    order_uuid TEXT,
                    strategy_id TEXT
                )
            """)
        conn.close()

    def test_init_and_record_decision(self):
        # Given: fresh test DB
        # When: recording a new decision
        dec_id = engine.record_decision(
            market="KRW-ETH",
            trigger_type="MANUAL_APPROVAL",
            before_params={"profit_margin": 1.008},
            after_params={"profit_margin": 1.005},
            reason="테스트 익절 마진 축소",
            timestamp="2026-10-01 10:00:00",
            db_path=self.decision_db
        )

        # Then: decision is stored properly
        self.assertTrue(dec_id.startswith("DEC_KRW_ETH_"))
        conn = engine.get_db_connection(self.decision_db)
        cur = conn.cursor()
        cur.execute("SELECT * FROM decisions WHERE decision_id = ?", (dec_id,))
        row = cur.fetchone()
        self.assertIsNotNone(row)
        self.assertEqual("KRW-ETH", row["market"])
        self.assertEqual("PENDING", row["status"])
        conn.close()

    def test_query_trade_metrics(self):
        # Given: synthetic trades in trade DB
        conn = sqlite3.connect(str(self.trade_db))
        with conn:
            # 2 bids, 1 ask with profit
            conn.execute("""
                INSERT INTO trades (timestamp, market, side, volume, price, action, cost_or_revenue, pnl)
                VALUES ('2026-10-02 12:00:00', 'KRW-ETH', 'bid', 0.01, 3500000, 'BUY_STAGE_1', 35000, 0.0)
            """)
            conn.execute("""
                INSERT INTO trades (timestamp, market, side, volume, price, action, cost_or_revenue, pnl)
                VALUES ('2026-10-03 12:00:00', 'KRW-ETH', 'bid', 0.01, 3450000, 'BUY_STAGE_2', 34500, 0.0)
            """)
            conn.execute("""
                INSERT INTO trades (timestamp, market, side, volume, price, action, cost_or_revenue, pnl)
                VALUES ('2026-10-04 12:00:00', 'KRW-ETH', 'ask', 0.02, 3520000, 'TAKE_PROFIT', 70400, 900.0)
            """)
        conn.close()

        # When: querying metrics
        m = engine.query_trade_metrics(
            market="KRW-ETH",
            start_ts="2026-10-01 00:00:00",
            end_ts="2026-10-05 00:00:00",
            trade_db_path=self.trade_db
        )

        # Then: correctly aggregated
        self.assertEqual(3, m["trade_count"])
        self.assertEqual(2, m["buy_count"])
        self.assertEqual(1, m["sell_count"])
        self.assertEqual(900.0, m["net_pnl"])
        self.assertEqual(1, m["cycle_count"])
        self.assertEqual(2, m["max_step_reached"])

    def test_evaluate_decision_too_early_returns_none(self):
        # Given: decision made 2 days ago, horizon is 7 days
        t0 = datetime(2026, 10, 5, 12, 0, 0)
        dec_id = engine.record_decision(
            market="KRW-ETH",
            trigger_type="TEST",
            before_params={},
            after_params={},
            reason="test",
            timestamp=t0.strftime("%Y-%m-%d %H:%M:%S"),
            db_path=self.decision_db
        )

        # When: evaluated at day 2
        res = engine.evaluate_decision_settlement(
            decision_id=dec_id,
            horizon_days=7,
            as_of=t0 + timedelta(days=2),
            db_path=self.decision_db,
            trade_db_path=self.trade_db
        )

        # Then: None returned (not yet due)
        self.assertIsNone(res)

    def test_evaluate_decision_matured_creates_settlement_and_memory(self):
        # Given: decision made 7 days ago
        t0 = datetime(2026, 10, 1, 10, 0, 0)
        dec_id = engine.record_decision(
            market="KRW-ETH",
            trigger_type="TEST_APPROVAL",
            before_params={"profit_margin": 1.008},
            after_params={"profit_margin": 1.005},
            reason="마진 축소",
            timestamp=t0.strftime("%Y-%m-%d %H:%M:%S"),
            db_path=self.decision_db
        )

        # Insert prior trades (0 pnl)
        conn = sqlite3.connect(str(self.trade_db))
        with conn:
            conn.execute("""
                INSERT INTO trades (timestamp, market, side, volume, price, action, cost_or_revenue, pnl)
                VALUES ('2026-09-28 10:00:00', 'KRW-ETH', 'ask', 0.01, 3500000, 'EXIT', 35000, -100.0)
            """)
            # Insert post trades (improved pnl & multiple trades)
            for i in range(5):
                conn.execute(f"""
                    INSERT INTO trades (timestamp, market, side, volume, price, action, cost_or_revenue, pnl)
                    VALUES ('2026-10-0{2+i} 10:00:00', 'KRW-ETH', 'ask', 0.01, 3500000, 'TAKE_PROFIT', 35000, 500.0)
                """)
        conn.close()

        # When: evaluated at day 7
        now_dt = t0 + timedelta(days=7, hours=1)
        res = engine.evaluate_decision_settlement(
            decision_id=dec_id,
            horizon_days=7,
            as_of=now_dt,
            db_path=self.decision_db,
            trade_db_path=self.trade_db
        )

        # Then: settlement evaluated successfully
        self.assertIsNotNone(res)
        self.assertEqual("KRW-ETH", res["market"])
        self.assertEqual(7, res["horizon_days"])
        self.assertEqual("EXCELLENT", res["grade"])
        self.assertEqual(2500.0, res["actual_pnl"])
        self.assertEqual(5, res["cycle_count"])

        # Check reflection memory stored
        memories = engine.get_market_reflections("KRW-ETH", db_path=self.decision_db)
        self.assertGreaterEqual(len(memories), 1)
        main_mem = next(m for m in memories if m["grade"] == "EXCELLENT")
        self.assertIn("KRW-ETH: 익절 마진 1.008 ➔ 1.005 적용 후 7일간", main_mem["rule_or_lesson"])

        # Check decision status updated to COMPLETED
        conn = engine.get_db_connection(self.decision_db)
        cur = conn.cursor()
        cur.execute("SELECT status FROM decisions WHERE decision_id = ?", (dec_id,))
        self.assertEqual("COMPLETED", cur.fetchone()["status"])
        conn.close()

    def test_run_due_settlements_sweeper(self):
        # Given: two decisions, one mature (7d) and one premature (1d)
        t_old = datetime(2026, 10, 1, 0, 0, 0)
        t_new = datetime(2026, 10, 7, 0, 0, 0)

        id_old = engine.record_decision(
            market="KRW-ETH",
            trigger_type="TEST",
            before_params={},
            after_params={},
            reason="old",
            timestamp=t_old.strftime("%Y-%m-%d %H:%M:%S"),
            db_path=self.decision_db
        )
        id_new = engine.record_decision(
            market="KRW-SOL",
            trigger_type="TEST",
            before_params={},
            after_params={},
            reason="new",
            timestamp=t_new.strftime("%Y-%m-%d %H:%M:%S"),
            db_path=self.decision_db
        )

        # When: running sweeper at 2026-10-08 12:00:00 (old is 7.5d old, new is 1.5d old)
        now_dt = datetime(2026, 10, 8, 12, 0, 0)
        settled = engine.run_due_settlements(
            as_of=now_dt,
            db_path=self.decision_db,
            trade_db_path=self.trade_db,
            send_telegram=False
        )

        # Then: only the mature decision is settled
        self.assertEqual(1, len(settled))
        self.assertEqual(id_old, settled[0]["decision_id"])

    def test_advisor_context_and_seed_baseline(self):
        # Given: fresh DB
        # When: seeding Phase 1 baseline
        seeds = engine.seed_phase1_baseline(db_path=self.decision_db, trade_db_path=self.trade_db)

        # Then: both ETH and SOL seeds are created
        self.assertIn("KRW-ETH", seeds)
        self.assertIn("KRW-SOL", seeds)

        # Check advisor context formatting
        ctx = engine.get_reflection_context_for_advisor("KRW-ETH", db_path=self.decision_db)
        self.assertIn("과거 정산 교훈: 아직 축적된 실전 사후 평가 데이터가 없습니다", ctx)


    def test_query_trade_metrics_with_idle_periods(self):
        # Given: trades with specific intervals (> 24h and < 24h)
        conn = sqlite3.connect(str(self.trade_db))
        with conn:
            # Trade 1: Day 1 10:00
            conn.execute("""
                INSERT INTO trades (timestamp, market, side, volume, price, action, cost_or_revenue, pnl)
                VALUES ('2026-10-01 10:00:00', 'KRW-SOL', 'bid', 0.1, 150000, 'BUY_ST1', 15000, 0.0)
            """)
            # Trade 2: Day 2 20:00 (Gap: 34.0 hours -> >= 24h idle!)
            conn.execute("""
                INSERT INTO trades (timestamp, market, side, volume, price, action, cost_or_revenue, pnl)
                VALUES ('2026-10-02 20:00:00', 'KRW-SOL', 'bid', 0.1, 145000, 'BUY_ST2', 14500, 0.0)
            """)
            # Trade 3: Day 3 12:00 (Gap: 16.0 hours -> < 24h)
            conn.execute("""
                INSERT INTO trades (timestamp, market, side, volume, price, action, cost_or_revenue, pnl)
                VALUES ('2026-10-03 12:00:00', 'KRW-SOL', 'ask', 0.2, 148000, 'TAKE_PROFIT', 29600, 100.0)
            """)
        conn.close()

        # When: querying metrics with end_ts at Day 5 12:00 (Trailing gap from Trade 3: 48.0h -> >= 24h idle!)
        m = engine.query_trade_metrics(
            market="KRW-SOL",
            start_ts="2026-10-01 00:00:00",
            end_ts="2026-10-05 12:00:00",
            trade_db_path=self.trade_db
        )

        # Then: detected 2 idle periods of >= 24h
        self.assertEqual(2, m["idle_24h_count"])
        self.assertEqual(48.0, m["max_idle_hours"])
        self.assertEqual(82.0, m["total_idle_hours"])
        self.assertEqual(2, len(m["idle_periods"]))
        self.assertEqual("2026-10-01 10:00:00", m["idle_periods"][0]["start_time"])
        self.assertEqual("2026-10-02 20:00:00", m["idle_periods"][0]["end_time"])

    def test_evaluate_decision_with_stagnation_generates_alert_memory(self):
        # Given: a decision made 7 days ago with large idle intervals
        t0 = datetime(2026, 10, 1, 10, 0, 0)
        dec_id = engine.record_decision(
            market="KRW-SOL",
            trigger_type="TEST_DECISION",
            before_params={"profit_margin": 1.010},
            after_params={"profit_margin": 1.005},
            reason="테스트 솔라나 마진 변경",
            timestamp=t0.strftime("%Y-%m-%d %H:%M:%S"),
            db_path=self.decision_db
        )

        conn = sqlite3.connect(str(self.trade_db))
        with conn:
            # 1 trade only on Day 2, then stagnant until Day 8 (gap > 24h)
            conn.execute("""
                INSERT INTO trades (timestamp, market, side, volume, price, action, cost_or_revenue, pnl)
                VALUES ('2026-10-02 10:00:00', 'KRW-SOL', 'bid', 0.1, 150000, 'BUY_1', 15000, 0.0)
            """)
        conn.close()

        # When: evaluated after 7 days
        now_dt = t0 + timedelta(days=7, hours=2)
        res = engine.evaluate_decision_settlement(
            decision_id=dec_id,
            horizon_days=7,
            as_of=now_dt,
            db_path=self.decision_db,
            trade_db_path=self.trade_db
        )

        # Then: evaluated with stagnation alert
        self.assertIsNotNone(res)
        self.assertEqual(1, res["idle_24h_count"])
        self.assertGreaterEqual(res["max_idle_hours"], 24.0)
        self.assertIn("거래 정체 진단", res["reflection_note"])
        self.assertIn("24시간 무거래 시 적응형 그리드 폭 축소", res["reflection_note"])

        # Check that both general memory and STAGNATION_ALERT memory were recorded
        memories = engine.get_market_reflections("KRW-SOL", db_path=self.decision_db)
        self.assertEqual(2, len(memories))
        stagnation_mems = [m for m in memories if m["grade"] == "STAGNATION_ALERT"]
        self.assertEqual(1, len(stagnation_mems))
        self.assertIn("체결 후 24시간 무거래 정체 감지", stagnation_mems[0]["rule_or_lesson"])

        # Check DB column persistence
        conn = engine.get_db_connection(self.decision_db)
        cur = conn.cursor()
        cur.execute("SELECT idle_24h_count, max_idle_hours FROM settlements WHERE decision_id = ?", (dec_id,))
        row = cur.fetchone()
        self.assertEqual(1, row["idle_24h_count"])
        self.assertGreaterEqual(row["max_idle_hours"], 24.0)
        conn.close()


if __name__ == "__main__":
    unittest.main()

