"""Regression coverage for the auditor's observation-only process policy."""

import unittest
from datetime import datetime
from unittest.mock import patch

from utils import system_auditor as auditor


class StopAudit(BaseException):
    """End one daemon iteration without being swallowed by its error boundary."""


class TestAuditorObservationOnly(unittest.TestCase):
    def test_direct_healing_never_controls_processes(self) -> None:
        # Given: a dead or stalled process and an available restart budget.
        with patch.object(auditor, "check_restart_rate_limit", return_value=True) as budget, \
                patch.object(auditor.subprocess, "run") as run, \
                patch.object(auditor.subprocess, "Popen") as spawn:
            for target in ("main.py", "dashboard.py", "paper_grid_xrp.py"):
                with self.subTest(target=target):
                    # When: even a direct recovery request is made.
                    success, _ = auditor.heal_dead_process(target, pid_to_kill=88888)
                    # Then: no control operation or restart history write occurs.
                    self.assertFalse(success)
            budget.assert_not_called()
            run.assert_not_called()
            spawn.assert_not_called()

    def test_legacy_true_flag_cannot_enable_healing(self) -> None:
        # Given: all managed processes are missing and the shared log is stalled.
        processes = {
            name: {"is_running": False, "pid": None, "uptime": "-"}
            for name in ("main.py", "dashboard.py", "paper_grid_xrp.py")
        }
        health = {"log_exists": True, "is_stalled": True, "last_modified_sec_ago": 3000, "recent_errors": []}
        exchange = {"upbit_connected": False, "issues": [], "markets": {}}
        with patch.object(auditor, "get_running_processes", return_value=processes), \
                patch.object(auditor, "check_heartbeat_and_log_health", return_value=health), \
                patch.object(auditor, "audit_exchange_and_ledgers", return_value=exchange), \
                patch.object(auditor, "heal_dead_process", return_value=(False, "disabled")) as heal, \
                patch.object(auditor, "send_message") as notify:
            # When: an existing caller explicitly requests auto-healing.
            result = auditor.run_full_system_audit(auto_heal_enabled=True, send_telegram=False)
            # Then: diagnostics remain available without recovery or notifications.
            self.assertEqual([], result["healed_actions"])
            self.assertEqual(processes, result["processes"])
            heal.assert_not_called()
            notify.assert_not_called()

    def test_startup_schedule_and_watchdog_only_observe(self) -> None:
        # Given: a scheduled audit instant and an intentionally stopped trader.
        dead = {"main.py": {"is_running": False, "pid": None, "uptime": "-"}}
        health = {"market_heartbeats": {}, "is_stalled": False, "log_exists": True, "recent_errors": []}
        with patch.object(auditor, "datetime") as clock, \
                patch.object(auditor, "get_running_processes", return_value=dead), \
                patch.object(auditor, "check_heartbeat_and_log_health", return_value=health), \
                patch.object(auditor, "run_full_system_audit") as audit, \
                patch.object(auditor, "heal_dead_process", return_value=(False, "disabled")) as heal, \
                patch.object(auditor, "send_message") as notify, \
                patch.object(auditor.time, "sleep", side_effect=StopAudit):
            clock.now.return_value = datetime(2026, 10, 9, 17, 0)
            # When: startup and one watchdog/scheduled iteration execute.
            with self.assertRaises(StopAudit):
                auditor.run_auditor_daemon()
            # Then: scheduled reports remain enabled, but watchdog outages are console-only.
            self.assertEqual(2, audit.call_count)
            self.assertTrue(all(not call.kwargs["auto_heal_enabled"] for call in audit.call_args_list))
            heal.assert_not_called()
            notify.assert_not_called()

    def test_repeated_stale_heartbeats_check_health_without_telegram(self) -> None:
        # Given: living processes and stale ETH/SOL reports across repeated checks.
        live = {"main.py": {"is_running": True, "pid": 88888, "uptime": "1h"}}
        health = {"market_heartbeats": {"KRW-ETH": {"is_stalled": True},
                                        "KRW-SOL": {"is_stalled": True}}}
        with patch.object(auditor, "datetime") as clock, \
                patch.object(auditor, "get_running_processes", return_value=live) as processes, \
                patch.object(auditor, "check_heartbeat_and_log_health", return_value=health) as check, \
                patch.object(auditor, "run_full_system_audit") as audit, \
                patch.object(auditor, "heal_dead_process") as heal, \
                patch.object(auditor, "send_message") as notify, \
                patch.object(auditor.time, "time", side_effect=[10000, 12000, 14000]), \
                patch.object(auditor.time, "sleep", side_effect=[None, None, StopAudit]):
            clock.now.return_value = datetime(2026, 10, 12, 17, 20)
            # When: three watchdog iterations run, beyond the old alert cooldown.
            with self.assertRaises(StopAudit):
                auditor.run_auditor_daemon()
            # Then: diagnostics continue without repeated Telegram or restart calls.
            self.assertEqual(3, check.call_count)
            self.assertEqual(3, processes.call_count)
            self.assertTrue(audit.call_args.kwargs["send_telegram"])
            notify.assert_not_called()
            heal.assert_not_called()


if __name__ == "__main__":
    unittest.main()
