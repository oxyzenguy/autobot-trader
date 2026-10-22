#!/usr/bin/env python3
"""
Unit test for G60 Telegram Command (/60, /g60) and Briefing Delivery
"""
import unittest
from unittest.mock import patch, Mock
import os

from main import send_telegram_g60_briefing


class TestG60TelegramCommand(unittest.TestCase):
    def test_send_telegram_g60_briefing_executes_cleanly(self):
        """send_telegram_g60_briefing 실행 시 오류 없이 정상 텔레그램 메시지 조립 및 전송 검증"""
        sent_alerts = []
        with patch("main.send_telegram_alert", side_effect=lambda msg: sent_alerts.append(msg)), \
             patch("pyupbit.get_current_price", return_value=3200000.0), \
             patch("utils.pocket_api.UpbitPocketClient.get_balance", return_value=600000.0):
            send_telegram_g60_briefing()

            self.assertEqual(len(sent_alerts), 1)
            msg = sent_alerts[0]
            self.assertIn("G60 서브포켓 스쿼드 그리드 실거래 현황", msg)
            self.assertIn("이더리움", msg)
            self.assertIn("솔라나", msg)
            self.assertIn("리플", msg)
            self.assertIn("서브포켓 예수금", msg)


if __name__ == "__main__":
    unittest.main()
