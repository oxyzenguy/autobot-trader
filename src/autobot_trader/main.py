from autobot_trader.strategies.moving_average import get_moving_average_signal
from autobot_trader.telegram_bot import send_message

def main():
    signal = get_moving_average_signal()
    if signal == "buy":
        msg = "📈 [매수 시그널] 비트코인 이동평균 골든크로스 발생"
        print(msg)
        send_message(msg)
    else:
        print("💤 대기 중...")

if __name__ == "__main__":
    main()