import time
import pyupbit
import math
from config import get_upbit_client  # config.py에서 upbit 클라이언트 가져오기
from strategy.matingale2x_logic import calculate_new_buy_prices  # strategy_logic.py에서 로직 함수 가져오기


# --- 설정 ---
MARKET = "KRW-SOL"  # 거래할 마켓
TICKER = "SOL"      # 거래할 코인
ORDER_UNIT = 1     # 한 번에 주문할 수량 (1 Unit)
SELL_PROFIT_MARGIN = 1.005  # ← 0.5% 수익률로 변경 (기존 0.11%)


# --- 유틸리티 함수 ---
def get_my_balance(upbit_client, ticker):
    """특정 코인의 보유 수량과 평단가를 조회합니다."""
    try:
        balance_info = upbit_client.get_balance(ticker, verbose=True)
        if balance_info and 'avg_buy_price' in balance_info:
            avg_price = float(balance_info['avg_buy_price'])
            quantity = float(balance_info['balance'])
            return avg_price, quantity
    except Exception as e:
        print(f"[ERROR] 잔고 조회 중 오류 발생: {e}")
    return None, None


# --- 메인 로직 ---
def run_trading_strategy():
    """전체 거래 로직을 실행하는 메인 함수"""
    try:
        upbit = get_upbit_client()
        print("업비트 클라이언트 초기화 완료")
    except Exception as e:
        print(f"[CRITICAL] 업비트 클라이언트 초기화 실패: {e}")
        return

    print("="*50)
    print("자동 매매 프로그램을 시작합니다.")
    print(f"대상 마켓: {MARKET}")
    print(f"주문 단위: {ORDER_UNIT} {TICKER}")
    print(f"매도 수익률: {(SELL_PROFIT_MARGIN - 1) * 100:.2f}%")  # ← 소수점 2자리로 변경
    print("="*50)

    while True:
        try:
            # 1. 미체결 주문 목록 조회
            open_orders = upbit.get_order(MARKET, state="wait")
            if open_orders is None:
                print(f"[{time.strftime('%H:%M:%S')}] [WARN] 미체결 주문 조회 실패. 10초 후 재시도.")
                time.sleep(10)
                continue

            sell_orders = [o for o in open_orders if o['side'] == 'ask']
            buy_orders = [o for o in open_orders if o['side'] == 'bid']
            num_sell, num_buy = len(sell_orders), len(buy_orders)

            print(f"\n[{time.strftime('%H:%M:%S')}] [INFO] 미체결 주문 확인: 매도 {num_sell}건, 매수 {num_buy}건")

            # 2. 만약 '미체결 매도 주문이 1개, 미체결 매수 주문이 3개'라면 2초 기다리고 다시 1번으로 돌아간다.
            if num_sell == 1 and num_buy == 3:
                print("[STATUS] 정상 상태. 2초 후 다음 확인.")
                time.sleep(2)

            # 3. 만약 '미체결 매도 주문이 0개'(매도 완료)라면,
            elif num_sell == 0:
                print("[ACTION] Case 3: 매도 완료 감지. 포지션 재설정 시작.")
                # 3-1. 모든 미체결 매수 주문을 취소한다.
                for order in buy_orders:
                    upbit.cancel_order(order['uuid'])
                    print(f"  - 매수 주문 취소: {order['uuid']}")
                    time.sleep(0.2)  # API 요청 간격

                # 3-2. 시장가 매수로 1Unit만큼을 매수한다.
                print(f"  - 시장가 매수 실행: {MARKET}, {ORDER_UNIT} {TICKER}")
                upbit.buy_market_order(MARKET, ORDER_UNIT * pyupbit.get_current_price(MARKET))  # 수량 * 현재가로 원화 환산
                time.sleep(1)  # 체결 대기

                # 3-3. 해당 market의 보유하고 있는 평단가와 보유 수량을 API를 통해 불러온다.
                avg_price, quantity = get_my_balance(upbit, TICKER)
                if avg_price is None or quantity is None:
                    time.sleep(10)
                    continue
                print(f"  - 현재 잔고: 평단가 {avg_price:.2f}, 보유수량 {quantity}")

                # 3-4. 현재 평균 단가 대비 0.5%에 모든 보유 수량의 매도 주문을 건다.
                sell_price = math.ceil(avg_price * SELL_PROFIT_MARGIN)
                upbit.sell_limit_order(MARKET, sell_price, quantity)
                print(f"  - 신규 매도 주문: 가격 {sell_price}, 수량 {quantity}")
                time.sleep(0.2)

                # 3-5. 로직 코드에서 매수 가격 3개의 리스트를 받아서 해당 가격에 1Unit어치의 매수 주문을 건다.
                new_buy_prices = calculate_new_buy_prices(avg_price=avg_price)
                print(f"  - 신규 매수 가격 계산 (3개): {new_buy_prices}")
                for price in new_buy_prices:
                    upbit.buy_limit_order(MARKET, price, ORDER_UNIT)
                    print(f"    - 신규 매수 주문: 가격 {price}, 수량 {ORDER_UNIT}")
                    time.sleep(0.2)

            # 4. 만약 '미체결 매도 주문이 1개, 미체결 매수 주문이 2개 이하'라면,
            elif num_sell == 1 and num_buy <= 2:
                print(f"[ACTION] Case 4: 부분 매수 체결 감지. 주문 재조정 시작.")
                # 4-1. 기존 미체결 매도 주문 1개를 취소한다.
                upbit.cancel_order(sell_orders[0]['uuid'])
                print(f"  - 기존 매도 주문 취소: {sell_orders[0]['uuid']}")
                time.sleep(0.2)

                # 4-2. 해당 market의 보유하고 있는 평단가와 보유 수량을 API를 통해 불러온다.
                avg_price, quantity = get_my_balance(upbit, TICKER)
                if avg_price is None or quantity is None:
                    time.sleep(10)
                    continue
                print(f"  - 현재 잔고: 평단가 {avg_price:.2f}, 보유수량 {quantity}")

                # 4-3. 현재 평균 단가 대비 0.5%에 모든 보유 수량의 매도 주문을 건다.
                sell_price = math.ceil(avg_price * SELL_PROFIT_MARGIN)
                upbit.sell_limit_order(MARKET, sell_price, quantity)
                print(f"  - 신규 매도 주문: 가격 {sell_price}, 수량 {quantity}")
                time.sleep(0.2)

                # 4-4. 로직 코드에서 매수 리스트를 받아서 받은 리스트 개수만큼 해당 가격에 1Unit어치의 매수 주문을 건다.
                buy_order_details = [{'price': float(o['price'])} for o in buy_orders]
                new_buy_prices = calculate_new_buy_prices(open_buy_orders=buy_order_details)
                print(f"  - 신규 매수 가격 계산 ({len(new_buy_prices)}개): {new_buy_prices}")
                for price in new_buy_prices:
                    upbit.buy_limit_order(MARKET, price, ORDER_UNIT)
                    print(f"    - 신규 매수 주문: 가격 {price}, 수량 {ORDER_UNIT}")
                    time.sleep(0.2)
            
            else:
                print(f"[WARN] 예외 상태 감지. 수동 확인이 필요할 수 있습니다. 60초 후 재시도.")
                time.sleep(60)

        except Exception as e:
            print(f"[CRITICAL] 메인 루프에서 오류 발생: {e}")
            time.sleep(60)  # 오류 발생 시 잠시 대기 후 재시도

        # 전체 루프 반복 대기
        time.sleep(10)


if __name__ == "__main__":
    run_trading_strategy()
