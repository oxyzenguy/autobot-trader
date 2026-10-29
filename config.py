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
REGIME_SWITCH_COOLDOWN_HOURS = 6  # 국면전환 부분손절 후 재진입 방지 쿨다운 (6시간, 200 MA 경계 휩쏘 방지)
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

REGIME_SWITCH_BUFFER_PCT = 0.003      # 200 MA 기준 ±0.3% 완충 구간 (이전 0.1% → 잔파동 오판 방지 강화)
REGIME_SWITCH_CONFIRMATION_CANDLES = 3  # 확정 1시간봉 3개 연속 확인 필요 (이전 2개 → 최소 3시간 연속 하회 시만 BEAR 전환)

# 하이브리드 하락장(BEAR) 마틴게일 매직스플릿 방어 설정 (Dual Exit: 바스켓 익절 OR 개별 차수 반등 익절)
USE_MAGIC_SPLIT_DEFENSE = True       # 매직스플릿 개별 익절 병행 방어 모드
MAGIC_SPLIT_DOWN_PCTS = {
    "KRW-ETH": 0.007,  # 이더리움: 초미세 마틴게일 단타형 (-0.7% 빠른 물타기 간격)
    "KRW-SOL": 0.040,  # 솔라나: (-4.0% 안전 간격)
}
MAGIC_SPLIT_TRANCHE_PROFITS = {
    "KRW-ETH": 0.012,  # 이더리움: 초미세 마틴게일 단타형 (+1.2% 개별 차수 반등 단독 익절)
    "KRW-SOL": 0.030,  # 솔라나: (+3.0% 개별 차수 반등 단독 익절)
}
MAGIC_SPLIT_TRANCHE_PROFIT = 0.03   # 기본 하위 호환용 개별 차수 반등 시 단독 익절 목표 마진 (+3.0%)
MAGIC_SPLIT_DOWN_PCT = 0.04         # 기본 하위 호환용 추가 차수 물타기 간격 (-4.0%)
MARTINGALE_MULTIPLIERS = [1, 1, 2, 4]  # 기본 마틴게일 투입 스케줄 (1-1-2-4)

import ast

_last_config_mtime = 0.0

def _sync_config_from_disk():
    global _last_config_mtime, PROFIT_MARGINS, MAGIC_SPLIT_DOWN_PCTS, MAGIC_SPLIT_TRANCHE_PROFITS, MARKET_STRATEGIES, MARTINGALE_MAX_STEPS
    try:
        cfg_path = os.environ.get("AUTOTRADE_CONFIG_PATH") or os.path.abspath(__file__)
        if not os.path.exists(cfg_path):
            return
        mtime = os.path.getmtime(cfg_path)
        if mtime <= _last_config_mtime:
            return
        _last_config_mtime = mtime
        with open(cfg_path, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename=cfg_path)
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        if target.id in ("PROFIT_MARGINS", "MAGIC_SPLIT_DOWN_PCTS", "MAGIC_SPLIT_TRANCHE_PROFITS", "MARKET_STRATEGIES", "MARTINGALE_MAX_STEPS"):
                            try:
                                val = ast.literal_eval(node.value)
                                cur = globals().get(target.id)
                                if isinstance(cur, dict) and isinstance(val, dict):
                                    cur.clear()
                                    cur.update(val)
                                else:
                                    globals()[target.id] = val
                            except Exception:
                                pass
    except Exception:
        pass

def get_magic_split_down_pct(market: str) -> float:
    """해당 마켓의 마틴게일 매직스플릿 물타기 간격을 반환합니다."""
    _sync_config_from_disk()
    return MAGIC_SPLIT_DOWN_PCTS.get(market, MAGIC_SPLIT_DOWN_PCT)

def get_magic_split_tranche_profit(market: str) -> float:
    """해당 마켓의 매직스플릿 개별 차수 단독 익절 마진을 반환합니다."""
    _sync_config_from_disk()
    return MAGIC_SPLIT_TRANCHE_PROFITS.get(market, MAGIC_SPLIT_TRANCHE_PROFIT)

# 마켓별 운용 전략 모델 설정
# - "HYBRID": 200 MA 기반 상승장(추세/적립/트레일링) + 하락장(마틴게일 매직스플릿) 국면전환 전략
# - "PURE_MARTINGALE": 국면전환/손절 없이 24시간 1-1-2-4 무제한 순환 마틴게일 매직스플릿 (이더리움 최적화)
# - "EXIT_ONLY": 추가 매수 없이 기존 보유 물량 익절만 수행하고 종료 (포켓 이전 대기)
MARKET_STRATEGIES = {
    "KRW-ETH": "PURE_MARTINGALE",
    "KRW-SOL": "HYBRID",
}

def get_market_strategy(market: str) -> str:
    """해당 마켓의 운용 전략 모델을 반환합니다."""
    _sync_config_from_disk()
    return MARKET_STRATEGIES.get(market, "HYBRID")

# C-E 적응형 그리드(Adaptive Grid) 전략 실전 활성화 Feature Flag
# 주의: 전략 간 혼선 및 사이클 내 전략 뒤섞임 방지를 위해 무중단 동적 동기화에서 배제되며, 반드시 프로세스 재시작 시에만 반영됩니다.
USE_ADAPTIVE_GRID = {
    "KRW-BTC": False,
    "KRW-ETH": True,   # ETH C-E 실전 엔진 활성화
    "KRW-SOL": True,   # SOL C-E 실전 엔진 활성화 (동적 변동성 그리드 전환)
}

# C-E 적응형 그리드 섀도우 모드(Shadow Mode) 활성화 플래그
# - 실제 매매는 기존 전략으로 100% 집행하되, C-E 신호를 백그라운드에서 병행 계산하여 로그 대조 기록
# (프로세스 기동 시 로드)
ENABLE_ADAPTIVE_GRID_SHADOW = {
    "KRW-BTC": False,
    "KRW-ETH": False,  # ETH 실전 전환 완료 (섀도우 OFF)
    "KRW-SOL": False,
}

def is_adaptive_grid_enabled(market: str) -> bool:
    """
    해당 마켓의 C-E 적응형 그리드 전략 실전 활성화 여부를 반환합니다.
    (안전 원칙: 봇 재기동 시에만 새 설정이 적용되며 무중단 핫스왑은 차단됩니다)
    """
    return USE_ADAPTIVE_GRID.get(market, False)

def is_adaptive_grid_shadow_enabled(market: str) -> bool:
    """
    해당 마켓의 C-E 적응형 그리드 섀도우 모드 활성화 여부를 반환합니다.
    (안전 원칙: 봇 재기동 시에만 새 설정이 적용됩니다)
    """
    return ENABLE_ADAPTIVE_GRID_SHADOW.get(market, False)

# 국면 전환 (BULL ➔ BEAR: 200 MA 하향 돌파 시) 부분 손절 비율
# ETH: 순수 마틴게일 모드로 200선 국면전환 손절 배제 (0.00)
# SOL: 70% 부분 손절 후 남은 30%만 마틴게일로 인계하여 폭락 리스크 차단
REGIME_SWITCH_LIQUIDATION_PCT = {
    "KRW-ETH": 0.00,  # 이더리움: 국면전환 손절 없음 (순수 마틴게일 100% 방어)
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
    "KRW-SOL": 4,     # 솔라나: 추가 매수 전면 중단 (기존 4차수 유지 후 익절 정리 대기)
    "KRW-ETH": 9999,  # 이더리움: 예수금 허용 한도 내 무제한 스쿼드 투입
}

def get_martingale_max_steps(market: str) -> int:
    """해당 마켓의 마틴게일 최대 허용 차수를 반환합니다."""
    _sync_config_from_disk()
    return MARTINGALE_MAX_STEPS.get(market, 16 if market == "KRW-SOL" else 9999)

# 종목별 맞춤 익절 마진 (1단계 추천 적용: SOL 0.5%, ETH 0.8%)
PROFIT_MARGINS = {
    "KRW-SOL": 1.005,  # 솔라나: +0.5% (초단타 빠른 회전)
    "KRW-ETH": 1.005,  # 이더리움: +0.5% (초단타 빠른 회전)
    "KRW-BTC": 1.005,  # 비트코인: +0.5%
}

# 텔레그램 정기 현황 브리핑 발송 시각 (KST 기준 하루 2회: 08:30, 18:00)
TELEGRAM_BRIEFING_SCHEDULE = ["08:30", "18:00"]

# 텔레그램 정기 시스템 감사 리포트 발송 시각 (KST 기준 하루 2회: 08:00, 17:00)
AUDITOR_SCHEDULE = ["08:00", "17:00"]
AUDITOR_SILENT_WATCHDOG_INTERVAL_SEC = 60      # 비상 생존 감시 주기 (1분)
AUDITOR_HEARTBEAT_STALL_TIMEOUT_SEC = 2400     # 하트비트 스톨 기준 시간 (40분)
MAX_AUTO_HEAL_ATTEMPTS_PER_HOUR = 3            # 1시간 내 최대 자동 재기동 횟수 (무한 루프 방지)



def get_profit_margin(market: str) -> float:
    """해당 마켓의 익절 목표 마진을 반환합니다."""
    _sync_config_from_disk()
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
    from pathlib import Path
    import sys
    from utils.account_coordination import CoordinatedUpbit, register_legacy_process
    client = CoordinatedUpbit(pyupbit.Upbit(ak, sk))
    entry = getattr(sys.modules.get("__main__"), "__file__", "")
    if entry and Path(entry).resolve() == Path(__file__).resolve().parent / "main.py":
        register_legacy_process()
    return client


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
