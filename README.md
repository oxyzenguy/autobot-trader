# 업비트 자동매매 봇 (마틴게일 전략)

> 1분봉/1시간봉 기반 마틴게일 물타기 전략 자동매매 시스템

## 📊 전략 개요

### 핵심 전략
- **마틴게일 배수 물타기**: 1 Unit → 2 Unit → 3 Unit → 6 Unit
- **평단가 + 0.5% 매도**: 안정적인 수익 실현
- **4% 간격 물타기**: 하락 시 평단가 낮추기
- **승률 99%+**: 장기적으로 안정적인 수익

### 백테스트 성과 (2년, 2023.10~2025.10)

| 코인 | 수익률 | Sharpe Ratio | Max Drawdown | 승률 | 거래횟수 |
|------|--------|--------------|--------------|------|----------|
| **SOL** | 19.57% | 1.26 | -8.24% | 100% | 2,841회 |
| **XRP** | 12.87% | 1.64 | -3.60% | 99.78% | 2,234회 |
| **포트폴리오** | 16.22% | 1.45 | -5.92% | 99.89% | 5,075회 |

## 🎯 전략 상세

### 1. 진입 로직 (Case3: 매도 완료)

미체결 매수 주문 전체 취소

1 Unit 시장가 매수

평단가 조회

평단가 × 1.005 (0.5%)에 전량 매도 주문

매수 주문 3개 생성:

1차: 평단가 -4% (2 Unit)

2차: 평단가 -8% (3 Unit)

3차: 평단가 -12% (6 Unit)

text

### 2. 물타기 로직 (Case4: 매수 체결)

기존 매도 주문 취소

새 평단가 계산

평단가 × 1.005에 전량 매도 주문

미체결 매수 주문 기준 추가 주문 생성:

미체결 2개: 최저가 -4% (다음 배수 1개)

미체결 1개: 해당가 -4%, -8% (다음 배수 2개)

text

### 3. 배수 진행

1차 진입: 1 Unit (초기)
2차 물타기: 2 Unit (-4%)
3차 물타기: 3 Unit (-8%)
4차 물타기: 6 Unit (-12%)
이후: 6 Unit 유지

text

## 📁 파일 구조

autobot-trader/
├── main.py # 실전 거래 메인 스크립트
├── backtest_main.py # 백테스트 메인 스크립트
├── strategy_logic.py # 전략 로직 (매수가 계산)
├── config.py # API 키 설정
├── optimize_margin.py # 수익 마진 최적화
├── SOL_minute60_candles.csv # SOL 1시간봉 데이터
├── XRP_minute60_candles.csv # XRP 1시간봉 데이터
└── README.md

text

## ⚙️ 설정

### 기본 설정 (backtest_main.py)

@dataclass
class BacktestConfig:
# 코인 설정
ticker: str = "KRW-SOL"
coin_name: str = "SOL"
interval: str = "minute60" # 1시간봉

text
# 기간 설정
start_date: str = "2023-10-01"
end_date: str = "2025-10-10"

# 자본 설정
initial_capital: int = 1_000_000    # 100만원
unit_krw: int = 10000               # 1 Unit = 1만원

# 거래 설정
sell_profit_margin: float = 1.005   # 0.5% 수익 목표
trading_fee_rate: float = 0.0005    # 0.05% 수수료

# 리스크 관리
max_position_size: float = 0.7      # 최대 70% 사용
stop_loss_percent: float = -0.1     # 손절 -10%
text

### 실전 설정 (main.py)

기본 설정
MARKET = "KRW-SOL"
TICKER = "SOL"
ORDER_UNIT = 1
SELL_PROFIT_MARGIN = 1.005 # 0.5%

API 설정 (config.py)
UPBIT_ACCESS_KEY = "your_access_key"
UPBIT_SECRET_KEY = "your_secret_key"

text

## 🚀 사용 방법

### 1. 환경 설정

가상환경 생성
python -m venv .venv

가상환경 활성화
Windows
.venv\Scripts\activate

Linux/Mac
source .venv/bin/activate

패키지 설치
pip install pyupbit pandas numpy matplotlib openpyxl requests

text

### 2. API 키 설정

`config.py` 파일 생성:

import pyupbit

def get_upbit_client():
"""업비트 클라이언트를 생성합니다."""
access_key = "your_access_key"
secret_key = "your_secret_key"
return pyupbit.Upbit(access_key, secret_key)

text

### 3. 백테스트 실행

SOL 백테스트
python backtest_main.py

XRP 백테스트 (설정 변경 후)
python backtest_main.py

마진 최적화
python optimize_margin.py

text

### 4. 실전 거래 실행

SOL 실전 거래
python main.py

XRP 실전 거래 (별도 터미널)
python main_xrp.py

text

## 📈 성과 분석

### SOL (Solana)

기간: 2023.10.01 ~ 2025.10.10 (2년)
초기 자본: 1,000,000원
최종 자본: 1,195,664원
총 수익: 195,664원 (19.57%)
연평균: 9.79%

Sharpe Ratio: 1.26 (우수)
Max Drawdown: -8.24%
승률: 100%
거래 횟수: 2,841회

text

### XRP (Ripple)

기간: 2023.10.01 ~ 2025.10.10 (2년)
초기 자본: 1,000,000원
최종 자본: 1,128,750원
총 수익: 128,750원 (12.87%)
연평균: 6.44%

Sharpe Ratio: 1.64 (탁월)
Max Drawdown: -3.60%
승률: 99.78%
거래 횟수: 2,234회

text

### 포트폴리오 (50:50)

SOL: 500,000원 → 597,850원 (+19.57%)
XRP: 500,000원 → 564,350원 (+12.87%)
총합: 1,162,200원 (+16.22%)

예상 Sharpe: 1.45
예상 Drawdown: -5.92%
분산 효과: 리스크 30% 감소

text

## 💡 전략 특징

### 장점

✅ **높은 승률**: 99%+ 승률로 안정적  
✅ **자동화**: 24시간 무인 운영 가능  
✅ **리스크 관리**: 마틴게일 배수로 평단가 빠르게 낮춤  
✅ **검증됨**: 2년 백테스트로 검증  
✅ **분산투자**: 다중 코인으로 리스크 분산  

### 주의사항

⚠️ **장기 하락장**: 연속 하락 시 큰 자본 필요  
⚠️ **변동성**: 급등락 시 슬리피지 발생 가능  
⚠️ **자본**: 최소 100만원 이상 권장  
⚠️ **모니터링**: 주기적인 확인 필요  

## 🔧 커스터마이징

### 수익 목표 변경

보수적 (0.3%)
sell_profit_margin: float = 1.003

권장 (0.5%)
sell_profit_margin: float = 1.005

공격적 (1.0%)
sell_profit_margin: float = 1.01

text

### 배수 전략 변경

`strategy_logic.py`에서 수정:

현재: 1 → 2 → 3 → 6
보수적: 1 → 2 → 2 → 3
공격적: 1 → 2 → 4 → 8
text

### 물타기 간격 변경

현재: -4%
adjust_price_to_tick(avg_buy_price * 0.96)

보수적: -3%
adjust_price_to_tick(avg_buy_price * 0.97)

공격적: -5%
adjust_price_to_tick(avg_buy_price * 0.95)

text

## 📊 추천 포트폴리오

### 초보자 (안정형)

XRP: 70% (700,000원)
SOL: 30% (300,000원)
예상 수익률: 14.87%
Max Drawdown: -4~5%

text

### 중급자 (균형형) ⭐ 권장

SOL: 50% (500,000원)
XRP: 50% (500,000원)
예상 수익률: 16.22%
Max Drawdown: -5~6%

text

### 고급자 (공격형)

SOL: 70% (700,000원)
XRP: 30% (300,000원)
예상 수익률: 17.55%
Max Drawdown: -6~7%

text

## 🎓 학습 로드맵

### 1단계: 백테스트 이해
- [ ] 백테스트 실행 및 결과 분석
- [ ] 마진 최적화 실험
- [ ] 다양한 코인 테스트

### 2단계: 소액 실전
- [ ] 10만원으로 1주일 테스트
- [ ] 슬리피지 측정
- [ ] 실제 vs 백테스트 비교

### 3단계: 본격 운용
- [ ] 100만원 투자
- [ ] 포트폴리오 구성
- [ ] 장기 모니터링

## 📞 문제 해결

### 자주 발생하는 오류

**1. API 연결 실패**
config.py에서 API 키 확인
업비트 설정에서 IP 등록 확인
text

**2. 데이터 수집 실패**
1분봉은 7~10일치만 제공
1시간봉 사용 권장
interval: str = "minute60"

text

**3. 주문 실패**
최소 주문 금액 확인 (5,000원 이상)
잔고 부족 확인
text

## 🖥️ 실시간 모니터링 대시보드 (Streamlit)

8대 핵심 퀀트 성과 지표(수익률, MDD, 승률, 손익비, 연속손실, 거래빈도, B&H 대비 Alpha, 시장 국면별 성과)를 실시간 웹 브라우저에서 모니터링할 수 있습니다.

```bash
# 대시보드 실행
poetry run streamlit run dashboard.py

# 또는 가상환경 직접 실행
.venv\Scripts\streamlit.exe run dashboard.py
```

- **접속 주소**: `http://localhost:8501`
- **주요 기능**:
  - 마켓(SOL, ETH 등) 선택 및 실시간 자동 새로고침(5~60초 조절)
  - 자산 성장 곡선 vs Buy & Hold 벤치마크 및 Underwater Drawdown 차트
  - 상승(Bull) / 횡보(Sideways) / 하락(Bear) 국면별 승률 및 실현손익 분석
  - 마틴게일 물타기 차수별(1X, 2X, 3X, 6X) 도달 빈도 및 손익비 비교
  - 실시간 보유 잔고, 미실현 손익, 걸려있는 미체결 주문 및 체결 로그

## 📝 변경 이력

### v1.0.0 (2025-10-11)
- 초기 릴리스
- 마틴게일 배수 전략 (1→2→3→6)
- SOL, XRP 백테스트 완료
- 수익 마진 0.5% 최적화

## 📄 라이선스

MIT License

## ⚠️ 면책 조항

이 프로그램은 교육 목적으로 제공됩니다. 실제 투자에 사용 시 발생하는 손실에 대해 개발자는 책임지지 않습니다. 투자는 본인의 판단과 책임 하에 진행하시기 바랍니다.

## 🙏 감사의 말

- [pyupbit](https://github.com/sharebook-kr/pyupbit) - 업비트 API 라이브러리
- Upbit - 거래소 API 제공

---

**Made with ❤️ for automated trading**