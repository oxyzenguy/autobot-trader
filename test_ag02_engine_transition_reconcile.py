"""AG-02 tests: Engine transition (LEGACY <-> ADAPTIVE_GRID_C_E) order reconciliation.

Validates that when switching strategy engines:
1. Completed buy prior to transition is reconciled to tranches + bot_quantity, logged to DB, and checkpoint updated (not dropped as cancel/0).
2. Partial fill during transition cancellation is credited exactly once.
3. Completed basket ask prior to transition is reconciled, PnL recorded, tranches cleared, and basket_limit_ask removed.
4. Exchange query / cancel / state save failure triggers halt and blocks engine transition.
5. Bi-directional transitions (LEGACY -> ADAPTIVE_GRID_C_E and ADAPTIVE_GRID_C_E -> LEGACY) both execute safely.
6. Manual orders remain untouched across engine transition.

Fake exchange only; temporary SQLite & isolated state files; operational DB/JSON untouched.
"""

from __future__ import annotations

import copy
import io
import os
import sqlite3
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import main
from utils import db_logger
from test_p1_squad_exit import EndTick, SquadFakeExchange


class TestAG02EngineTransitionReconcile(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.temp_dir = tmp.name
        self.db_path = os.path.join(self.temp_dir, "test_ag02.db")
        self.state_path = os.path.join(self.temp_dir, "test_state.json")

        p_db = patch.object(db_logger, "DB_PATH", self.db_path)
        p_db.start()
        self.addCleanup(p_db.stop)

        sync = types.SimpleNamespace(trigger_snapshot_sync=lambda: None)
        p_sync = patch.dict(sys.modules, {"utils.sync_manager": sync})
        p_sync.start()
        self.addCleanup(p_sync.stop)

        p_base = patch.object(main, "BASE_DIR", self.temp_dir)
        p_base.start()
        self.addCleanup(p_base.stop)

        p_state_path = patch.object(
            main, "get_state_file_path",
            lambda m: os.path.join(self.temp_dir, f"real_strategy_state_{m.replace('-', '_')}.json")
        )
        p_state_path.start()
        self.addCleanup(p_state_path.stop)

        p_save = patch.object(main, "save_strategy_state", lambda m, s: True)
        p_save.start()
        self.addCleanup(p_save.stop)

        p_alert = patch.object(main, "send_telegram_alert", lambda msg: None)
        p_alert.start()
        self.addCleanup(p_alert.stop)

        p_req = patch("requests.post", side_effect=AssertionError("live HTTP request forbidden in unit tests"))
        p_req.start()
        self.addCleanup(p_req.stop)

        db_logger.init_db()

    def _make_state(self, market="KRW-ETH", engine="LEGACY"):
        return {
            "market": market,
            "current_regime": "BEAR",
            "initial_entry_done": True,
            "strategy_engine": engine,
            "bot_quantity": 0.1,
            "bot_avg_price": 4_000_000.0,
            "tranches": [{
                "tranche_id": 1,
                "step": 1,
                "squad_id": 1,
                "squad_stage": 1,
                "units": 1,
                "volume": 0.1,
                "buy_price": 4_000_000.0
            }],
            "bear_limit_buys": {},
            "basket_limit_ask": None,
            "completed_cycles": 0,
        }

    def test_completed_buy_prior_to_transition_is_reconciled_not_cancelled(self):
        """전환 직전 거래소에서 이미 완료(done)된 매수는 cancel로 덮어쓰지 않고 장부와 DB에 정상 반영."""
        market = "KRW-ETH"
        state = self._make_state(market, engine="LEGACY")
        state["bear_limit_buys"]["bid-done-1"] = {
            "state": "wait", "units": 1, "squad_id": 1, "squad_stage": 2,
            "executed_volume": 0.0, "executed_funds": 0.0
        }

        ex = SquadFakeExchange(main.PROTECTED_BALANCES[market] + 0.15, 3_700_000.0)
        done_order = {
            "uuid": "bid-done-1", "side": "bid", "state": "done",
            "executed_volume": "0.05", "trades": [{"funds": "185000", "volume": "0.05", "price": "3700000"}]
        }
        ex.orders["bid-done-1"] = done_order
        # Note: done order is NOT in open_orders

        with patch.object(main, "save_strategy_state", lambda m, s: True):
            ok = main.reconcile_and_settle_engine_transition_orders(ex, market, state)

        self.assertTrue(ok)
        self.assertAlmostEqual(0.15, state["bot_quantity"])
        self.assertEqual("done", state["bear_limit_buys"]["bid-done-1"]["state"])
        self.assertAlmostEqual(0.05, state["bear_limit_buys"]["bid-done-1"]["executed_volume"])
        self.assertAlmostEqual(185000.0, state["bear_limit_buys"]["bid-done-1"]["executed_funds"])
        self.assertEqual(2, len(state["tranches"]))
        self.assertAlmostEqual(0.05, state["tranches"][1]["volume"])

        # DB 검증
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute("SELECT volume, cost_or_revenue FROM trades WHERE order_uuid='bid-done-1'").fetchone()
            self.assertIsNotNone(row)
            self.assertAlmostEqual(0.05, row[0])
            self.assertAlmostEqual(185000.0, row[1])

    def test_partial_fill_during_cancel_credited_exactly_once(self):
        """취소 중 발생한 부분체결은 정확히 1회만 tranches/bot_quantity에 반영."""
        market = "KRW-ETH"
        state = self._make_state(market, engine="LEGACY")
        state["bear_limit_buys"]["bid-part-1"] = {
            "state": "wait", "units": 1, "squad_id": 1, "squad_stage": 2,
            "executed_volume": 0.0, "executed_funds": 0.0
        }

        ex = SquadFakeExchange(main.PROTECTED_BALANCES[market] + 0.13, 3_700_000.0)
        # Initially open with 0 fills
        open_bid = {
            "uuid": "bid-part-1", "side": "bid", "state": "wait", "price": "3700000",
            "volume": "0.05", "executed_volume": "0", "trades": []
        }
        ex.open_orders = [open_bid.copy()]
        ex.orders["bid-part-1"] = open_bid

        # When cancel is called, exchange reports cancelled with executed_volume=0.03
        def cancel_hook(identifier):
            ex.orders[identifier].update(
                state="cancel",
                executed_volume="0.03",
                trades=[{"funds": "111000", "volume": "0.03", "price": "3700000"}]
            )
            ex.open_orders = []
            ex.cancelled.append(identifier)
            return {"uuid": identifier}

        ex.cancel_order = cancel_hook

        with patch.object(main, "save_strategy_state", lambda m, s: True):
            ok = main.reconcile_and_settle_engine_transition_orders(ex, market, state)

        self.assertTrue(ok)
        self.assertAlmostEqual(0.13, state["bot_quantity"])
        self.assertEqual("cancel", state["bear_limit_buys"]["bid-part-1"]["state"])
        self.assertAlmostEqual(0.03, state["bear_limit_buys"]["bid-part-1"]["executed_volume"])
        self.assertAlmostEqual(111000.0, state["bear_limit_buys"]["bid-part-1"]["executed_funds"])
        self.assertEqual(2, len(state["tranches"]))
        self.assertAlmostEqual(0.03, state["tranches"][1]["volume"])

    def test_completed_basket_ask_prior_to_transition_reconciled_and_cleared(self):
        """전환 전 이미 완료된 바스켓 매도는 익절 실현손익을 기록하고 tranches를 비우며 ask checkpoint를 제거."""
        market = "KRW-ETH"
        state = self._make_state(market, engine="LEGACY")
        state["basket_limit_ask"] = {"uuid": "ask-done-1", "price": 4_050_000.0, "volume": 0.1}

        ex = SquadFakeExchange(main.PROTECTED_BALANCES[market], 4_050_000.0)
        done_ask = {
            "uuid": "ask-done-1", "side": "ask", "state": "done",
            "executed_volume": "0.1", "price": "4050000",
            "trades": [{"funds": "405000", "volume": "0.1", "price": "4050000"}]
        }
        ex.orders["ask-done-1"] = done_ask

        with patch.object(main, "save_strategy_state", lambda m, s: True):
            ok = main.reconcile_and_settle_engine_transition_orders(ex, market, state)

        self.assertTrue(ok)
        self.assertAlmostEqual(0.0, state["bot_quantity"])
        self.assertAlmostEqual(0.0, state["bot_avg_price"])
        self.assertEqual([], state["tranches"])
        self.assertIsNone(state.get("basket_limit_ask"))
        self.assertEqual(1, state["completed_cycles"])

        # DB 검증: BASKET_TAKE_PROFIT_LIMIT 확인
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute("SELECT action, volume, cost_or_revenue, pnl FROM trades WHERE order_uuid='ask-done-1'").fetchone()
            self.assertIsNotNone(row)
            self.assertEqual("BASKET_TAKE_PROFIT_LIMIT", row[0])
            self.assertAlmostEqual(0.1, row[1])
            self.assertAlmostEqual(405000.0, row[2])
            self.assertAlmostEqual(5000.0, row[3])  # 405,000 - 400,000 = +5,000

    def test_ask_previous_partial_then_done_no_double_deduct(self):
        """AG02-R1: 이전 부분 매도(0.04) 후 done(0.1) 시 누적 차액(0.06)만 차감하여 실제 잔여와 장부가 일치하고 DB 합계가 0.1/405,000원."""
        market = "KRW-ETH"
        state = self._make_state(market, engine="LEGACY")
        state["basket_limit_ask"] = {"uuid": "ask-part-done", "price": 4_050_000.0, "volume": 0.1}

        # 0.04 ETH는 이미 매도되어 장부 0.06 남고, 별도 매수 0.05가 추가되어 총 0.11 ETH 보유 상태
        actual_remaining = 0.05
        state["tranches"][0]["volume"] = 0.06
        state["tranches"].append({
            "tranche_id": 2, "step": 2, "units": 1,
            "volume": 0.05, "buy_price": 3_600_000.0, "squad_id": 1, "squad_stage": 2
        })
        state["bot_quantity"] = 0.11
        state["bot_avg_price"] = (0.06 * 4_000_000 + 0.05 * 3_600_000) / 0.11
        state["_reconciled_asks"] = {"ask-part-done": 0.04}

        # 이전 부분 매도 DB 기록 (0.04 ETH / 162,000원)
        db_logger.log_real_trade(
            market=market, ticker="ETH", side="ask",
            action="LIMIT_ASK_CANCEL_FILL", price=4_050_000.0, volume=0.04,
            cost_or_revenue=162_000.0, order_uuid="ask-part-done"
        )

        ex = SquadFakeExchange(main.PROTECTED_BALANCES[market] + actual_remaining, 3_700_000.0)
        ex.orders["ask-part-done"] = {
            "uuid": "ask-part-done", "side": "ask", "state": "done",
            "executed_volume": "0.1", "price": "4050000",
            "trades": [{"funds": "405000", "volume": "0.1", "price": "4050000"}]
        }

        with patch.object(main, "save_strategy_state", lambda m, s: True):
            ok = main.reconcile_and_settle_engine_transition_orders(ex, market, state)

        self.assertTrue(ok)
        # 실제 잔여와 장부 잔여가 모두 0.05 ETH로 일치 (0.01로 과다 차감되지 않음)
        self.assertAlmostEqual(actual_remaining, state["bot_quantity"])
        self.assertEqual(1, len(state["tranches"]))
        self.assertAlmostEqual(0.05, state["tranches"][0]["volume"])
        self.assertAlmostEqual(3_600_000.0, state["tranches"][0]["buy_price"])
        self.assertIsNone(state.get("basket_limit_ask"))
        self.assertEqual(1, state["completed_cycles"])

        # DB 검증: 2행, 총 매도 수량 0.1 ETH, 총 매도 금액 405,000원
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(volume), 0), COALESCE(SUM(cost_or_revenue), 0) "
                "FROM trades WHERE side='ask' AND order_uuid='ask-part-done'"
            ).fetchone()
            self.assertEqual(2, rows[0])
            self.assertAlmostEqual(0.1, rows[1])
            self.assertAlmostEqual(405_000.0, rows[2])

    def test_ask_done_reprocessing_idempotent(self):
        """동일한 완료(done) 응답을 재처리해도 추가 차감·기록·사이클 증가가 발생하지 않음 (멱등성)."""
        market = "KRW-ETH"
        state = self._make_state(market, engine="LEGACY")
        state["basket_limit_ask"] = {"uuid": "ask-idemp", "price": 4_050_000.0, "volume": 0.1}

        ex = SquadFakeExchange(main.PROTECTED_BALANCES[market], 4_050_000.0)
        ex.orders["ask-idemp"] = {
            "uuid": "ask-idemp", "side": "ask", "state": "done",
            "executed_volume": "0.1", "price": "4050000",
            "trades": [{"funds": "405000", "volume": "0.1", "price": "4050000"}]
        }

        with patch.object(main, "save_strategy_state", lambda m, s: True):
            ok1 = main.reconcile_and_settle_engine_transition_orders(ex, market, state)
        self.assertTrue(ok1)
        self.assertAlmostEqual(0.0, state["bot_quantity"])
        self.assertEqual(1, state["completed_cycles"])

        # 재처리 실행 (동일 state & exchange)
        with patch.object(main, "save_strategy_state", lambda m, s: True):
            ok2 = main.reconcile_and_settle_engine_transition_orders(ex, market, state)
        self.assertTrue(ok2)
        self.assertAlmostEqual(0.0, state["bot_quantity"])
        self.assertEqual(1, state["completed_cycles"])  # 사이클 중복 증가 없음

        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(volume), 0), COALESCE(SUM(cost_or_revenue), 0) "
                "FROM trades WHERE side='ask' AND order_uuid='ask-idemp'"
            ).fetchone()
            self.assertEqual(1, rows[0])  # 추가 행 없음
            self.assertAlmostEqual(0.1, rows[1])
            self.assertAlmostEqual(405_000.0, rows[2])

    def test_ask_partial_fills_varying_prices_delta_funds_exact(self):
        """부분체결 간 체결가격이 달라도 금액 차액과 평균단가가 정확히 계산되어 DB에 기록."""
        market = "KRW-ETH"
        state = self._make_state(market, engine="LEGACY")
        state["basket_limit_ask"] = {"uuid": "ask-var-price", "price": 4_050_000.0, "volume": 0.1}
        # 첫 번째 체결: 0.04 ETH @ 4,000,000원 = 160,000원
        state["_reconciled_asks"] = {"ask-var-price": {"volume": 0.04, "funds": 160_000.0}}
        db_logger.log_real_trade(
            market=market, ticker="ETH", side="ask",
            action="LIMIT_ASK_CANCEL_FILL", price=4_000_000.0, volume=0.04,
            cost_or_revenue=160_000.0, order_uuid="ask-var-price"
        )

        # 최종 체결: 총 0.10 ETH, 2차 체결분은 0.06 ETH @ 4,100,000원 = 246,000원
        # 총 funds = 160,000 + 246,000 = 406,000원
        ex = SquadFakeExchange(main.PROTECTED_BALANCES[market], 4_100_000.0)
        ex.orders["ask-var-price"] = {
            "uuid": "ask-var-price", "side": "ask", "state": "done",
            "executed_volume": "0.1", "price": "4050000",
            "trades": [
                {"funds": "160000", "volume": "0.04", "price": "4000000"},
                {"funds": "246000", "volume": "0.06", "price": "4100000"}
            ]
        }

        with patch.object(main, "save_strategy_state", lambda m, s: True):
            ok = main.reconcile_and_settle_engine_transition_orders(ex, market, state)

        self.assertTrue(ok)
        with sqlite3.connect(self.db_path) as conn:
            trades = conn.execute(
                "SELECT action, volume, cost_or_revenue, price FROM trades "
                "WHERE side='ask' AND order_uuid='ask-var-price' ORDER BY id"
            ).fetchall()
            self.assertEqual(2, len(trades))
            # 1차 부분체결
            self.assertAlmostEqual(0.04, trades[0][1])
            self.assertAlmostEqual(160_000.0, trades[0][2])
            self.assertAlmostEqual(4_000_000.0, trades[0][3])
            # 2차 완료체결 (차액 정확히 반영)
            self.assertAlmostEqual(0.06, trades[1][1])
            self.assertAlmostEqual(246_000.0, trades[1][2])
            self.assertAlmostEqual(4_100_000.0, trades[1][3])

            # 총합
            total_vol = sum(t[1] for t in trades)
            total_funds = sum(t[2] for t in trades)
            self.assertAlmostEqual(0.10, total_vol)
            self.assertAlmostEqual(406_000.0, total_funds)

    def test_exchange_query_failure_blocks_transition_and_returns_false(self):
        """주문 조회 실패 시 False를 반환하여 엔진 전환 및 신규 주문 배치를 차단."""
        market = "KRW-ETH"
        state = self._make_state(market, engine="LEGACY")
        state["bear_limit_buys"]["bid-fail"] = {
            "state": "wait", "units": 1, "squad_id": 1, "squad_stage": 2,
            "executed_volume": 0.0, "executed_funds": 0.0
        }

        ex = SquadFakeExchange(main.PROTECTED_BALANCES[market] + 0.1, 3_700_000.0)
        with patch.object(ex, "get_order", side_effect=ConnectionError("Upbit API unreachable")):
            ok = main.reconcile_and_settle_engine_transition_orders(ex, market, state)

        self.assertFalse(ok)

    def test_cancel_failure_blocks_transition_and_returns_false(self):
        """주문 취소 실패 시 False를 반환하여 안전 차단."""
        market = "KRW-ETH"
        state = self._make_state(market, engine="LEGACY")
        state["bear_limit_buys"]["bid-cancel-fail"] = {
            "state": "wait", "units": 1, "squad_id": 1, "squad_stage": 2,
            "executed_volume": 0.0, "executed_funds": 0.0
        }

        ex = SquadFakeExchange(main.PROTECTED_BALANCES[market] + 0.1, 3_700_000.0)
        open_bid = {
            "uuid": "bid-cancel-fail", "side": "bid", "state": "wait",
            "price": "3700000", "volume": "0.05", "executed_volume": "0", "trades": []
        }
        ex.open_orders = [open_bid.copy()]
        ex.orders["bid-cancel-fail"] = open_bid

        with patch.object(ex, "cancel_order", side_effect=RuntimeError("Cancel rejected")):
            ok = main.reconcile_and_settle_engine_transition_orders(ex, market, state)

        self.assertFalse(ok)

    def test_save_state_failure_blocks_transition_and_returns_false(self):
        """상태 저장 실패 시 False를 반환."""
        market = "KRW-ETH"
        state = self._make_state(market, engine="LEGACY")
        ex = SquadFakeExchange(main.PROTECTED_BALANCES[market] + 0.1, 3_700_000.0)

        with patch.object(main, "save_strategy_state", return_value=False):
            ok = main.reconcile_and_settle_engine_transition_orders(ex, market, state)

        self.assertFalse(ok)

    def test_bi_directional_transitions_safe(self):
        """LEGACY -> ADAPTIVE_GRID_C_E 및 반대 롤백 모두 안전하게 수행."""
        market = "KRW-ETH"
        ex = SquadFakeExchange(main.PROTECTED_BALANCES[market] + 0.1, 3_700_000.0)

        # 1. LEGACY -> ADAPTIVE
        state_fwd = self._make_state(market, engine="LEGACY")
        with patch.object(main, "save_strategy_state", lambda m, s: True):
            ok_fwd = main.reconcile_and_settle_engine_transition_orders(ex, market, state_fwd)
        self.assertTrue(ok_fwd)

        # 2. ADAPTIVE -> LEGACY
        state_rev = self._make_state(market, engine="ADAPTIVE_GRID_C_E")
        with patch.object(main, "save_strategy_state", lambda m, s: True):
            ok_rev = main.reconcile_and_settle_engine_transition_orders(ex, market, state_rev)
        self.assertTrue(ok_rev)

    def test_manual_orders_preserved_during_transition_cancellation(self):
        """엔진 전환 취소 시 봇 주문만 취소되고 수동 주문은 취소되지 않음."""
        market = "KRW-ETH"
        state = self._make_state(market, engine="LEGACY")
        state["bear_limit_buys"]["bot-bid-1"] = {
            "state": "wait", "units": 1, "squad_id": 1, "squad_stage": 2,
            "executed_volume": 0.0, "executed_funds": 0.0
        }

        ex = SquadFakeExchange(main.PROTECTED_BALANCES[market] + 0.1, 3_700_000.0)
        bot_bid = {"uuid": "bot-bid-1", "side": "bid", "state": "wait", "price": "3700000", "volume": "0.05", "executed_volume": "0", "trades": []}
        manual_bid = {"uuid": "manual-bid-1", "side": "bid", "state": "wait", "price": "3000000", "volume": "0.5", "executed_volume": "0", "trades": []}
        manual_ask = {"uuid": "manual-ask-1", "side": "ask", "state": "wait", "price": "5000000", "volume": "0.5", "executed_volume": "0", "trades": []}

        ex.open_orders = [bot_bid.copy(), manual_bid.copy(), manual_ask.copy()]
        ex.orders = {"bot-bid-1": bot_bid, "manual-bid-1": manual_bid, "manual-ask-1": manual_ask}

        with patch.object(main, "save_strategy_state", lambda m, s: True):
            ok = main.reconcile_and_settle_engine_transition_orders(ex, market, state)

        self.assertTrue(ok)
        self.assertEqual(["bot-bid-1"], ex.cancelled)
        remaining_uuids = {o["uuid"] for o in ex.open_orders}
        self.assertEqual({"manual-bid-1", "manual-ask-1"}, remaining_uuids)


    def test_ask_db_failure_blocks_transition_and_preserves_checkpoint(self):
        """AG02-R2: DB 기록 실패(sqlite3.OperationalError) 시 성공 처리 및 UUID 체크포인트 삭제를 금지하고 False 반환."""
        market = "KRW-ETH"
        state = self._make_state(market, engine="LEGACY")
        state["basket_limit_ask"] = {"uuid": "ask-db-fail", "price": 4_050_000.0, "volume": 0.1}

        ex = SquadFakeExchange(main.PROTECTED_BALANCES[market], 4_050_000.0)
        ex.orders["ask-db-fail"] = {
            "uuid": "ask-db-fail", "side": "ask", "state": "done",
            "executed_volume": "0.1", "price": "4050000",
            "trades": [{"funds": "405000", "volume": "0.1", "price": "4050000"}]
        }

        with patch.object(main, "log_real_trade", side_effect=sqlite3.OperationalError("injected DB failure")):
            ok = main.reconcile_and_settle_engine_transition_orders(ex, market, state)

        self.assertFalse(ok)
        self.assertIsNotNone(state.get("basket_limit_ask"))
        self.assertEqual("ask-db-fail", state["basket_limit_ask"]["uuid"])

    def test_ask_save_failure_restart_recovers_ledger_without_duplicate_trade(self):
        """AG02-R2: DB commit 후 JSON 저장 실패·재시작 시, 중복 거래 행 없이 장부(수량/tranches)가 정상 복구."""
        market = "KRW-ETH"
        state = self._make_state(market, engine="LEGACY")
        state["basket_limit_ask"] = {"uuid": "ask-save-fail", "price": 4_050_000.0, "volume": 0.1}

        ex = SquadFakeExchange(main.PROTECTED_BALANCES[market], 4_050_000.0)
        ex.orders["ask-save-fail"] = {
            "uuid": "ask-save-fail", "side": "ask", "state": "done",
            "executed_volume": "0.1", "price": "4050000",
            "trades": [{"funds": "405000", "volume": "0.1", "price": "4050000"}]
        }

        # 1차 실행: DB 커밋 성공, 하지만 save_strategy_state 실패 모의
        with patch.object(main, "save_strategy_state", return_value=False):
            first_ok = main.reconcile_and_settle_engine_transition_orders(ex, market, state)
        self.assertFalse(first_ok)

        # 1차 실패 후 DB에는 정상적으로 1건이 기록되어 있음
        with sqlite3.connect(self.db_path) as conn:
            rows1 = conn.execute("SELECT COUNT(*), COALESCE(SUM(volume),0), COALESCE(SUM(cost_or_revenue),0) FROM trades WHERE side='ask'").fetchone()
            self.assertEqual(1, rows1[0])
            self.assertAlmostEqual(0.1, rows1[1])
            self.assertAlmostEqual(405_000.0, rows1[2])

        # 2차 실행: 프로세스 재시작 시 이전 저장 상태(즉, 저장이 실패했으므로 원래의 state)를 로드
        reloaded_state = self._make_state(market, engine="LEGACY")
        reloaded_state["basket_limit_ask"] = {"uuid": "ask-save-fail", "price": 4_050_000.0, "volume": 0.1}

        with patch.object(main, "save_strategy_state", lambda m, s: True):
            second_ok = main.reconcile_and_settle_engine_transition_orders(ex, market, reloaded_state)
        self.assertTrue(second_ok)

        # 장부가 정상 차감되어 0.0이 되고 tranches 비워짐 (실제 잔여와 장부 일치)
        self.assertAlmostEqual(0.0, reloaded_state["bot_quantity"])
        self.assertEqual([], reloaded_state["tranches"])
        self.assertIsNone(reloaded_state.get("basket_limit_ask"))

        # DB에는 중복 기록 없이 여전히 1건만 존재해야 함 (0.2 ETH로 뻥튀기 금지)
        with sqlite3.connect(self.db_path) as conn:
            rows2 = conn.execute("SELECT COUNT(*), COALESCE(SUM(volume),0), COALESCE(SUM(cost_or_revenue),0) FROM trades WHERE side='ask'").fetchone()
            self.assertEqual(1, rows2[0])
            self.assertAlmostEqual(0.1, rows2[1])
            self.assertAlmostEqual(405_000.0, rows2[2])

    def test_late_bid_cancel_fill_journaled_in_trades(self):
        """AG02-R2: 취소 중 발생한 부분 매수 체결도 누락 없이 trades 테이블에 정상 기록."""
        market = "KRW-ETH"
        state = self._make_state(market, engine="LEGACY")
        state["bear_limit_buys"]["bid-late-1"] = {
            "state": "wait", "units": 1, "squad_id": 1, "squad_stage": 2,
            "executed_volume": 0.0, "executed_funds": 0.0
        }

        ex = SquadFakeExchange(main.PROTECTED_BALANCES[market] + 0.13, 3_700_000.0)
        open_bid = {
            "uuid": "bid-late-1", "side": "bid", "state": "wait", "price": "3700000",
            "volume": "0.05", "executed_volume": "0", "trades": []
        }
        ex.open_orders = [open_bid.copy()]
        ex.orders["bid-late-1"] = open_bid

        def cancel_with_fill(identifier):
            ex.orders[identifier].update(
                state="cancel",
                executed_volume="0.03",
                trades=[{"funds": "111000", "volume": "0.03", "price": "3700000"}]
            )
            ex.open_orders = []
            ex.cancelled.append(identifier)
            return {"uuid": identifier}

        ex.cancel_order = cancel_with_fill

        with patch.object(main, "save_strategy_state", lambda m, s: True):
            ok = main.reconcile_and_settle_engine_transition_orders(ex, market, state)

        self.assertTrue(ok)
        self.assertAlmostEqual(0.13, state["bot_quantity"])

        # DB 검증: trades 테이블에 누락 없이 체결 기록 1건 존재
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT volume, cost_or_revenue FROM trades WHERE order_uuid='bid-late-1'"
            ).fetchone()
            self.assertIsNotNone(row, "취소 중 체결된 매수 주문이 trades 테이블에 기록되어야 함")
            self.assertAlmostEqual(0.03, row[0])
            self.assertAlmostEqual(111_000.0, row[1])

    def test_bid_db_failure_blocks_transition(self):
        """AG02-R2: 매수 체결 DB 기록 실패 시 전환 허용 금지 및 False 반환."""
        market = "KRW-ETH"
        state = self._make_state(market, engine="LEGACY")
        state["bear_limit_buys"]["bid-db-fail"] = {
            "state": "wait", "units": 1, "squad_id": 1, "squad_stage": 2,
            "executed_volume": 0.0, "executed_funds": 0.0
        }

        ex = SquadFakeExchange(main.PROTECTED_BALANCES[market] + 0.15, 3_700_000.0)
        ex.orders["bid-db-fail"] = {
            "uuid": "bid-db-fail", "side": "bid", "state": "done",
            "executed_volume": "0.05", "trades": [{"funds": "185000", "volume": "0.05", "price": "3700000"}]
        }

        with patch.object(main, "log_real_trade", side_effect=sqlite3.OperationalError("injected DB failure")):
            ok = main.reconcile_and_settle_engine_transition_orders(ex, market, state)

        self.assertFalse(ok)
        self.assertIn("bid-db-fail", state.get("bear_limit_buys", {}))
        self.assertEqual("wait", state["bear_limit_buys"]["bid-db-fail"]["state"])
        self.assertAlmostEqual(0.0, state["bear_limit_buys"]["bid-db-fail"]["executed_volume"])
        self.assertAlmostEqual(0.0, state["bear_limit_buys"]["bid-db-fail"]["executed_funds"])
        self.assertAlmostEqual(0.1, state["bot_quantity"])
        self.assertEqual(1, len(state["tranches"]))
        with sqlite3.connect(self.db_path) as conn:
            cnt = conn.execute("SELECT COUNT(*) FROM trades WHERE order_uuid='bid-db-fail'").fetchone()[0]
            self.assertEqual(0, cnt)

    def test_bid_save_failure_restart_recovers_ledger_without_duplicate_trade(self):
        """AG02-R2: 매수 체결 DB commit 후 JSON 저장 실패·재시작 시, 중복 거래 행 없이 장부(수량/tranches)가 정상 복구."""
        market = "KRW-ETH"
        state = self._make_state(market, engine="LEGACY")
        state["bear_limit_buys"]["bid-save-fail"] = {
            "state": "wait", "units": 1, "squad_id": 1, "squad_stage": 2,
            "executed_volume": 0.0, "executed_funds": 0.0
        }

        ex = SquadFakeExchange(main.PROTECTED_BALANCES[market] + 0.15, 3_700_000.0)
        ex.orders["bid-save-fail"] = {
            "uuid": "bid-save-fail", "side": "bid", "state": "done",
            "executed_volume": "0.05", "trades": [{"funds": "185000", "volume": "0.05", "price": "3700000"}]
        }

        # 1차 실행: DB 커밋 성공, 하지만 save_strategy_state 실패
        with patch.object(main, "save_strategy_state", return_value=False):
            first_ok = main.reconcile_and_settle_engine_transition_orders(ex, market, state)
        self.assertFalse(first_ok)

        # 1차 실패 후 DB에는 정상적으로 1건이 기록되어 있음
        with sqlite3.connect(self.db_path) as conn:
            rows1 = conn.execute("SELECT COUNT(*), COALESCE(SUM(volume),0), COALESCE(SUM(cost_or_revenue),0) FROM trades WHERE side='bid'").fetchone()
            self.assertEqual(1, rows1[0])
            self.assertAlmostEqual(0.05, rows1[1])
            self.assertAlmostEqual(185_000.0, rows1[2])

        # 2차 실행: 프로세스 재시작 시 이전 저장 상태(즉, 저장이 실패했으므로 원래의 state)를 로드
        reloaded_state = self._make_state(market, engine="LEGACY")
        reloaded_state["bear_limit_buys"]["bid-save-fail"] = {
            "state": "wait", "units": 1, "squad_id": 1, "squad_stage": 2,
            "executed_volume": 0.0, "executed_funds": 0.0
        }

        with patch.object(main, "save_strategy_state", lambda m, s: True):
            second_ok = main.reconcile_and_settle_engine_transition_orders(ex, market, reloaded_state)
        self.assertTrue(second_ok)

        # 장부가 정상 가산되어 0.15가 되고 tranche가 추가됨
        self.assertAlmostEqual(0.15, reloaded_state["bot_quantity"])
        self.assertEqual(2, len(reloaded_state["tranches"]))
        self.assertEqual("done", reloaded_state["bear_limit_buys"]["bid-save-fail"]["state"])

        # DB에는 중복 기록 없이 여전히 1건만 존재해야 함
        with sqlite3.connect(self.db_path) as conn:
            rows2 = conn.execute("SELECT COUNT(*), COALESCE(SUM(volume),0), COALESCE(SUM(cost_or_revenue),0) FROM trades WHERE side='bid'").fetchone()
            self.assertEqual(1, rows2[0])
            self.assertAlmostEqual(0.05, rows2[1])
            self.assertAlmostEqual(185_000.0, rows2[2])

    def test_cancel_fill_bid_db_failure_blocks_transition(self):
        """AG02-R2: 취소 중 발생한 부분 매수 체결의 DB 기록 실패 시 transition 차단 및 False 반환."""
        market = "KRW-ETH"
        state = self._make_state(market, engine="LEGACY")
        state["bear_limit_buys"]["bid-late-fail"] = {
            "state": "wait", "units": 1, "squad_id": 1, "squad_stage": 2,
            "executed_volume": 0.0, "executed_funds": 0.0
        }

        ex = SquadFakeExchange(main.PROTECTED_BALANCES[market] + 0.13, 3_700_000.0)
        open_bid = {
            "uuid": "bid-late-fail", "side": "bid", "state": "wait", "price": "3700000",
            "volume": "0.05", "executed_volume": "0", "trades": []
        }
        ex.open_orders = [open_bid.copy()]
        ex.orders["bid-late-fail"] = open_bid

        def cancel_with_fill(identifier):
            ex.orders[identifier].update(
                state="cancel",
                executed_volume="0.03",
                trades=[{"funds": "111000", "volume": "0.03", "price": "3700000"}]
            )
            ex.open_orders = []
            ex.cancelled.append(identifier)
            return {"uuid": identifier}

        ex.cancel_order = cancel_with_fill

        with patch.object(main, "log_real_trade", side_effect=sqlite3.OperationalError("injected DB failure")):
            ok = main.reconcile_and_settle_engine_transition_orders(ex, market, state)

        self.assertFalse(ok)
        self.assertIn("bid-late-fail", state.get("bear_limit_buys", {}))
        self.assertEqual("wait", state["bear_limit_buys"]["bid-late-fail"]["state"])
        self.assertAlmostEqual(0.0, state["bear_limit_buys"]["bid-late-fail"]["executed_volume"])
        self.assertAlmostEqual(0.0, state["bear_limit_buys"]["bid-late-fail"]["executed_funds"])
        self.assertAlmostEqual(0.1, state["bot_quantity"])
        self.assertEqual(1, len(state["tranches"]))
        with sqlite3.connect(self.db_path) as conn:
            cnt = conn.execute("SELECT COUNT(*) FROM trades WHERE order_uuid='bid-late-fail'").fetchone()[0]
            self.assertEqual(0, cnt)

    def test_ask_db_query_failure_blocks_transition_and_normal_retry_recovers_without_duplicate(self):
        """AG02-R2: 매도 정산 시 기존 DB 기록 + 오래된 JSON 상태에서 DB 조회 실패 시 전환 차단 및 장부/UUID 보존, 정상 재시도 시 중복 없이 복구."""
        market = "KRW-ETH"
        state = self._make_state(market, engine="LEGACY")
        state["basket_limit_ask"] = {"uuid": "ask-query-fail", "price": 4_050_000.0, "volume": 0.1}

        # 기존 DB 기록: 이미 0.1 ETH / 405,000원이 DB에 기록되어 있음
        db_logger.log_real_trade(
            market=market, ticker="ETH", side="ask",
            action="BASKET_TAKE_PROFIT_LIMIT", price=4_050_000.0, volume=0.1,
            cost_or_revenue=405_000.0, order_uuid="ask-query-fail"
        )

        ex = SquadFakeExchange(main.PROTECTED_BALANCES[market], 4_050_000.0)
        ex.orders["ask-query-fail"] = {
            "uuid": "ask-query-fail", "side": "ask", "state": "done",
            "executed_volume": "0.1", "price": "4050000",
            "trades": [{"funds": "405000", "volume": "0.1", "price": "4050000"}]
        }

        # 1. DB 조회 실패 주입 -> False 반환, 장부와 basket_limit_ask 체크포인트 원형 보존
        with patch.object(main.sqlite3, "connect", side_effect=sqlite3.OperationalError("injected DB query failure")):
            first_ok = main.reconcile_and_settle_engine_transition_orders(ex, market, state)

        self.assertFalse(first_ok)
        self.assertIsNotNone(state.get("basket_limit_ask"))
        self.assertEqual("ask-query-fail", state["basket_limit_ask"]["uuid"])
        self.assertAlmostEqual(0.1, state["bot_quantity"])
        self.assertEqual(1, len(state["tranches"]))

        # 2. 정상 재시도 -> True 반환, 중복 DB 기록 없이 장부(수량 0.0, tranches 비움, basket ask 제거) 정확 복구
        with patch.object(main, "save_strategy_state", lambda m, s: True):
            second_ok = main.reconcile_and_settle_engine_transition_orders(ex, market, state)

        self.assertTrue(second_ok)
        self.assertAlmostEqual(0.0, state["bot_quantity"])
        self.assertEqual([], state["tranches"])
        self.assertIsNone(state.get("basket_limit_ask"))

        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute("SELECT COUNT(*), COALESCE(SUM(volume),0), COALESCE(SUM(cost_or_revenue),0) FROM trades WHERE order_uuid='ask-query-fail'").fetchone()
            self.assertEqual(1, rows[0])
            self.assertAlmostEqual(0.1, rows[1])
            self.assertAlmostEqual(405_000.0, rows[2])

    def test_bid_db_query_failure_blocks_transition_and_normal_retry_recovers_without_duplicate(self):
        """AG02-R2: 매수 정산 시 기존 DB 기록 + 오래된 JSON 상태에서 DB 조회 실패 시 전환 차단 및 장부/UUID 보존, 정상 재시도 시 중복 없이 복구."""
        market = "KRW-ETH"
        state = self._make_state(market, engine="LEGACY")
        state["bear_limit_buys"]["bid-query-fail"] = {
            "state": "wait", "units": 1, "squad_id": 1, "squad_stage": 2,
            "executed_volume": 0.0, "executed_funds": 0.0
        }

        # 기존 DB 기록: 이미 0.05 ETH / 185,000원이 DB에 기록되어 있음
        db_logger.log_real_trade(
            market=market, ticker="ETH", side="bid",
            action="MARTINGALE_LIMIT_BUY_SQUAD_1_STAGE_2", price=3_700_000.0, volume=0.05,
            cost_or_revenue=185_000.0, order_uuid="bid-query-fail"
        )

        ex = SquadFakeExchange(main.PROTECTED_BALANCES[market] + 0.15, 3_700_000.0)
        ex.orders["bid-query-fail"] = {
            "uuid": "bid-query-fail", "side": "bid", "state": "done",
            "executed_volume": "0.05", "trades": [{"funds": "185000", "volume": "0.05", "price": "3700000"}]
        }

        # 1. DB 조회 실패 주입 -> False 반환, 장부와 UUID 체크포인트 원형 보존
        with patch.object(main.sqlite3, "connect", side_effect=sqlite3.OperationalError("injected DB query failure")):
            first_ok = main.reconcile_and_settle_engine_transition_orders(ex, market, state)

        self.assertFalse(first_ok)
        self.assertEqual("wait", state["bear_limit_buys"]["bid-query-fail"]["state"])
        self.assertAlmostEqual(0.0, state["bear_limit_buys"]["bid-query-fail"]["executed_volume"])
        self.assertAlmostEqual(0.1, state["bot_quantity"])
        self.assertEqual(1, len(state["tranches"]))

        # 2. 정상 재시도 -> True 반환, 중복 DB 기록 없이 장부(수량 0.15, tranche 2개, state=done) 정확 복구
        with patch.object(main, "save_strategy_state", lambda m, s: True):
            second_ok = main.reconcile_and_settle_engine_transition_orders(ex, market, state)

        self.assertTrue(second_ok)
        self.assertEqual("done", state["bear_limit_buys"]["bid-query-fail"]["state"])
        self.assertAlmostEqual(0.05, state["bear_limit_buys"]["bid-query-fail"]["executed_volume"])
        self.assertAlmostEqual(0.15, state["bot_quantity"])
        self.assertEqual(2, len(state["tranches"]))

        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute("SELECT COUNT(*), COALESCE(SUM(volume),0), COALESCE(SUM(cost_or_revenue),0) FROM trades WHERE order_uuid='bid-query-fail'").fetchone()
            self.assertEqual(1, rows[0])
            self.assertAlmostEqual(0.05, rows[1])
            self.assertAlmostEqual(185_000.0, rows[2])

    def test_cancel_fill_bid_db_query_failure_blocks_transition_and_normal_retry_recovers_without_duplicate(self):
        """AG02-R2: 취소 중 부분체결 시 기존 DB 기록 + 오래된 JSON 상태에서 DB 조회 실패 시 전환 차단 및 장부/UUID 보존, 정상 재시도 시 중복 없이 복구."""
        market = "KRW-ETH"
        state = self._make_state(market, engine="LEGACY")
        state["bear_limit_buys"]["bid-cancel-query-fail"] = {
            "state": "wait", "units": 1, "squad_id": 1, "squad_stage": 2,
            "executed_volume": 0.0, "executed_funds": 0.0
        }

        # 기존 DB 기록: 이미 0.03 ETH / 111,000원이 DB에 기록되어 있음
        db_logger.log_real_trade(
            market=market, ticker="ETH", side="bid",
            action="MARTINGALE_LIMIT_BUY_SQUAD_1_STAGE_2", price=3_700_000.0, volume=0.03,
            cost_or_revenue=111_000.0, order_uuid="bid-cancel-query-fail"
        )

        ex = SquadFakeExchange(main.PROTECTED_BALANCES[market] + 0.13, 3_700_000.0)
        open_bid = {
            "uuid": "bid-cancel-query-fail", "side": "bid", "state": "wait", "price": "3700000",
            "volume": "0.05", "executed_volume": "0", "trades": []
        }
        ex.open_orders = [open_bid.copy()]
        ex.orders["bid-cancel-query-fail"] = open_bid

        def cancel_with_fill(identifier):
            ex.orders[identifier].update(
                state="cancel",
                executed_volume="0.03",
                trades=[{"funds": "111000", "volume": "0.03", "price": "3700000"}]
            )
            ex.open_orders = []
            ex.cancelled.append(identifier)
            return {"uuid": identifier}

        ex.cancel_order = cancel_with_fill

        # 1. DB 조회 실패 주입 -> False 반환, 장부와 UUID 체크포인트 원형 보존
        with patch.object(main.sqlite3, "connect", side_effect=sqlite3.OperationalError("injected DB query failure")):
            first_ok = main.reconcile_and_settle_engine_transition_orders(ex, market, state)

        self.assertFalse(first_ok)
        self.assertEqual("wait", state["bear_limit_buys"]["bid-cancel-query-fail"]["state"])
        self.assertAlmostEqual(0.0, state["bear_limit_buys"]["bid-cancel-query-fail"]["executed_volume"])
        self.assertAlmostEqual(0.1, state["bot_quantity"])
        self.assertEqual(1, len(state["tranches"]))

        # 2. 정상 재시도 -> True 반환, 중복 DB 기록 없이 장부(수량 0.13, tranche 2개, state=cancel) 정확 복구
        with patch.object(main, "save_strategy_state", lambda m, s: True):
            second_ok = main.reconcile_and_settle_engine_transition_orders(ex, market, state)

        self.assertTrue(second_ok)
        self.assertEqual("cancel", state["bear_limit_buys"]["bid-cancel-query-fail"]["state"])
        self.assertAlmostEqual(0.03, state["bear_limit_buys"]["bid-cancel-query-fail"]["executed_volume"])
        self.assertAlmostEqual(0.13, state["bot_quantity"])
        self.assertEqual(2, len(state["tranches"]))

        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute("SELECT COUNT(*), COALESCE(SUM(volume),0), COALESCE(SUM(cost_or_revenue),0) FROM trades WHERE order_uuid='bid-cancel-query-fail'").fetchone()
            self.assertEqual(1, rows[0])
            self.assertAlmostEqual(0.03, rows[1])
            self.assertAlmostEqual(111_000.0, rows[2])

    def test_engine_marker_save_failure_legacy_to_adaptive_halts_and_blocks_new_orders(self):
        """AG02-R3: LEGACY -> ADAPTIVE 전환 시 마커 저장 실패 시 전환 알림 발송 및 신규 호가 등록을 금지하고, 이전 엔진 유지 및 거래 중단(halt)."""
        market = "KRW-ETH"
        state = self._make_state(market, engine="LEGACY")
        state["basket_limit_ask"] = {"uuid": "old-ask", "price": 4_050_000.0, "volume": 0.1}
        state["bear_limit_buys"]["old-bid"] = {
            "state": "wait", "units": 1, "squad_id": 1, "squad_stage": 2,
            "executed_volume": 0.0, "executed_funds": 0.0
        }

        ex = SquadFakeExchange(main.PROTECTED_BALANCES[market] + 0.1, 3_700_000.0)
        ex.orders["old-ask"] = {"uuid": "old-ask", "side": "ask", "state": "cancel", "executed_volume": "0.0", "trades": []}
        ex.orders["old-bid"] = {"uuid": "old-bid", "side": "bid", "state": "cancel", "executed_volume": "0.0", "trades": []}
        ex.open_orders = [
            {"uuid": "old-ask", "side": "ask", "state": "wait", "price": "4050000", "volume": "0.1", "executed_volume": "0"},
            {"uuid": "old-bid", "side": "bid", "state": "wait", "price": "3700000", "volume": "0.05", "executed_volume": "0"}
        ]

        failed_marker = False
        alerts = []

        def fail_only_marker(market_name: str, current_state) -> bool:
            nonlocal failed_marker
            if current_state.get("strategy_engine") == "ADAPTIVE_GRID_C_E" and not failed_marker:
                failed_marker = True
                return False
            return True

        def bounded_sleep(seconds: float) -> None:
            if seconds >= 2:
                raise EndTick()

        def price_once(market_name: str) -> float:
            return 3_700_000.0

        with patch.multiple(main,
            acquire_market_process_lock=lambda m: True,
            init_db=lambda: None,
            get_upbit_client=lambda: ex,
            load_strategy_state=lambda m: state,
            save_strategy_state=fail_only_marker,
            load_trailing_exit_recovery=lambda m: None,
            check_krw_balance_alert=lambda *args, **kwargs: 1_000_000,
            send_telegram_alert=alerts.append,
            safe_get_current_price=price_once,
            get_hybrid_regime_and_signals=lambda m: {"is_bull": False, "signal": "HOLD", "ma5": 3_700_000},
            is_adaptive_grid_enabled=lambda m: True,
            log_real_trade=lambda **kwargs: None), \
            patch.object(main.time, "sleep", side_effect=bounded_sleep):
            try:
                main.run_trading_strategy(market)
            except EndTick:
                pass

        # 1. 마커 저장 실패 주입 확인
        self.assertTrue(failed_marker)
        # 2. 거래 안전 중단 (trading_halted == True)
        self.assertTrue(state.get("trading_halted", False))
        self.assertIn("전략 엔진 전환 마커 저장 실패", state.get("halt_reason", ""))
        # 3. 메모리 상 엔진이 미확정된 새 엔진으로 전진하지 않고 이전 엔진(LEGACY)으로 복귀/유지
        self.assertEqual("LEGACY", state.get("strategy_engine"))
        # 4. 신규 주문이 전혀 제출되지 않음 (신규 ask 0건, 신규 bid 0건)
        self.assertEqual(0, len(ex.limit_sells))
        self.assertEqual(0, len(ex.limit_buys))
        # 5. 전환 성공 알림은 발송되지 않고, 중단 알림만 발송됨
        self.assertFalse(any("기존 주문 정산 및 취소 완료 후 새 호가로 자동 재등록합니다" in a for a in alerts))
        self.assertTrue(any("거래 중단" in a for a in alerts))

    def test_engine_marker_save_failure_adaptive_to_legacy_halts_and_blocks_new_orders(self):
        """AG02-R3: ADAPTIVE -> LEGACY 롤백 시 마커 저장 실패 시 전환 알림 발송 및 신규 호가 등록을 금지하고, 이전 엔진 유지 및 거래 중단(halt)."""
        market = "KRW-ETH"
        state = self._make_state(market, engine="ADAPTIVE_GRID_C_E")
        state["basket_limit_ask"] = {"uuid": "old-ag-ask", "price": 4_050_000.0, "volume": 0.1}
        state["bear_limit_buys"]["old-ag-bid"] = {
            "state": "wait", "units": 1, "squad_id": 1, "squad_stage": 2,
            "executed_volume": 0.0, "executed_funds": 0.0
        }

        ex = SquadFakeExchange(main.PROTECTED_BALANCES[market] + 0.1, 3_700_000.0)
        ex.orders["old-ag-ask"] = {"uuid": "old-ag-ask", "side": "ask", "state": "cancel", "executed_volume": "0.0", "trades": []}
        ex.orders["old-ag-bid"] = {"uuid": "old-ag-bid", "side": "bid", "state": "cancel", "executed_volume": "0.0", "trades": []}
        ex.open_orders = [
            {"uuid": "old-ag-ask", "side": "ask", "state": "wait", "price": "4050000", "volume": "0.1", "executed_volume": "0"},
            {"uuid": "old-ag-bid", "side": "bid", "state": "wait", "price": "3700000", "volume": "0.05", "executed_volume": "0"}
        ]

        failed_marker = False
        alerts = []

        def fail_only_marker(market_name: str, current_state) -> bool:
            nonlocal failed_marker
            if current_state.get("strategy_engine") == "LEGACY" and not failed_marker:
                failed_marker = True
                return False
            return True

        def bounded_sleep(seconds: float) -> None:
            if seconds >= 2:
                raise EndTick()

        def price_once(market_name: str) -> float:
            return 3_700_000.0

        with patch.multiple(main,
            acquire_market_process_lock=lambda m: True,
            init_db=lambda: None,
            get_upbit_client=lambda: ex,
            load_strategy_state=lambda m: state,
            save_strategy_state=fail_only_marker,
            load_trailing_exit_recovery=lambda m: None,
            check_krw_balance_alert=lambda *args, **kwargs: 1_000_000,
            send_telegram_alert=alerts.append,
            safe_get_current_price=price_once,
            get_hybrid_regime_and_signals=lambda m: {"is_bull": False, "signal": "HOLD", "ma5": 3_700_000},
            is_adaptive_grid_enabled=lambda m: False,
            log_real_trade=lambda **kwargs: None), \
            patch.object(main.time, "sleep", side_effect=bounded_sleep):
            try:
                main.run_trading_strategy(market)
            except EndTick:
                pass

        # 1. 마커 저장 실패 주입 확인
        self.assertTrue(failed_marker)
        # 2. 거래 안전 중단 (trading_halted == True)
        self.assertTrue(state.get("trading_halted", False))
        self.assertIn("전략 엔진 전환 마커 저장 실패", state.get("halt_reason", ""))
        # 3. 메모리 상 엔진이 미확정된 새 엔진으로 전진하지 않고 이전 엔진(ADAPTIVE_GRID_C_E)으로 복귀/유지
        self.assertEqual("ADAPTIVE_GRID_C_E", state.get("strategy_engine"))
        # 4. 신규 주문이 전혀 제출되지 않음 (신규 ask 0건, 신규 bid 0건)
        self.assertEqual(0, len(ex.limit_sells))
        self.assertEqual(0, len(ex.limit_buys))
        # 5. 전환 성공 알림은 발송되지 않고, 중단 알림만 발송됨
        self.assertFalse(any("기존 주문 정산 및 취소 완료 후 새 호가로 자동 재등록합니다" in a for a in alerts))
        self.assertTrue(any("거래 중단" in a for a in alerts))

    def test_engine_marker_and_halt_save_failure_does_not_resume_from_stale_disk_json(self):
        """AG02-R3: 마커 저장 및 halt 저장까지 모두 실패한 경우, 디스크의 이전 un-halted 옛 JSON을 읽어 자동 재개되지 않고 계속 중단 유지."""
        market = "KRW-ETH"
        stale_disk_state = self._make_state(market, engine="LEGACY")
        stale_disk_state["trading_halted"] = False

        state = copy.deepcopy(stale_disk_state)
        ex = SquadFakeExchange(main.PROTECTED_BALANCES[market] + 0.1, 3_700_000.0)

        marker_called = False

        def fail_marker_and_halt(market_name: str, current_state) -> bool:
            nonlocal marker_called
            if current_state.get("strategy_engine") == "ADAPTIVE_GRID_C_E" or current_state.get("trading_halted"):
                marker_called = True
                return False
            return True

        sleep_count = 0

        def bounded_sleep(seconds: float) -> None:
            nonlocal sleep_count
            sleep_count += 1
            if sleep_count >= 2:
                raise EndTick()

        load_count = 0

        def mock_load_state(m):
            nonlocal load_count
            load_count += 1
            if load_count == 1:
                return state
            return copy.deepcopy(stale_disk_state)

        with patch.multiple(main,
            acquire_market_process_lock=lambda m: True,
            init_db=lambda: None,
            get_upbit_client=lambda: ex,
            load_strategy_state=mock_load_state,
            save_strategy_state=fail_marker_and_halt,
            load_trailing_exit_recovery=lambda m: None,
            check_krw_balance_alert=lambda *args, **kwargs: 1_000_000,
            send_telegram_alert=lambda text: None,
            safe_get_current_price=lambda m: 3_700_000.0,
            get_hybrid_regime_and_signals=lambda m: {"is_bull": False, "signal": "HOLD", "ma5": 3_700_000},
            is_adaptive_grid_enabled=lambda m: True,
            log_real_trade=lambda **kwargs: None), \
            patch.object(main.time, "sleep", side_effect=bounded_sleep):
            try:
                main.run_trading_strategy(market)
            except EndTick:
                pass

        # 1. 마커/halt 저장 실패 주입 확인
        self.assertTrue(marker_called)
        # 2. 거래 중단 유지 (_halt_persisted == False이므로 stale_disk_state로 자동 재개되지 않고 load_strategy_state 호출도 스킵)
        self.assertTrue(state.get("trading_halted", False))
        self.assertEqual(1, load_count)
        # 3. 신규 주문 전혀 제출되지 않음 (신규 ask 0건, 신규 bid 0건)
        self.assertEqual(0, len(ex.limit_sells))
        self.assertEqual(0, len(ex.limit_buys))

    def test_engine_marker_save_success_transitions_engine_and_sends_alert(self):
        """AG02-R3 정상 경로: 마커 저장이 성공하면 strategy_engine이 갱신되고 성공 알림이 발송되며 정상 진행."""
        market = "KRW-ETH"
        state = self._make_state(market, engine="LEGACY")
        ex = SquadFakeExchange(main.PROTECTED_BALANCES[market] + 0.1, 3_700_000.0)
        alerts = []

        def bounded_sleep(seconds: float) -> None:
            raise EndTick()

        with patch.multiple(main,
            acquire_market_process_lock=lambda m: True,
            init_db=lambda: None,
            get_upbit_client=lambda: ex,
            load_strategy_state=lambda m: state,
            save_strategy_state=lambda m, s: True,
            load_trailing_exit_recovery=lambda m: None,
            check_krw_balance_alert=lambda *args, **kwargs: 1_000_000,
            send_telegram_alert=alerts.append,
            safe_get_current_price=lambda m: 3_700_000.0,
            get_hybrid_regime_and_signals=lambda m: {"is_bull": False, "signal": "HOLD", "ma5": 3_700_000},
            is_adaptive_grid_enabled=lambda m: True,
            log_real_trade=lambda **kwargs: None), \
            patch.object(main.time, "sleep", side_effect=bounded_sleep):
            try:
                main.run_trading_strategy(market)
            except EndTick:
                pass

        # 1. 정상 전환 완료
        self.assertEqual("ADAPTIVE_GRID_C_E", state.get("strategy_engine"))
        self.assertFalse(state.get("trading_halted", False))
        # 2. 전환 성공 알림 발송 확인
        self.assertTrue(any("전략 엔진 전환" in a and "ADAPTIVE_GRID_C_E" in a for a in alerts))


if __name__ == "__main__":
    unittest.main()
