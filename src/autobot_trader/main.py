# src/autobot_trader/main.py

import pyupbit

def main():
    price = pyupbit.get_current_price("KRW-BTC")
    print(f"현재 비트코인 가격: {price}")

if __name__ == "__main__":
    main()
