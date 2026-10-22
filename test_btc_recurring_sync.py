"""Unit tests for Upbit BTC recurring buy (코인 모으기) synchronization."""

import copy
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from strategy.btc_accumulator import (
    sync_upbit_btc_recurring_buys,
    load_btc_state,
    save_btc_state,
    check_btc_tiered_status,
)


class TestBtcRecurringSync(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_trade_history.db")
        self.state_file = os.path.join(self.temp_dir.name, "test_state_KRW_BTC.json")

        # Initialize test DB with trades table
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                ticker TEXT,
                market TEXT,
                side TEXT,
                action TEXT,
                price REAL,
                volume REAL,
                cost_or_revenue REAL,
                pnl REAL DEFAULT 0.0,
                cycle INTEGER DEFAULT 0,
                strategy TEXT,
                strategy_id TEXT,
                order_uuid TEXT
            )
        """)
        # Pre-seed one 08:50 bot buy
        cur.execute("""
            INSERT INTO trades (
                timestamp, ticker, market, side, action, price, volume, cost_or_revenue, pnl, cycle, strategy, order_uuid
            ) VALUES (
                '2026-09-24 08:50:00', 'BTC', 'KRW-BTC', 'bid', 'BTC_TIERED_BUY_T1',
                116000000.0, 0.0000862068965517, 10000.0, 0.0, 0, 'BTC_TIERED_ACCUMULATOR', 'pre-existing-bot-uuid'
            )
        """)
        conn.commit()
        conn.close()

        # Patch DB_PATH and BTC_STATE_FILE
        self.patch_db = patch("strategy.btc_accumulator.DB_PATH", self.db_path)
        self.patch_state = patch("strategy.btc_accumulator.BTC_STATE_FILE", self.state_file)
        self.patch_db_logger = patch("utils.db_logger.DB_PATH", self.db_path)
        self.patch_sync = patch("utils.sync_manager.trigger_snapshot_sync", lambda: None)
        self.patch_db.start()
        self.patch_state.start()
        self.patch_db_logger.start()
        self.patch_sync.start()

    def tearDown(self):
        self.patch_sync.stop()
        self.patch_db.stop()
        self.patch_state.stop()
        self.patch_db_logger.stop()
        self.temp_dir.cleanup()


    def test_sync_filters_and_records_ioc_orders_correctly(self):
        """Only KRW-BTC bid IOC orders >= start_date are synced."""
        mock_orders = [
            # 1. Matching Upbit app recurring buy
            {
                "uuid": "ioc-buy-1",
                "market": "KRW-BTC",
                "side": "bid",
                "state": "done",
                "time_in_force": "ioc",
                "price": "115000000",
                "executed_volume": "0.0000869565217391",
                "created_at": "2026-09-25T15:07:00+09:00",
                "trades": [{"funds": "10000.0", "volume": "0.0000869565217391", "price": "115000000"}]
            },
            # 2. Another matching recurring buy
            {
                "uuid": "ioc-buy-2",
                "market": "KRW-BTC",
                "side": "bid",
                "state": "done",
                "time_in_force": "ioc",
                "price": "114000000",
                "executed_volume": "0.0000877192982456",
                "created_at": "2026-09-26T15:06:00+09:00",
                "trades": [{"funds": "10000.0", "volume": "0.0000877192982456", "price": "114000000"}]
            },
            # 3. Different market (KRW-ETH) -> Must be ignored
            {
                "uuid": "eth-buy-1",
                "market": "KRW-ETH",
                "side": "bid",
                "state": "done",
                "time_in_force": "ioc",
                "price": "3500000",
                "executed_volume": "0.002857",
                "created_at": "2026-09-26T15:06:00+09:00",
            },
            # 4. Normal limit order (not IOC) -> Must be ignored
            {
                "uuid": "limit-order-1",
                "market": "KRW-BTC",
                "side": "bid",
                "state": "done",
                "time_in_force": None,
                "price": "114000000",
                "executed_volume": "0.000087",
                "created_at": "2026-09-26T12:00:00+09:00",
            },
            # 5. Outdated order before start_date -> Must be ignored
            {
                "uuid": "old-ioc-order",
                "market": "KRW-BTC",
                "side": "bid",
                "state": "done",
                "time_in_force": "ioc",
                "price": "110000000",
                "executed_volume": "0.00009",
                "created_at": "2026-09-20T06:30:00+09:00",
            }
        ]

        result = sync_upbit_btc_recurring_buys(
            upbit_client=None,
            start_date="2026-09-24",
            orders=mock_orders
        )

        self.assertEqual(result["status"], "SUCCESS")
        self.assertEqual(result["synced_count"], 2)
        # Pre-existing 1 + 2 new = 3 total DCA buys
        self.assertEqual(result["total_dca_buys_count"], 3)
        self.assertAlmostEqual(result["total_dca_invested_krw"], 30000.0, places=1)
        expected_vol = 0.0000862068965517 + 0.0000869565217391 + 0.0000877192982456
        self.assertAlmostEqual(result["total_dca_volume"], expected_vol, places=8)
        self.assertAlmostEqual(result["dca_avg_price"], 30000.0 / expected_vol, places=0)

        # Verify state file was updated
        saved_state = load_btc_state()
        self.assertEqual(saved_state["total_dca_buys_count"], 3)
        self.assertAlmostEqual(saved_state["total_dca_invested_krw"], 30000.0, places=1)
        self.assertAlmostEqual(saved_state["dca_avg_price"], 30000.0 / expected_vol, places=0)
        self.assertIsNotNone(saved_state.get("last_sync_time"))

    def test_idempotency_prevents_duplicate_records(self):
        """Calling sync multiple times with the same orders does not create duplicates."""
        mock_orders = [
            {
                "uuid": "ioc-buy-1",
                "market": "KRW-BTC",
                "side": "bid",
                "state": "done",
                "time_in_force": "ioc",
                "price": "115000000",
                "executed_volume": "0.0000869565217391",
                "created_at": "2026-09-25T15:07:00+09:00",
                "trades": [{"funds": "10000.0", "volume": "0.0000869565217391", "price": "115000000"}]
            }
        ]

        # First sync
        res1 = sync_upbit_btc_recurring_buys(upbit_client=None, start_date="2026-09-24", orders=mock_orders)
        self.assertEqual(res1["synced_count"], 1)
        self.assertEqual(res1["total_dca_buys_count"], 2)

        # Second sync with identical order list
        res2 = sync_upbit_btc_recurring_buys(upbit_client=None, start_date="2026-09-24", orders=mock_orders)
        self.assertEqual(res2["synced_count"], 0)
        self.assertEqual(res2["total_dca_buys_count"], 2)

        # Check DB row count directly
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM trades WHERE market = 'KRW-BTC'")
        count = cur.fetchone()[0]
        conn.close()
        self.assertEqual(count, 2)

    def test_sync_preserves_unsettled_fallback_checkpoint(self):
        """정기 동기화는 미확정 주문의 보조 영속 체크포인트를 삭제하지 않아야 합니다."""
        from strategy import btc_accumulator as btc
        from pathlib import Path
        cp_path = btc._get_btc_checkpoint_path()

        # 보조 체크포인트 생성 (미확정 주문 존재 상태)
        btc._save_btc_fallback_checkpoint({
            "date": "2026-10-05",
            "pending_buy_order": {"uuid": "unsettled-uuid", "tier": 1}
        })
        self.assertTrue(Path(cp_path).exists())

        # 정기 동기화 실행 (orders=[])
        sync_upbit_btc_recurring_buys(upbit_client=None, start_date="2026-09-24", orders=[])

        # 동기화 후에도 보조 체크포인트가 보존되어야 함
        self.assertTrue(Path(cp_path).exists())
        state = load_btc_state()
        self.assertIsNotNone(state.get("pending_buy_order"))
        self.assertEqual(state["pending_buy_order"]["uuid"], "unsettled-uuid")

        # 정리
        btc._clear_btc_fallback_checkpoint()


if __name__ == "__main__":
    unittest.main()
