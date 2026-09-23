"""
전략 로직 모듈
마틴게일 배수 전략: 1 → 1 → 2 → 4 Unit (총 8 Unit = 80,000 KRW)
"""
from typing import List, Dict, Optional, Any
import math
import pyupbit
from config import MARTINGALE_MULTIPLIERS


def adjust_price_to_tick(price: float, method: str = "floor") -> float:
    """
    가격을 업비트 거래소 공식 호가 단위에 맞춰 조정합니다.
    - method="floor": 매수가 계산 시 내림
    - method="ceil": 매도가 계산 시 올림
    """
    try:
        return pyupbit.get_tick_size(price, method=method)
    except Exception:
        # Fallback 호가 단위 계산
        if price >= 2_000_000:
            tick = 1000
        elif price >= 1_000_000:
            tick = 500
        elif price >= 500_000:
            tick = 100
        elif price >= 100_000:
            tick = 50
        elif price >= 10_000:
            tick = 10
        elif price >= 1_000:
            tick = 1
        elif price >= 100:
            tick = 0.1
        elif price >= 10:
            tick = 0.01
        else:
            tick = 0.001

        if method == "ceil":
            func = math.ceil
        elif method == "round":
            func = round
        else:
            func = math.floor
        return func(price / tick) * tick


def calculate_new_buy_prices(
    avg_buy_price: float = None, 
    existing_orders: Optional[List[Any]] = None,
    max_steps: int = 4,
    current_step: int = 0,
    multipliers: Optional[List[int]] = None,
    **kwargs
) -> List[Dict]:
    """
    매수 가격 및 수량(Unit 배수)을 계산합니다.
    - max_steps: 최대 허용 차수 (SOL: 4회차 캡 후 홀딩, ETH: 최대 10회차 확장)
    - current_step: 현재 보유 중인 차수
    """
    if current_step >= max_steps:
        return []

    if multipliers is None:
        multipliers = MARTINGALE_MULTIPLIERS

    if avg_buy_price is None:
        avg_buy_price = kwargs.get('avg_price', 0.0)
    if existing_orders is None:
        existing_orders = kwargs.get('open_buy_orders') or kwargs.get('buy_orders')

    # existing_orders 내부 요소 정규화
    normalized_orders: List[Dict] = []
    if existing_orders:
        for item in existing_orders:
            if isinstance(item, dict):
                p = float(item.get('price', 0))
                u = int(item.get('units', item.get('volume_unit', 1)))
                normalized_orders.append({'price': p, 'units': u})
            elif isinstance(item, (int, float)):
                normalized_orders.append({'price': float(item), 'units': 1})

    # Case3: 매도 완료 후 신규/재진입 (평단가 기준 단계별 예약)
    if not normalized_orders:
        orders = []
        down_factor = 0.96
        num_to_create = min(3, max_steps - max(1, current_step))
        cum_factor = 1.0
        start_step = max(1, current_step)
        for s in range(1, num_to_create + 1):
            cum_factor *= down_factor
            target_p = adjust_price_to_tick(avg_buy_price * cum_factor, method="floor")
            step_idx = start_step + s - 1
            unit_mult = multipliers[step_idx % len(multipliers)]
            orders.append({
                'price': target_p,
                'units': unit_mult
            })
        return orders

    # Case4: 매수 체결 시 (미체결 주문 기준 추가 주문)
    num_existing = len(normalized_orders)
    # 현재 보유 차수 + 미체결 주문 수가 최대 차수 이상이면 추가 주문 금지
    allowed_new = max_steps - (current_step + num_existing)
    if allowed_new <= 0:
        return []

    if num_existing >= 2:
        lowest_order = min(normalized_orders, key=lambda x: x['price'])
        lowest_price = lowest_order['price']
        current_units = lowest_order.get('units', 1)

        next_idx = current_step + num_existing
        next_units = multipliers[next_idx % len(multipliers)]

        orders = [
            {
                'price': adjust_price_to_tick(lowest_price * 0.96, method="floor"),
                'units': next_units
            }
        ]
        return orders[:allowed_new]

    elif num_existing == 1:
        # 미체결 1개: 다음 단계 추가 (최대 max_steps 제한)
        base_order = normalized_orders[0]
        base_price = base_order['price']

        next_idx_1 = current_step + num_existing
        next_units_1 = multipliers[next_idx_1 % len(multipliers)]
        next_idx_2 = next_idx_1 + 1
        next_units_2 = multipliers[next_idx_2 % len(multipliers)]

        orders = [
            {
                'price': adjust_price_to_tick(base_price * 0.96, method="floor"),
                'units': next_units_1
            },
            {
                'price': adjust_price_to_tick(base_price * 0.9216, method="floor"),
                'units': next_units_2
            },
        ]
        return orders[:allowed_new]

    # 예외 상황 fallback
    next_idx = current_step + num_existing
    fallback_units = multipliers[next_idx % len(multipliers)]
    return [{'price': adjust_price_to_tick(avg_buy_price * 0.96, method="floor"), 'units': fallback_units}][:allowed_new]

