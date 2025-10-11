import os
import pyupbit
from dotenv import load_dotenv


def get_upbit_client():
    from pathlib import Path
    env_path = Path(__file__).resolve().parent / ".env"
    load_dotenv(dotenv_path=env_path)

    access_key = os.getenv("UPBIT_ACCESS_KEY")
    secret_key = os.getenv("UPBIT_SECRET_KEY")
    if not access_key or not secret_key:
        raise ValueError("업비트 API 키가 .env 파일에 설정되지 않았습니다. .env 파일을 확인해주세요.")  
    return pyupbit.Upbit(access_key, secret_key)

# 종목별 총 투자금 (원화 기준)
INVESTMENTS = {}

def load_investments():
    """
    .env에서 종목별 총 투자금을 불러오고
    1Unit = 총 투자금 / 200 으로 계산
    """
    investments = {}
    for key, value in os.environ.items():
        if key.startswith("INVEST_"):
            market = key.replace("INVEST_", "").replace("_", "-")  # KRW_BTC -> KRW-BTC
            total_invest = int(value)
            unit = total_invest // 100  # 총 투자금의 1/200
            investments[market] = {
                "total": total_invest,
                "unit": unit
            }
    return investments

INVESTMENTS = load_investments()

if __name__ == "__main__":
    print("[INVESTMENTS 설정 확인]")
    for market, info in INVESTMENTS.items():
        print(f"{market}: 총 투자금={info['total']}원, 1Unit={info['unit']}원")