"""
AutoBot Trader - 프로세스 관리 및 원격 재기동 유틸리티
(Live Bot, Paper Grid Bots, Streamlit Dashboard)
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Any

import requests

BASE_DIR = Path(__file__).resolve().parent.parent


def get_python_executable() -> str:
    """우선적으로 venv python3을 탐색하고 없으면 sys.executable을 반환합니다."""
    venv_py = BASE_DIR / "venv" / "bin" / "python3"
    if venv_py.exists():
        return str(venv_py)
    return sys.executable


def find_process_pids(script_name: str) -> List[int]:
    """지정된 스크립트명으로 실행 중인 프로세스의 PID 목록을 반환합니다."""
    pids = []
    my_pid = os.getpid()
    try:
        out = subprocess.check_output(["ps", "-eo", "pid,command"], text=True)
        for line in out.splitlines():
            line_str = line.strip()
            if not line_str:
                continue
            parts = line_str.split(None, 1)
            if len(parts) < 2:
                continue
            pid_str, cmd = parts[0], parts[1]
            try:
                pid = int(pid_str)
            except ValueError:
                continue
            if pid == my_pid:
                continue

            tokens = cmd.split()
            first_cmd = os.path.basename(tokens[0]) if tokens else ""
            if any(tool in cmd for tool in ["grep", "ps -eo", "system_auditor.py"]):
                continue
            if first_cmd in ["vim", "nvim", "nano", "vi", "cat", "less", "more"]:
                continue

            matched = False
            for token in tokens:
                if token == script_name or token.endswith("/" + script_name):
                    matched = True
                    break
            if matched:
                pids.append(pid)
    except Exception:
        pass
    return pids


def get_all_bot_statuses() -> Dict[str, Dict[str, Any]]:
    """모든 주요 트레이딩 봇의 프로세스 가동 상태를 조회합니다."""
    targets = {
        "main.py": "메인 실거래 봇 (main.py)",
        "pocket_grid_xrp.py": "G60 포켓 실거래 (XRP)",
        "pocket_grid_eth.py": "G60 포켓 실거래 (ETH)",
        "pocket_grid_sol.py": "G60 포켓 실거래 (SOL)",
        "dashboard.py": "웹 대시보드 (dashboard.py)"
    }
    res = {}
    for script, name in targets.items():
        pids = find_process_pids(script)
        res[script] = {
            "name": name,
            "is_running": len(pids) > 0,
            "pids": pids
        }
    return res


def kill_processes(pids: List[int], timeout_sec: float = 2.5) -> None:
    """주어진 PID 목록의 프로세스를 안전하게 종료(SIGTERM 후 미종료 시 SIGKILL)합니다."""
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass

    start = time.time()
    while time.time() - start < timeout_sec:
        alive = []
        for pid in pids:
            try:
                os.kill(pid, 0)
                alive.append(pid)
            except OSError:
                pass
        if not alive:
            return
        time.sleep(0.1)

    for pid in alive:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def restart_pocket_bots() -> str:
    """G60 포켓 실거래 데몬 3종(XRP, ETH, SOL)을 종료 후 백그라운드로 안전하게 재기동합니다."""
    scripts = ["pocket_grid_xrp.py", "pocket_grid_eth.py", "pocket_grid_sol.py"]
    new_pids = {}
    python_bin = get_python_executable()

    for script in scripts:
        old_pids = find_process_pids(script)
        if old_pids:
            kill_processes(old_pids)

        log_file = BASE_DIR / script.replace(".py", ".log")
        with open(log_file, "a") as f:
            proc = subprocess.Popen(
                [python_bin, "-u", script],
                cwd=str(BASE_DIR),
                stdout=f,
                stderr=subprocess.STDOUT,
                start_new_session=True
            )
            new_pids[script] = proc.pid
        time.sleep(0.3)

    return (
        "🚀 <b>[G60 포켓 실거래 3종 가동 완료]</b>\n"
        f"• XRP: <code>PID {new_pids.get('pocket_grid_xrp.py', '-')}</code>\n"
        f"• ETH: <code>PID {new_pids.get('pocket_grid_eth.py', '-')}</code>\n"
        f"• SOL: <code>PID {new_pids.get('pocket_grid_sol.py', '-')}</code>\n"
        "서브포켓 실거래 호가 감시 및 자동 매매가 시작되었습니다."
    )


def restart_paper_bots() -> str:
    """가상매매 데몬 3종(XRP, ETH, SOL)을 종료 후 백그라운드로 안전하게 재기동합니다."""
    scripts = ["paper_grid_xrp.py", "paper_grid_eth.py", "paper_grid_sol.py"]
    new_pids = {}
    python_bin = get_python_executable()

    for script in scripts:
        old_pids = find_process_pids(script)
        if old_pids:
            kill_processes(old_pids)

        log_file = BASE_DIR / script.replace(".py", ".log")
        with open(log_file, "a") as f:
            proc = subprocess.Popen(
                [python_bin, "-u", script],
                cwd=str(BASE_DIR),
                stdout=f,
                stderr=subprocess.STDOUT,
                start_new_session=True
            )
            new_pids[script] = proc.pid
        time.sleep(0.3)

    return (
        "✅ <b>[가상매매 3종 재기동 완료]</b>\n"
        f"• XRP: <code>PID {new_pids.get('paper_grid_xrp.py', '-')}</code>\n"
        f"• ETH: <code>PID {new_pids.get('paper_grid_eth.py', '-')}</code>\n"
        f"• SOL: <code>PID {new_pids.get('paper_grid_sol.py', '-')}</code>\n"
        "장부 및 호가 감시가 정상 재개되었습니다."
    )


def restart_dashboard_bot() -> str:
    """Streamlit 웹 대시보드(포트 8501)를 종료 후 백그라운드로 재기동합니다."""
    old_pids = find_process_pids("dashboard.py")
    if old_pids:
        kill_processes(old_pids)

    log_file = BASE_DIR / "dashboard.log"
    python_bin = get_python_executable()

    with open(log_file, "a") as f:
        proc = subprocess.Popen(
            [python_bin, "-m", "streamlit", "run", "dashboard.py", "--server.port", "8501", "--server.headless", "true"],
            cwd=str(BASE_DIR),
            stdout=f,
            stderr=subprocess.STDOUT,
            start_new_session=True
        )
    time.sleep(0.5)

    return f"✅ <b>[웹 대시보드 재기동 완료]</b>\n• Streamlit PID: <code>{proc.pid}</code> (포트 8501)"


def trigger_live_bot_restart(token: str, chat_id: str, offset: Optional[int] = None) -> None:
    """
    현재 실행 중인 main.py 프로세스를 os.execv를 통해 원자적으로 즉시 재기동합니다.
    1. 텔레그램 메시지 중복 처리 방지를 위해 getUpdates offset을 선확인
    2. 표준 입출력 버퍼 플러시
    3. 동일 프로세스 주소 공간을 새 main.py 프로세스로 완전히 교체
    """
    if offset and token:
        try:
            requests.get(
                f"https://api.telegram.org/bot{token}/getUpdates",
                params={"offset": offset, "limit": 1},
                timeout=5
            )
        except Exception:
            pass

    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:
        pass

    time.sleep(1.2)

    python_bin = get_python_executable()
    script_path = str(BASE_DIR / "main.py")
    os.execv(python_bin, [python_bin, "-u", script_path])


def handle_restart_command(
    text: str,
    token: str,
    chat_id: str,
    offset: Optional[int] = None,
    send_alert_fn: Optional[Any] = None
) -> None:
    """
    텔레그램에서 수신된 재기동 명령어를 파싱하고 대상에 맞게 실행합니다.
    """
    if send_alert_fn is None:
        def send_alert_fn(msg: str):
            try:
                requests.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={"chat_id": chat_id, "text": msg, "parse_mode": "HTML"},
                    timeout=10
                )
            except Exception:
                pass

    parts = text.strip().split()
    sub_cmd = parts[1].strip().lower() if len(parts) >= 2 else "live"

    if sub_cmd in ["실거래", "live", "main", "main.py"]:
        send_alert_fn(
            f"🔄 <b>[실거래 봇 재기동 시작]</b>\n"
            f"사용자 텔레그램 명령으로 실거래 봇(main.py)을 안전하게 재시작합니다.\n"
            f"• 직전 PID: <code>{os.getpid()}</code>\n"
            f"• 잠시 후 재기동 완료 알림이 전송됩니다..."
        )
        trigger_live_bot_restart(token, chat_id, offset)

    elif sub_cmd in ["가상", "paper", "가상매매", "grid"]:
        send_alert_fn("🔄 <b>[가상매매 재기동]</b> XRP/ETH/SOL 3개 가상매매 데몬을 재시작합니다...")
        msg = restart_paper_bots()
        send_alert_fn(msg)

    elif sub_cmd in ["포켓", "pocket", "g60", "서브포켓"]:
        send_alert_fn("🔄 <b>[G60 포켓 실거래 재기동]</b> XRP/ETH/SOL 3개 서브포켓 데몬을 재시작합니다...")
        msg = restart_pocket_bots()
        send_alert_fn(msg)

    elif sub_cmd in ["대시보드", "dashboard", "streamlit"]:
        send_alert_fn("🔄 <b>[웹 대시보드 재기동]</b> Streamlit 대시보드를 재시작합니다...")
        msg = restart_dashboard_bot()
        send_alert_fn(msg)

    elif sub_cmd in ["전체", "all"]:
        send_alert_fn(
            f"🔄 <b>[전체 시스템 재기동]</b>\n"
            f"G60 포켓 실거래, 가상매매 데몬 및 대시보드를 재시작한 후 실거래 봇(main.py)을 재기동합니다.\n"
            f"• 직전 PID: <code>{os.getpid()}</code>"
        )
        restart_pocket_bots()
        restart_paper_bots()
        restart_dashboard_bot()
        trigger_live_bot_restart(token, chat_id, offset)

    elif sub_cmd in ["도움말", "help"]:
        help_text = (
            "📋 <b>[재기동 명령어 사용법]</b>\n\n"
            "• <code>/재기동</code> : 실거래 봇(main.py) 재기동 (기본값)\n"
            "• <code>/재기동 실거래</code> : 실거래 봇 재기동\n"
            "• <code>/재기동 포켓</code> : G60 포켓 실거래 3종(XRP/ETH/SOL) 재기동\n"
            "• <code>/재기동 가상</code> : 가상매매 3종(XRP/ETH/SOL) 재기동\n"
            "• <code>/재기동 대시보드</code> : Streamlit 웹 대시보드 재기동\n"
            "• <code>/재기동 전체</code> : 실거래 / 포켓 / 가상매매 / 대시보드 전체 재기동\n\n"
            "💡 영어 명령어 <code>/restart</code>, <code>/restart all</code> 등도 동일 지원됩니다."
        )
        send_alert_fn(help_text)

    else:
        send_alert_fn(
            f"⚠️ 알 수 없는 재기동 대상: <code>{sub_cmd}</code>\n"
            "사용법: <code>/재기동</code>, <code>/재기동 가상</code>, <code>/재기동 전체</code>, <code>/재기동 도움말</code>"
        )
