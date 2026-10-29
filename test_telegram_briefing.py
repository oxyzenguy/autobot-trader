"""
Unit tests for Telegram Briefing and On-demand Status Commands
"""
import unittest
from unittest.mock import patch, MagicMock
import os

from utils.telegram_briefing import (
    build_status_briefing,
    build_main_bot_briefing,
    build_cascade_briefing,
    build_balance_briefing,
    build_recent_trades_briefing,
    build_help_briefing,
)
from main import (
    send_telegram_status_briefing,
    send_telegram_main_bot_briefing,
    send_telegram_cascade_briefing,
    send_telegram_balance_briefing,
    send_telegram_recent_trades,
    send_telegram_help_briefing,
)


class TestTelegramBriefing(unittest.TestCase):
    def test_build_status_briefing(self):
        msg = build_status_briefing()
        self.assertIsInstance(msg, str)
        self.assertIn("통합 현황 브리핑", msg)
        self.assertIn("메인봇 실거래", msg)
        self.assertIn("KRW-BTC", msg)
        self.assertIn("KRW-ETH", msg)
        self.assertIn("KRW-SOL", msg)
        self.assertIn("Cascade 서브포켓", msg)
        self.assertIn("G60 서브포켓", msg)

    def test_build_main_bot_briefing(self):
        msg = build_main_bot_briefing()
        self.assertIsInstance(msg, str)
        self.assertIn("메인 봇 실거래 상세 현황", msg)
        self.assertIn("비트코인", msg)
        self.assertIn("이더리움", msg)
        self.assertIn("솔라나", msg)
        self.assertIn("EXIT_ONLY", msg)

    def test_build_cascade_briefing(self):
        msg = build_cascade_briefing()
        self.assertIsInstance(msg, str)
        self.assertIn("Cascade 서브포켓", msg)
        self.assertIn("솔라나", msg)
        self.assertIn("이더리움", msg)
        self.assertIn("200단", msg)

    def test_build_balance_briefing(self):
        msg = build_balance_briefing()
        self.assertIsInstance(msg, str)
        self.assertIn("전 계좌 자산 및 잔고 총정리", msg)
        self.assertIn("메인 계좌", msg)
        self.assertIn("Cascade 서브포켓", msg)
        self.assertIn("G60 서브포켓", msg)
        self.assertIn("합산 총 자산", msg)

    def test_build_recent_trades_briefing(self):
        msg = build_recent_trades_briefing(limit=5)
        self.assertIsInstance(msg, str)
        self.assertTrue("최근 체결 내역" in msg or "내역이 없습니다" in msg)

    def test_build_help_briefing(self):
        msg = build_help_briefing()
        self.assertIsInstance(msg, str)
        self.assertIn("/현황", msg)
        self.assertIn("/메인", msg)
        self.assertIn("/cascade", msg)
        self.assertIn("/60", msg)
        self.assertIn("/잔고", msg)
        self.assertIn("/체결", msg)
        self.assertIn("/재기동", msg)

    def test_main_dispatchers(self):
        sent_messages = []
        with patch("main.send_telegram_alert", side_effect=lambda m: sent_messages.append(m)):
            send_telegram_status_briefing()
            send_telegram_main_bot_briefing()
            send_telegram_cascade_briefing()
            send_telegram_balance_briefing()
            send_telegram_recent_trades()
            send_telegram_help_briefing()

        self.assertEqual(len(sent_messages), 6)
        self.assertIn("통합 현황 브리핑", sent_messages[0])
        self.assertIn("메인 봇 실거래 상세 현황", sent_messages[1])
        self.assertIn("Cascade 서브포켓", sent_messages[2])
        self.assertIn("전 계좌 자산 및 잔고 총정리", sent_messages[3])
        self.assertTrue("최근 체결 내역" in sent_messages[4] or "내역이 없습니다" in sent_messages[4])
        self.assertIn("컨트롤 명령어 안내", sent_messages[5])


if __name__ == "__main__":
    unittest.main()
