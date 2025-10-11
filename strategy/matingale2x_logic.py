"""
전략 로직 모듈
마틴게일 배수 전략: 1 → 2 → 3 → 6 Unit
"""
from typing import List, Dict, Optional
import math


def adjust_price_to_tick(price: float) -> float:
    """가격을 거래소 호가 단위에 맞춰 조정하고 소수점 제거"""
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
        tick = 5
    elif price >= 100:
        tick = 1
    else:
        tick = 0.1
    
    adjusted = math.floor(price / tick) * tick
    return round(adjusted)


def calculate_new_buy_prices(
    avg_buy_price: float, 
    existing_orders: Optional[List[Dict]] = None
) -> List[Dict]:
    """
    매수 가격 및 수량(Unit 배수)을 계산합니다.
    
    마틴게일 전략:
    - 1차: 1 Unit (초기)
    - 2차: 2 Unit (평단가 -4%)
    - 3차: 3 Unit (2차 가격 -4%)
    - 4차: 6 Unit (3차 가격 -4%)
    
    Args:
        avg_buy_price: 현재 평균 매수가
        existing_orders: 미체결 매수 주문 리스트 (Case4용)
        
    Returns:
        매수 주문 리스트 [{'price': float, 'units': int}, ...]
    """
    
    # Case3: 매도 완료 후 재진입 (평단가 기준)
    if existing_orders is None or len(existing_orders) == 0:
        orders = [
            {
                'price': adjust_price_to_tick(avg_buy_price * 0.96),  # -4%
                'units': 2  # 2 Unit
            },
            {
                'price': adjust_price_to_tick(avg_buy_price * 0.9216),  # -4% -4% = -7.84%
                'units': 3  # 3 Unit
            },
            {
                'price': adjust_price_to_tick(avg_buy_price * 0.8847),  # -4% -4% -4% = -11.53%
                'units': 6  # 6 Unit
            },
        ]
        return orders
    
    # Case4: 매수 체결 시 (미체결 주문 기준)
    num_existing = len(existing_orders)
    
    if num_existing >= 2:
        # 미체결 2개 이상: 더 낮은 가격에서 -4% (다음 배수)
        lowest_order = min(existing_orders, key=lambda x: x['price'])
        lowest_price = lowest_order['price']
        
        # 현재 최하위 주문의 units를 확인하여 다음 배수 결정
        current_units = lowest_order.get('units', 1)
        
        # 배수 진행: 1 → 2 → 3 → 6 → 6 (최대 6 Unit)
        if current_units == 1:
            next_units = 2
        elif current_units == 2:
            next_units = 3
        elif current_units == 3:
            next_units = 6
        else:
            next_units = 6  # 최대 6 Unit 유지
        
        orders = [
            {
                'price': adjust_price_to_tick(lowest_price * 0.96),  # -4%
                'units': next_units
            }
        ]
        return orders
    
    elif num_existing == 1:
        # 미체결 1개: 다음 2단계 추가
        base_order = existing_orders[0]
        base_price = base_order['price']
        current_units = base_order.get('units', 1)
        
        # 다음 배수 결정
        if current_units == 1:
            next_units_1 = 2
            next_units_2 = 3
        elif current_units == 2:
            next_units_1 = 3
            next_units_2 = 6
        elif current_units == 3:
            next_units_1 = 6
            next_units_2 = 6
        else:
            next_units_1 = 6
            next_units_2 = 6
        
        orders = [
            {
                'price': adjust_price_to_tick(base_price * 0.96),    # -4%
                'units': next_units_1
            },
            {
                'price': adjust_price_to_tick(base_price * 0.9216),  # -8%
                'units': next_units_2
            },
        ]
        return orders
    
    # 예상치 못한 경우: 기본값
    return [{'price': adjust_price_to_tick(avg_buy_price * 0.96), 'units': 2}]
