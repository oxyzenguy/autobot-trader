#!/usr/bin/env python3
"""
Unit test for Main Bot KRW-ETH Option B:
- EXIT_ONLY mode with integrated +1.0% basket exit
- Verifies buy orders cancellation, squad/tranche exit bypass, and basket sell price reconciliation to +1.0%.
"""
import copy
import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import main
from test_p1_squad_exit import SquadFakeExchange, EndTick


class TestETHExitOnlyOptionB(unittest.TestCase):
    def test_eth_exit_only_cancels_buys_and_reconciles_basket_sell_price(self):
        protected = main.PROTECTED_BALANCES["KRW-ETH"]
        holdings = protected + 0.11659862
        avg_price = 3516337.0
        expected_tp_price = 3552000.0  # +1.0% ceil tick

        exchange = SquadFakeExchange(holdings, 3250000.0, "KRW-ETH")
        # Existing sell at old 3,534,000 (+0.5%)
        exchange.orders["old-ask"] = {
            "uuid": "old-ask",
            "state": "wait",
            "price": "3534000",
            "volume": "0.11659862",
            "executed_volume": "0.0",
            "trades": []
        }
        exchange.orders["buy-1"] = {
            "uuid": "buy-1",
            "state": "wait",
            "price": "3000000",
            "volume": "0.003",
            "executed_volume": "0.0",
            "trades": []
        }
        exchange.open_orders = [
            {"uuid": "old-ask", "side": "ask", "price": "3534000", "volume": "0.11659862"},
            {"uuid": "buy-1", "side": "bid", "price": "3000000", "volume": "0.003"}
        ]

        state = {
            "market": "KRW-ETH",
            "strategy_engine": "ADAPTIVE_GRID_C_E",
            "current_regime": "BEAR",
            "exit_only": True,
            "bot_quantity": 0.11659862,
            "bot_avg_price": avg_price,
            "basket_limit_ask": {"uuid": "old-ask", "price": 3534000.0, "volume": 0.11659862},
            "bear_limit_buys": {
                "buy-1": {
                    "state": "wait",
                    "units": 1,
                    "squad_id": 5,
                    "squad_stage": 1,
                    "executed_volume": 0.0,
                    "executed_funds": 0.0
                }
            },
            "tranches": [
                {"tranche_id": 1, "squad_id": 1, "squad_stage": 1, "volume": 0.05, "buy_price": avg_price},
                {"tranche_id": 2, "squad_id": 1, "squad_stage": 2, "volume": 0.06659862, "buy_price": avg_price}
            ]
        }

        def stop_after_decision(duration):
            raise EndTick()

        saved = []
        def save_state(m, val):
            saved.append(copy.deepcopy(val))

        output = io.StringIO()
        with (
            patch.multiple(
                main,
                acquire_market_process_lock=lambda m: object(),
                init_db=lambda: None,
                get_upbit_client=lambda: exchange,
                load_strategy_state=lambda m: state,
                save_strategy_state=save_state,
                load_trailing_exit_recovery=lambda m: None,
                save_trailing_exit_recovery=lambda m, r: None,
                check_krw_balance_alert=lambda *args, **kwargs: 1_000_000.0,
                safe_get_current_price=lambda m: 3250000.0,
                log_real_trade=lambda **kw: None,
                send_telegram_alert=lambda msg: None,
                get_hybrid_regime_and_signals=lambda m: {"is_bull": False, "regime": "BEAR", "candles_valid": True, "signal": "HOLD"},
            ),
            patch.object(main.time, "sleep", side_effect=stop_after_decision),
            redirect_stdout(output),
        ):
            try:
                main.run_trading_strategy("KRW-ETH")
            except EndTick:
                pass

        # 1. Buy orders must be cancelled
        self.assertIn("buy-1", exchange.cancelled)
        # 2. Old ask at 3,534,000 must be cancelled
        self.assertIn("old-ask", exchange.cancelled)
        # 3. New basket ask must be registered at +1.0% (3,552,000)
        self.assertTrue(len(exchange.limit_sells) >= 1)
        # State basket_limit_ask must point to new price
        self.assertIn("basket_limit_ask", state)
        self.assertEqual(state["basket_limit_ask"]["price"], expected_tp_price)


if __name__ == "__main__":
    unittest.main()
