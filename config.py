import os
from pathlib import Path
import pyupbit
from dotenv import load_dotenv

# 1. 환경변수(.env) 로드
ENV_PATH = Path(__file__).resolve().parent / ".env"
load_dotenv(dotenv_path=ENV_PATH)

# 2. 기본 API 및 거래소 설정
UPBIT_ACCESS_KEY = os.getenv("UPBIT_ACCESS_KEY")
UPBIT_SECRET_KEY = os.getenv("UPBIT_SECRET_KEY")

try:
    import streamlit as st
    if hasattr(st, "secrets"):
        UPBIT_ACCESS_KEY = UPBIT_ACCESS_KEY or st.secrets.get("UPBIT_ACCESS_KEY")
        UPBIT_SECRET_KEY = UPBIT_SECRET_KEY or st.secrets.get("UPBIT_SECRET_KEY")
except Exception:
    pass

ACCESS_KEY = UPBIT_ACCESS_KEY
SECRET_KEY = UPBIT_SECRET_KEY
BASE_URL = "https://api.upbit.com"

# 3. 매매 및 리스크 관리 기본 설정
MIN_ORDER_KRW = 5000          # 업비트 최소 주문 금액 (5,000원)
STOP_LOSS_PERCENT = -0.10     # -10.0% 손절선 (평단가 대비)
FEE_RATE = 0.0005             # 0.05% 수수료율

# 가상매매 (모의투자/Paper Trading) 모드 여부 (.env에서 변경 가능, 기본 True)
IS_PAPER_TRADING = os.getenv("IS_PAPER_TRADING", "true").lower() in ("true", "1", "yes")

# 종목별 맞춤 익절 마진 (1단계 추천 적용: SOL 0.5%, ETH 0.8%)
PROFIT_MARGINS = {
    "KRW-SOL": 1.005,  # 솔라나: +0.5% (초단타 빠른 회전)
    "KRW-ETH": 1.008,  # 이더리움: +0.8% (추세 반영 최적 마진)
    "KRW-XRP": 1.005,  # 리플: +0.5%
    "KRW-BTC": 1.005,  # 비트코인: +0.5%
}


def get_profit_margin(market: str) -> float:
    """해당 마켓의 익절 목표 마진을 반환합니다."""
    return PROFIT_MARGINS.get(market, 1.005)



def get_upbit_client():
    """업비트 클라이언트를 생성하여 반환합니다."""
    if not UPBIT_ACCESS_KEY or not UPBIT_SECRET_KEY:
        raise ValueError("업비트 API 키가 .env 파일에 설정되지 않았습니다. .env 파일을 확인해주세요.")
    return pyupbit.Upbit(UPBIT_ACCESS_KEY, UPBIT_SECRET_KEY)


def load_investments():
    """
    .env에서 종목별 총 투자금을 불러오고 1Unit 금액을 계산합니다.
    - 1Unit = max(MIN_ORDER_KRW, 총 투자금 // 100)
    - 업비트 최소 주문 금액(5,000원) 미만이 되지 않도록 보정합니다.
    """
    investments = {}
    for key, value in os.environ.items():
        if key.startswith("INVEST_"):
            try:
                market = key.replace("INVEST_", "").replace("_", "-")  # INVEST_KRW_SOL -> KRW-SOL
                total_invest = int(value)
                unit = max(MIN_ORDER_KRW, total_invest // 100)
                investments[market] = {
                    "total": total_invest,
                    "unit": unit
                }
            except ValueError:
                continue

    # .env에 없을 경우 기본 fallback 설정
    if not investments:
        investments = {
            "KRW-SOL": {"total": 500_000, "unit": 5_000},
            "KRW-ETH": {"total": 500_000, "unit": 5_000},
            "KRW-XRP": {"total": 500_000, "unit": 5_000}
        }

    return investments


INVESTMENTS = load_investments()


if __name__ == "__main__":
    print("=" * 50)
    print("[INVESTMENTS 설정 확인]")
    for market, info in INVESTMENTS.items():
        margin = (get_profit_margin(market) - 1) * 100
        print(f"  - {market}: 총 투자금={info['total']:,}원, 1Unit={info['unit']:,}원, 익절=+{margin:.2f}%")
    print(f"  - 최소 주문 금액: {MIN_ORDER_KRW:,}원")
    print(f"  - 손절 기준: {STOP_LOSS_PERCENT * 100:.2f}%")
    print(f"  - 가상매매(Paper Trading) 모드: {IS_PAPER_TRADING}")
    print("=" * 50)