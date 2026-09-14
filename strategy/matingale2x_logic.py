"""
전략 로직 모듈
마틴게일 배수 전략: 1 → 2 → 3 → 6 Unit
"""
from typing import List, Dict, Optional, Any
import math
import pyupbit


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
    **kwargs
) -> List[Dict]:
    """
    매수 가격 및 수량(Unit 배수)을 계산합니다.
    
    마틴게일 배수 물타기:
    - 1차: 1 Unit (초기 진입)
    - 2차: 2 Unit (평단가 -4%)
    - 3차: 3 Unit (2차 가격 -4%)
    - 4차: 6 Unit (3차 가격 -4%)
    
    Args:
        avg_buy_price: 현재 평균 매수가 (키워드 avg_price 도 호환 지원)
        existing_orders: 미체결 매수 주문 리스트 (키워드 open_buy_orders, buy_orders 도 호환 지원)
        
    Returns:
        매수 주문 리스트 [{'price': float, 'units': int}, ...]
    """
    # 호환성 지원: 인자명이 다르게 전달되더라도 유연하게 수용
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

    # Case3: 매도 완료 후 신규/재진입 (평단가 기준)
    if not normalized_orders:
        orders = [
            {
                'price': adjust_price_to_tick(avg_buy_price * 0.96, method="floor"),       # -4%
                'units': 2  # 2 Unit
            },
            {
                'price': adjust_price_to_tick(avg_buy_price * 0.9216, method="floor"),     # -4% -4% = -7.84%
                'units': 3  # 3 Unit
            },
            {
                'price': adjust_price_to_tick(avg_buy_price * 0.8847, method="floor"),     # -4% -4% -4% = -11.53%
                'units': 6  # 6 Unit
            },
        ]
        return orders

    # Case4: 매수 체결 시 (미체결 주문 기준 추가 주문)
    num_existing = len(normalized_orders)

    if num_existing >= 2:
        # 미체결 2개 이상: 더 낮은 가격에서 -4% (다음 배수)
        lowest_order = min(normalized_orders, key=lambda x: x['price'])
        lowest_price = lowest_order['price']
        current_units = lowest_order.get('units', 1)

        # 배수 진행: 1 → 2 → 3 → 6 → 6 (최대 6 Unit 유지)
        if current_units == 1:
            next_units = 2
        elif current_units == 2:
            next_units = 3
        elif current_units == 3:
            next_units = 6
        else:
            next_units = 6

        orders = [
            {
                'price': adjust_price_to_tick(lowest_price * 0.96, method="floor"),
                'units': next_units
            }
        ]
        return orders

    elif num_existing == 1:
        # 미체결 1개: 다음 2단계 추가
        base_order = normalized_orders[0]
        base_price = base_order['price']
        current_units = base_order.get('units', 1)

        if current_units == 1:
            next_units_1, next_units_2 = 2, 3
        elif current_units == 2:
            next_units_1, next_units_2 = 3, 6
        elif current_units == 3:
            next_units_1, next_units_2 = 6, 6
        else:
            next_units_1, next_units_2 = 6, 6

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
        return orders

    # 예외 상황 fallback
    return [{'price': adjust_price_to_tick(avg_buy_price * 0.96, method="floor"), 'units': 2}]
