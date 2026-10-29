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
    def __init__(
        self,
        access_key: Optional[str] = None,
        secret_key: Optional[str] = None,
        env_prefix: str = "UPBIT_POCKET"
    ):
        self.env_prefix = env_prefix
        self.access_key = (
            access_key
            or os.getenv(f"{env_prefix}_ACCESS_KEY")
            or (os.getenv("UPBIT_CASCADE_ACCESS_KEY") if env_prefix == "UPBIT_POCKET2" else None)
        )
        self.secret_key = (
            secret_key
            or os.getenv(f"{env_prefix}_SECRET_KEY")
            or (os.getenv("UPBIT_CASCADE_SECRET_KEY") if env_prefix == "UPBIT_POCKET2" else None)
        )

        if not self.access_key or not self.secret_key:
            if env_prefix == "UPBIT_POCKET":
                raise ValueError("UPBIT_POCKET_ACCESS_KEY 또는 UPBIT_POCKET_SECRET_KEY가 설정되지 않았습니다.")
            else:
                fb_ak = os.getenv("UPBIT_POCKET_ACCESS_KEY")
                fb_sk = os.getenv("UPBIT_POCKET_SECRET_KEY")
                if fb_ak and fb_sk and os.getenv("ALLOW_POCKET_FALLBACK", "0") == "1":
                    self.access_key = fb_ak
                    self.secret_key = fb_sk
                else:
                    raise ValueError(
                        f"[{env_prefix}] API 키가 설정되지 않았습니다. .env 파일에 {env_prefix}_ACCESS_KEY 및 {env_prefix}_SECRET_KEY를 등록해주세요."
                    )

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

    def get_open_orders(self, market: str) -> List[Dict[str, Any]]:
        """해당 마켓의 미체결 주문 목록 조회 (state=wait)"""
        params = {"market": market, "state": "wait"}
        headers = self._get_headers(params)
        r = requests.get(f"{UPBIT_HOST}/v1/orders?{urlencode(params)}", headers=headers, timeout=5)
        if r.status_code == 200:
            return r.json()
        raise RuntimeError(f"Failed to get open orders for {market}: {r.status_code} {r.text}")

    def create_limit_order(
        self,
        market: str,
        side: str,
        volume: float,
        price: float,
        identifier: Optional[str] = None
    ) -> Dict[str, Any]:
        """지정가 주문 접수 (side: 'bid' 또는 'ask')"""
        clean_vol = f"{volume:.8f}".rstrip("0").rstrip(".")
        clean_price = str(int(price)) if price >= 1.0 else f"{price:.4f}".rstrip("0").rstrip(".")

        body = {
            "market": market,
            "side": side,
            "volume": clean_vol,
            "price": clean_price,
            "ord_type": "limit"
        }
        if identifier:
            body["identifier"] = identifier

        headers = self._get_headers(body)
        r = requests.post(f"{UPBIT_HOST}/v1/orders", json=body, headers=headers, timeout=5)
        if r.status_code in (200, 201):
            return r.json()
        raise RuntimeError(f"Limit order ({side} {volume} @ {price}) failed: {r.status_code} {r.text}")

    def cancel_order(self, order_uuid: str) -> Dict[str, Any]:
        """주문 취소 접수"""
        params = {"uuid": order_uuid}
        headers = self._get_headers(params)
        r = requests.delete(f"{UPBIT_HOST}/v1/order?{urlencode(params)}", headers=headers, timeout=5)
        if r.status_code == 200:
            return r.json()
        raise RuntimeError(f"Cancel order {order_uuid} failed: {r.status_code} {r.text}")

    def get_coin_balance_and_avg(self, currency: str) -> Tuple[float, float]:
        """특정 코인의 보유 수량 및 평단가 반환 (수량, 평단가)"""
        accounts = self.get_accounts()
        for acc in accounts:
            if acc.get("currency") == currency:
                bal = float(acc.get("balance", 0.0) or 0.0)
                locked = float(acc.get("locked", 0.0) or 0.0)
                avg = float(acc.get("avg_buy_price", 0.0) or 0.0)
                return (bal + locked, avg)
        return (0.0, 0.0)


class UpbitPocketManager:
    """업비트 포켓 관리 및 포켓 간 자산 이전 전용 클라이언트 (UPBIT_MGMT_ACCESS_KEY 사용)"""
    def __init__(
        self,
        access_key: Optional[str] = None,
        secret_key: Optional[str] = None,
        server_url: str = UPBIT_HOST
    ):
        self.access_key = access_key or os.getenv("UPBIT_MGMT_ACCESS_KEY")
        self.secret_key = secret_key or os.getenv("UPBIT_MGMT_SECRET_KEY")
        self.server_url = server_url

        if not self.access_key or not self.secret_key:
            raise ValueError(
                "UPBIT_MGMT_ACCESS_KEY 또는 UPBIT_MGMT_SECRET_KEY가 설정되지 않았습니다. .env 파일을 확인해주세요."
            )

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

    def list_pockets(self) -> List[Dict[str, Any]]:
        """계정 내 전체 포켓 목록 조회"""
        headers = self._get_headers()
        r = requests.get(f"{self.server_url}/v1/pockets", headers=headers, timeout=5)
        if r.status_code == 200:
            return r.json()
        raise RuntimeError(f"Failed to list pockets: {r.status_code} {r.text}")

    def get_pocket_uuid(self, name_or_uuid: str) -> str:
        """포켓 이름 또는 UUID로 포켓 UUID 확인"""
        pockets = self.list_pockets()
        for p in pockets:
            if p.get("uuid") == name_or_uuid or p.get("name") == name_or_uuid:
                return p["uuid"]
        raise ValueError(f"포켓을 찾을 수 없습니다: {name_or_uuid} (조회된 포켓: {[p.get('name') for p in pockets]})")

    def transfer(
        self,
        from_pocket: str,
        to_pocket: str,
        currency: str,
        amount: float,
        identifier: Optional[str] = None
    ) -> Dict[str, Any]:
        """포켓 간 자산 이전 실행 (POST /v1/pockets/universal_transfers)"""
        from_uuid = self.get_pocket_uuid(from_pocket)
        to_uuid = self.get_pocket_uuid(to_pocket)
        if from_uuid == to_uuid:
            raise ValueError("출금 포켓과 입금 포켓이 동일합니다.")

        clean_amount = str(int(amount)) if amount == int(amount) else f"{amount:.8f}".rstrip("0").rstrip(".")
        ident = identifier or f"transfer_{uuid.uuid4().hex[:12]}"
        body = {
            "from": from_uuid,
            "to": to_uuid,
            "currency": currency,
            "amount": clean_amount,
            "identifier": ident
        }
        headers = self._get_headers(body)
        r = requests.post(
            f"{self.server_url}/v1/pockets/universal_transfers",
            json=body,
            headers=headers,
            timeout=5
        )
        if r.status_code in (200, 201):
            return r.json()
        raise RuntimeError(f"Pocket transfer failed ({r.status_code}): {r.text}")

    def list_transfers(self, limit: int = 100) -> List[Dict[str, Any]]:
        """메인포켓 자산 이전 목록 조회 (GET /v1/pockets/universal_transfers)"""
        params = {"limit": limit}
        headers = self._get_headers(params)
        r = requests.get(
            f"{self.server_url}/v1/pockets/universal_transfers?{urlencode(params)}",
            headers=headers,
            timeout=5
        )
        if r.status_code == 200:
            return r.json()
        raise RuntimeError(f"Failed to list transfers ({r.status_code}): {r.text}")



