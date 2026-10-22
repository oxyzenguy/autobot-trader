"""
Unit tests for cross-watchdog monitoring and emergency revival.
Fully isolated with TemporaryDirectory; never touches production state files.
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from utils.cross_watchdog import (
    LiveBotStatus,
    get_live_bot_status,
    is_live_bot_running,
    run_cross_watchdog_tick,
    poll_telegram_for_revival,
    save_watchdog_state,
    load_watchdog_state,
    set_custom_paths,
    start_live_bot_verified
)


class TestCrossWatchdog(unittest.TestCase):
    def setUp(self):
        import utils.cross_watchdog
        utils.cross_watchdog._last_check_ts = 0.0
        utils.cross_watchdog._cached_offset = None

        self.temp_dir = tempfile.TemporaryDirectory()
        self.state_file = Path(self.temp_dir.name) / "test_state.json"
        self.lock_file = Path(self.temp_dir.name) / "test_lock.lock"
        set_custom_paths(state_file=self.state_file, lock_file=self.lock_file)

    def tearDown(self):
        set_custom_paths(None, None)
        self.temp_dir.cleanup()

    def test_live_bot_detection_tri_state(self):
        # ALIVE when PID running
        with patch("utils.process_manager.find_process_pids", return_value=[1234]), \
             patch("os.kill", return_value=None):
            status, pid = get_live_bot_status()
            self.assertEqual(status, LiveBotStatus.ALIVE)
            self.assertEqual(pid, 1234)
            self.assertTrue(is_live_bot_running())

        # DEAD when clean empty list
        with patch("utils.process_manager.find_process_pids", return_value=[]):
            status, pid = get_live_bot_status()
            self.assertEqual(status, LiveBotStatus.DEAD)
            self.assertIsNone(pid)
            self.assertFalse(is_live_bot_running())

        # UNKNOWN when inspection exception occurs (must NOT be treated as DEAD)
        with patch("utils.process_manager.find_process_pids", side_effect=OSError("ps failed")):
            status, pid = get_live_bot_status()
            self.assertEqual(status, LiveBotStatus.UNKNOWN)
            self.assertIsNone(pid)
            self.assertFalse(is_live_bot_running())

    def test_unknown_status_never_triggers_death_alert(self):
        mock_send = MagicMock()
        with patch("utils.cross_watchdog.get_live_bot_status", return_value=(LiveBotStatus.UNKNOWN, None)), \
             patch("utils.cross_watchdog.time.time", return_value=1000.0), \
             patch("utils.bot.send_message", mock_send):

            run_cross_watchdog_tick()
            mock_send.assert_not_called()
            # State remains default/unmodified
            state = load_watchdog_state()
            self.assertEqual(state["status"], LiveBotStatus.ALIVE.value)

    def test_cross_watchdog_requires_consecutive_dead_before_alert(self):
        mock_send = MagicMock()
        with patch("utils.cross_watchdog.get_live_bot_status", return_value=(LiveBotStatus.DEAD, None)), \
             patch("utils.bot.send_message", mock_send), \
             patch("utils.cross_watchdog.poll_telegram_for_revival"):

            # 1st tick: transient DEAD observation - must NOT alert yet
            with patch("utils.cross_watchdog.time.time", return_value=1000.0):
                run_cross_watchdog_tick()
            mock_send.assert_not_called()
            state = load_watchdog_state()
            self.assertEqual(state["consecutive_dead_count"], 1)

            # Reset throttle timer for next tick
            import utils.cross_watchdog
            utils.cross_watchdog._last_check_ts = 0.0

            # 2nd tick: confirmed DEAD - alert is fired!
            with patch("utils.cross_watchdog.time.time", return_value=1035.0):
                run_cross_watchdog_tick()
            mock_send.assert_called_once()
            self.assertIn("실거래 봇 중단 감지", mock_send.call_args[0][0])
            state = load_watchdog_state()
            self.assertEqual(state["status"], LiveBotStatus.DEAD.value)
            self.assertEqual(state["consecutive_dead_count"], 2)

    def test_cross_watchdog_recovery_alert(self):
        save_watchdog_state({
            "status": LiveBotStatus.DEAD.value,
            "is_alive": False,
            "consecutive_dead_count": 2,
            "last_alert_ts": 1000.0
        })
        mock_send = MagicMock()

        with patch("utils.cross_watchdog.get_live_bot_status", return_value=(LiveBotStatus.ALIVE, 5555)), \
             patch("utils.cross_watchdog.time.time", return_value=2000.0), \
             patch("utils.bot.send_message", mock_send):

            run_cross_watchdog_tick()
            mock_send.assert_called_once()
            self.assertIn("실거래 봇 정상 복구 확인", mock_send.call_args[0][0])
            state = load_watchdog_state()
            self.assertEqual(state["status"], LiveBotStatus.ALIVE.value)
            self.assertEqual(state["consecutive_dead_count"], 0)
            self.assertEqual(state["last_seen_pid"], 5555)

    def test_poll_telegram_for_revival_starts_live_bot(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "ok": True,
            "result": [
                {
                    "update_id": 999,
                    "message": {
                        "chat": {"id": 12345},
                        "text": "/재기동"
                    }
                }
            ]
        }

        with patch("requests.get", return_value=mock_resp), \
             patch("requests.post") as mock_post, \
             patch("utils.cross_watchdog.start_live_bot_verified", return_value=(True, 8888, "정상 가동 확인")) as mock_start:

            revived = poll_telegram_for_revival(token="test_tok", chat_id="12345")
            self.assertTrue(revived)
            mock_start.assert_called_once()

    def test_poll_telegram_for_revival_routes_subcommands(self):
        # Case A: /재기동 가상 should route to restart_paper_bots without starting main.py
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "ok": True,
            "result": [{"update_id": 1001, "message": {"chat": {"id": 12345}, "text": "/재기동 가상"}}]
        }

        with patch("requests.get", return_value=mock_resp), \
             patch("requests.post"), \
             patch("utils.process_manager.restart_paper_bots", return_value="가상재기동성공") as mock_paper, \
             patch("utils.cross_watchdog.start_live_bot_verified") as mock_live:

            revived = poll_telegram_for_revival(token="test_tok", chat_id="12345")
            self.assertTrue(revived)
            mock_paper.assert_called_once()
            mock_live.assert_not_called()

        # Case B: Unknown subcommand does not launch main.py
        mock_resp.json.return_value = {
            "ok": True,
            "result": [{"update_id": 1002, "message": {"chat": {"id": 12345}, "text": "/재기동 미지의명령"}}]
        }
        with patch("requests.get", return_value=mock_resp), \
             patch("requests.post"), \
             patch("utils.cross_watchdog.start_live_bot_verified") as mock_live:

            revived = poll_telegram_for_revival(token="test_tok", chat_id="12345")
            self.assertFalse(revived)
            mock_live.assert_not_called()

    def test_start_live_bot_verified_fails_if_immediate_crash(self):
        mock_proc = MagicMock()
        mock_proc.pid = 9999
        mock_proc.poll.return_value = 1  # Exited with error immediately!

        with patch("subprocess.Popen", return_value=mock_proc):
            ok, pid, msg = start_live_bot_verified(timeout_sec=0.0)
            self.assertFalse(ok)
            self.assertIn("종료되었습니다", msg)


if __name__ == "__main__":
    unittest.main()
