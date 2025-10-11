import requests
from api.auth import build_query_string, get_headers
from config import BASE_URL

def create_order(market: str, side: str, volume: str, price: str, ord_type: str):
    """주문 생성"""

    # None 값은 제외해서 body 구성
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
