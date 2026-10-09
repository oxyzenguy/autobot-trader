"""Adversarial tests for PURE_MARTINGALE transition and Telegram command listener."""

from __future__ import annotations

import copy
import io
import unittest
from unittest.mock import patch, MagicMock

import main


class EndTick(BaseException):
    """Stop the loop after the decision."""


class FakeExchange:
    def __init__(self, holdings: float, price: float):
        self.holdings = holdings
        self.price = price
        self.market_buys: list[float] = []
        self.market_sells: list[float] = []

    def get_balance(self, currency: str, verbose: bool = False):
        if currency == "KRW":
            return 100_000.0
        if currency == "ETH" and verbose:
            return {"balance": self.holdings, "locked": 0.0}
        if currency == "ETH":
            return self.holdings
        raise AssertionError(f"Unexpected currency: {currency}")

    def get_order(self, identifier: str, state: str | None = None):
        return []

    def buy_market_order(self, market: str, price: float):
        self.market_buys.append(price)
        vol = price / self.price
        self.holdings += vol
        return {
            "uuid": "fake-buy",
            "state": "done",
            "executed_volume": str(vol),
            "trades": [{"funds": str(price)}],
            "paid_fee": "0",
        }

    def sell_market_order(self, market: str, volume: float):
        self.market_sells.append(volume)
        self.holdings -= volume
        return {
            "uuid": "fake-sell",
            "state": "done",
            "executed_volume": str(volume),
            "trades": [{"funds": str(volume * self.price)}],
            "paid_fee": "0",
        }

    def sell_limit_order(self, market: str, price: float, volume: float):
        return {
            "uuid": "fake-limit-sell",
            "side": "ask",
            "price": str(price),
            "volume": str(volume),
            "state": "wait",
        }

    def buy_limit_order(self, market: str, price: float, volume: float):
        return {
            "uuid": "fake-limit-buy",
            "side": "bid",
            "price": str(price),
            "volume": str(volume),
            "state": "wait",
        }


class TestP1PureMartingaleTransition(unittest.TestCase):
    def test_legacy_bull_state_migrated_to_bear_and_stop_loss_never_executed(self):
        """저장 상태가 BULL이고 가격이 -20% 폭락해도, PURE_MARTINGALE에서는 손절 없이 바스켓으로 마이그레이션되어야 합니다."""
        market = "KRW-ETH"
        initial_price = 3_000_000.0
        # 평단가 4,000,000원 대비 현재가 3,000,000원 -> -25% 폭락 상태
        state = {
            "market": market,
            "current_regime": "BULL",
            "active_mode": "TREND",
            "bot_quantity": 0.01,
            "bot_avg_price": 4_000_000.0,
            "trailing_stop_active": True,
            "trend_peak_price": 4_500_000.0,
            "tranches": [],
            "trading_halted": False,
        }
        exchange = FakeExchange(holdings=0.01, price=initial_price)
        saved = []
        logged = []

        def save_state(m: str, value: dict):
            saved.append(copy.deepcopy(value))

        price_queries = 0

        def get_price(m: str):
            nonlocal price_queries
            price_queries += 1
            if price_queries > 1:
                raise EndTick()
            return initial_price

        sleep_queries = 0

        def bounded_sleep(s):
            nonlocal sleep_queries
            sleep_queries += 1
            if sleep_queries > 10:
                raise EndTick("Sleep limit exceeded in test")

        with (
            patch.multiple(
                main,
                acquire_market_process_lock=lambda m: object(),
                init_db=lambda: None,
                get_upbit_client=lambda: exchange,
                load_strategy_state=lambda m: state,
                save_strategy_state=save_state,
                check_krw_balance_alert=lambda *args, **kwargs: 100_000.0,
                send_telegram_alert=lambda message: None,
                safe_get_current_price=get_price,
                get_hybrid_regime_and_signals=lambda m: {
                    "is_bull": True,
                    "signal": "HOLD",
                    "ma5": initial_price,
                    "ma20": initial_price,
                    "ma200": initial_price,
                    "regime_korean": "상승 국면",
                },
                log_real_trade=lambda **trade: logged.append(trade),
                cancel_all_orders=lambda *args, **kwargs: True,
            ),
            patch("main.get_market_strategy", return_value="PURE_MARTINGALE"),
            patch("main.PROTECTED_BALANCES", {market: 0.0}),
            patch("time.sleep", side_effect=bounded_sleep),
        ):
            try:
                main.run_trading_strategy(market)
            except EndTick:
                pass

        # 1. 상태가 즉시 BEAR 및 MARTINGALE_MAGIC_SPLIT으로 마이그레이션되었는지 검증
        self.assertEqual(state["current_regime"], "BEAR")
        self.assertEqual(state["active_mode"], "MARTINGALE_MAGIC_SPLIT")
        self.assertFalse(state["trailing_stop_active"])
        self.assertEqual(len(state["tranches"]), 1)
        self.assertEqual(state["tranches"][0]["source"], "MIGRATION_FROM_BULL")

        # 2. -25% 폭락 상태였음에도 불구하고 손절 매도(market_sells)가 전혀 실행되지 않았는지 검증
        self.assertEqual(len(exchange.market_sells), 0, "PURE_MARTINGALE에서는 절대로 BULL 손절이 실행되어서는 안 됩니다.")
        self.assertEqual(len(logged), 0)

    def test_telegram_command_listener_handles_commands_without_name_error(self):
        """텔레그램 수동 명령(/감사, 감사, /상태, 상태, /전략승인, 전략승인)이 NameError 없이 정상 처리되는지 검증합니다."""
        fake_updates = [
            {"update_id": 1, "message": {"chat": {"id": 12345}, "text": "/상태"}},
            {"update_id": 2, "message": {"chat": {"id": 12345}, "text": "감사"}},
            {"update_id": 3, "message": {"chat": {"id": 12345}, "text": "/전략승인 SOL"}},
            {"update_id": 4, "message": {"chat": {"id": 12345}, "text": "전략승인 ETH"}},
        ]

        # Simulate get updates
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"ok": True, "result": fake_updates}

        audit_called = []
        status_called = []
        approval_called = []

        def fake_audit(*args, **kwargs):
            audit_called.append(True)

        def fake_status():
            status_called.append(True)

        def fake_approval(market):
            approval_called.append(market)
            return True, f"{market} 승인 완료"

        poll_count = 0

        def fake_get(*args, **kwargs):
            nonlocal poll_count
            poll_count += 1
            if poll_count == 1:
                return mock_resp
            raise EndTick()

        with (
            patch("requests.get", side_effect=fake_get),
            patch("utils.bot.TOKEN", "fake-token"),
            patch("utils.bot.CHAT_ID", "12345"),
            patch("main.send_telegram_status_briefing", side_effect=fake_status),
            patch("main.send_telegram_alert", return_value=True),
            patch("utils.system_auditor.run_full_system_audit", side_effect=fake_audit),
            patch("strategy.strategy_advisor.apply_strategy_approval", side_effect=fake_approval),
            patch("time.sleep", side_effect=lambda s: None),
        ):
            try:
                main.telegram_command_listener_worker()
            except EndTick:
                pass

        self.assertEqual(len(status_called), 1)
        self.assertEqual(len(audit_called), 1)
        self.assertEqual(approval_called, ["SOL", "ETH"])


if __name__ == "__main__":
    unittest.main()
