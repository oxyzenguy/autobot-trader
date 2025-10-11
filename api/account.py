import requests
from api.auth import get_headers
from config import BASE_URL

def get_account():
    """계정 잔고 조회"""
    headers = get_headers()
    url = f"{BASE_URL}/v1/accounts"
    response = requests.get(url, headers=headers)
    return response.json()
