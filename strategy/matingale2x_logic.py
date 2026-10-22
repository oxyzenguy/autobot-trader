"""
전략 로직 모듈
마틴게일 배수 전략: 1 → 1 → 2 → 4 Unit (총 8 Unit = 80,000 KRW)
"""
from typing import List, Dict, Optional, Any
import math
import pyupbit
from config import MARTINGALE_MULTIPLIERS


def get_upbit_tick_size(price: float) -> float:
    """
    업비트 거래소 원화(KRW) 마켓 공식 주문 가격 단위(호가 단위)
    """
    if price >= 2_000_000:
        return 1000.0
    elif price >= 1_000_000:
        return 1000.0
    elif price >= 500_000:
        return 500.0
    elif price >= 100_000:
        return 100.0
    elif price >= 50_000:
        return 50.0
    elif price >= 10_000:
        return 10.0
    elif price >= 5_000:
        return 5.0
    elif price >= 1_000:
        return 1.0
    elif price >= 100:
        return 1.0
    elif price >= 10:
        return 0.1
    elif price >= 1:
        return 0.01
    elif price >= 0.1:
        return 0.001
    elif price >= 0.01:
        return 0.0001
    else:
        return 0.00001


def adjust_price_to_tick(price: float, method: str = "floor") -> float:
    """
    가격을 업비트 거래소 공식 호가 단위에 맞춰 조정합니다.
    - method="floor": 매수가 계산 시 내림
    - method="ceil": 매도가 계산 시 올림
    - method="round": 반올림
    """
    tick = get_upbit_tick_size(price)
    if method == "ceil":
        func = math.ceil
    elif method == "round":
        func = round
    else:
        func = math.floor
    adjusted = func(price / tick) * tick
    if tick >= 1.0:
        return int(round(adjusted))
    return round(adjusted, 8)



SQUAD_STAGE_MULTIPLIERS = {1: 1, 2: 1, 3: 2, 4: 4}


def _parse_slot(sid: Any, stg: Any) -> Optional[tuple]:
    try:
        if sid is not None and stg is not None:
            s_int = int(sid)
            stg_int = int(stg)
            if s_int > 0 and stg_int > 0:
                return (s_int, stg_int)
    except (ValueError, TypeError):
        pass
    return None


def get_existing_stages_from_state(state: dict) -> Optional[List[tuple]]:
    """
    Extract active (squad_id, squad_stage) pairs from state tranches and waiting limit buys.

    Canonical Slot Resolution:
    1. Resolve each UUID's canonical slot before deduplication:
       - If a legacy tranche lacks tags but the owned order checkpoint has validated squad/stage tags,
         use that known identity rather than inferring from step.
       - If both sources have explicit but conflicting metadata, return None (conflict error).
       - If an untagged tranche has no matching order tags, fall back to step progression.
    2. Active waiting orders in bear_limit_buys:
       - Must have a resolvable (squad_id, squad_stage).
       - If an active owned waiting order has no resolvable identity, return None (unresolvable slot error).
       - An order linked to a tranche by UUID owns exactly one slot (deduplicated).
    3. If any conflict or unresolvable active slot is detected, returns None to block new buys.
    """
    if not state or not isinstance(state, dict):
        return []

    tranches = state.get("tranches", [])
    bear_buys = state.get("bear_limit_buys", {})

    # 1. Map order_uuid -> validated checkpoint slot from bear_limit_buys
    order_tags = {}
    active_wait_uuids = set()

    for uuid, rec in bear_buys.items():
        if not isinstance(rec, dict):
            continue
        slot = _parse_slot(rec.get("squad_id"), rec.get("squad_stage"))
        if slot:
            order_tags[uuid] = slot
        if rec.get("state") == "wait":
            active_wait_uuids.add(uuid)
            if not slot:
                # Active waiting order with NO tags in checkpoint:
                # Check if a tranche with this order_uuid has explicit tags:
                matching_tr = next((t for t in tranches if isinstance(t, dict) and t.get("order_uuid") == uuid), None)
                if matching_tr:
                    t_slot = _parse_slot(matching_tr.get("squad_id"), matching_tr.get("squad_stage"))
                    if t_slot:
                        order_tags[uuid] = t_slot
                        slot = t_slot
                if not slot:
                    # Active waiting order is genuinely unresolvable:
                    # Cannot establish ladder reservation -> block new buys
                    return None

    # 2. Resolve each tranche's canonical slot
    uuid_canonical_slot = {}
    resolved_tranche_slots = []

    for idx, t in enumerate(tranches):
        if not isinstance(t, dict):
            continue
        u = t.get("order_uuid")
        t_slot = _parse_slot(t.get("squad_id"), t.get("squad_stage"))
        o_slot = order_tags.get(u) if u else None

        if t_slot is not None and o_slot is not None:
            if t_slot != o_slot:
                # Explicit metadata conflict between tranche and order checkpoint!
                return None
            canonical = t_slot
        elif t_slot is not None:
            canonical = t_slot
        elif o_slot is not None:
            # Untagged legacy tranche with validated order checkpoint tags:
            # Use order checkpoint tags instead of guessing from step!
            canonical = o_slot
        else:
            # Untagged legacy tranche with no order checkpoint: fallback to step
            try:
                raw_step = t.get("step")
                k = int(raw_step) if raw_step is not None and int(raw_step) > 0 else (idx + 1)
            except (ValueError, TypeError):
                return None
            canonical = ((k - 1) // 4 + 1, (k - 1) % 4 + 1)

        if u:
            if u in uuid_canonical_slot and uuid_canonical_slot[u] != canonical:
                # Same UUID has conflicting slots across tranches
                return None
            uuid_canonical_slot[u] = canonical

        resolved_tranche_slots.append((u, canonical))

    # 3. Deduplicate occupied slots
    stages = []
    seen_slots = set()
    accounted_uuids = set()

    for u, slot in resolved_tranche_slots:
        if u:
            accounted_uuids.add(u)
        if slot not in seen_slots:
            seen_slots.add(slot)
            stages.append(slot)

    # 4. Include active waiting orders not already accounted for by a tranche
    for u in active_wait_uuids:
        if u in accounted_uuids:
            # Partial fill of order already represented in tranches -> owns 1 slot
            continue
        slot = order_tags.get(u)
        if slot is None:
            return None
        if slot not in seen_slots:
            seen_slots.add(slot)
            stages.append(slot)

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
    down_pct: Optional[float] = None,
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
        if existing_stages is None:
            return []
        legacy_count = len([t for t in state.get("tranches", []) if not t.get("squad_id") or int(t.get("squad_id", 0)) <= 0])
    else:
        # state가 없는 경우 kwargs 및 existing_orders에서 추출
        if kwargs.get('tranches'):
            for t in kwargs['tranches']:
                sid = t.get('squad_id')
                stg = t.get('squad_stage')
                if sid is not None and stg is not None and int(sid) > 0 and int(stg) > 0:
                    pair = (int(sid), int(stg))
                    if pair not in existing_stages:
                        existing_stages.append(pair)
                else:
                    legacy_count += 1
        for o in normalized_orders:
            sid = o.get('squad_id')
            stg = o.get('squad_stage')
            if sid is not None and stg is not None and int(sid) > 0 and int(stg) > 0:
                pair = (int(sid), int(stg))
                if pair not in existing_stages:
                    existing_stages.append(pair)

        if not existing_stages and current_step > 0:
            # 하위 호환: squad 정보가 없는 경우 current_step을 기준으로 추론
            for k in range(1, current_step + 1):
                sid = (k - 1) // 4 + 1
                stg = (k - 1) % 4 + 1
                existing_stages.append((sid, stg))

    # 물타기 하락 간격 (down_pct) 결정
    if down_pct is None:
        down_pct = kwargs.get('down_pct')
    if down_pct is None:
        market = (state.get("market") if isinstance(state, dict) else None) or kwargs.get("market")
        if market:
            from config import get_magic_split_down_pct
            down_pct = get_magic_split_down_pct(market)
        else:
            from config import MAGIC_SPLIT_DOWN_PCT
            down_pct = MAGIC_SPLIT_DOWN_PCT
    down_factor = 1.0 - float(down_pct)

    # Case 3: 매도 완료 후 신규/재진입 (평단가 기준 단계별 예약)
    if not normalized_orders:
        orders = []
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

