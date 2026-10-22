import unittest
from unittest.mock import patch, MagicMock
import tempfile
import json
import time
from pathlib import Path

from utils.system_auditor import (
    get_running_processes,
    check_heartbeat_and_log_health,
    check_restart_rate_limit,
    build_telegram_report,
    heal_dead_process
)


class TestSystemAuditor(unittest.TestCase):

    def test_build_telegram_report_all_green(self):
        processes = {
            "main.py": {"is_running": True, "pid": 1234, "uptime": "05:00:00"},
            "dashboard.py": {"is_running": True, "pid": 5678, "uptime": "10:00:00"},
            "paper_grid_xrp.py": {"is_running": True, "pid": 9012, "uptime": "01:30:00"}
        }
        log_health = {
            "log_exists": True,
            "last_modified_sec_ago": 120,
            "is_stalled": False,
            "recent_errors": []
        }
        exchange_audit = {
            "upbit_connected": True,
            "krw_balance": 1200000.0,
            "krw_locked": 100000.0,
            "total_krw": 1300000.0,
            "protected_assets_safe": True,
            "markets": {
                "KRW-BTC": {"bot_quantity": 0.0, "bot_avg_price": 0.0, "ticker": "BTC", "open_asks": 0, "open_bids": 0},
                "KRW-ETH": {"bot_quantity": 0.005, "bot_avg_price": 3600000.0, "ticker": "ETH", "open_asks": 1, "open_bids": 3}
            },
            "issues": []
        }
        report = build_telegram_report(processes, log_health, exchange_audit, healed_actions=[], audit_label="테스트")
        self.assertIn("ALL GREEN 🟢", report)
        self.assertIn("KRW-ETH", report)
        self.assertIn("1,300,000원", report)
        self.assertIn("100% 안전 격리", report)

    def test_build_telegram_report_with_issues_and_healing(self):
        processes = {
            "main.py": {"is_running": False, "pid": None, "uptime": "-"},
            "dashboard.py": {"is_running": True, "pid": 5678, "uptime": "10:00:00"},
            "paper_grid_xrp.py": {"is_running": True, "pid": 9012, "uptime": "01:30:00"}
        }
        log_health = {
            "log_exists": True,
            "last_modified_sec_ago": 3000,
            "is_stalled": True,
            "recent_errors": ["Traceback (most recent call last): Exception"]
        }
        exchange_audit = {
            "upbit_connected": True,
            "krw_balance": 1200000.0,
            "krw_locked": 100000.0,
            "total_krw": 1300000.0,
            "protected_assets_safe": False,
            "markets": {},
            "issues": ["보호 자산 침해 위험"]
        }
        report = build_telegram_report(
            processes, log_health, exchange_audit,
            healed_actions=["main.py 실전 매매 데몬 자동 재가동 성공"],
            audit_label="장애 복구 테스트"
        )
        self.assertIn("주의/조치 필요 🟡", report)
        self.assertIn("중단 감지", report)
        self.assertIn("스톨(멈춤) 의심", report)
        self.assertIn("침해 위험", report)
        self.assertIn("자가치유(Self-Healing) 조치 내역", report)

    def test_heal_dead_process_leaves_process_alive_in_observation_mode(self):
        """관측 전용에서는 기존 프로세스를 종료하지 않고 False를 반환해야 합니다."""
        with patch("utils.system_auditor.check_restart_rate_limit", return_value=False):
            with patch("subprocess.run") as mock_run:
                success, msg = heal_dead_process("main.py", pid_to_kill=88888)
                self.assertFalse(success)
                # subprocess.run (kill -9) must NEVER be called
                mock_run.assert_not_called()

    def test_audit_exchange_and_ledgers_flags_discrepancy_and_order_failure(self):
        """거래소 잔고 0 vs 장부 0.1 ETH, 주문 조회 비정상 응답 시 ALL GREEN이 아닌 경고를 발생시켜야 합니다."""
        from utils.system_auditor import audit_exchange_and_ledgers

        mock_upbit = MagicMock()
        # 원화 100만, ETH 0개
        mock_upbit.get_balances.return_value = [
            {"currency": "KRW", "balance": "1000000.0", "locked": "0.0"},
            {"currency": "BTC", "balance": "0.03741469", "locked": "0.0"},
            {"currency": "ETH", "balance": "0.0", "locked": "0.0"},
            {"currency": "SOL", "balance": "6.66887531", "locked": "0.0"},
        ]
        # 주문 조회 비정상 응답 (None 또는 에러 딕셔너리)
        mock_upbit.get_order.return_value = None

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            # ETH 상태 파일 생성: 장부에 0.1 ETH 존재
            eth_state = tmp_path / "real_strategy_state_KRW_ETH.json"
            with open(eth_state, "w", encoding="utf-8") as f:
                json.dump({"market": "KRW-ETH", "bot_quantity": 0.1, "bot_avg_price": 3500000.0, "current_regime": "BEAR"}, f)

            with patch("pyupbit.Upbit", return_value=mock_upbit):
                with patch("utils.system_auditor.BASE_DIR", tmp_path):
                    with patch("utils.system_auditor.PROTECTED_BALANCES", {"KRW-BTC": 0.03741469, "KRW-ETH": 0.0, "KRW-SOL": 6.66887531}):
                        audit_res = audit_exchange_and_ledgers()

        # 장부 불일치 및 주문조회 실패가 반드시 issues에 등록되어야 함
        issues_str = " ".join(audit_res["issues"])
        self.assertIn("장부-거래소 잔고 불일치", issues_str)
        self.assertIn("미체결 주문 조회 실패", issues_str)

        # 리포트 생성 시 ALL GREEN이 절대로 나오지 않아야 함
        processes = {"main.py": {"is_running": True, "pid": 1234, "uptime": "01:00:00"}}
        log_health = {"log_exists": True, "last_modified_sec_ago": 10, "is_stalled": False, "recent_errors": []}
        report = build_telegram_report(processes, log_health, audit_res, healed_actions=[], audit_label="정합성 테스트")
        self.assertNotIn("ALL GREEN", report)
        self.assertIn("주의/조치 필요 🟡", report)

    def test_auditor_rejects_relative_path_escape(self):
        """프로젝트 디렉터리 내에서 상대경로(python ../other-project/main.py)로 실행된 외부 프로세스는 식별에서 배제되어야 합니다."""
        import subprocess
        def fake_check_output(args, **kwargs):
            if args[0] == "ps":
                return "345 00:10 python ../other-project/main.py\n"
            return "p345\nn" + str(Path(__file__).resolve().parent) + "\n"

        with patch("utils.system_auditor.subprocess.check_output", side_effect=fake_check_output):
            procs = get_running_processes()
            self.assertFalse(procs["main.py"]["is_running"])
            self.assertIsNone(procs["main.py"]["pid"])


if __name__ == "__main__":
    unittest.main()
