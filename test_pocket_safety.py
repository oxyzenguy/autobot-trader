#!/usr/bin/env python3
"""
Test Pocket Grid Safety & Hostile Audit Verification Suite
- Verifies resolution of all P0 & P1 blockers identified in the hostile audit report:
  1. P0 — 운영 장부 정상 복구 및 0원 단가 방어
  2. P1 — 미확정 주문 중복 제출 방지 (Durable Pending Intent & Checkpoint)
  3. P1 — 부분매도 잔량 보존 (Volume Conservation)
  4. P1 — Fail-Closed 영속성 (DB/JSON 손상 시 안전 중단)
  5. P1 — 체결 내역 누락 시 단가 오인 추정 제거
  6. P1 — 첫 진입 예산 초과 방지
  7. P2 — 매수/매도 수수료 완전 차감
"""
import os
import sys
import json
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from utils.pocket_api import UpbitPocketClient
from utils.pocket_grid_core import PocketGridRunner


class TestPocketGridSafety(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.state_file = os.path.join(self.temp_dir.name, "state.json")
        self.db_file = os.path.join(self.temp_dir.name, "trades.sqlite3")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_p0_zero_funds_and_trades_parsing(self):
        """P0: Upbit trades 배열로부터 funds, volume, price 정상 산출 확인"""
        client = UpbitPocketClient("test-key", "test-secret")
        order_done = {
            "uuid": "test-uuid-1",
            "state": "done",
            "market": "KRW-XRP",
            "side": "bid",
            "trades": [
                {"price": "1800", "volume": "5", "funds": "9000"},
                {"price": "1810", "volume": "5", "funds": "9050"}
            ],
            "paid_fee": "9.025"
        }
        with patch.object(client, "get_order", return_value=order_done), \
             patch("requests.post", return_value=SimpleNamespace(status_code=200, json=lambda: {"uuid": "test-uuid-1"})):
            res = client.buy_market_order_and_wait("KRW-XRP", 18050)
            self.assertEqual(res["status"], "FILLED")
            self.assertEqual(res["volume"], 10.0)
            self.assertEqual(res["funds"], 18050.0)
            self.assertAlmostEqual(res["price"], 1805.0)
            self.assertEqual(res["fee"], 9.025)

    def test_p1_missing_trades_does_not_infer_bogus_cost(self):
        """P1: 체결 trades 누락 시 주문 총액을 단가로 오인하지 않고 에러 발생 확인"""
        client = UpbitPocketClient("test-key", "test-secret")
        order_no_trades = {
            "uuid": "test-uuid-2",
            "state": "cancel",
            "market": "KRW-XRP",
            "side": "bid",
            "price": "10000",
            "executed_volume": "10"
            # 'trades' key missing
        }
        with patch.object(client, "get_order", return_value=order_no_trades), \
             patch("requests.post", return_value=SimpleNamespace(status_code=200, json=lambda: {"uuid": "test-uuid-2"})):
            with self.assertRaises(RuntimeError) as ctx:
                client.buy_market_order_and_wait("KRW-XRP", 10000)
            self.assertIn("trades details missing", str(ctx.exception))

    def test_p1_unresolved_buy_not_replayed(self):
        """P1: 타임아웃 발생 시 durable pending order 생성 및 다음 틱에서 재주문 차단 확인"""
        fake_client = Mock()
        fake_client.get_balance.return_value = 900000
        fake_client.buy_market_order_and_wait.side_effect = TimeoutError("Simulated network timeout")

        with patch("utils.pocket_grid_core.UpbitPocketClient", return_value=fake_client):
            runner = PocketGridRunner(market="KRW-XRP", unit_krw=10000, grid_budget=300000)
            runner.state_file = self.state_file
            runner.db_file = self.db_file
            runner.init_db()

            state = runner.load_state()
            # 1st tick: Timeout occurs
            runner.process_tick(state, 1800, "2026-10-21 21:00:00")
            self.assertIsNotNone(state.get("pending_order"))
            self.assertEqual(state["pending_order"]["action"], "BUY")

            # 2nd tick: Pending order exists, must NOT submit again!
            fake_client.get_order_by_identifier.return_value = None  # Still not found or processing
            runner.process_tick(state, 1800, "2026-10-21 21:00:05")

            # Must have only called buy_market_order_and_wait ONCE!
            self.assertEqual(fake_client.buy_market_order_and_wait.call_count, 1)

    def test_p1_partial_sell_volume_conservation(self):
        """P1: 부분 매도 체결 시 미체결 잔여 수량 및 원가가 장부에 온전히 보존되는지 검증"""
        fake_client = Mock()
        fake_client.get_balance.return_value = 900000

        with patch("utils.pocket_grid_core.UpbitPocketClient", return_value=fake_client):
            runner = PocketGridRunner(market="KRW-XRP", unit_krw=10000, grid_budget=300000)
            runner.state_file = self.state_file
            runner.db_file = self.db_file
            runner.init_db()

            state = runner.load_state()
            # Initial holding: 10 XRP @ 1,000 KRW (Cost 10,000 KRW, Fee 5 KRW, TP 1,010 KRW)
            grid = {"lvl": 1, "squad": 1, "buy_p": 1000.0, "vol": 10.0, "cost": 10000.0, "fee": 5.0, "tp_p": 1010.0}
            state.update(base_price=1000.0, active_grids=[grid])

            # Partial sell: only 4 XRP executed out of 10
            partial_res = {
                "status": "PARTIAL",
                "price": 1100.0,
                "volume": 4.0,
                "funds": 4400.0,
                "fee": 2.2,
                "uuid": "partial-uuid"
            }
            fake_client.sell_market_order_and_wait.return_value = partial_res
            state = runner.process_tick(state, 1100.0, "2026-10-21 21:00:00")

            # Must retain 6 XRP in active_grids!
            self.assertEqual(len(state["active_grids"]), 1)
            remaining_grid = state["active_grids"][0]
            self.assertAlmostEqual(remaining_grid["vol"], 6.0)
            self.assertAlmostEqual(remaining_grid["cost"], 6000.0)
            self.assertAlmostEqual(remaining_grid["fee"], 3.0)

            # Check DB record for partial trade
            conn = sqlite3.connect(self.db_file)
            c = conn.cursor()
            trade = c.execute("SELECT action, volume, cost_or_revenue, pnl FROM pocket_trades").fetchone()
            conn.close()
            self.assertIn("PARTIAL", trade[0])
            self.assertEqual(trade[1], 4.0)
            # Net PnL = 4400 (rev) - 4000 (cost) - 2.0 (allocated buy fee) - 2.2 (sell fee) = 395.8
            self.assertAlmostEqual(trade[3], 395.8)

    def test_p1_corrupt_state_json_halts_trading(self):
        """P1: 손상된 JSON은 임의로 빈 계좌로 초기화하지 않고 trading_halted=True로 안전 중단"""
        runner = PocketGridRunner(market="KRW-XRP")
        runner.state_file = self.state_file
        with open(self.state_file, "w") as f:
            f.write("{broken_json_content")

        state = runner.load_state()
        self.assertTrue(state.get("trading_halted"))
        self.assertEqual(state.get("active_grids"), [])

        # process_tick on halted state does not execute trades
        runner.execute_live_buy = Mock()
        runner.process_tick(state, 1800, "2026-10-21 21:00:00")
        runner.execute_live_buy.assert_not_called()

    def test_p1_database_failure_fails_closed(self):
        """P1: DB 저장 실패 시 매수 성공 처리를 거부하고 fail-closed 동작 확인"""
        fake_client = Mock()
        fake_client.get_balance.return_value = 900000
        fake_client.buy_market_order_and_wait.return_value = {
            "status": "FILLED",
            "price": 1000.0,
            "volume": 10.0,
            "funds": 10000.0,
            "fee": 5.0,
            "uuid": "test-db-fail"
        }

        with patch("utils.pocket_grid_core.UpbitPocketClient", return_value=fake_client):
            runner = PocketGridRunner(market="KRW-XRP", unit_krw=10000, grid_budget=300000)
            runner.state_file = self.state_file
            runner.db_file = self.db_file

            with patch("sqlite3.connect", side_effect=sqlite3.OperationalError("disk I/O error")):
                with self.assertRaises(sqlite3.OperationalError):
                    runner.execute_live_buy(1, 1, 1000.0, "2026-10-21 21:00:00")

    def test_p1_first_buy_budget_bypass_prevented(self):
        """P1: 1회 주문 단위가 배정 예산을 초과하는 경우 첫 1단계 매수 진입 차단 검증"""
        fake_client = Mock()
        fake_client.get_balance.return_value = 900000

        with patch("utils.pocket_grid_core.UpbitPocketClient", return_value=fake_client):
            # unit_krw 10,000 > grid_budget 5,000
            runner = PocketGridRunner(market="KRW-XRP", unit_krw=10000, grid_budget=5000)
            runner.state_file = self.state_file
            runner.db_file = self.db_file
            runner.init_db()

            state = runner.load_state()
            runner.execute_live_buy = Mock()
            runner.process_tick(state, 1800, "2026-10-21 21:00:00")
            runner.execute_live_buy.assert_not_called()

    def test_p2_fee_accounting_deducts_both_buy_and_sell_fees(self):
        """P2: 실현 손익 계산 시 매수 수수료와 매도 수수료가 모두 정확히 차감되는지 검증"""
        fake_client = Mock()
        fake_client.get_balance.return_value = 900000

        with patch("utils.pocket_grid_core.UpbitPocketClient", return_value=fake_client):
            runner = PocketGridRunner(market="KRW-XRP", unit_krw=10000, grid_budget=300000)
            runner.state_file = self.state_file
            runner.db_file = self.db_file
            runner.init_db()

            state = runner.load_state()
            # Bought 10 @ 1000 = 10,000 KRW, Buy Fee 5.0 KRW
            grid = {"lvl": 1, "squad": 1, "buy_p": 1000.0, "vol": 10.0, "cost": 10000.0, "fee": 5.0, "tp_p": 1010.0}
            state.update(base_price=1000.0, active_grids=[grid])

            # Sold 10 @ 1100 = 11,000 KRW, Sell Fee 5.5 KRW
            sell_res = {
                "status": "FILLED",
                "price": 1100.0,
                "volume": 10.0,
                "funds": 11000.0,
                "fee": 5.5,
                "uuid": "full-sell-uuid"
            }
            fake_client.sell_market_order_and_wait.return_value = sell_res
            state = runner.process_tick(state, 1100.0, "2026-10-21 21:00:00")

            # Net PnL must be 11000 - 10000 - 5.0 (buy fee) - 5.5 (sell fee) = 989.5 KRW!
            self.assertAlmostEqual(state["realized_pnl"], 989.5)
            self.assertAlmostEqual(state["total_fees"], 10.5)

    def test_p3_buy_and_sell_trigger_telegram_alerts(self):
        """P3: G60 서브포켓 매수 및 매도 체결 시 텔레그램 알림 발송 검증"""
        fake_client = Mock()
        fake_client.get_balance.return_value = 300000.0
        with patch("utils.pocket_grid_core.UpbitPocketClient", return_value=fake_client):
            runner = PocketGridRunner(market="KRW-XRP", unit_krw=10000, grid_budget=300000)
            runner.state_file = self.state_file
            runner.db_file = self.db_file
            runner.init_db()

            sent_msgs = []
            runner.send_telegram = lambda msg: sent_msgs.append(msg)

            state = runner.load_state()
            state.update(base_price=1000.0, active_grids=[])

            # 1. 매수 체결
            buy_res = {
                "status": "FILLED",
                "price": 1000.0,
                "volume": 10.0,
                "funds": 10000.0,
                "fee": 5.0,
                "uuid": "test-buy-uuid"
            }
            fake_client.buy_market_order_and_wait.return_value = buy_res
            state = runner.process_tick(state, 1000.0, "2026-10-22 10:00:00")

            self.assertTrue(any("G60 실거래 매수" in m and "Lvl 1" in m for m in sent_msgs))

            # 2. 매도 체결
            sell_res = {
                "status": "FILLED",
                "price": 1020.0,
                "volume": 10.0,
                "funds": 10200.0,
                "fee": 5.1,
                "uuid": "test-sell-uuid"
            }
            fake_client.sell_market_order_and_wait.return_value = sell_res
            state = runner.process_tick(state, 1020.0, "2026-10-22 10:05:00")

            self.assertTrue(any("G60 실거래 개별 익절" in m and "Lvl 1" in m for m in sent_msgs))


if __name__ == "__main__":
    unittest.main()
