import requests
from api.auth import build_query_string, get_headers
from config import BASE_URL


def create_order(market: str, side: str, volume: str, price: str, ord_type: str):
    """주문 생성 (side: bid/ask, ord_type: limit/price/market)"""
    body = {
        "market": market,
        "side": side,
        "ord_type": ord_type
    }
    if volume is not None:
        body["volume"] = volume
    if price is not None:
        body["price"] = price

    query_string = build_query_string(body)
    headers = get_headers(query_string)
    headers["Content-Type"] = "application/json"

    url = f"{BASE_URL}/v1/orders"
    response = requests.post(url, json=body, headers=headers)
    return response.json()


def get_open_orders(market: str):
    """미체결 주문 목록 조회 (state='wait')"""
    params = {
        "market": market,
        "state": "wait"
    }
    query_string = build_query_string(params)
    headers = get_headers(query_string)

    url = f"{BASE_URL}/v1/orders?{query_string}"
    response = requests.get(url, headers=headers)
    if response.status_code == 200:
        return response.json()
    return None


def cancel_orders(uuids: list):
    """미체결 주문 취소 (uuid 목록)"""
    results = []
    for uuid in uuids:
        params = {"uuid": uuid}
        query_string = build_query_string(params)
        headers = get_headers(query_string)

        url = f"{BASE_URL}/v1/order?{query_string}"
        response = requests.delete(url, headers=headers)
        results.append(response.json())
    return results

