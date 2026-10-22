"""
Upbit Subpocket (G60) REST API Wrapper
- 독립된 서브포켓 전용 키(UPBIT_POCKET_ACCESS_KEY, UPBIT_POCKET_SECRET_KEY)를 사용하여
  메인 계좌와 완전히 격리된 주문 및 자산 조회를 안전하게 수행합니다.
"""
import os
import time
import json
import uuid
import hashlib
import requests
import jwt
from urllib.parse import urlencode, unquote
from typing import Dict, List, Any, Optional, Tuple
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(BASE_DIR, ".env"))

UPBIT_HOST = "https://api.upbit.com"


class UpbitPocketClient:
    def __init__(self, access_key: Optional[str] = None, secret_key: Optional[str] = None):
        self.access_key = access_key or os.getenv("UPBIT_POCKET_ACCESS_KEY")
        self.secret_key = secret_key or os.getenv("UPBIT_POCKET_SECRET_KEY")
        if not self.access_key or not self.secret_key:
            raise ValueError("UPBIT_POCKET_ACCESS_KEY 또는 UPBIT_POCKET_SECRET_KEY가 설정되지 않았습니다.")

    def _get_headers(self, params: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
        payload = {
            "access_key": self.access_key,
            "nonce": str(uuid.uuid4())
        }
        if params:
            query_str = unquote(urlencode(params))
            m = hashlib.sha512(query_str.encode("utf-8")).hexdigest()
            payload["query_hash"] = m
            payload["query_hash_alg"] = "SHA512"

        token = jwt.encode(payload, self.secret_key, algorithm="HS256")
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }

    def get_accounts(self) -> List[Dict[str, Any]]:
        headers = self._get_headers()
        r = requests.get(f"{UPBIT_HOST}/v1/accounts", headers=headers, timeout=5)
        if r.status_code == 200:
            return r.json()
        raise RuntimeError(f"Failed to get pocket accounts: {r.status_code} {r.text}")

    def get_balance(self, currency: str = "KRW") -> float:
        accounts = self.get_accounts()
        for acc in accounts:
            if acc.get("currency") == currency:
                return float(acc.get("balance", 0.0) or 0.0)
        return 0.0

    def get_order(self, order_uuid: str) -> Dict[str, Any]:
        params = {"uuid": order_uuid}
        headers = self._get_headers(params)
        r = requests.get(f"{UPBIT_HOST}/v1/order?{urlencode(params)}", headers=headers, timeout=5)
        if r.status_code == 200:
            return r.json()
        raise RuntimeError(f"Failed to get order {order_uuid}: {r.status_code} {r.text}")

    def get_order_by_identifier(self, identifier: str) -> Optional[Dict[str, Any]]:
        params = {"identifier": identifier}
        headers = self._get_headers(params)
        r = requests.get(f"{UPBIT_HOST}/v1/order?{urlencode(params)}", headers=headers, timeout=5)
        if r.status_code == 200:
            return r.json()
        elif r.status_code == 404:
            return None
        raise RuntimeError(f"Failed to get order by identifier {identifier}: {r.status_code} {r.text}")

    def parse_order_execution(self, order_info: Dict[str, Any], req_vol: Optional[float] = None) -> Optional[Dict[str, Any]]:
        state = order_info.get("state")
        trades = order_info.get("trades", [])
        paid_fee = float(order_info.get("paid_fee", 0.0) or 0.0)
        order_uuid = order_info.get("uuid", "")
        identifier = order_info.get("identifier")
        raw_exec_vol = float(order_info.get("executed_volume", 0.0) or 0.0)

        if trades:
            exec_vol = sum(float(t.get("volume", 0.0) or 0.0) for t in trades)
            exec_funds = sum(float(t.get("funds", 0.0) or (float(t.get("price", 0.0)) * float(t.get("volume", 0.0)))) for t in trades)
            avg_p = exec_funds / exec_vol if exec_vol > 0 else 0.0
        else:
            exec_vol = raw_exec_vol
            exec_funds = 0.0
            avg_p = 0.0

        if state in ("done", "cancel"):
            # 1. 체결 없이 취소된 정상 Zero-fill Cancel 확인
            if state == "cancel" and (not trades) and exec_vol <= 0:
                return {
                    "status": "CANCELLED",
                    "uuid": order_uuid,
                    "identifier": identifier,
                    "price": 0.0,
                    "volume": 0.0,
                    "funds": 0.0,
                    "fee": paid_fee,
                    "remaining_volume": req_vol if req_vol is not None else 0.0,
                    "raw": order_info
                }

            # 2. 체결이 발생했다고 주장하지만 체결 내역(trades)이 누락되었거나 체결량/단가가 0 이하인 비정상 응답: Fail-Closed
            if not trades or exec_vol <= 0 or avg_p <= 0:
                raise RuntimeError(f"Order {order_uuid} terminal state '{state}' but trades details missing or zero volume")

            is_partial = False
            if req_vol is not None and exec_vol < (req_vol - 1e-8):
                is_partial = True

            return {
                "status": "PARTIAL" if is_partial else "FILLED",
                "uuid": order_uuid,
                "identifier": identifier,
                "price": avg_p,
                "volume": exec_vol,
                "funds": exec_funds,
                "fee": paid_fee,
                "remaining_volume": max(0.0, req_vol - exec_vol) if req_vol is not None else 0.0,
                "raw": order_info
            }
        return None

    def buy_market_order_and_wait(
        self,
        market: str,
        amount_krw: float,
        identifier: Optional[str] = None,
        timeout_sec: float = 10.0
    ) -> Dict[str, Any]:
        """시장가 매수 (amount_krw 원화 투입) 및 체결 확인"""
        clean_amt = int(amount_krw)
        if clean_amt < 5000:
            raise ValueError(f"최소 주문 금액은 5,000원입니다. (입력: {clean_amt}원)")

        body = {
            "market": market,
            "side": "bid",
            "price": str(clean_amt),
            "ord_type": "price"
        }
        if identifier:
            body["identifier"] = identifier

        headers = self._get_headers(body)
        r = requests.post(f"{UPBIT_HOST}/v1/orders", json=body, headers=headers, timeout=5)
        if r.status_code not in (200, 201):
            raise RuntimeError(f"Market buy order failed: {r.status_code} {r.text}")

        res = r.json()
        order_uuid = res.get("uuid")

        # 체결 대기 및 대사
        start_t = time.time()
        while time.time() - start_t < timeout_sec:
            time.sleep(0.3)
            order_info = self.get_order(order_uuid)
            parsed = self.parse_order_execution(order_info)
            if parsed:
                return parsed

        raise TimeoutError(f"Market buy order {order_uuid} (identifier: {identifier}) timed out after {timeout_sec}s")

    def sell_market_order_and_wait(
        self,
        market: str,
        volume: float,
        identifier: Optional[str] = None,
        timeout_sec: float = 10.0
    ) -> Dict[str, Any]:
        """시장가 매도 (volume 수량 청산) 및 체결 확인"""
        clean_vol = f"{volume:.8f}".rstrip("0").rstrip(".")
        req_vol = float(clean_vol)
        if req_vol <= 0:
            raise ValueError(f"매도 수량이 0 이하입니다: {volume}")

        body = {
            "market": market,
            "side": "ask",
            "volume": clean_vol,
            "ord_type": "market"
        }
        if identifier:
            body["identifier"] = identifier

        headers = self._get_headers(body)
        r = requests.post(f"{UPBIT_HOST}/v1/orders", json=body, headers=headers, timeout=5)
        if r.status_code not in (200, 201):
            raise RuntimeError(f"Market sell order failed: {r.status_code} {r.text}")

        res = r.json()
        order_uuid = res.get("uuid")

        # 체결 대기 및 대사
        start_t = time.time()
        while time.time() - start_t < timeout_sec:
            time.sleep(0.3)
            order_info = self.get_order(order_uuid)
            parsed = self.parse_order_execution(order_info, req_vol=req_vol)
            if parsed:
                return parsed

        raise TimeoutError(f"Market sell order {order_uuid} (identifier: {identifier}) timed out after {timeout_sec}s")
