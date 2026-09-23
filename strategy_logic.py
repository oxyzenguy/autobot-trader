"""
전략 로직 모듈 (Legacy 호환성 유지)
현재 프로젝트의 최신 실전 전략은 strategy/matingale2x_logic.py 및 strategy/hybrid_trader.py를 사용합니다.
본 모듈은 과거 레거시 스크립트와의 하위 호환성을 위해 유지됩니다.
"""
from typing import List, Dict, Optional
import math
from strategy.matingale2x_logic import adjust_price_to_tick as modern_adjust_price_to_tick


def adjust_price_to_tick(price: float) -> float:
    """가격을 거래소 호가 단위에 맞춰 조정하고 소수점 제거 (호환용)"""
    return modern_adjust_price_to_tick(price, method="floor")


def calculate_new_buy_prices(avg_buy_price: float, existing_orders: Optional[List[Dict]] = None) -> List[float]:
    """
    매수 가격 리스트를 계산합니다 (레거시 단일 가격 리스트 반환 호환용).
    
    Args:
        avg_buy_price: 현재 평균 매수가
        existing_orders: 미체결 매수 주문 리스트
        
    Returns:
        매수 가격 리스트 (1~3개)
    """
    if existing_orders is None or len(existing_orders) == 0:
        return [
            adjust_price_to_tick(avg_buy_price * 0.96),   # -4%
            adjust_price_to_tick(avg_buy_price * 0.92),   # -8%
            adjust_price_to_tick(avg_buy_price * 0.88),   # -12%
        ]
    
    num_existing = len(existing_orders)
    
    if num_existing == 2:
        lowest_price = min(order['price'] for order in existing_orders)
        return [adjust_price_to_tick(lowest_price * 0.96)]
    
    elif num_existing == 1:
        base_price = existing_orders[0]['price']
        return [
            adjust_price_to_tick(base_price * 0.96),
            adjust_price_to_tick(base_price * 0.92),
        ]
    
    return [adjust_price_to_tick(avg_buy_price * 0.96)]
