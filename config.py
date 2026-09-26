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
STOP_LOSS_PERCENT = -0.10     # 평단가 대비 -10.0% 손절선
BULL_STOP_LOSS_PCT = -0.10    # 상승장 평단가 대비 -10.0% 긴급 손절선 (추세 청산 없이 -10% 최후 방어)
STOP_LOSS_COOLDOWN_HOURS = 2  # 손절 후 재진입 방지 쿨다운 시간 (2시간 동안 신규 진입 전면 차단)
FEE_RATE = 0.0005             # 0.05% 수수료율
MIN_KRW_ALERT_THRESHOLD = 100_000  # 예수금 10만원 이하 알림 기준 (100,000원)

# 기존 보유 자산 보호 설정 (봇이 절대 매도/청산하지 않는 기준 수량) - 순서: BTC, ETH, SOL
PROTECTED_BALANCES = {
    "KRW-BTC": 0.03741469,  # 기존 보유 비트코인 전량 보호 (봇 매도 원천 배제)
    "KRW-ETH": 0.40756151,  # 기존 보유 이더리움 전량 보호
    "KRW-SOL": 6.66887531,  # 기존 보유 솔라나 전량 보호
}

# 비트코인(KRW-BTC) 계층형 가중 적립 전략 설정 (무손절 Buy-Only)
BTC_DCA_CLOSING_CHECK_HOUR = 8       # 일봉 종가 직전 체크 시각 (08시 KST)
BTC_DCA_CLOSING_CHECK_MINUTE = 55    # 일봉 종가 직전 체크 분 (55분 KST)
BTC_UNIT_KRW = 10_000                # 가중 매수 기본 단위 (10,000원)
BTC_UPBIT_DCA_HOUR = 15              # 업비트 자체 모으기 시간 (15:05 KST)
BTC_UPBIT_DCA_MINUTE = 5

# 하이브리드 상승장(BULL) 다이나믹 트레일링 스탑 설정 (물량 축적 규모에 따른 동적 목표가 적용)
USE_TRAILING_STOP = True          # 트레일링 스탑 사용 여부
TRAILING_STOP_TRIGGER = 0.10      # 기본 트레일링 스탑 트리거 (+10.0%)
TRAILING_STOP_DROP = 0.03         # 포지션 최고가 대비 -3.0% 하락 시 조기 익절 청산

# 상승장 물량 증가에 따른 동적 익절 목표가 (골대 후퇴 방지 및 90% 승률 달성)
# 1~3회차 (1~3만원): +10.0% (초기 가벼운 상태에서 대세 랠리 추종)
# 4~6회차 (4~6만원): +7.0% (물량 누적 시 평단가 상승 방어)
# 7~20회차 (7~20만원): +5.0% (대규모 물량 신속 현금화 및 리셋)
USE_DYNAMIC_TRAILING_STOP = True
DYNAMIC_TS_LEVELS = [
    (3, 0.10),   # 누적 1~3회차: +10.0%
    (6, 0.07),   # 누적 4~6회차: +7.0%
    (20, 0.05),  # 누적 7~20회차: +5.0%
]

def get_bull_trailing_stop_trigger(step_count: int) -> float:
    """현재 적립 차수에 따른 동적 트레일링 스탑 트리거 퍼센트 반환"""
    if not USE_DYNAMIC_TRAILING_STOP:
        return TRAILING_STOP_TRIGGER
    # 0회차는 1회차와 동일 취급
    effective_steps = max(1, step_count)
    for max_step, trig in DYNAMIC_TS_LEVELS:
        if effective_steps <= max_step:
            return trig
    return 0.05

# 하이브리드 상승장(BULL) 12시간 정기 시간 분할 적립(Time-DCA) 및 일봉 종가매수 설정
USE_BULL_TIME_DCA = True             # 상승장 12시간 정기 분할 적립 매수 사용 (12h마다 1U)
BULL_TIME_DCA_INTERVAL_HOURS = 12    # 정기 적립 간격 (12시간)
MAX_BULL_DCA_STEPS = 20              # 최대 적립 차수 (20회 = 총 20만 원, 또는 예수금 한도)
USE_BULL_CLOSING_BUY = True          # 당일 일봉 양봉 종가매매 (08:50 KST 양봉 & 5일선 지지 시 1U)
MIN_BULL_BUY_GAP_HOURS = 3           # 매수 간 최소 보호 간격 (최근 매수 후 3시간 이내 중복 매수 방지)
USE_BULL_PYRAMID = False             # 상승장 가격 돌파 불타기 OFF (12h 적립 + 일봉 종가매수로 안정화)
PYRAMID_STEP_PCT = 0.03              # 직전 매수가 대비 +3.0% 상승 시 추가매수
MAX_PYRAMID_STEPS = 20               # 최대 누적 차수

# 하이브리드 하락장(BEAR) 마틴게일 매직스플릿 방어 설정 (Dual Exit: 바스켓 +0.5% OR 개별 +3%)
USE_MAGIC_SPLIT_DEFENSE = True       # 매직스플릿 개별 익절 병행 방어 모드
MAGIC_SPLIT_TRANCHE_PROFIT = 0.03   # 개별 차수 반등 시 단독 익절 목표 마진 (+3.0%)
MAGIC_SPLIT_DOWN_PCT = 0.04         # 추가 차수 물타기 간격 (-4.0%)
MARTINGALE_MULTIPLIERS = [1, 1, 2, 4]  # 기본 마틴게일 투입 스케줄 (1-1-2-4)

# 국면 전환 (BULL ➔ BEAR: 200 MA 하향 돌파 시) 부분 손절 비율
# ETH: 50% 부분 손절 후 남은 50%는 마틴게일로 인계하여 물타기 탈출
# SOL: 70% 부분 손절 후 남은 30%만 마틴게일로 인계하여 폭락 리스크 차단
REGIME_SWITCH_LIQUIDATION_PCT = {
    "KRW-ETH": 0.50,  # 이더리움: 50% 손절
    "KRW-SOL": 0.70,  # 솔라나: 70% 손절
}

# 하락장(BEAR) 마틴게일 스쿼드(Squad = 1-1-2-4) 투입 스케줄 (종목별 차별화)
# 1 Squad = [1, 1, 2, 4] 배수 (총 8 Units = 80,000원)
# SOL: 1-1-2-4 스쿼드 4번 투입 (총 16차수 = 32 Units / 32만 원 캡 후 홀딩)
# ETH: 1-1-2-4 스쿼드 무제한 순환 투입 (1-1-2-4-1-1-2-4... 예수금 한도 내 무한 물타기 및 탈출)
MARTINGALE_SCHEDULE = {
    "KRW-SOL": [1, 1, 2, 4] * 4,  # 총 4개 스쿼드 (16차수 / 32 Units 캡 후 홀딩)
    "KRW-ETH": [1, 1, 2, 4],      # 1-1-2-4 스쿼드 무제한 순환 패턴
}

MARTINGALE_MAX_STEPS = {
    "KRW-SOL": 16,    # 솔라나: 최대 16차수(4스쿼드 / 32U) 도달 시 추가 매수 전면 중단 및 반등 홀딩
    "KRW-ETH": 9999,  # 이더리움: 예수금 허용 한도 내 무제한 스쿼드 투입
}

# 종목별 맞춤 익절 마진 (1단계 추천 적용: SOL 0.5%, ETH 0.8%)
PROFIT_MARGINS = {
    "KRW-SOL": 1.005,  # 솔라나: +0.5% (초단타 빠른 회전)
    "KRW-ETH": 1.008,  # 이더리움: +0.8% (추세 반영 최적 마진)
    "KRW-BTC": 1.005,  # 비트코인: +0.5%
}

# 텔레그램 정기 현황 브리핑 발송 시각 (KST 기준 하루 4회: 09:30, 12:40, 16:40, 19:00)
TELEGRAM_BRIEFING_SCHEDULE = ["09:30", "12:40", "16:40", "19:00"]


def get_profit_margin(market: str) -> float:
    """해당 마켓의 익절 목표 마진을 반환합니다."""
    return PROFIT_MARGINS.get(market, 1.005)



def get_upbit_keys():
    """런타임 시점에 환경변수 및 Streamlit Secrets에서 API 키를 탐색하고 정제합니다."""
    ak = os.getenv("UPBIT_ACCESS_KEY")
    sk = os.getenv("UPBIT_SECRET_KEY")

    try:
        import streamlit as st
        # Streamlit Secrets 탐색 (다양한 키 명칭 및 섹션 호환 지원)
        if hasattr(st, "secrets"):
            for ak_name in ["UPBIT_ACCESS_KEY", "upbit_access_key", "ACCESS_KEY", "access_key"]:
                if ak_name in st.secrets:
                    val = str(st.secrets[ak_name]).strip().strip('"').strip("'")
                    if val:
                        ak = val
                        break
            for sk_name in ["UPBIT_SECRET_KEY", "upbit_secret_key", "SECRET_KEY", "secret_key"]:
                if sk_name in st.secrets:
                    val = str(st.secrets[sk_name]).strip().strip('"').strip("'")
                    if val:
                        sk = val
                        break
            # [upbit] 섹션 하위 탐색
            if not ak and "upbit" in st.secrets:
                u_ak = st.secrets["upbit"].get("access_key") or st.secrets["upbit"].get("UPBIT_ACCESS_KEY")
                u_sk = st.secrets["upbit"].get("secret_key") or st.secrets["upbit"].get("UPBIT_SECRET_KEY")
                if u_ak:
                    ak = str(u_ak).strip().strip('"').strip("'")
                if u_sk:
                    sk = str(u_sk).strip().strip('"').strip("'")
    except Exception:
        pass

    if ak:
        ak = str(ak).strip().strip('"').strip("'")
    if sk:
        sk = str(sk).strip().strip('"').strip("'")

    return ak, sk


def get_upbit_client():
    """업비트 클라이언트를 생성하여 반환합니다."""
    ak, sk = get_upbit_keys()
    if not ak or not sk:
        raise ValueError("업비트 API 키가 설정되지 않았습니다. .env 파일 또는 Streamlit Secrets를 확인해주세요.")
    return pyupbit.Upbit(ak, sk)


def load_investments():
    """
    .env 및 Streamlit Secrets에서 종목별 총 투자금을 불러오고 1Unit 금액을 계산합니다.
    - 1Unit = max(MIN_ORDER_KRW, 총 투자금 // 100)
    - 업비트 최소 주문 금액(5,000원) 미만이 되지 않도록 보정합니다.
    """
    investments = {}

    # 1. os.environ 탐색 (.env)
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

    # 2. Streamlit Secrets 탐색 (웹 대시보드 클라우드 호환)
    try:
        import streamlit as st
        if hasattr(st, "secrets"):
            for key in st.secrets.keys():
                if str(key).startswith("INVEST_"):
                    try:
                        market = str(key).replace("INVEST_", "").replace("_", "-")
                        total_invest = int(st.secrets[key])
                        unit = max(MIN_ORDER_KRW, total_invest // 100)
                        investments[market] = {
                            "total": total_invest,
                            "unit": unit
                        }
                    except ValueError:
                        continue
            # [investments] 하위 섹션이 있을 경우
            if "investments" in st.secrets:
                for k, v in st.secrets["investments"].items():
                    m = k.replace("_", "-").upper()
                    if not m.startswith("KRW-"):
                        m = f"KRW-{m}"
                    total_invest = int(v)
                    unit = max(MIN_ORDER_KRW, total_invest // 100)
                    investments[m] = {
                        "total": total_invest,
                        "unit": unit
                    }
    except Exception:
        pass

    # 3. .env 및 Secrets 모두 없을 경우 기본 fallback 설정 (순서: BTC, ETH, SOL)
    if not investments:
        investments = {
            "KRW-BTC": {"total": 1_000_000, "unit": 10_000},
            "KRW-ETH": {"total": 1_000_000, "unit": 10_000},
            "KRW-SOL": {"total": 1_000_000, "unit": 10_000}
        }

    # 종목 표시/실행 순서 엄격 확정: 1. 비트코인, 2. 이더리움, 3. 솔라나
    market_priority = ["KRW-BTC", "KRW-ETH", "KRW-SOL"]
    ordered_investments = {}
    for m in market_priority:
        if m in investments:
            ordered_investments[m] = investments[m]
    for m, v in investments.items():
        if m not in ordered_investments:
            ordered_investments[m] = v

    return ordered_investments


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