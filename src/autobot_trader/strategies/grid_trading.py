import pandas as pd
import json
import os
from datetime import datetime

# 설정값
GRID_COUNT = 10
STATE_FILE = "grid_state.json"
COIN_UNIT = 0.001  # 매수 수량

def get_dynamic_grid(df: pd.DataFrame):
    """최근 30일 기준 동적 그리드 구간 계산"""
    grid_low = df["low"].rolling(30).min().iloc[-1]
    grid_high = df["high"].rolling(30).max().iloc[-1]
    grid_step = (grid_high - grid_low) / GRID_COUNT
    grid_levels = [grid_low + i * grid_step for i in range(GRID_COUNT + 1)]
    return grid_low, grid_high, grid_step, grid_levels

def load_state():
    """보유 상태 불러오기"""
    if not os.path.exists(STATE_FILE):
        return {}
    with open(STATE_FILE, "r") as f:
        return json.load(f)

def save_state(state: dict):
    """보유 상태 저장"""
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def get_current_level(price: float, grid_low: float, grid_step: float):
    level = int((price - grid_low) // grid_step)
    return max(0, min(GRID_COUNT - 1, level))

def get_grid_trading_signal(df: pd.DataFrame):
    """그리드 트레이딩 시그널 생성"""
    try:
        if df is None or len(df) < 30:
            print("[grid_trading] 데이터 부족 또는 None")
            return None

        price = df["close"].iloc[-1]
        grid_low, grid_high, grid_step, grid_levels = get_dynamic_grid(df)
        level = get_current_level(price, grid_low, grid_step)
        state = load_state()

        signals = []

        # 매수 조건
        if f"grid_{level}" not in state and level <= 3:
            signals.append({
                "action": "buy",
                "level": level,
                "price": round(grid_levels[level], 0),
                "quantity": COIN_UNIT
            })
            state[f"grid_{level}"] = {
                "buy_price": round(grid_levels[level], 0),
                "quantity": COIN_UNIT,
                "timestamp": datetime.now().isoformat()
            }

        # 매도 조건 (레벨 4 이상 차이날 경우)
        for key in list(state.keys()):
            hold_level = int(key.split("_")[1])
            if level >= hold_level + 4:
                buy_price = state[key]["buy_price"]
                quantity = state[key]["quantity"]
                signals.append({
                    "action": "sell",
                    "level": hold_level,
                    "price": price,
                    "quantity": quantity,
                    "buy_price": buy_price
                })
                del state[key]

        save_state(state)
        return signals if signals else None

    except Exception as e:
        print(f"[grid_trading] 전략 실행 오류: {e}")
        return None
