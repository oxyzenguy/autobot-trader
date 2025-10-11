import requests
from api.auth import build_query_string, get_headers
from config import BASE_URL

def get_open_orders(market: str = None, limit: int = 10):
    """체결 대기 주문 목록 조회"""
    params = {"limit": limit}
    if market:
        params["market"] = market

    query_string = build_query_string(params)
    headers = get_headers(query_string)

    url = f"{BASE_URL}/v1/orders/open?{query_string}"
    response = requests.get(url, headers=headers)
    return response.json()

def create_order(market: str, side: str, volume: str, price: str, ord_type: str):
    """주문 생성"""
    body = {
        "market": market,
        "side": side,       # bid(매수), ask(매도)
        "volume": volume,   # 수량 (시장가 매수는 None)
        "price": price,     # 가격 (시장가 매도는 None)
        "ord_type": ord_type
    }

    query_string = build_query_string(body)
    headers = get_headers(query_string)
    headers["Content-Type"] = "application/json"

    url = f"{BASE_URL}/v1/orders"
    response = requests.post(url, json=body, headers=headers)
    return response.json()

def cancel_orders(uuids: list[str] = None, identifiers: list[str] = None):
    """id로 주문 목록 취소 접수"""
    params = {}
    if uuids:
        params["uuids[]"] = uuids
    elif identifiers:
        params["identifiers[]"] = identifiers
    else:
        raise ValueError("uuids 또는 identifiers 중 하나는 반드시 필요합니다.")

    query_string = build_query_string(params)
    headers = get_headers(query_string)

    url = f"{BASE_URL}/v1/orders/uuids?{query_string}"
    response = requests.delete(url, headers=headers)
    return response.json()
