import os
import json
import time
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional, Tuple, List
import pandas as pd
import requests

try:
    from cryptography.fernet import Fernet
except ImportError:
    Fernet = None

from utils.analytics import get_total_account_summary, get_all_active_strategies

SNAPSHOT_DIR = Path(__file__).resolve().parent.parent / "data"
SNAPSHOT_FILE = SNAPSHOT_DIR / "latest_snapshot.json"
SYNC_ISSUE_TITLE = "[AutoBot] Realtime Data Sync"

# 캐시된 Issue 번호 (매번 목록 검색 방지)
_CACHED_ISSUE_NUMBER = None
_BG_THREAD_STARTED = False
_SYNC_LOCK = threading.Lock()
_SYNC_EVENT = threading.Event()
_LAST_PUSH_WARN_TIME = 0.0


def get_sync_credentials() -> Tuple[Optional[str], Optional[str], str]:
    """
    환경변수 또는 Streamlit Secrets에서 동기화에 필요한 자격증명을 가져옵니다.
    반환: (sync_secret_key, github_token, github_repo)
    """
    secret_key = os.getenv("SYNC_SECRET_KEY")
    gh_token = os.getenv("GITHUB_TOKEN")
    gh_repo = os.getenv("GITHUB_REPO", "oxyzenguy/autobot-trader")

    try:
        import streamlit as st
        if hasattr(st, "secrets"):
            secret_key = secret_key or st.secrets.get("SYNC_SECRET_KEY")
            gh_token = gh_token or st.secrets.get("GITHUB_TOKEN")
            gh_repo = st.secrets.get("GITHUB_REPO", gh_repo)
            
            # 소문자 및 [sync] 섹션 호환
            if not secret_key and "sync_secret_key" in st.secrets:
                secret_key = st.secrets["sync_secret_key"]
            if not gh_token and "github_token" in st.secrets:
                gh_token = st.secrets["github_token"]
            if "sync" in st.secrets:
                secret_key = secret_key or st.secrets["sync"].get("secret_key")
                gh_token = gh_token or st.secrets["sync"].get("github_token")
                gh_repo = st.secrets["sync"].get("github_repo", gh_repo)
    except Exception:
        pass

    if secret_key:
        secret_key = str(secret_key).strip().strip('"').strip("'")
    if gh_token:
        gh_token = str(gh_token).strip().strip('"').strip("'")
    if gh_repo:
        gh_repo = str(gh_repo).strip().strip('"').strip("'")

    return secret_key, gh_token, gh_repo


def encrypt_payload(data_dict: Dict[str, Any], secret_key: Optional[str] = None) -> str:
    """데이터 딕셔너리를 Fernet(AES-128-CBC + HMAC)으로 암호화하여 문자열로 반환합니다."""
    if Fernet is None:
        raise RuntimeError("cryptography 패키지가 설치되지 않았습니다. pip install cryptography")
    
    if not secret_key:
        secret_key, _, _ = get_sync_credentials()
    if not secret_key:
        raise ValueError("SYNC_SECRET_KEY가 설정되지 않았습니다.")

    f = Fernet(secret_key.encode("utf-8"))
    json_bytes = json.dumps(data_dict, ensure_ascii=False, default=str).encode("utf-8")
    encrypted_bytes = f.encrypt(json_bytes)
    return encrypted_bytes.decode("utf-8")


def decrypt_payload(ciphertext_str: str, secret_key: Optional[str] = None) -> Dict[str, Any]:
    """암호화된 문자열을 복호화하여 데이터 딕셔너리로 반환합니다."""
    if Fernet is None:
        raise RuntimeError("cryptography 패키지가 설치되지 않았습니다. pip install cryptography")

    if not secret_key:
        secret_key, _, _ = get_sync_credentials()
    if not secret_key:
        raise ValueError("SYNC_SECRET_KEY가 설정되지 않았습니다.")

    f = Fernet(secret_key.encode("utf-8"))
    decrypted_bytes = f.decrypt(ciphertext_str.strip().encode("utf-8"))
    return json.loads(decrypted_bytes.decode("utf-8"))


def create_dashboard_snapshot(account_data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    현재 계좌 요약과 모든 전략의 실시간 성과를 직렬화 가능한 스냅샷으로 생성합니다.
    account_data가 제공되면 중복 조회를 방지하기 위해 이를 재사용합니다.
    """
    if account_data is None:
        account_data = get_total_account_summary()
    active_strategies = get_all_active_strategies()

    # DataFrame을 직렬화 가능한 dict 리스트로 변환 및 불필요한 대용량 내부 캐시 필터링
    serializable_strategies = []
    for strat in active_strategies:
        s_copy = dict(strat)
        trades_df = s_copy.get("trades_df")
        if isinstance(trades_df, pd.DataFrame):
            # 타임라인 및 최근 내역 렌더링에 필요한 최근 100건으로 슬라이싱하여 경량화
            s_copy["trades_list"] = trades_df.head(100).to_dict(orient="records")
        else:
            s_copy["trades_list"] = []
        s_copy.pop("trades_df", None)

        # runtime_state 경량화: 대시보드 미사용 대용량 캐시(_reconciled_asks 등) 제거
        r_state = s_copy.get("runtime_state")
        if isinstance(r_state, dict):
            rs_clean = dict(r_state)
            rs_clean.pop("_reconciled_asks", None)
            rs_clean.pop("_reconciled_bids", None)
            # bear_limit_buys는 미체결 대기 주문(state == 'wait')만 유지하여 수백 개 누적 방지
            if "bear_limit_buys" in rs_clean and isinstance(rs_clean["bear_limit_buys"], dict):
                rs_clean["bear_limit_buys"] = {
                    k: v for k, v in rs_clean["bear_limit_buys"].items()
                    if isinstance(v, dict) and v.get("state") == "wait"
                }
            s_copy["runtime_state"] = rs_clean

        serializable_strategies.append(s_copy)

    # G60 서브포켓 실거래 데이터 포함 (ETH, SOL, XRP)
    pocket_states = {}
    for p_file in SNAPSHOT_DIR.parent.glob("pocket_grid_state_KRW_*.json"):
        try:
            with open(p_file, "r", encoding="utf-8") as pf:
                m_key = p_file.stem.replace("pocket_grid_state_", "").replace("_", "-")
                pocket_states[m_key] = json.load(pf)
        except Exception:
            pass

    pocket_trades_list = []
    pocket_db_file = SNAPSHOT_DIR.parent / "pocket_trade_history.db"
    if pocket_db_file.exists():
        try:
            conn = sqlite3.connect(str(pocket_db_file), timeout=2.0)
            p_trades_df = pd.read_sql_query(
                "SELECT id, timestamp, market, side, action, price, volume, cost_or_revenue, fee, pnl, level, strategy FROM pocket_trades ORDER BY id DESC LIMIT 100",
                conn
            )
            conn.close()
            pocket_trades_list = p_trades_df.to_dict(orient="records")
        except Exception:
            pass

    pocket_grid_data = {
        "states": pocket_states,
        "trades_list": pocket_trades_list
    }

    # 가상 매매 (Paper Trading) 데이터 포함 (XRP, ETH, SOL 등 다중 마켓 자동 탐색)
    paper_states = {}
    for p_file in SNAPSHOT_DIR.parent.glob("paper_grid_state_KRW_*.json"):
        try:
            with open(p_file, "r", encoding="utf-8") as pf:
                m_key = p_file.stem.replace("paper_grid_state_", "").replace("_", "-")
                paper_states[m_key] = json.load(pf)
        except Exception:
            pass
    paper_trading_data = {"states": paper_states}
    if "KRW-XRP" in paper_states:
        paper_trading_data["state"] = paper_states["KRW-XRP"]
    paper_scalper_file = SNAPSHOT_DIR.parent / "paper_scalper_state.json"
    paper_db_file = SNAPSHOT_DIR.parent / "paper_trade_history.db"
    if paper_scalper_file.exists():
        try:
            with open(paper_scalper_file, "r", encoding="utf-8") as pf:
                paper_trading_data["scalper_state"] = json.load(pf)
        except Exception:
            pass
    if paper_db_file.exists():
        try:
            conn = sqlite3.connect(str(paper_db_file), timeout=2.0)
            p_trades_df = pd.read_sql_query("SELECT id, timestamp, market, strategy, side, action, price, volume, cost_or_revenue, pnl FROM paper_trades ORDER BY id DESC LIMIT 50", conn)
            conn.close()
            paper_trading_data["trades_list"] = p_trades_df.to_dict(orient="records")
        except Exception:
            pass

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return {
        "version": 1,
        "updated_at": now_str,
        "sync_timestamp": time.time(),
        "account_data": account_data,
        "active_strategies": serializable_strategies,
        "paper_trading": paper_trading_data,
        "pocket_grid": pocket_grid_data
    }


def save_local_snapshot(snapshot: Dict[str, Any]) -> str:
    """스냅샷을 로컬 파일(data/latest_snapshot.json)에 저장합니다."""
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    with open(SNAPSHOT_FILE, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2, default=str)
    return str(SNAPSHOT_FILE)


def load_local_snapshot() -> Optional[Dict[str, Any]]:
    """로컬 파일(data/latest_snapshot.json)에서 스냅샷을 읽어옵니다."""
    if not SNAPSHOT_FILE.exists():
        return None
    try:
        with open(SNAPSHOT_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[WARN] 로컬 스냅샷 읽기 실패: {e}")
        return None


def _get_or_create_sync_issue(repo: str, token: str) -> Optional[int]:
    """GitHub 저장소에서 동기화용 Issue 번호를 조회하거나 신규 생성합니다."""
    global _CACHED_ISSUE_NUMBER
    if _CACHED_ISSUE_NUMBER is not None:
        return _CACHED_ISSUE_NUMBER

    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "AutoBot-Sync"
    }

    # 1. Issue #1 우선 확인 (기본 동기화 Issue)
    try:
        url_1 = f"https://api.github.com/repos/{repo}/issues/1"
        resp_1 = requests.get(url_1, headers=headers, timeout=10)
        if resp_1.status_code == 200:
            if resp_1.json().get("title") == SYNC_ISSUE_TITLE:
                _CACHED_ISSUE_NUMBER = 1
                return 1
    except Exception:
        pass

    # 2. 전체 목록에서 동기화 Issue 검색
    try:
        url = f"https://api.github.com/repos/{repo}/issues?state=all&per_page=30"
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            for issue in resp.json():
                if issue.get("title") == SYNC_ISSUE_TITLE:
                    _CACHED_ISSUE_NUMBER = issue.get("number")
                    return _CACHED_ISSUE_NUMBER

        # 3. 없으면 신규 생성
        create_url = f"https://api.github.com/repos/{repo}/issues"
        payload = {
            "title": SYNC_ISSUE_TITLE,
            "body": "INITIALIZING_SYNC_DATA"
        }
        create_resp = requests.post(create_url, headers=headers, json=payload, timeout=10)
        if create_resp.status_code in [200, 201]:
            _CACHED_ISSUE_NUMBER = create_resp.json().get("number")
            print(f"[SYNC] 신규 동기화 Issue #{_CACHED_ISSUE_NUMBER} 생성 완료")
            return _CACHED_ISSUE_NUMBER
        else:
            global _LAST_PUSH_WARN_TIME
            now = time.time()
            if now - _LAST_PUSH_WARN_TIME > 300:
                _LAST_PUSH_WARN_TIME = now
                if create_resp.status_code == 401:
                    print(f"[WARN] 동기화 Issue 접근 실패 (401 Bad credentials): .env의 GITHUB_TOKEN이 만료되었거나 유효하지 않습니다.")
                else:
                    print(f"[WARN] 동기화 Issue 생성 실패: {create_resp.status_code} {create_resp.text[:200]}")
    except Exception as e:
        print(f"[WARN] GitHub Issue 조회/생성 중 오류: {e}")

    return None


def push_snapshot_to_cloud(snapshot: Optional[Dict[str, Any]] = None, account_data: Optional[Dict[str, Any]] = None) -> bool:
    """
    현재 스냅샷을 생성하여 로컬 파일에 저장하고, GitHub Issue에 암호화하여 푸시합니다.
    진행 중인 동기화 작업이 있을 경우 블로킹되지 않고 즉시 스킵(Non-blocking Coalesce)합니다.
    """
    if not _SYNC_LOCK.acquire(blocking=False):
        return False
    try:
        if snapshot is None:
            snapshot = create_dashboard_snapshot(account_data=account_data)

        # 1. 로컬 저장 (백업 및 로컬 오프라인 뷰어용)
        save_local_snapshot(snapshot)

        # 2. 자격증명 확인
        secret_key, gh_token, gh_repo = get_sync_credentials()
        if not secret_key or not gh_token:
            print("[SYNC] SYNC_SECRET_KEY 또는 GITHUB_TOKEN 부재로 클라우드 푸시를 건너뜁니다.")
            return False

        # 3. 페이로드 암호화
        encrypted_token = encrypt_payload(snapshot, secret_key)

        # 4. GitHub Issue 조회/생성
        issue_num = _get_or_create_sync_issue(gh_repo, gh_token)
        if not issue_num:
            return False

        # 5. Issue 본문 덮어쓰기 (PATCH) - 커밋 미생성
        headers = {
            "Authorization": f"token {gh_token}",
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "AutoBot-Sync"
        }
        patch_url = f"https://api.github.com/repos/{gh_repo}/issues/{issue_num}"
        patch_resp = requests.patch(patch_url, headers=headers, json={"body": encrypted_token}, timeout=25)

        if patch_resp.status_code == 200:
            return True
        else:
            global _LAST_PUSH_WARN_TIME
            now = time.time()
            if now - _LAST_PUSH_WARN_TIME > 300:  # 5분에 1회만 경고 출력
                _LAST_PUSH_WARN_TIME = now
                if patch_resp.status_code == 401:
                    print(f"[WARN] 클라우드 동기화 실패 (401 Bad credentials): .env의 GITHUB_TOKEN이 만료되었습니다. 새 GitHub Token을 발급받아 갱신해주세요.")
                else:
                    print(f"[WARN] 클라우드 동기화 실패 ({patch_resp.status_code}): {patch_resp.text[:200]}")
            return False
    except Exception as e:
        print(f"[WARN] push_snapshot_to_cloud 예외 발생: {e}")
        return False
    finally:
        _SYNC_LOCK.release()


def fetch_snapshot_from_cloud() -> Tuple[Optional[Dict[str, Any]], str]:
    """
    클라우드(GitHub Issue)에서 암호화된 최신 스냅샷을 가져와 복호화합니다.
    공개 저장소이므로 토큰이 없거나 만료(401)되어도 비인증 GET으로 자동 폴백하여 조회합니다.
    반환: (snapshot_dict, status_message)
    """
    secret_key, gh_token, gh_repo = get_sync_credentials()

    if not secret_key:
        return None, "SYNC_SECRET_KEY가 설정되지 않았습니다."

    def _do_get(url: str, token: Optional[str]) -> Tuple[Optional[int], Optional[requests.Response]]:
        h = {
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "AutoBot-Sync-Viewer"
        }
        if token:
            h["Authorization"] = f"token {token}"
        try:
            r = requests.get(url, headers=h, timeout=25)
            if r.status_code == 401 and token:
                # 토큰 만료 또는 인증 실패 시 비인증(공개 저장소 조회) 자동 폴백
                h.pop("Authorization", None)
                r = requests.get(url, headers=h, timeout=25)
            return r.status_code, r
        except Exception:
            return None, None

    try:
        issue_body = None
        global _CACHED_ISSUE_NUMBER
        target_num = _CACHED_ISSUE_NUMBER or 1

        # 1. 대상 Issue (기본 #1 또는 캐시된 번호) 직접 조회
        url = f"https://api.github.com/repos/{gh_repo}/issues/{target_num}"
        status_code, resp = _do_get(url, gh_token)
        if status_code == 200 and resp:
            data = resp.json()
            if data.get("title") == SYNC_ISSUE_TITLE:
                _CACHED_ISSUE_NUMBER = target_num
                issue_body = data.get("body")

        # 2. 만약 #1이 동기화 Issue가 아니라면 전체 목록에서 탐색
        if not issue_body:
            url = f"https://api.github.com/repos/{gh_repo}/issues?state=all&per_page=30"
            status_code, resp = _do_get(url, gh_token)
            if status_code == 200 and resp:
                for issue in resp.json():
                    if issue.get("title") == SYNC_ISSUE_TITLE:
                        _CACHED_ISSUE_NUMBER = issue.get("number")
                        issue_body = issue.get("body")
                        break
            else:
                # 목록 조회 실패 시 로컬 파일 폴백 시도
                local = load_local_snapshot()
                if local:
                    return local, f"로컬 스냅샷 (클라우드 조회 실패 HTTP {status_code})"
                return None, f"GitHub Issue 조회 실패 (HTTP {status_code})"

        if not issue_body or issue_body == "INITIALIZING_SYNC_DATA":
            local = load_local_snapshot()
            if local:
                return local, "로컬 스냅샷 (클라우드 데이터 대기 중)"
            return None, "클라우드에 아직 등록된 동기화 데이터가 없습니다."

        # 복호화
        snapshot = decrypt_payload(issue_body, secret_key)
        return snapshot, "SUCCESS"
    except Exception as e:
        local = load_local_snapshot()
        if local:
            return local, f"로컬 스냅샷 (클라우드 조회 오류: {e})"
        return None, f"동기화 데이터 복호화 실패: {e}"


def trigger_snapshot_sync():
    """
    실거래 체결(매수/매도/손절/익절) 이벤트 발생 시 즉시 계좌 스냅샷 및 클라우드 동기화를 트리거합니다.
    백그라운드 스레드가 가동 중이면 이벤트를 신호(_SYNC_EVENT)로 전달하여,
    체결이 몰릴 때 발생하는 중복 스레드 생성과 API 과다 호출을 단일 루프로 병합(coalesce) 처리합니다.
    """
    import sys
    if "unittest" in sys.modules or os.getenv("TESTING"):
        return

    global _BG_THREAD_STARTED
    if _BG_THREAD_STARTED:
        _SYNC_EVENT.set()
    else:
        # 백그라운드 스레드가 없는 환경(단독 테스트 등) 안전 폴백
        def _fallback():
            if _SYNC_LOCK.locked():
                return
            push_snapshot_to_cloud()
        threading.Thread(target=_fallback, daemon=True).start()


def start_background_sync_thread(interval_sec: int = 30):
    """
    자동매매 봇 내부에서 백그라운드 스레드로 주기적 스냅샷 동기화를 실행합니다.
    실거래 이벤트 수신 시 즉시 깨어나며 연쇄 체결은 1초 디바운스로 병합 처리합니다.
    """
    global _BG_THREAD_STARTED
    if _BG_THREAD_STARTED:
        return
    _BG_THREAD_STARTED = True

    def _sync_loop():
        print(f"📦 [SYNC] 클라우드 스냅샷 자동 동기화 스레드 시작 (주기: {interval_sec}초)")
        # 시작 직후 최초 1회 즉시 동기화
        time.sleep(5)
        push_snapshot_to_cloud()

        while True:
            try:
                # interval_sec 동안 대기하되, 실거래 체결 이벤트(_SYNC_EVENT) 수신 시 즉시 깨어남
                triggered = _SYNC_EVENT.wait(timeout=interval_sec)
                if triggered:
                    # 연쇄 거래 발생 시 API 호출 폭주 방지용 1초 디바운스 병합
                    time.sleep(1.0)
                    _SYNC_EVENT.clear()

                push_snapshot_to_cloud()
            except Exception as e:
                print(f"[WARN] 동기화 루프 오류: {e}")
                time.sleep(10)

    t = threading.Thread(target=_sync_loop, daemon=True, name="SnapshotSyncThread")
    t.start()
