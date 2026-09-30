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


SQUAD_STAGE_MULTIPLIERS = {1: 1, 2: 1, 3: 2, 4: 4}


def get_existing_stages_from_state(state: dict) -> List[tuple]:
    """
    Extract active (squad_id, squad_stage) pairs from state tranches and waiting limit buys.
    Legacy/unassigned tranches without squad_id can infer stage from step for order sequence,
    while exit logic continues to quarantine them.
    """
    stages = []
    for idx, t in enumerate(state.get("tranches", [])):
        sid = t.get("squad_id")
        stage = t.get("squad_stage")
        if sid is not None and stage is not None and int(sid) > 0 and int(stage) > 0:
            stages.append((int(sid), int(stage)))
        elif not sid:
            # Fallback for tranches created before squad tagging to maintain correct order progression
            k = int(t.get("step", idx + 1))
            if k > 0:
                s_id = (k - 1) // 4 + 1
                s_stg = (k - 1) % 4 + 1
                stages.append((s_id, s_stg))

    for uuid, rec in state.get("bear_limit_buys", {}).items():
        if rec.get("state") == "wait":
            sid = rec.get("squad_id")
            stage = rec.get("squad_stage")
            if sid is not None and stage is not None and int(sid) > 0 and int(stage) > 0:
                stages.append((int(sid), int(stage)))

    return stages


def get_next_squad_stages(
    existing_stages: List[tuple],
    count: int = 1,
    max_steps: int = 9999,
    legacy_count: int = 0
) -> List[Dict[str, int]]:
    """
    Determine the next `count` sequential squad stages based on the highest existing (squad_id, squad_stage).
    Each squad contains stages 1, 2, 3, 4 with multipliers 1, 1, 2, 4.
    Returns list of dicts: [{'squad_id': s_id, 'squad_stage': stage, 'units': mult}, ...]
    """
    results = []
    current_stages = list(existing_stages)

    for _ in range(count):
        if not current_stages:
            next_sid = 1
            next_stage = 1
        else:
            max_sid, max_stage = max(current_stages)
            if max_stage < 4:
                next_sid = max_sid
                next_stage = max_stage + 1
            else:
                next_sid = max_sid + 1
                next_stage = 1

        stage_idx = legacy_count + (next_sid - 1) * 4 + next_stage
        if stage_idx > max_steps:
            break
        # Also enforce max squads if max_steps corresponds to squads (e.g. SOL 16 steps = 4 squads)
        max_squads = max(1, max_steps // 4)
        if next_sid > max_squads and max_steps <= 16:
            break

        units = SQUAD_STAGE_MULTIPLIERS.get(next_stage, 1)
        results.append({
            "squad_id": next_sid,
            "squad_stage": next_stage,
            "units": units
        })
        current_stages.append((next_sid, next_stage))

    return results


def calculate_new_buy_prices(
    avg_buy_price: float = None, 
    existing_orders: Optional[List[Any]] = None,
    max_steps: int = 4,
    current_step: int = 0,
    multipliers: Optional[List[int]] = None,
    **kwargs
) -> List[Dict]:
    """
    스쿼드 기반 매수 가격 및 수량(Unit 배수)을 계산합니다.
    - 1 스쿼드 = 1-1-2-4 Unit (4개 차수)
    - ETH: 예수금 한도 내 무제한 스쿼드 순환 (Squad 1, Squad 2, Squad 3...)
    - SOL: 최대 16차수 (4개 스쿼드) 캡
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
                entry = {'price': p, 'units': u}
                if item.get('squad_id') is not None:
                    entry['squad_id'] = int(item['squad_id'])
                if item.get('squad_stage') is not None:
                    entry['squad_stage'] = int(item['squad_stage'])
                normalized_orders.append(entry)
            elif isinstance(item, (int, float)):
                normalized_orders.append({'price': float(item), 'units': 1})

    # 상태 및 기존 스쿼드 단계 추출
    state = kwargs.get('state')
    existing_stages = []
    legacy_count = 0
    if state and isinstance(state, dict):
        existing_stages = get_existing_stages_from_state(state)
        legacy_count = len([t for t in state.get("tranches", []) if not t.get("squad_id") or int(t.get("squad_id", 0)) <= 0])
    else:
        # state가 없는 경우 kwargs 및 existing_orders에서 추출
        if kwargs.get('tranches'):
            for t in kwargs['tranches']:
                sid = t.get('squad_id')
                stg = t.get('squad_stage')
                if sid is not None and stg is not None and int(sid) > 0 and int(stg) > 0:
                    existing_stages.append((int(sid), int(stg)))
                else:
                    legacy_count += 1
        for o in normalized_orders:
            sid = o.get('squad_id')
            stg = o.get('squad_stage')
            if sid is not None and stg is not None and int(sid) > 0 and int(stg) > 0:
                existing_stages.append((int(sid), int(stg)))

        if not existing_stages and current_step > 0:
            # 하위 호환: squad 정보가 없는 경우 current_step을 기준으로 추론
            for k in range(1, current_step + 1):
                sid = (k - 1) // 4 + 1
                stg = (k - 1) % 4 + 1
                existing_stages.append((sid, stg))

    # Case 3: 매도 완료 후 신규/재진입 (평단가 기준 단계별 예약)
    if not normalized_orders:
        orders = []
        down_factor = 0.96
        num_to_create = min(3, max(0, max_steps - (legacy_count + len(existing_stages))))
        planned_stages = get_next_squad_stages(
            existing_stages=existing_stages,
            count=num_to_create,
            max_steps=max_steps,
            legacy_count=legacy_count
        )
        cum_factor = 1.0
        for s_info in planned_stages:
            cum_factor *= down_factor
            target_p = adjust_price_to_tick(avg_buy_price * cum_factor, method="floor")
            orders.append({
                'price': target_p,
                'units': s_info['units'],
                'squad_id': s_info['squad_id'],
                'squad_stage': s_info['squad_stage']
            })
        return orders

    # Case 4: 매수 체결 시 (미체결 주문 기준 추가 주문)
    num_existing = len(normalized_orders)
    allowed_new = max_steps - (legacy_count + len(existing_stages))
    if allowed_new <= 0:
        return []

    num_to_create = min(max(1, 3 - num_existing), allowed_new)
    planned_stages = get_next_squad_stages(
        existing_stages=existing_stages,
        count=num_to_create,
        max_steps=max_steps,
        legacy_count=legacy_count
    )
    if not planned_stages:
        return []

    lowest_order = min(normalized_orders, key=lambda x: x['price'])
    lowest_price = lowest_order['price']

    orders = []
    down_factor = 0.96
    cum_factor = 1.0
    for s_info in planned_stages:
        cum_factor *= down_factor
        target_p = adjust_price_to_tick(lowest_price * cum_factor, method="floor")
        orders.append({
            'price': target_p,
            'units': s_info['units'],
            'squad_id': s_info['squad_id'],
            'squad_stage': s_info['squad_stage']
        })
    return orders

