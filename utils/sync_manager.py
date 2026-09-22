import os
import json
import time
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


def create_dashboard_snapshot() -> Dict[str, Any]:
    """
    현재 계좌 요약과 모든 전략의 실시간 성과를 직렬화 가능한 스냅샷으로 생성합니다.
    """
    account_data = get_total_account_summary()
    active_strategies = get_all_active_strategies()

    # DataFrame을 직렬화 가능한 dict 리스트로 변환
    serializable_strategies = []
    for strat in active_strategies:
        s_copy = dict(strat)
        trades_df = s_copy.get("trades_df")
        if isinstance(trades_df, pd.DataFrame):
            # 타임라인 테이블 렌더링에 필요한 컬럼 포함
            s_copy["trades_list"] = trades_df.to_dict(orient="records")
        else:
            s_copy["trades_list"] = []
        # DataFrame 원본 객체는 JSON 직렬화 불가하므로 제거
        s_copy.pop("trades_df", None)
        serializable_strategies.append(s_copy)

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return {
        "version": 1,
        "updated_at": now_str,
        "sync_timestamp": time.time(),
        "account_data": account_data,
        "active_strategies": serializable_strategies
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

    # 1. 기존 동기화 Issue 검색
    try:
        url = f"https://api.github.com/repos/{repo}/issues?state=all&per_page=30"
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            for issue in resp.json():
                if issue.get("title") == SYNC_ISSUE_TITLE:
                    _CACHED_ISSUE_NUMBER = issue.get("number")
                    return _CACHED_ISSUE_NUMBER

        # 2. 없으면 신규 생성
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
            print(f"[WARN] 동기화 Issue 생성 실패: {create_resp.status_code} {create_resp.text}")
    except Exception as e:
        print(f"[WARN] GitHub Issue 조회/생성 중 오류: {e}")

    return None


def push_snapshot_to_cloud(snapshot: Optional[Dict[str, Any]] = None) -> bool:
    """
    현재 스냅샷을 생성하여 로컬 파일에 저장하고, GitHub Issue에 암호화하여 푸시합니다.
    """
    try:
        if snapshot is None:
            snapshot = create_dashboard_snapshot()

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
        patch_resp = requests.patch(patch_url, headers=headers, json={"body": encrypted_token}, timeout=10)

        if patch_resp.status_code == 200:
            return True
        else:
            print(f"[WARN] 클라우드 동기화 실패 ({patch_resp.status_code}): {patch_resp.text[:200]}")
            return False
    except Exception as e:
        print(f"[WARN] push_snapshot_to_cloud 예외 발생: {e}")
        return False


def fetch_snapshot_from_cloud() -> Tuple[Optional[Dict[str, Any]], str]:
    """
    클라우드(GitHub Issue)에서 암호화된 최신 스냅샷을 가져와 복호화합니다.
    반환: (snapshot_dict, status_message)
    """
    secret_key, gh_token, gh_repo = get_sync_credentials()

    if not secret_key:
        return None, "SYNC_SECRET_KEY가 설정되지 않았습니다."

    headers = {
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "AutoBot-Sync-Viewer"
    }
    if gh_token:
        headers["Authorization"] = f"token {gh_token}"

    try:
        # Issue 검색 또는 Issue #1 직접 조회
        issue_body = None
        global _CACHED_ISSUE_NUMBER
        if _CACHED_ISSUE_NUMBER:
            url = f"https://api.github.com/repos/{gh_repo}/issues/{_CACHED_ISSUE_NUMBER}"
            resp = requests.get(url, headers=headers, timeout=10)
            if resp.status_code == 200:
                issue_body = resp.json().get("body")

        if not issue_body:
            # 전체 목록에서 탐색
            url = f"https://api.github.com/repos/{gh_repo}/issues?state=all&per_page=30"
            resp = requests.get(url, headers=headers, timeout=10)
            if resp.status_code == 200:
                for issue in resp.json():
                    if issue.get("title") == SYNC_ISSUE_TITLE:
                        _CACHED_ISSUE_NUMBER = issue.get("number")
                        issue_body = issue.get("body")
                        break
            else:
                return None, f"GitHub Issue 조회 실패 (HTTP {resp.status_code})"

        if not issue_body or issue_body == "INITIALIZING_SYNC_DATA":
            # 로컬 파일 폴백 시도
            local = load_local_snapshot()
            if local:
                return local, "로컬 스냅샷 (클라우드 데이터 대기 중)"
            return None, "클라우드에 아직 등록된 동기화 데이터가 없습니다."

        # 복호화
        snapshot = decrypt_payload(issue_body, secret_key)
        return snapshot, "SUCCESS"
    except Exception as e:
        # 실패 시 로컬 파일 폴백
        local = load_local_snapshot()
        if local:
            return local, f"로컬 스냅샷 (클라우드 조회 오류: {e})"
        return None, f"동기화 데이터 복호화 실패: {e}"


def start_background_sync_thread(interval_sec: int = 30):
    """
    자동매매 봇 내부에서 백그라운드 스레드로 주기적 스냅샷 동기화를 실행합니다.
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
                time.sleep(interval_sec)
                push_snapshot_to_cloud()
            except Exception as e:
                print(f"[WARN] 동기화 루프 오류: {e}")
                time.sleep(10)

    t = threading.Thread(target=_sync_loop, daemon=True, name="SnapshotSyncThread")
    t.start()
