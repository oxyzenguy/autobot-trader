import uuid
import hashlib
import jwt
from urllib.parse import urlencode, unquote
from typing import Dict, Any
from config import ACCESS_KEY, SECRET_KEY

def build_query_string(params: Dict[str, Any]) -> str:
    """쿼리 파라미터 dict -> 문자열 변환"""
    return unquote(urlencode(params, doseq=True))

def create_jwt_token(query_string: str = "") -> str:
    """JWT 토큰 생성"""
    payload = {
        "access_key": ACCESS_KEY,
        "nonce": str(uuid.uuid4()),
    }

    if query_string:
        query_hash = hashlib.sha512(query_string.encode("utf-8")).hexdigest()
        payload["query_hash"] = query_hash
        payload["query_hash_alg"] = "SHA512"

    token = jwt.encode(payload, SECRET_KEY, algorithm="HS512")
    return token if isinstance(token, str) else token.decode("utf-8")

def get_headers(query_string: str = "") -> Dict[str, str]:
    """API 호출용 헤더 생성"""
    token = create_jwt_token(query_string)
    return {"Authorization": f"Bearer {token}"}
