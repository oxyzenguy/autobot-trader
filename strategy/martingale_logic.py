"""
전략 로직 모듈
매수 가격을 계산하는 로직을 포함
"""
from typing import List, Dict
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
    return round(adjusted)  # 소수점 제거


def calculate_new_buy_prices(avg_buy_price: float, existing_orders: List[Dict] = None) -> List[float]:
    """
    매수 가격 리스트를 계산합니다.
    
    Args:
        avg_buy_price: 현재 평균 매수가
        existing_orders: 미체결 매수 주문 리스트 (Case4용)
        
    Returns:
        매수 가격 리스트 (1~3개)
    """
    
    # Case3: 매도 완료 후 재진입 (평단가 기준)
    if existing_orders is None or len(existing_orders) == 0:
        prices = [
            adjust_price_to_tick(avg_buy_price * 0.96),   # -4%
            adjust_price_to_tick(avg_buy_price * 0.92),   # -8%
            adjust_price_to_tick(avg_buy_price * 0.88),   # -12%
        ]
        return prices
    
    # Case4: 매수 체결 시 (미체결 주문 기준)
    num_existing = len(existing_orders)
    
    if num_existing == 2:
        # 미체결 2개: 더 낮은 가격에서 -4%
        lowest_price = min(order['price'] for order in existing_orders)
        prices = [
            adjust_price_to_tick(lowest_price * 0.96)   # -4%
        ]
        return prices
    
    elif num_existing == 1:
        # 미체결 1개: 해당 가격에서 -4%, -8%
        base_price = existing_orders[0]['price']
        prices = [
            adjust_price_to_tick(base_price * 0.96),    # -4%
            adjust_price_to_tick(base_price * 0.92),    # -8%
        ]
        return prices
    
    # 예상치 못한 경우: 기본 로직
    return [adjust_price_to_tick(avg_buy_price * 0.96)]
