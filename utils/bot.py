import os
import requests
from pathlib import Path
from dotenv import load_dotenv

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(dotenv_path=ENV_PATH)

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")


def send_message(text: str):
    """텔레그램 메시지를 발송합니다."""
    if not TOKEN or not CHAT_ID:
        return None

    try:
        url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
        payload = {
            "chat_id": CHAT_ID,
            "text": text,
            "parse_mode": "HTML"
        }
        response = requests.post(url, data=payload, timeout=5)
        return response
    except Exception as e:
        print(f"[WARN] 텔레그램 메시지 전송 실패: {e}")
        return None

