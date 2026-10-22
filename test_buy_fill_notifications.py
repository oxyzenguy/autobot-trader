"""Fake-exchange notifications must distinguish real fills from ladder repair."""
from __future__ import annotations

from contextlib import redirect_stdout
import io
import unittest
from unittest.mock import patch
import main
from test_p1_squad_exit import SquadFakeExchange, EndTick


class TestBuyFillNotifications(unittest.TestCase):
    def state(self, market):
        return {"market": market, "current_regime": "BEAR", "strategy_engine": "LEGACY",
                "initial_entry_done": True, "bot_quantity": .1, "bot_avg_price": 100000.,
                "bear_buy_rebalance_pending": True, "bear_limit_buys": {},
                "tranches": [{"tranche_id": 1, "step": 1, "squad_id": 1, "squad_stage": 1,
                              "units": 1, "volume": .1, "buy_price": 100000.}]}

    def run_tick(self, exchange, state):
        queries = 0
        def price(market):
            nonlocal queries
            queries += 1
            if queries > 1:
                raise EndTick()
            return 80000.
        output = io.StringIO()
        with patch.multiple(main, acquire_market_process_lock=lambda market: object(),
                            init_db=lambda: None, get_upbit_client=lambda: exchange,
                            load_strategy_state=lambda market: state,
                            save_strategy_state=lambda market, state: True,
                            load_trailing_exit_recovery=lambda market: None,
                            safe_get_current_price=price,
                            get_market_strategy=lambda market: "HYBRID",
                            is_adaptive_grid_enabled=lambda market: False,
                            check_krw_balance_alert=lambda *args, **kwargs: 1000000.,
                            get_hybrid_regime_and_signals=lambda market: {"is_bull": False, "signal": "MARTINGALE_BUY_DIP", "ma5": 80000., "candles_valid": True, "is_valid": True},
                            _get_db_cumulative_trades=lambda uuid, side, market: (0., 0.),
                            log_real_trade=lambda **kwargs: None), \
                patch.object(main.time, "sleep", lambda seconds: None), \
                patch.object(main, "send_telegram_alert") as alerts, \
                patch("requests.sessions.Session.request", side_effect=AssertionError("network forbidden")), \
                redirect_stdout(output):
            # A real single daemon tick is the boundary, not a notification helper.
            try:
                main.run_trading_strategy(exchange.market)
            except EndTick:
                pass
        self.assertNotIn("[CRITICAL] 메인 루프 에러", output.getvalue())
        return [call for call in alerts.call_args_list if "[매수 체결]" in call.args[0]]

    def test_eth_and_sol_rebalance_without_fill_sends_no_buy_alert(self):
        # Given: existing positions with one basket and no pending buys after restart.
        for market in ("KRW-ETH", "KRW-SOL"):
            with self.subTest(market=market):
                state = self.state(market)
                exchange = SquadFakeExchange(.1 + main.PROTECTED_BALANCES[market], 80000., market)
                order = {"uuid": "basket-old", "side": "ask", "price": "100500", "volume": ".1", "state": "wait", "executed_volume": "0", "trades": []}
                exchange.orders[order["uuid"]] = order.copy()
                exchange.open_orders = [order.copy()]
                state["basket_limit_ask"] = {"uuid": order["uuid"], "price": 100500., "volume": .1}
                # When: the production loop repairs the ladder.
                alerts = self.run_tick(exchange, state)
                # Then: accepted resting buys are not reported as executions.
                self.assertGreater(len(exchange.limit_buys), 0)
                self.assertEqual([], alerts)
                self.assertEqual(.1, state["bot_quantity"])

    def test_existing_position_basket_recovery_is_not_new_entry(self):
        # Given: an owned position whose basket is missing.
        state = self.state("KRW-ETH")
        exchange = SquadFakeExchange(.1 + main.PROTECTED_BALANCES["KRW-ETH"], 80000.)
        # When: Case3 restores the basket without a market buy.
        alerts = self.run_tick(exchange, state)
        # Then: no fictitious initial-entry notification is sent.
        self.assertEqual([], exchange.market_buys)
        self.assertEqual([], alerts)

    def test_confirmed_market_entry_still_notifies_with_uuid(self):
        # Given: an empty position and a valid initial-entry signal.
        state = self.state("KRW-ETH")
        state.update(bot_quantity=0., bot_avg_price=0., tranches=[], initial_entry_done=False)
        exchange = SquadFakeExchange(main.PROTECTED_BALANCES["KRW-ETH"], 80000.)
        # When: the real loop observes the market buy's confirmed execution.
        alerts = self.run_tick(exchange, state)
        # Then: a true entry remains observable, not globally muted.
        self.assertEqual(1, len(exchange.market_buys))
        self.assertEqual(1, len(alerts))
        self.assertIn("mkt-buy-1", alerts[0].args[0])

    def test_loop_confirmed_limit_fill_notifies_once_without_rebalance_duplicate(self):
        # Given: a confirmed fill awaiting credit plus an existing basket.
        state = self.state("KRW-ETH")
        state["bear_limit_buys"]["new-fill"] = {"units": 1, "squad_id": 1, "squad_stage": 2,
            "state": "wait", "executed_volume": 0., "executed_funds": 0.}
        state["basket_limit_ask"] = {"uuid": "basket-old", "price": 100500., "volume": .1}
        exchange = SquadFakeExchange(.13 + main.PROTECTED_BALANCES["KRW-ETH"], 80000.)
        exchange.orders["new-fill"] = {"uuid": "new-fill", "state": "done", "executed_volume": ".03", "trades": [{"funds": "2400"}]}
        order = {"uuid": "basket-old", "side": "ask", "price": "100500", "volume": ".1", "state": "wait", "executed_volume": "0", "trades": []}
        exchange.orders["basket-old"] = order.copy()
        exchange.open_orders = [order.copy()]
        # When: the full daemon tick credits the UUID and rebalances its orders.
        alerts = self.run_tick(exchange, state)
        # Then: only the genuine fill is announced, not the subsequent rebalance.
        self.assertEqual(1, len(alerts))
        self.assertIn("new-fill", alerts[0].args[0])

    def test_partial_fill_alert_once_after_successful_save(self):
        # Given: one tracked UUID with a newly confirmed partial fill.
        state = self.state("KRW-ETH")
        state["bear_limit_buys"]["partial"] = {"units": 1, "squad_id": 1, "squad_stage": 2,
            "state": "wait", "executed_volume": 0., "executed_funds": 0.}
        exchange = SquadFakeExchange(.13 + main.PROTECTED_BALANCES["KRW-ETH"], 80000.)
        exchange.orders["partial"] = {"uuid": "partial", "state": "wait", "executed_volume": ".03", "trades": [{"funds": "2400"}]}
        saved = []
        with patch.object(main, "_get_db_cumulative_trades", return_value=(0., 0.)), \
                patch.object(main, "log_real_trade"), \
                patch.object(main, "save_strategy_state", side_effect=lambda m, s: saved.append(s.copy()) or True), \
                patch.object(main, "send_telegram_alert") as alerts:
            # When: the same cumulative partial is reconciled twice.
            self.assertTrue(main.reconcile_bear_limit_buys(exchange, "KRW-ETH", state, notify=True))
            self.assertTrue(main.reconcile_bear_limit_buys(exchange, "KRW-ETH", state, notify=True))
        # Then: one notification refers to the real UUID after persistence.
        self.assertEqual(1, alerts.call_count)
        self.assertIn("partial", alerts.call_args[0][0])
        self.assertEqual(1, len(saved))

    def test_json_save_failure_and_db_replay_do_not_notify(self):
        # Given: either failed JSON persistence or a fill already present in the DB.
        for save_ok, db_totals in ((False, (0., 0.)), (True, (.03, 2400.))):
            state = self.state("KRW-ETH")
            state["bear_limit_buys"]["partial"] = {"units": 1, "squad_id": 1, "squad_stage": 2,
                "state": "wait", "executed_volume": 0., "executed_funds": 0.}
            exchange = SquadFakeExchange(.13, 80000.)
            exchange.orders["partial"] = {"uuid": "partial", "state": "done", "executed_volume": ".03", "trades": [{"funds": "2400"}]}
            with patch.object(main, "_get_db_cumulative_trades", return_value=db_totals), \
                    patch.object(main, "log_real_trade"), \
                    patch.object(main, "save_strategy_state", return_value=save_ok), \
                    patch.object(main, "send_telegram_alert") as alerts:
                # When: reconciliation restores or fails the checkpoint.
                main.reconcile_bear_limit_buys(exchange, "KRW-ETH", state, notify=True)
            # Then: neither case announces a new confirmed buy.
            alerts.assert_not_called()


if __name__ == "__main__":
    unittest.main()
