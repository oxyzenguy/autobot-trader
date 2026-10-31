#!/usr/bin/env python3
"""
Test Suite for G60 Pocket Re-audit Resolution (R1~R5)
- R1: Multi-grid squad exits succeed without SQLite UNIQUE constraint errors via keyed allocations.
- R2: Terminal pending SELL reconciles inventory reduction, DB journal, and PnL update.
- R3: Crash between DB commit and JSON save recovers idempotently without UNIQUE conflicts.
- R4: Same-tick timeout immediately halts the tick, preventing overwriting pending checkpoint.
- R5: Parseable but invalid accounting data sets trading_halted=True rather than silently deleting.
"""
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch
import copy
import json
import os
import sqlite3
import sys
import tempfile
import unittest

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from utils.pocket_api import UpbitPocketClient
from utils.pocket_grid_core import PocketGridRunner


class TestPocketReauditSafety(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="reaudit-test-")
        self.state_file = os.path.join(self.temp_dir.name, "state.json")
        self.db_file = os.path.join(self.temp_dir.name, "trades.sqlite3")
        self.tg_patcher = patch("utils.pocket_grid_core.send_message")
        self.mock_send_message = self.tg_patcher.start()

    def tearDown(self):
        self.tg_patcher.stop()
        self.temp_dir.cleanup()

    def _make_grid(self, level=1, volume=10.0, buy_p=1000.0, squad=1):
        return {
            "lvl": level,
            "squad": squad,
            "buy_p": buy_p,
            "vol": volume,
            "cost": volume * buy_p,
            "fee": volume * buy_p * 0.0005,
            "tp_p": buy_p * 1.01
        }

    def _make_runner(self, fake_client):
        with patch("utils.pocket_grid_core.UpbitPocketClient", return_value=fake_client):
            runner = PocketGridRunner(market="KRW-XRP", unit_krw=10000.0, grid_budget=300000.0)
        runner.state_file = self.state_file
        runner.db_file = self.db_file
        runner.send_telegram = Mock()
        runner.init_db()
        return runner

    def test_r1_squad_exit_keyed_allocations_no_unique_conflict(self):
        """R1: 다중 그리드 스쿼드 일괄 매도 시 allocation_id 분리로 UNIQUE 오류 없이 완전 정산"""
        fake_client = Mock()
        fake_client.get_balance.return_value = 900000
        runner = self._make_runner(fake_client)

        state = runner.load_state()
        # 2개 레벨 (각 10 XRP)
        state.update(base_price=1000.0, active_grids=[self._make_grid(1), self._make_grid(2)])

        squad_sale_res = {
            "status": "FILLED",
            "price": 1100.0,
            "volume": 20.0,
            "funds": 22000.0,
            "fee": 11.0,
            "uuid": "squad-shared-uuid"
        }
        fake_client.sell_market_order_and_wait.return_value = squad_sale_res

        # Execute tick at 1100 (triggers squad exit)
        state = runner.process_tick(state, 1100.0, "2026-10-21 21:00:00")

        # Must NOT raise sqlite3.IntegrityError!
        # Both levels must be liquidated
        self.assertEqual(len(state["active_grids"]), 0)
        self.assertIsNone(state.get("pending_order"))

        # DB must contain exactly 2 rows for the 2 levels, sharing order_uuid but with unique allocation_ids
        with sqlite3.connect(runner.db_file) as db:
            rows = db.execute("SELECT level, volume, cost_or_revenue, fee, pnl, order_uuid, allocation_id FROM pocket_trades").fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][0], 1)
        self.assertEqual(rows[0][1], 10.0)
        self.assertEqual(rows[0][5], "squad-shared-uuid")
        self.assertEqual(rows[0][6], "squad-shared-uuid:sell_lvl_1")

        self.assertEqual(rows[1][0], 2)
        self.assertEqual(rows[1][1], 10.0)
        self.assertEqual(rows[1][5], "squad-shared-uuid")
        self.assertEqual(rows[1][6], "squad-shared-uuid:sell_lvl_2")

        # Total allocated volume must equal confirmed 20 XRP
        self.assertEqual(sum(r[1] for r in rows), 20.0)
        # Total revenue must equal 22000 KRW
        self.assertEqual(sum(r[2] for r in rows), 22000.0)
        # Net PnL must be 22000 - 20000 - 10 (buy fee) - 11 (sell fee) = 1979 KRW
        self.assertAlmostEqual(state["realized_pnl"], 1979.0)

    def test_r2_pending_sell_reconciliation_settles_inventory_and_journal(self):
        """R2: 미확정 매도 주문이 거래소 체결로 확인되면 장부 수량 차감 및 DB 저널 정상 정산"""
        fake_client = Mock()
        fake_client.get_balance.return_value = 900000
        runner = self._make_runner(fake_client)

        state = runner.load_state()
        state.update(
            base_price=1000.0,
            active_grids=[self._make_grid(1)],
            pending_order={
                "identifier": "pending-sale-ident",
                "action": "SELL",
                "exit_type": "INDIVIDUAL",
                "lvl": 1,
                "squad": 1,
                "target_vol": 10.0,
                "target_grids": [self._make_grid(1)],
                "uuid": None
            }
        )

        # Upbit confirms order execution
        fake_order = {
            "uuid": "pending-sale-uuid",
            "market": "KRW-XRP",
            "side": "ask",
            "state": "done",
            "executed_volume": "10",
            "paid_fee": "5.5",
            "trades": [{"price": "1100", "volume": "10", "funds": "11000"}]
        }
        fake_client.get_order_by_identifier.return_value = fake_order

        # Reconcile pending order
        state = runner.reconcile_pending_order(state, state["pending_order"], "2026-10-21 21:00:00")

        # 1. Pending order must be cleared
        self.assertIsNone(state.get("pending_order"))
        # 2. Active grids must be empty (10 sold)
        self.assertEqual(len(state["active_grids"]), 0)
        # 3. DB must record the sell trade!
        with sqlite3.connect(runner.db_file) as db:
            rows = db.execute("SELECT action, volume, cost_or_revenue, pnl FROM pocket_trades").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1], 10.0)
        self.assertAlmostEqual(rows[0][3], 989.5)  # 11000 - 10000 - 5 - 5.5 = 989.5
        self.assertAlmostEqual(state["realized_pnl"], 989.5)

    def test_r3_crash_between_db_commit_and_json_save_recovers_idempotently(self):
        """R3: DB 커밋 후 JSON 저장 전 크래시가 발생해도 재시작 시 멱등 정산으로 완전 복구"""
        fake_client = Mock()
        fake_client.get_balance.return_value = 900000
        runner = self._make_runner(fake_client)

        state = runner.load_state()
        fake_buy_res = {
            "status": "FILLED",
            "price": 1000.0,
            "volume": 10.0,
            "funds": 10000.0,
            "fee": 5.0,
            "uuid": "committed-buy-uuid"
        }
        fake_client.buy_market_order_and_wait.return_value = fake_buy_res

        # 1. Run tick: executes buy and commits to DB
        runner.process_tick(state, 1000.0, "2026-10-21 21:00:00")

        # Verify DB has committed fill
        with sqlite3.connect(runner.db_file) as db:
            count = db.execute("SELECT count(*) FROM pocket_trades WHERE order_uuid = 'committed-buy-uuid'").fetchone()[0]
        self.assertEqual(count, 1)

        # 2. Simulate process death: manually overwrite state file to simulate crash before JSON save
        restarted_state = {
            "market": "KRW-XRP",
            "allocated_budget": 300000.0,
            "active_grids": [],  # Lost memory position
            "pending_order": {
                "identifier": "restarted-ident",
                "action": "BUY",
                "lvl": 1,
                "squad": 1,
                "target_cost": 10000.0,
                "uuid": "committed-buy-uuid"
            }
        }
        with open(runner.state_file, "w") as f:
            json.dump(restarted_state, f)

        reloaded = runner.load_state()
        self.assertEqual(len(reloaded["active_grids"]), 0)
        self.assertIsNotNone(reloaded.get("pending_order"))

        # Upbit returns the committed buy order
        fake_order = {
            "uuid": "committed-buy-uuid",
            "market": "KRW-XRP",
            "side": "bid",
            "state": "cancel",
            "executed_volume": "10",
            "paid_fee": "5.0",
            "trades": [{"price": "1000", "volume": "10", "funds": "10000"}]
        }
        fake_client.get_order.return_value = fake_order

        # 3. Reconciliation attempt 1: must recover position idempotently without UNIQUE error
        reloaded = runner.reconcile_pending_order(reloaded, reloaded["pending_order"], "2026-10-21 21:00:05")
        self.assertIsNone(reloaded.get("pending_order"))
        self.assertEqual(len(reloaded["active_grids"]), 1)
        self.assertEqual(reloaded["active_grids"][0]["vol"], 10.0)

        # 4. Replay attempt 2: calling reconciliation again is a strict NO-OP
        reloaded = runner.reconcile_pending_order(reloaded, restarted_state["pending_order"], "2026-10-21 21:00:10")
        self.assertEqual(len(reloaded["active_grids"]), 1)
        with sqlite3.connect(runner.db_file) as db:
            final_count = db.execute("SELECT count(*) FROM pocket_trades WHERE order_uuid = 'committed-buy-uuid'").fetchone()[0]
        self.assertEqual(final_count, 1)  # Still exactly 1 row!

    def test_r4_same_tick_timeout_halts_tick_immediately(self):
        """R4: 스쿼드 매도 타임아웃 발생 시 해당 틱을 즉시 종료하여 개별 매도 중복 제출 및 덮어쓰기 방지"""
        fake_client = Mock()
        fake_client.get_balance.return_value = 900000
        runner = self._make_runner(fake_client)

        state = runner.load_state()
        state.update(base_price=1000.0, active_grids=[self._make_grid(1), self._make_grid(2)])

        # Timeout occurs on sell
        fake_client.sell_market_order_and_wait.side_effect = TimeoutError("Simulated sell timeout")

        # Execute tick at 1100.0 (both squad and individual exit conditions met)
        state = runner.process_tick(state, 1100.0, "2026-10-21 21:00:00")

        # sell_market_order_and_wait must have been called EXACTLY ONCE!
        self.assertEqual(fake_client.sell_market_order_and_wait.call_count, 1)
        # The single request was for 20 XRP (squad volume)
        called_vol = fake_client.sell_market_order_and_wait.call_args[0][1]
        self.assertEqual(called_vol, 20.0)

        # Pending order must be preserved for SQUAD (not overwritten by level 1 or level 2 individual exits!)
        self.assertIsNotNone(state.get("pending_order"))
        self.assertEqual(state["pending_order"]["exit_type"], "SQUAD")
        self.assertEqual(state["pending_order"]["target_vol"], 20.0)

    def test_r5_parseable_invalid_accounting_halts_trading(self):
        """R5: 구문 정상이나 가격/수량이 0 이하인 비정상 그리드 감지 시 임의 삭제 없이 trading_halted=True 설정"""
        fake_client = Mock()
        runner = self._make_runner(fake_client)

        state = runner.load_state()
        # Non-positive buy price
        state["active_grids"] = [dict(self._make_grid(), buy_p=0.0)]
        with open(runner.state_file, "w") as f:
            json.dump(state, f)

        loaded = runner.load_state()
        self.assertTrue(loaded.get("trading_halted"))
        self.assertIn("INVALID_GRID_ACCOUNTING", loaded.get("error", ""))
        # Grid must NOT be silently wiped out
        self.assertEqual(len(loaded["active_grids"]), 1)

    def test_r6_three_and_five_level_squad_exits(self):
        """다단계(3단, 5단) 스쿼드 바스켓 일괄 매도 시 allocation_id 분리 및 수량/비용 보존 검증"""
        # Part A: 3-level squad exit
        fake_client = Mock()
        fake_client.get_balance.return_value = 900000
        runner = self._make_runner(fake_client)

        state = runner.load_state()
        state.update(base_price=1000.0, active_grids=[self._make_grid(1), self._make_grid(2), self._make_grid(3)])

        squad_sale_res = {
            "status": "FILLED",
            "price": 1100.0,
            "volume": 30.0,
            "funds": 33000.0,
            "fee": 16.5,
            "uuid": "squad-3lvl-uuid"
        }
        fake_client.sell_market_order_and_wait.return_value = squad_sale_res

        state = runner.process_tick(state, 1100.0, "2026-10-21 21:00:00")
        self.assertEqual(len(state["active_grids"]), 0)

        with sqlite3.connect(runner.db_file) as db:
            rows = db.execute("SELECT level, volume, cost_or_revenue, fee, pnl, order_uuid, allocation_id FROM pocket_trades").fetchall()
        self.assertEqual(len(rows), 3)
        self.assertEqual([r[0] for r in rows], [1, 2, 3])
        self.assertEqual(sum(r[1] for r in rows), 30.0)
        self.assertEqual(sum(r[2] for r in rows), 33000.0)
        # Net PnL = 33000 - 30000 - 15 (buy fee) - 16.5 (sell fee) = 2968.5
        self.assertAlmostEqual(state["realized_pnl"], 2968.5)

        # Part B: 5-level squad exit
        fake_client_5 = Mock()
        fake_client_5.get_balance.return_value = 900000
        runner_5 = self._make_runner(fake_client_5)
        state_5 = runner_5.load_state()
        grids_5 = [self._make_grid(i) for i in range(1, 6)]
        state_5.update(base_price=1000.0, active_grids=grids_5)

        squad_5_res = {
            "status": "FILLED",
            "price": 1100.0,
            "volume": 50.0,
            "funds": 55000.0,
            "fee": 27.5,
            "uuid": "squad-5lvl-uuid"
        }
        fake_client_5.sell_market_order_and_wait.return_value = squad_5_res
        state_5 = runner_5.process_tick(state_5, 1100.0, "2026-10-21 21:00:00")
        self.assertEqual(len(state_5["active_grids"]), 0)

        with sqlite3.connect(runner_5.db_file) as db:
            rows_5 = db.execute("SELECT level, volume, cost_or_revenue, fee, pnl, order_uuid, allocation_id FROM pocket_trades WHERE order_uuid = 'squad-5lvl-uuid'").fetchall()
        self.assertEqual(len(rows_5), 5)
        self.assertEqual([r[0] for r in rows_5], [1, 2, 3, 4, 5])
        self.assertEqual(sum(r[1] for r in rows_5), 50.0)
        self.assertEqual(sum(r[2] for r in rows_5), 55000.0)
        # Net PnL = 2968.5 (Part A) + 4947.5 (Part B: 55000 - 50000 - 25 - 27.5) = 7916.0
        self.assertAlmostEqual(state_5["realized_pnl"], 2968.5 + 4947.5)

    def test_r7_partial_pending_sell_reconciliation(self):
        """미확정 매도 주문이 부분 체결(Partial)로 확인될 때 미체결 잔여 수량/원가 보존 검증"""
        fake_client = Mock()
        fake_client.get_balance.return_value = 900000
        runner = self._make_runner(fake_client)

        state = runner.load_state()
        state.update(
            base_price=1000.0,
            active_grids=[self._make_grid(1, volume=10.0, buy_p=1000.0)],
            pending_order={
                "identifier": "partial-sale-ident",
                "action": "SELL",
                "exit_type": "INDIVIDUAL",
                "lvl": 1,
                "squad": 1,
                "target_vol": 10.0,
                "target_grids": [self._make_grid(1, volume=10.0, buy_p=1000.0)],
                "uuid": None
            }
        )

        # Upbit confirms terminal cancel with partial fill: 4 out of 10 executed
        fake_order = {
            "uuid": "partial-sale-uuid",
            "market": "KRW-XRP",
            "side": "ask",
            "state": "cancel",
            "executed_volume": "4",
            "paid_fee": "2.2",
            "trades": [{"price": "1100", "volume": "4", "funds": "4400"}]
        }
        fake_client.get_order_by_identifier.return_value = fake_order

        state = runner.reconcile_pending_order(state, state["pending_order"], "2026-10-21 21:00:00")
        self.assertIsNone(state.get("pending_order"))
        # 6 volume must remain in active_grids
        self.assertEqual(len(state["active_grids"]), 1)
        self.assertAlmostEqual(state["active_grids"][0]["vol"], 6.0)
        self.assertAlmostEqual(state["active_grids"][0]["cost"], 6000.0)
        self.assertAlmostEqual(state["active_grids"][0]["fee"], 3.0)

        # DB must have recorded the 4 volume partial sale
        with sqlite3.connect(runner.db_file) as db:
            row = db.execute("SELECT action, volume, cost_or_revenue, pnl FROM pocket_trades").fetchone()
        self.assertIn("PARTIAL", row[0])
        self.assertEqual(row[1], 4.0)
        self.assertAlmostEqual(row[3], 395.8)  # 4400 - 4000 - 2 (buy fee) - 2.2 (sell fee) = 395.8

    def test_r8_crash_after_sell_db_commit_recovers_idempotently(self):
        """매도 DB 커밋 후 JSON 저장 전 크래시 발생 시 재시작 대사로 중복 없이 완전 복구 및 반복 대사 멱등성 검증"""
        fake_client = Mock()
        fake_client.get_balance.return_value = 900000
        runner = self._make_runner(fake_client)

        state = runner.load_state()
        state.update(base_price=1000.0, active_grids=[self._make_grid(1)])

        fake_sell_res = {
            "status": "FILLED",
            "price": 1100.0,
            "volume": 10.0,
            "funds": 11000.0,
            "fee": 5.5,
            "uuid": "sell-crash-uuid"
        }
        fake_client.sell_market_order_and_wait.return_value = fake_sell_res

        # 1. Execute sell
        state = runner.process_tick(state, 1100.0, "2026-10-21 21:00:00")
        self.assertEqual(len(state["active_grids"]), 0)

        # 2. Simulate process crash before JSON save by loading an un-updated state with pending_order
        restarted_state = {
            "market": "KRW-XRP",
            "allocated_budget": 300000.0,
            "active_grids": [self._make_grid(1)],  # State file still has old active grid
            "pending_order": {
                "identifier": "sell-crash-ident",
                "action": "SELL",
                "exit_type": "INDIVIDUAL",
                "lvl": 1,
                "squad": 1,
                "target_vol": 10.0,
                "target_grids": [self._make_grid(1)],
                "uuid": "sell-crash-uuid"
            }
        }
        with open(runner.state_file, "w") as f:
            json.dump(restarted_state, f)

        reloaded = runner.load_state()
        self.assertEqual(len(reloaded["active_grids"]), 1)

        fake_order = {
            "uuid": "sell-crash-uuid",
            "market": "KRW-XRP",
            "side": "ask",
            "state": "done",
            "executed_volume": "10",
            "paid_fee": "5.5",
            "trades": [{"price": "1100", "volume": "10", "funds": "11000"}]
        }
        fake_client.get_order.return_value = fake_order

        # 3. Reconcile on restart: must deduct grid and clear pending without UNIQUE errors
        reloaded = runner.reconcile_pending_order(reloaded, reloaded["pending_order"], "2026-10-21 21:00:05")
        self.assertIsNone(reloaded.get("pending_order"))
        self.assertEqual(len(reloaded["active_grids"]), 0)
        self.assertAlmostEqual(reloaded["realized_pnl"], 989.5)

        # DB must still have exactly 1 sell row
        with sqlite3.connect(runner.db_file) as db:
            sell_rows = db.execute("SELECT count(*) FROM pocket_trades WHERE order_uuid = 'sell-crash-uuid'").fetchone()[0]
        self.assertEqual(sell_rows, 1)

        # 4. Replay attempt 2: calling reconciliation again is a strict NO-OP (P1-V3-1 방어)
        reloaded = runner.reconcile_pending_order(reloaded, restarted_state["pending_order"], "2026-10-21 21:00:10")
        self.assertEqual(len(reloaded["active_grids"]), 0)
        self.assertAlmostEqual(reloaded["realized_pnl"], 989.5)
        self.assertAlmostEqual(reloaded["total_fees"], 10.5)
        with sqlite3.connect(runner.db_file) as db:
            sell_rows = db.execute("SELECT count(*) FROM pocket_trades WHERE order_uuid = 'sell-crash-uuid'").fetchone()[0]
        self.assertEqual(sell_rows, 1)

    def test_r9_individual_sell_repeated_reconciliation_noop(self):
        """P1-V3-1: 개별 전량 매도 체결 건에 대해 reconcile_pending_order 반복 호출 시 엄격한 No-Op 검증"""
        fake_client = Mock()
        runner = self._make_runner(fake_client)

        state = runner.load_state()
        grid = self._make_grid(1, volume=10.0, buy_p=1000.0)
        pending = {
            "identifier": "repeat-full-sale",
            "action": "SELL",
            "exit_type": "INDIVIDUAL",
            "lvl": 1,
            "squad": 1,
            "target_vol": 10.0,
            "target_grids": [copy.deepcopy(grid)],
            "uuid": None
        }
        state.update(active_grids=[copy.deepcopy(grid)], pending_order=pending)

        order = {
            "uuid": "full-sale-uuid",
            "identifier": "repeat-full-sale",
            "market": "KRW-XRP",
            "side": "ask",
            "state": "done",
            "executed_volume": "10.0",
            "paid_fee": "5.5",
            "trades": [{"price": "1100", "volume": "10.0", "funds": "11000"}]
        }
        fake_client.get_order_by_identifier.return_value = order

        # 1차 정산
        st1 = runner.reconcile_pending_order(state, pending, "2026-10-21 21:00:00")
        qty1 = sum(g["vol"] for g in st1["active_grids"])
        pnl1 = st1["realized_pnl"]
        fees1 = st1["total_fees"]
        trades1 = st1.get("total_trades", 0)
        wins1 = st1.get("win_count", 0)

        self.assertEqual(qty1, 0)
        self.assertAlmostEqual(pnl1, 989.5)
        self.assertAlmostEqual(fees1, 10.5)

        # 2차 정산 (동일 execution replay)
        st2 = runner.reconcile_pending_order(st1, pending, "2026-10-21 21:00:05")
        qty2 = sum(g["vol"] for g in st2["active_grids"])
        pnl2 = st2["realized_pnl"]
        fees2 = st2["total_fees"]
        trades2 = st2.get("total_trades", 0)
        wins2 = st2.get("win_count", 0)

        # 3차 정산 (디스크에서 재로드 후 replay)
        reloaded = runner.load_state()
        st3 = runner.reconcile_pending_order(reloaded, pending, "2026-10-21 21:00:10")
        qty3 = sum(g["vol"] for g in st3["active_grids"])
        pnl3 = st3["realized_pnl"]
        fees3 = st3["total_fees"]

        # 모든 상태 지표가 완전히 불변이어야 함
        self.assertEqual((qty1, pnl1, fees1, trades1, wins1), (qty2, pnl2, fees2, trades2, wins2))
        self.assertEqual((qty1, pnl1, fees1), (qty3, pnl3, fees3))

        # DB에는 여전히 정확히 1건만 존재
        with sqlite3.connect(runner.db_file) as db:
            rows = db.execute("SELECT count(*), sum(volume), sum(pnl) FROM pocket_trades").fetchone()
        self.assertEqual(rows[0], 1)
        self.assertAlmostEqual(rows[1], 10.0)
        self.assertAlmostEqual(rows[2], 989.5)

    def test_r10_partial_sell_repeated_reconciliation_noop(self):
        """P1-V3-1: 부분 매도 체결(4 out of 10) 건에 대해 reconcile_pending_order 반복 호출 시 엄격한 No-Op 검증"""
        fake_client = Mock()
        runner = self._make_runner(fake_client)

        state = runner.load_state()
        grid = self._make_grid(1, volume=10.0, buy_p=1000.0)
        pending = {
            "identifier": "repeat-partial-sale",
            "action": "SELL",
            "exit_type": "INDIVIDUAL",
            "lvl": 1,
            "squad": 1,
            "target_vol": 10.0,
            "target_grids": [copy.deepcopy(grid)],
            "uuid": None
        }
        state.update(active_grids=[copy.deepcopy(grid)], pending_order=pending)

        order = {
            "uuid": "partial-sale-uuid-2",
            "identifier": "repeat-partial-sale",
            "market": "KRW-XRP",
            "side": "ask",
            "state": "cancel",
            "executed_volume": "4.0",
            "paid_fee": "2.2",
            "trades": [{"price": "1100", "volume": "4.0", "funds": "4400"}]
        }
        fake_client.get_order_by_identifier.return_value = order

        # 1차 정산
        st1 = runner.reconcile_pending_order(state, pending, "2026-10-21 21:00:00")
        qty1 = sum(g["vol"] for g in st1["active_grids"])
        cost1 = sum(g["cost"] for g in st1["active_grids"])
        pnl1 = st1["realized_pnl"]
        fees1 = st1["total_fees"]

        self.assertAlmostEqual(qty1, 6.0)
        self.assertAlmostEqual(cost1, 6000.0)
        self.assertAlmostEqual(pnl1, 395.8)
        self.assertAlmostEqual(fees1, 4.2)

        # 2차 정산 (동일 execution replay)
        st2 = runner.reconcile_pending_order(st1, pending, "2026-10-21 21:00:05")
        qty2 = sum(g["vol"] for g in st2["active_grids"])
        cost2 = sum(g["cost"] for g in st2["active_grids"])
        pnl2 = st2["realized_pnl"]
        fees2 = st2["total_fees"]

        # 3차 정산 (재로드 후 replay)
        reloaded = runner.load_state()
        st3 = runner.reconcile_pending_order(reloaded, pending, "2026-10-21 21:00:10")
        qty3 = sum(g["vol"] for g in st3["active_grids"])
        cost3 = sum(g["cost"] for g in st3["active_grids"])
        pnl3 = st3["realized_pnl"]
        fees3 = st3["total_fees"]

        self.assertEqual((qty1, cost1, pnl1, fees1), (qty2, cost2, pnl2, fees2))
        self.assertEqual((qty1, cost1, pnl1, fees1), (qty3, cost3, pnl3, fees3))

        with sqlite3.connect(runner.db_file) as db:
            rows = db.execute("SELECT count(*), sum(volume), sum(pnl) FROM pocket_trades").fetchone()
        self.assertEqual(rows[0], 1)
        self.assertAlmostEqual(rows[1], 4.0)
        self.assertAlmostEqual(rows[2], 395.8)

    def test_r11_squad_sell_repeated_reconciliation_noop(self):
        """P1-V3-1: 다중 그리드 스쿼드 일괄 매도 건에 대해 reconcile_pending_order 반복 호출 시 엄격한 No-Op 검증"""
        fake_client = Mock()
        runner = self._make_runner(fake_client)

        state = runner.load_state()
        g1 = self._make_grid(1, volume=10.0, buy_p=1000.0)
        g2 = self._make_grid(2, volume=10.0, buy_p=995.0)
        pending = {
            "identifier": "repeat-squad-sale",
            "action": "SELL",
            "exit_type": "SQUAD",
            "lvl": 2,
            "squad": 1,
            "target_vol": 20.0,
            "target_grids": [copy.deepcopy(g1), copy.deepcopy(g2)],
            "uuid": None
        }
        state.update(active_grids=[copy.deepcopy(g1), copy.deepcopy(g2)], pending_order=pending)

        order = {
            "uuid": "squad-sale-repeat-uuid",
            "identifier": "repeat-squad-sale",
            "market": "KRW-XRP",
            "side": "ask",
            "state": "done",
            "executed_volume": "20.0",
            "paid_fee": "11.0",
            "trades": [{"price": "1100", "volume": "20.0", "funds": "22000"}]
        }
        fake_client.get_order_by_identifier.return_value = order

        # 1차 정산
        st1 = runner.reconcile_pending_order(state, pending, "2026-10-21 21:00:00")
        qty1 = sum(g["vol"] for g in st1["active_grids"])
        pnl1 = st1["realized_pnl"]
        fees1 = st1["total_fees"]
        trades1 = st1.get("total_trades", 0)

        self.assertEqual(qty1, 0)
        self.assertEqual(trades1, 2)

        # 2차 정산 (동일 execution replay)
        st2 = runner.reconcile_pending_order(st1, pending, "2026-10-21 21:00:05")
        qty2 = sum(g["vol"] for g in st2["active_grids"])
        pnl2 = st2["realized_pnl"]
        fees2 = st2["total_fees"]
        trades2 = st2.get("total_trades", 0)

        # 3차 정산 (재로드 후 replay)
        reloaded = runner.load_state()
        st3 = runner.reconcile_pending_order(reloaded, pending, "2026-10-21 21:00:10")
        qty3 = sum(g["vol"] for g in st3["active_grids"])
        pnl3 = st3["realized_pnl"]
        fees3 = st3["total_fees"]
        trades3 = st3.get("total_trades", 0)

        self.assertEqual((qty1, pnl1, fees1, trades1), (qty2, pnl2, fees2, trades2))
        self.assertEqual((qty1, pnl1, fees1, trades1), (qty3, pnl3, fees3, trades3))

        with sqlite3.connect(runner.db_file) as db:
            rows = db.execute("SELECT count(*), sum(volume) FROM pocket_trades").fetchone()
        self.assertEqual(rows[0], 2)
        self.assertAlmostEqual(rows[1], 20.0)

    def test_r12_state_contract_validation(self):
        """P2: 상태 파일 검증 강화 (active_grids 누락/타입, 음수 원가, 0 이하 tp_p, 마켓 불일치 감지 시 안전 중단)"""
        runner = self._make_runner(Mock())
        fresh = runner.load_state()

        # 1. active_grids 누락
        missing_grids = copy.deepcopy(fresh)
        missing_grids.pop("active_grids")
        with open(runner.state_file, "w") as f:
            json.dump(missing_grids, f)
        loaded = runner.load_state()
        self.assertTrue(loaded.get("trading_halted"))
        self.assertEqual(loaded.get("error"), "MISSING_OR_INVALID_ACTIVE_GRIDS")

        # 2. active_grids 비정상 타입
        invalid_grids_type = copy.deepcopy(fresh)
        invalid_grids_type["active_grids"] = "not-a-list"
        with open(runner.state_file, "w") as f:
            json.dump(invalid_grids_type, f)
        loaded = runner.load_state()
        self.assertTrue(loaded.get("trading_halted"))
        self.assertEqual(loaded.get("error"), "MISSING_OR_INVALID_ACTIVE_GRIDS")

        # 3. 음수 cost 및 0 tp_p
        invalid_grid = copy.deepcopy(fresh)
        invalid_grid["active_grids"] = [dict(self._make_grid(1), cost=-10000.0, tp_p=0.0)]
        with open(runner.state_file, "w") as f:
            json.dump(invalid_grid, f)
        loaded = runner.load_state()
        self.assertTrue(loaded.get("trading_halted"))
        self.assertIn("INVALID_GRID_ACCOUNTING", loaded.get("error", ""))

        # 4. 마켓 불일치
        mismatched_market = copy.deepcopy(fresh)
        mismatched_market["market"] = "KRW-BTC"
        with open(runner.state_file, "w") as f:
            json.dump(mismatched_market, f)
        loaded = runner.load_state()
        self.assertTrue(loaded.get("trading_halted"))
        self.assertIn("MARKET_MISMATCH", loaded.get("error", ""))

    def test_r13_zero_fill_cancellation_and_terminal_error_fail_closed(self):
        """P2: 정상 zero-fill 취소 시 보류 해제 및 체결자료 누락 시 fail-closed 검증"""
        fake_client = Mock()
        runner = self._make_runner(fake_client)

        state = runner.load_state()
        grid = self._make_grid(1)
        pending = {
            "identifier": "zero-cancel-ident",
            "action": "SELL",
            "lvl": 1,
            "squad": 1,
            "target_vol": 10.0,
            "target_grids": [grid],
            "uuid": None
        }
        state.update(active_grids=[grid], pending_order=pending)

        # 1. 정상 zero-fill cancel 확인: 예외 없이 status=CANCELLED 반환 및 pending_order 해제
        zero_cancel_order = {
            "uuid": "cancel-zero-uuid",
            "identifier": "zero-cancel-ident",
            "market": "KRW-XRP",
            "side": "ask",
            "state": "cancel",
            "executed_volume": "0",
            "trades": []
        }
        fake_client.get_order_by_identifier.return_value = zero_cancel_order

        st_after = runner.reconcile_pending_order(state, pending, "2026-10-21 21:00:00")
        self.assertIsNone(st_after.get("pending_order"))
        self.assertEqual(len(st_after["active_grids"]), 1)
        self.assertEqual(st_after["active_grids"][0]["vol"], 10.0)

        # 2. 체결량이 10인데 trades가 비어있는 비정상 취소: Fail-Closed
        fake_client_bad = Mock()
        runner_bad = self._make_runner(fake_client_bad)
        state_bad = runner_bad.load_state()
        state_bad.update(active_grids=[grid], pending_order=pending)
        bad_cancel_order = {
            "uuid": "bad-cancel-uuid",
            "identifier": "zero-cancel-ident",
            "market": "KRW-XRP",
            "side": "ask",
            "state": "cancel",
            "executed_volume": "10.0",
            "trades": []
        }
        fake_client_bad.get_order_by_identifier.return_value = bad_cancel_order
        st_bad_after = runner_bad.reconcile_pending_order(state_bad, pending, "2026-10-21 21:00:00")
        # pending_order must NOT be cleared!
        self.assertIsNotNone(st_bad_after.get("pending_order"))

    def test_r14_partial_buy_reconciliation_and_restart_recovery(self):
        """부분 매수 체결(Partial Buy on Cancel)에 대한 정확한 장부 반영, DB 저널링 및 재시작 멱등 복구 검증"""
        fake_client = Mock()
        fake_client.get_balance.return_value = 900000
        runner = self._make_runner(fake_client)

        state = runner.load_state()
        state.update(
            base_price=1000.0,
            active_grids=[],
            pending_order={
                "identifier": "partial-buy-ident",
                "action": "BUY",
                "lvl": 1,
                "squad": 1,
                "uuid": "partial-buy-uuid"
            }
        )

        # 거래소 응답: 부분 체결 후 잔여 취소 (6 out of 10 executed)
        fake_order = {
            "uuid": "partial-buy-uuid",
            "identifier": "partial-buy-ident",
            "market": "KRW-XRP",
            "side": "bid",
            "state": "cancel",
            "executed_volume": "6.0",
            "paid_fee": "3.0",
            "trades": [{"price": "1000", "volume": "6.0", "funds": "6000"}]
        }
        fake_client.get_order.return_value = fake_order

        # 1. 1차 정산 대사: active_grids에 부분 체결분(vol=6.0, cost=6000) 정확히 등록 및 pending 해제
        st_after = runner.reconcile_pending_order(state, state["pending_order"], "2026-10-21 21:00:00")
        self.assertIsNone(st_after.get("pending_order"))
        self.assertEqual(len(st_after["active_grids"]), 1)
        grid = st_after["active_grids"][0]
        self.assertEqual(grid["lvl"], 1)
        self.assertAlmostEqual(grid["vol"], 6.0)
        self.assertAlmostEqual(grid["cost"], 6000.0)
        self.assertAlmostEqual(grid["fee"], 3.0)
        self.assertAlmostEqual(grid["tp_p"], 1010.0)  # +1.0%

        # DB 저널 기록 검증
        with sqlite3.connect(runner.db_file) as db:
            rows = db.execute("SELECT side, action, price, volume, cost_or_revenue, fee, allocation_id FROM pocket_trades").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "bid")
        self.assertEqual(rows[0][3], 6.0)
        self.assertEqual(rows[0][4], 6000.0)
        self.assertEqual(rows[0][5], 3.0)
        self.assertEqual(rows[0][6], "partial-buy-uuid:buy_lvl_1")

        # 2. 크래시 & 재시작 복구 시뮬레이션 (DB에는 들어갔으나 JSON 상태 파일이 업데이트되지 않은 상태)
        crashed_state = {
            "market": "KRW-XRP",
            "allocated_budget": 300000.0,
            "active_grids": [],  # 크래시로 active_grids 미반영
            "pending_order": {
                "identifier": "partial-buy-ident",
                "action": "BUY",
                "lvl": 1,
                "squad": 1,
                "uuid": "partial-buy-uuid"
            }
        }
        with open(runner.state_file, "w") as f:
            json.dump(crashed_state, f)

        reloaded = runner.load_state()
        self.assertEqual(len(reloaded["active_grids"]), 0)
        self.assertIsNotNone(reloaded.get("pending_order"))

        # 재시작 후 대사: 중복 저널 오류 없이 active_grids가 복구되고 pending_order가 해제되어야 함
        reloaded = runner.reconcile_pending_order(reloaded, reloaded["pending_order"], "2026-10-21 21:00:05")
        self.assertIsNone(reloaded.get("pending_order"))
        self.assertEqual(len(reloaded["active_grids"]), 1)
        self.assertAlmostEqual(reloaded["active_grids"][0]["vol"], 6.0)

        # DB는 여전히 1개 행만 유지되어야 함
        with sqlite3.connect(runner.db_file) as db:
            count = db.execute("SELECT count(*) FROM pocket_trades WHERE order_uuid = 'partial-buy-uuid'").fetchone()[0]
        self.assertEqual(count, 1)

        # 3. 추가 중복 대사 호출 시 엄격한 No-Op 검증
        reloaded = runner.reconcile_pending_order(reloaded, crashed_state["pending_order"], "2026-10-21 21:00:10")
        self.assertEqual(len(reloaded["active_grids"]), 1)
        self.assertAlmostEqual(reloaded["active_grids"][0]["vol"], 6.0)


if __name__ == "__main__":
    unittest.main()
