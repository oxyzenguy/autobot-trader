import json
import os

STATE_FILE = "trading_state.json"

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    else:
        # 초기 상태 예시
        return {
            "budget": 300000,        # 초기 예산
            "position": None,        # 현재 포지션 정보(없으면 None)
            "last_trade_time": None  # 마지막 거래 시각 등 필요에 따라 추가
        }