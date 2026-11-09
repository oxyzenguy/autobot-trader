#!/usr/bin/env python3
"""
Unit tests for get_krw_balance and check_krw_balance_alert in main.py
Verifies:
1. Safe KRW balance retrieval with retries and graceful handling of None/exceptions.
2. Prevention of false alarm alerts (0원 경고 방지) when API calls fail (return None).
3. Clear account labeling in alert telegram messages ([메인 계좌 - 예수금 부족 경고]).
4. Thread-safe cooldown enforcement.
"""
import unittest
from unittest.mock import MagicMock, patch
import main


class TestKrwBalanceAlert(unittest.TestCase):
    def setUp(self):
        # Reset alert cooldown timer before each test
        main.LAST_KRW_ALERT_TIME = 0

    def test_get_krw_balance_success(self):
        client = MagicMock()
        client.get_balance.return_value = 301126.0
        bal = main.get_krw_balance(client)
        self.assertEqual(bal, 301126.0)
        self.assertEqual(client.get_balance.call_count, 1)

    def test_get_krw_balance_none_returns_none(self):
        """When pyupbit returns None on network error, get_krw_balance returns None after retries."""
        client = MagicMock()
        client.get_balance.return_value = None
        bal = main.get_krw_balance(client, retries=2, delay=0.01)
        self.assertIsNone(bal)
        self.assertEqual(client.get_balance.call_count, 2)

    def test_get_krw_balance_exception_returns_none(self):
        """When client throws exception on timeout/network error, get_krw_balance handles gracefully."""
        client = MagicMock()
        client.get_balance.side_effect = ConnectionError("Upbit API timeout")
        bal = main.get_krw_balance(client, retries=2, delay=0.01)
        self.assertIsNone(bal)
        self.assertEqual(client.get_balance.call_count, 2)

    @patch("main.send_telegram_alert")
    def test_check_krw_balance_alert_normal_balance_no_alert(self, mock_send):
        """When balance is >= 100,000, no alert is sent."""
        client = MagicMock()
        client.get_balance.return_value = 250000.0
        res = main.check_krw_balance_alert(client, context="테스트 감시")
        self.assertEqual(res, 250000.0)
        mock_send.assert_not_called()

    @patch("main.send_telegram_alert")
    def test_check_krw_balance_alert_api_error_no_false_alarm(self, mock_send):
        """When API fails (returns None), no false alarm of '0원' is sent to Telegram."""
        client = MagicMock()
        client.get_balance.return_value = None
        res = main.check_krw_balance_alert(client, context="테스트 감시")
        self.assertEqual(res, 0.0)
        # MUST NOT send telegram alert on API failure!
        mock_send.assert_not_called()

    @patch("main.send_telegram_alert")
    def test_check_krw_balance_alert_low_balance_specifies_account(self, mock_send):
        """When real balance is < 100,000, alert is sent specifying the account name."""
        client = MagicMock()
        client.get_balance.return_value = 50000.0
        res = main.check_krw_balance_alert(
            client,
            context="KRW-SOL 실시간 감시",
            account_name="메인 계좌",
        )
        self.assertEqual(res, 50000.0)
        mock_send.assert_called_once()
        sent_msg = mock_send.call_args[0][0]
        self.assertIn("[메인 계좌 - 예수금 부족 경고]", sent_msg)
        self.assertIn("• 대상 계좌: <b>메인 계좌</b>", sent_msg)
        self.assertIn("50,000원", sent_msg)
        self.assertIn("KRW-SOL 실시간 감시", sent_msg)

    @patch("main.send_telegram_alert")
    def test_check_krw_balance_alert_cooldown_enforced(self, mock_send):
        """Subsequent low balance checks within 1 hour do not trigger duplicate alerts."""
        client = MagicMock()
        client.get_balance.return_value = 40000.0
        # 1st call triggers alert
        main.check_krw_balance_alert(client, context="1차 감시")
        self.assertEqual(mock_send.call_count, 1)

        # 2nd call right after is throttled by cooldown
        main.check_krw_balance_alert(client, context="2차 감시")
        self.assertEqual(mock_send.call_count, 1)


if __name__ == "__main__":
    unittest.main()
