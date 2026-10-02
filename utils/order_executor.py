"""
========================================================================================
🛡️ [AutoBot Trader] 실전 자가치유형 안전 주문 집행기 (Safe Order Executor)
========================================================================================
특징:
1. 주문 전 사전 검증 (Pre-Flight Checks):
   - 예수금 + 수수료(0.05%) 버퍼 사전 검증 (잔고 부족 에러 원천 차단)
   - 최소 주문 금액(5,000 KRW) 미달 사전 차단 (거절 에러 방지)
   - 호가 단위 및 수량 소수점(8자리 버림, 원화 정수형) 자동 규격화
2. 네트워크 탄력성 및 지수 백오프 (Network Resilience):
   - 일시적 502/504 Bad Gateway, Connection Timeout, 429 Rate Limit 자동 재시도
   - 일시 장애로 봇 쓰레드가 종료(Crash)되거나 무한 동결(Halt)되는 사태 방지
3. 자가 치유 체결 대조 및 자동 복구 (Self-Healing Auto-Reconciliation):
   - 체결 지연 시 5초 만에 성급히 멈추지 않고 최대 25~30초 점진적 추적
   - 미체결 지속 시 자동 취소(Auto-Cancel) 후 상태 롤백 ➔ 무중단 정상 거래 재개
   - 재시작 시 잔여 pending_order_uuid 자동 조회 및 원장 자동 동기화
========================================================================================
"""

import math
import time
import requests
from typing import Dict, Any, Optional, Tuple, Callable


MIN_ORDER_KRW = 5000.0        # 업비트 최소 주문 금액 (KRW)
DEFAULT_FEE_RATE = 0.0005     # 업비트 0.05% taker 수수료
MAX_NETWORK_RETRIES = 3       # 일시적 네트워크 예외 재시도 횟수
INITIAL_RETRY_DELAY = 0.5     # 재시도 초기 지연 시간(초)


class PreflightError:
    INSUFFICIENT_KRW = "INSUFFICIENT_KRW"
    UNDER_MIN_ORDER = "UNDER_MIN_ORDER"
    INSUFFICIENT_VOLUME = "INSUFFICIENT_VOLUME"
    INVALID_PRICE = "INVALID_PRICE"


def preflight_check_buy(
    market: str,
    amount_krw: float,
    available_krw: float,
    fee_rate: float = DEFAULT_FEE_RATE
) -> Tuple[bool, int, str]:
    """
    시장가 매수 전 검증:
    1. 최소 주문액(5,000원) 이상인지 확인
    2. 수수료를 포함한 가용 예수금이 충분한지 확인
    3. 업비트 시장가 매수 규격에 맞게 정수(int) 원화 금액으로 정제
    반환: (성공여부, 정제된 정수 금액, 실패사유)
    """
    clean_amount = int(math.floor(amount_krw))
    if clean_amount < MIN_ORDER_KRW:
        return False, clean_amount, f"주문 금액({clean_amount:,}원)이 최소 주문액({MIN_ORDER_KRW:,}원) 미만입니다."

    required_krw = clean_amount * (1.0 + fee_rate)
    if available_krw < required_krw:
        return (
            False,
            clean_amount,
            f"주문가능 예수금 부족 (필요: {required_krw:,.0f}원, 가용: {available_krw:,.0f}원)"
        )

    return True, clean_amount, ""


def preflight_check_sell(
    market: str,
    volume: float,
    current_price: float,
    available_volume: float
) -> Tuple[bool, float, str]:
    """
    시장가 매도 전 검증:
    1. 가용 보유 수량을 초과하지 않는지 확인 (부동소수점 오차 방지: 소수점 8자리 버림)
    2. 예상 체결 금액(수량 * 현재가)이 5,000원 이상인지 확인
    반환: (성공여부, 정제된 8자리 수량, 실패사유)
    """
    if volume <= 0 or current_price <= 0:
        return False, 0.0, "매도 수량 또는 현재가가 0 이하입니다."

    # 업비트 허용 최대 8자리 소수점 버림 처리
    clean_volume = math.floor(volume * 1e8) / 1e8
    clean_avail = math.floor(available_volume * 1e8) / 1e8

    if clean_volume > clean_avail:
        # 미세 오차(1e-8 수준)인 경우 가용 잔고 전체로 자동 클리핑
        if clean_volume - clean_avail <= 1e-7 and clean_avail > 0:
            clean_volume = clean_avail
        else:
            return False, clean_volume, f"가용 수량 부족 (요청: {clean_volume:.8f}, 가용: {clean_avail:.8f})"

    est_revenue = clean_volume * current_price
    if est_revenue < MIN_ORDER_KRW:
        return (
            False,
            clean_volume,
            f"예상 매도 금액({est_revenue:,.0f}원)이 업비트 최소 주문액({MIN_ORDER_KRW:,}원) 미만입니다."
        )

    return True, clean_volume, ""


def get_order_fill_metrics(order: dict, executed_volume: float) -> Tuple[float, float]:
    """체결된 주문에서 실제 가중평균 체결가 및 수수료 추출"""
    trades = order.get("trades", []) if isinstance(order, dict) else []
    total_funds = 0.0
    paid_fee = float(order.get("paid_fee", 0.0) or 0.0)

    if isinstance(trades, list) and executed_volume > 0:
        for t in trades:
            if isinstance(t, dict):
                try:
                    total_funds += float(t.get("funds", 0.0) or 0.0)
                except (TypeError, ValueError):
                    pass

    fill_price = total_funds / executed_volume if executed_volume > 0 and total_funds > 0 else 0.0
    return fill_price, paid_fee


def execute_market_order_resilient(
    upbit_client: Any,
    market: str,
    side: str,
    amount_or_volume: float,
    max_retries: int = MAX_NETWORK_RETRIES
) -> Tuple[Optional[dict], str]:
    """
    네트워크 장애(타임아웃, 502/504, 429)에 탄력적인 시장가 주문 전송기.
    - 일시적 통신 오류 시 지수 백오프로 최대 max_retries회 재시도.
    - 잔고 부족 등 비가역적 거절은 재시도 없이 즉시 거절 사유 반환.
    """
    last_err = ""
    for attempt in range(max_retries):
        try:
            if side == "bid":
                resp = upbit_client.buy_market_order(market, int(amount_or_volume))
            else:
                resp = upbit_client.sell_market_order(market, float(amount_or_volume))

            if isinstance(resp, dict):
                if resp.get("uuid"):
                    return resp, ""
                # 비가역적 오류 (예: 잔고 부족, 파라미터 에러)
                err_dict = resp.get("error", {})
                err_name = err_dict.get("name") if isinstance(err_dict, dict) else str(err_dict)
                err_msg = err_dict.get("message") if isinstance(err_dict, dict) else str(resp)
                
                # 429 Too Many Requests는 재시도 대상
                if "too_many_requests" in str(err_name).lower():
                    time.sleep(1.0 + attempt * 0.5)
                    continue

                return None, f"거래소 거절 ({err_name}): {err_msg}"
            else:
                last_err = f"알 수 없는 응답 형식: {resp}"

        except Exception as e:
            last_err = f"주문 전송 예외: {e}"

        time.sleep(INITIAL_RETRY_DELAY * (2 ** attempt))

    return None, f"주문 전송 재시도 초과 ({last_err})"


def poll_order_settlement(
    upbit_client: Any,
    order_uuid: str,
    max_wait_sec: float = 25.0,
    initial_interval: float = 1.0
) -> dict:
    """
    주문 체결 대기 및 지수 백오프 폴링 (최대 max_wait_sec).
    - 성급한 5초 타임아웃 방지.
    - 체결 완료(done) 시 체결가/수량 반환.
    - 미체결(wait) 지속 시 거래소 취소(cancel)를 시도하여 자가 치유 후 상태 반환.
    """
    start_time = time.time()
    interval = initial_interval
    last_state = "unknown"

    while time.time() - start_time < max_wait_sec:
        try:
            order = upbit_client.get_order(order_uuid)
        except Exception as e:
            order = None

        if isinstance(order, dict) and order.get("uuid") == order_uuid:
            last_state = order.get("state", "unknown")
            executed_vol = float(order.get("executed_volume", 0.0) or 0.0)

            # 1. 완전 체결 완료
            if last_state == "done":
                fill_price, fee = get_order_fill_metrics(order, executed_vol)
                return {
                    "status": "FILLED",
                    "uuid": order_uuid,
                    "volume": executed_vol,
                    "price": fill_price,
                    "fee": fee,
                    "state": "done"
                }

            # 2. 거래소 자체 취소 또는 거절
            if last_state in ("cancel", "reject"):
                if executed_vol > 0:
                    fill_price, fee = get_order_fill_metrics(order, executed_vol)
                    return {
                        "status": "PARTIAL_CANCELLED",
                        "uuid": order_uuid,
                        "volume": executed_vol,
                        "price": fill_price,
                        "fee": fee,
                        "state": last_state
                    }
                return {
                    "status": "CANCELLED",
                    "uuid": order_uuid,
                    "volume": 0.0,
                    "price": 0.0,
                    "state": last_state
                }

        time.sleep(interval)
        interval = min(2.5, interval * 1.3)

    # 3. 타임아웃 도달: 미체결 상태(wait)로 머물러 있는 경우 자가 취소 시도
    if last_state == "wait":
        try:
            cancel_resp = upbit_client.cancel_order(order_uuid)
            time.sleep(1.0)
            order_after_cancel = upbit_client.get_order(order_uuid)
            if isinstance(order_after_cancel, dict):
                exec_vol = float(order_after_cancel.get("executed_volume", 0.0) or 0.0)
                if exec_vol > 0:
                    fill_p, fee = get_order_fill_metrics(order_after_cancel, exec_vol)
                    return {
                        "status": "PARTIAL_CANCELLED",
                        "uuid": order_uuid,
                        "volume": exec_vol,
                        "price": fill_p,
                        "fee": fee,
                        "state": "cancel"
                    }
                else:
                    return {
                        "status": "CANCELLED_TIMEOUT",
                        "uuid": order_uuid,
                        "volume": 0.0,
                        "price": 0.0,
                        "reason": f"주문 체결 대기 시간 초과({max_wait_sec}s)로 인한 자동 취소 완료"
                    }
        except Exception as e:
            pass

    # 4. 취소도 실패하고 상태 확인이 불가능한 진정한 비상 사태
    return {
        "status": "HALT_REQUIRED",
        "uuid": order_uuid,
        "reason": f"주문 체결 확인 불가 및 취소 미확정 (최종 상태: {last_state})"
    }


def execute_safe_market_buy(
    upbit_client: Any,
    market: str,
    amount_krw: float,
    available_krw: float,
    on_accepted: Optional[Callable[[str], None]] = None,
    max_wait_sec: float = 25.0
) -> dict:
    """
    통합 안전 시장가 매수 함수:
    사전 검증 ➔ 주문 접수 (네트워크 재시도) ➔ 체결 추적 및 자가 치유
    """
    # 1. 사전 검증
    ok, clean_amt, err_msg = preflight_check_buy(market, amount_krw, available_krw)
    if not ok:
        return {"status": "PREFLIGHT_REJECTED", "reason": err_msg, "volume": 0.0, "price": 0.0}

    # 2. 주문 전송
    resp, send_err = execute_market_order_resilient(upbit_client, market, "bid", clean_amt)
    if not resp or not resp.get("uuid"):
        return {"status": "SUBMIT_FAILED", "reason": send_err, "volume": 0.0, "price": 0.0}

    order_uuid = resp["uuid"]
    if on_accepted:
        try:
            on_accepted(order_uuid)
        except Exception:
            pass

    # 3. 체결 대기 및 자가 치유 폴링
    return poll_order_settlement(upbit_client, order_uuid, max_wait_sec=max_wait_sec)


def execute_safe_market_sell(
    upbit_client: Any,
    market: str,
    volume: float,
    current_price: float,
    available_volume: float,
    on_accepted: Optional[Callable[[str], None]] = None,
    max_wait_sec: float = 25.0
) -> dict:
    """
    통합 안전 시장가 매도 함수:
    사전 검증 ➔ 주문 접수 (네트워크 재시도) ➔ 체결 추적 및 자가 치유
    """
    # 1. 사전 검증
    ok, clean_vol, err_msg = preflight_check_sell(market, volume, current_price, available_volume)
    if not ok:
        return {"status": "PREFLIGHT_REJECTED", "reason": err_msg, "volume": 0.0, "price": 0.0}

    # 2. 주문 전송
    resp, send_err = execute_market_order_resilient(upbit_client, market, "ask", clean_vol)
    if not resp or not resp.get("uuid"):
        return {"status": "SUBMIT_FAILED", "reason": send_err, "volume": 0.0, "price": 0.0}

    order_uuid = resp["uuid"]
    if on_accepted:
        try:
            on_accepted(order_uuid)
        except Exception:
            pass

    # 3. 체결 대기 및 자가 치유 폴링
    return poll_order_settlement(upbit_client, order_uuid, max_wait_sec=max_wait_sec)
