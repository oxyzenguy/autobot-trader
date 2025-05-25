import pyupbit
import matplotlib.pyplot as plt
import matplotlib.font_manager as fm
import os
from dotenv import load_dotenv

# .env 불러오기
load_dotenv()

ACCESS_KEY = os.getenv("UPBIT_ACCESS_KEY")
SECRET_KEY = os.getenv("UPBIT_SECRET_KEY")

# 한글 폰트 설정
plt.rcParams['font.family'] = 'Malgun Gothic'
plt.rcParams['axes.unicode_minus'] = False

# 업비트 API 객체
upbit = pyupbit.Upbit(ACCESS_KEY, SECRET_KEY)

# 마켓 필터
valid_tickers = pyupbit.get_tickers(fiat="KRW")
balances = upbit.get_balances()

labels = []
values = []

for b in balances:
    currency = b['currency']
    if b['balance'] == '0' or currency == "KRW":
        continue

    ticker = f"KRW-{currency}"
    if ticker not in valid_tickers:
        print(f"❌ 제외됨: {ticker}")
        continue

    amount = float(b['balance'])
    price = pyupbit.get_current_price(ticker)
    if price is None:
        print(f"❌ 가격 조회 실패: {ticker}")
        continue

    value = amount * price
    labels.append(currency)
    values.append(value)

# 시각화
if labels and values:
    plt.figure(figsize=(8, 6))
    plt.pie(values, labels=labels, autopct="%1.1f%%", startangle=140)
    plt.title("📊 업비트 포트폴리오 보유 비중")
    plt.axis("equal")
    plt.tight_layout()
    plt.show()
else:
    print("⚠️ 시각화할 자산이 없습니다.")
