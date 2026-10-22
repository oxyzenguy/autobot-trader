"""
Unit tests for Telegram remote restart functionality.
"""
import unittest
from unittest.mock import patch, MagicMock

from utils.process_manager import (
    find_process_pids,
    get_all_bot_statuses,
    handle_restart_command,
    trigger_live_bot_restart,
    restart_paper_bots,
    restart_dashboard_bot,
)


class TestTelegramRestart(unittest.TestCase):
    def setUp(self):
        self.sent_alerts = []

        def mock_send(msg):
            self.sent_alerts.append(msg)

        self.mock_send = mock_send

    def test_find_process_pids(self):
        fake_ps = (
            "  101 /usr/bin/python3 main.py\n"
            "  202 python3 ./paper_grid_xrp.py\n"
            "  303 streamlit run dashboard.py --server.port 8501\n"
            "  404 grep main.py\n"
        )
        with patch("subprocess.check_output", return_value=fake_ps), patch("os.getpid", return_value=999):
            pids_main = find_process_pids("main.py")
            self.assertEqual(pids_main, [101])

            pids_paper = find_process_pids("paper_grid_xrp.py")
            self.assertEqual(pids_paper, [202])

            pids_dash = find_process_pids("dashboard.py")
            self.assertEqual(pids_dash, [303])

    def test_handle_restart_default_live(self):
        with patch("utils.process_manager.trigger_live_bot_restart") as mock_live_restart:
            handle_restart_command(
                text="/재기동",
                token="test_token",
                chat_id="12345",
                offset=42,
                send_alert_fn=self.mock_send
            )
            self.assertTrue(len(self.sent_alerts) >= 1)
            self.assertIn("실거래 봇 재기동 시작", self.sent_alerts[0])
            mock_live_restart.assert_called_once_with("test_token", "12345", 42)

    def test_handle_restart_live_subcommand(self):
        with patch("utils.process_manager.trigger_live_bot_restart") as mock_live_restart:
            handle_restart_command(
                text="/restart live",
                token="test_token",
                chat_id="12345",
                offset=100,
                send_alert_fn=self.mock_send
            )
            mock_live_restart.assert_called_once_with("test_token", "12345", 100)

    def test_handle_restart_paper(self):
        with patch("utils.process_manager.restart_paper_bots", return_value="가상매매 재기동 성공"):
            handle_restart_command(
                text="/재기동 가상",
                token="test_token",
                chat_id="12345",
                offset=50,
                send_alert_fn=self.mock_send
            )
            self.assertTrue(any("가상매매 재기동" in alert for alert in self.sent_alerts))
            self.assertTrue(any("가상매매 재기동 성공" in alert for alert in self.sent_alerts))

    def test_handle_restart_dashboard(self):
        with patch("utils.process_manager.restart_dashboard_bot", return_value="대시보드 재기동 성공"):
            handle_restart_command(
                text="/재기동 대시보드",
                token="test_token",
                chat_id="12345",
                offset=51,
                send_alert_fn=self.mock_send
            )
            self.assertTrue(any("대시보드 재기동" in alert for alert in self.sent_alerts))
            self.assertTrue(any("대시보드 재기동 성공" in alert for alert in self.sent_alerts))

    def test_handle_restart_all(self):
        with patch("utils.process_manager.restart_paper_bots") as mock_paper, \
             patch("utils.process_manager.restart_dashboard_bot") as mock_dash, \
             patch("utils.process_manager.trigger_live_bot_restart") as mock_live:
            handle_restart_command(
                text="/재기동 전체",
                token="test_token",
                chat_id="12345",
                offset=99,
                send_alert_fn=self.mock_send
            )
            mock_paper.assert_called_once()
            mock_dash.assert_called_once()
            mock_live.assert_called_once_with("test_token", "12345", 99)

    def test_handle_restart_help(self):
        handle_restart_command(
            text="/재기동 도움말",
            token="test_token",
            chat_id="12345",
            offset=10,
            send_alert_fn=self.mock_send
        )
        self.assertTrue(any("재기동 명령어 사용법" in alert for alert in self.sent_alerts))

    def test_handle_restart_invalid(self):
        handle_restart_command(
            text="/재기동 이상한명령",
            token="test_token",
            chat_id="12345",
            offset=10,
            send_alert_fn=self.mock_send
        )
        self.assertTrue(any("알 수 없는 재기동 대상" in alert for alert in self.sent_alerts))

    def test_trigger_live_bot_restart_ack_and_execv(self):
        with patch("requests.get") as mock_req, \
             patch("os.execv") as mock_execv, \
             patch("time.sleep"):
            trigger_live_bot_restart("test_token", "12345", offset=77)
            mock_req.assert_called_once()
            params = mock_req.call_args[1].get("params", {})
            self.assertEqual(params.get("offset"), 77)
            mock_execv.assert_called_once()


if __name__ == "__main__":
    unittest.main()
