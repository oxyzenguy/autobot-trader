#!/usr/bin/env python3
"""
Unit tests for G60 Exit-Only Mode, +1% Basket Rebound Liquidation, and Dynamic Budget Reallocation
"""
import os
import sys
import json
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from utils.pocket_grid_core import PocketGridRunner


class TestPocketGridExitOnly(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.state_file = os.path.join(self.temp_dir.name, "pocket_grid_state_KRW_ETH.json")
        self.db_file = os.path.join(self.temp_dir.name, "pocket_trade_history.db")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_exit_only_suppresses_level_1_entry(self):
        """exit_only=True인 경우 포지션이 0개여도 신규 1단계 매수 진입을 전면 차단해야 함"""
        fake_client = Mock()
        fake_client.get_balance.return_value = 500000.0

        with patch("utils.pocket_grid_core.UpbitPocketClient", return_value=fake_client):
            runner = PocketGridRunner(
                market="KRW-ETH",
                exit_only=True,
                basket_exit_pct=0.01
            )
            runner.state_file = self.state_file
            runner.db_file = self.db_file
            runner.init_db()

            state = runner.load_state()
            self.assertEqual(len(state["active_grids"]), 0)

            # Process tick at 3,000,000 KRW
            res_state = runner.process_tick(state, 3000000.0, "2026-10-31 10:00:00")
            # Must remain 0 active grids, no live buy attempted
            self.assertEqual(len(res_state["active_grids"]), 0)
            fake_client.buy_market_order_and_wait.assert_not_called()

    def test_exit_only_suppresses_dip_buys(self):
        """exit_only=True인 경우 기존 포지션이 있고 가격이 급락해도 추가 물타기 매수를 전면 차단해야 함"""
        fake_client = Mock()
        fake_client.get_balance.return_value = 500000.0

        with patch("utils.pocket_grid_core.UpbitPocketClient", return_value=fake_client):
            runner = PocketGridRunner(
                market="KRW-ETH",
                exit_only=True,
                basket_exit_pct=0.01
            )
            runner.state_file = self.state_file
            runner.db_file = self.db_file
            runner.init_db()

            state = runner.load_state()
            state["base_price"] = 3200000.0
            state["active_grids"] = [
                {
                    "lvl": 1,
                    "squad": 1,
                    "buy_p": 3200000.0,
                    "vol": 0.003125,
                    "cost": 10000.0,
                    "fee": 5.0,
                    "tp_p": 3222400.0,
                    "order_uuid": "uuid-lvl-1",
                    "buy_time": "2026-10-31 09:00:00"
                }
            ]
            runner.save_state(state)

            # Price drops dramatically to 2,500,000 KRW (deep dip)
            res_state = runner.process_tick(state, 2500000.0, "2026-10-31 10:00:00")
            # Active grids must still be exactly 1, no dip buy
            self.assertEqual(len(res_state["active_grids"]), 1)
            fake_client.buy_market_order_and_wait.assert_not_called()

    def test_execute_live_buy_raises_under_exit_only(self):
        """exit_only=True일 때 직접 execute_live_buy가 호출되어도 거부하고 예외를 발생시켜야 함"""
        fake_client = Mock()
        fake_client.get_balance.return_value = 500000.0

        with patch("utils.pocket_grid_core.UpbitPocketClient", return_value=fake_client):
            runner = PocketGridRunner(
                market="KRW-ETH",
                exit_only=True,
                basket_exit_pct=0.01
            )
            runner.state_file = self.state_file
            runner.db_file = self.db_file
            runner.init_db()

            with self.assertRaises(RuntimeError) as ctx:
                runner.execute_live_buy(1, 1, 3000000.0, "2026-10-31 10:00:00")
            self.assertIn("exit_only", str(ctx.exception))

    def test_basket_exit_triggers_single_liquidation_and_bypasses_individual_tp(self):
        """basket_exit_pct 모드에서는 개별 및 스쿼드 TP를 우회하고 전체 평단 +1% 도달 시 전량 일괄 매도해야 함"""
        fake_client = Mock()
        fake_client.get_balance.return_value = 500000.0

        with patch("utils.pocket_grid_core.UpbitPocketClient", return_value=fake_client):
            runner = PocketGridRunner(
                market="KRW-ETH",
                exit_only=True,
                basket_exit_pct=0.01
            )
            runner.state_file = self.state_file
            runner.db_file = self.db_file
            runner.init_db()

            # 3 grids held:
            # Lvl 1: buy 3,200,000, vol 0.003125, cost 10,000, tp_p 3,222,400 (+0.7%)
            # Lvl 2: buy 3,100,000, vol 0.0032258, cost 10,000, tp_p 3,121,700 (+0.7%)
            # Lvl 3: buy 3,000,000, vol 0.0033333, cost 10,000, tp_p 3,021,000 (+0.7%)
            # Total vol = 0.0096841, Total cost = 30,000
            # VWAP = 30,000 / 0.0096841 = 3,097,861.44
            # Target +1% = 3,097,861.44 * 1.01 = 3,128,840.06
            state = runner.load_state()
            state["active_grids"] = [
                {"lvl": 1, "squad": 1, "buy_p": 3200000.0, "vol": 0.003125, "cost": 10000.0, "fee": 5.0, "tp_p": 3222400.0, "order_uuid": "u1"},
                {"lvl": 2, "squad": 1, "buy_p": 3100000.0, "vol": 0.0032258, "cost": 10000.0, "fee": 5.0, "tp_p": 3121700.0, "order_uuid": "u2"},
                {"lvl": 3, "squad": 1, "buy_p": 3000000.0, "vol": 0.0033333, "cost": 10000.0, "fee": 5.0, "tp_p": 3021000.0, "order_uuid": "u3"}
            ]
            runner.save_state(state)

            # Tick 1: Price is 3,050,000.
            # Notice 3,050,000 is ABOVE Lvl 3's individual tp_p (3,021,000).
            # But basket target is 3,128,840.
            # Basket exit MUST NOT sell Lvl 3 individually!
            res_state = runner.process_tick(state, 3050000.0, "2026-10-31 10:00:00")
            self.assertEqual(len(res_state["active_grids"]), 3)
            fake_client.sell_market_order_and_wait.assert_not_called()

            # Tick 2: Price reaches 3,130,000 (>= 3,128,840 basket target)
            total_vol = 0.003125 + 0.0032258 + 0.0033333
            fake_client.sell_market_order_and_wait.return_value = {
                "status": "FILLED",
                "uuid": "basket-sell-uuid",
                "price": 3130000.0,
                "volume": total_vol,
                "funds": 30311.23,
                "fee": 15.15
            }
            with patch.object(runner, "send_telegram") as mock_tg:
                res_state = runner.process_tick(state, 3130000.0, "2026-10-31 10:00:05")
                fake_client.sell_market_order_and_wait.assert_called_once()
                self.assertEqual(len(res_state["active_grids"]), 0)
                self.assertTrue(res_state["basket_exit_settled"])
                self.assertEqual(res_state["last_basket_revenue"], 30311.23)
                mock_tg.assert_called()

    def test_dynamic_budget_expansion_for_sol_and_xrp(self):
        """ETH 청산 전에는 300,000원(30단), ETH 청산 후에는 406,000원(40단)으로 SOL/XRP 예산이 자동 확장되어야 함"""
        # 1. Before ETH retirement: state file has 21 grids active
        eth_state_path = os.path.join(BASE_DIR, "pocket_grid_state_KRW_ETH.json")
        fake_eth_state = {
            "market": "KRW-ETH",
            "exit_only": True,
            "active_grids": [{"lvl": i, "vol": 0.003, "cost": 10000.0} for i in range(1, 22)],
            "basket_exit_settled": False
        }

        sol_runner = PocketGridRunner(market="KRW-SOL", grid_budget=300000.0, max_levels=30, auto_reallocate_eth=True)
        sol_runner.db_file = self.db_file
        sol_runner.init_db()

        with patch("builtins.open", unittest.mock.mock_open(read_data=json.dumps(fake_eth_state))):
            with patch("os.path.exists", return_value=True):
                eff_budget, eff_levels = sol_runner.get_effective_budget_and_levels()
                # Before retirement: 300,000 KRW, 30 levels
                self.assertEqual(eff_budget, 300000.0)
                self.assertEqual(eff_levels, 30)

        # 2. After ETH retirement: active_grids is empty, basket_exit_settled is True, revenue 212,000
        retired_eth_state = {
            "market": "KRW-ETH",
            "exit_only": True,
            "active_grids": [],
            "basket_exit_settled": True,
            "last_basket_revenue": 212000.0
        }
        with patch("builtins.open", unittest.mock.mock_open(read_data=json.dumps(retired_eth_state))):
            with patch("os.path.exists", return_value=True):
                eff_budget, eff_levels = sol_runner.get_effective_budget_and_levels()
                # +106,000 KRW added (half of 212,000)
                self.assertEqual(eff_budget, 406000.0)
                self.assertEqual(eff_levels, 40)


if __name__ == "__main__":
    unittest.main()
