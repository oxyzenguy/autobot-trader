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
MIN_KRW_ALERT_THRESHOLD = 100_000  # 예수금 10만원 이하 알림 기준 (100,000원)

# 기존 보유 자산 보호 설정 (봇이 절대 매도/청산하지 않는 기준 수량)
PROTECTED_BALANCES = {
    "KRW-SOL": 6.66887531,  # 기존 보유 솔라나 전량 보호
    "KRW-ETH": 0.40756151,  # 기존 보유 이더리움 전량 보호
    "KRW-BTC": 0.03724281,  # 기존 보유 비트코인 전량 보호
}

# 하이브리드 상승장(BULL) 다이나믹 트레일링 스탑 설정 (Freqtrade Supertrend/Bandtastic 방식)
USE_TRAILING_STOP = True          # 트레일링 스탑 사용 여부
TRAILING_STOP_TRIGGER = 0.10      # 진입가 대비 +10.0% 도달 시 트레일링 스탑 가동
TRAILING_STOP_DROP = 0.03         # 포지션 최고가 대비 -3.0% 하락 시 조기 익절 청산

# 하이브리드 하락장(BEAR) 마틴게일 매직스플릿 방어 설정 (Dual Exit: 바스켓 +0.5% OR 개별 +3%)
USE_MAGIC_SPLIT_DEFENSE = True       # 매직스플릿 개별 익절 병행 방어 모드
MAGIC_SPLIT_TRANCHE_PROFIT = 0.03   # 개별 차수 반등 시 단독 익절 목표 마진 (+3.0%)
MAGIC_SPLIT_DOWN_PCT = 0.04         # 추가 차수 물타기 간격 (-4.0%)

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
    print(f"  - 예수금 경고 기준: {MIN_KRW_ALERT_THRESHOLD:,}원 이하")
    print(f"  - 매매 모드: 실전매매 (Real Trading)")
    print("=" * 50)