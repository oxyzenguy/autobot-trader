import time
from api.orders import get_open_orders, create_order, cancel_orders
from api.account import get_account
from config import INVESTMENTS
from strategy.logic import trading_logic

def main():
    market = "KRW-XRP"
    unit_krw = INVESTMENTS[market]["unit"]

    print(f"[INIT] Market={market}, Unit={unit_krw} KRW")

    while True:
        try:
            # 1. 체결 대기 주문 목록 조회
            orders = get_open_orders(market)
            if orders is None:
                print("[WARN] 주문 목록 조회 실패")
                time.sleep(2)
                continue

            sell_orders = [o for o in orders if o["side"] == "ask"]
            buy_orders = [o for o in orders if o["side"] == "bid"]

            # 2. 매도=1, 매수=3 → 대기
            if len(sell_orders) == 1 and len(buy_orders) == 3:
                print("[INFO] 매도=1, 매수=3 → 대기")
                time.sleep(2)
                continue

            # 3. 매도=0 (매도 완료) → Case3
            elif len(sell_orders) == 0:
                print("[INFO] 매도 주문 없음 → Case3")

                # 3-1. 모든 미체결 매수 주문 취소
                if buy_orders:
                    uuids = [o["uuid"] for o in buy_orders]
                    cancel_result = cancel_orders(uuids=uuids)
                    print("[CANCEL BUY ORDERS]", cancel_result)

                # 3-2. 시장가 매수 (1Unit)
                create_order(market, "bid", None, str(unit_krw), "price")
                print(f"[BUY] 시장가 매수 {unit_krw} KRW")

                # 3-3. 보유 자산 조회 (평단가, 수량)
                account = get_account()
                xrp_info = next((a for a in account if a["currency"] == "XRP"), None)
                avg_price = float(xrp_info["avg_buy_price"])
                volume = float(xrp_info["balance"])
                print(f"[ACCOUNT] 평단가={avg_price}, 보유수량={volume}")

                # 3-4. 평균 단가 대비 +0.11%에 매도 주문 (먼저 매도)
                sell_price = round(avg_price * 1.0011)
                create_order(market, "ask", str(volume), str(sell_price), "limit")
                print(f"[SELL ORDER] {sell_price}원에 전량({volume}) 매도")

                # 3-5. 로직 실행 → 매수 리스트 3개 (그 후 매수)
                buy_price_list = trading_logic(avg_price=avg_price)
                for price in buy_price_list:
                    volume_calc = round(unit_krw / price, 6)  # 수량 환산
                    create_order(market, "bid", str(volume_calc), str(price), "limit")
                    print(f"[BUY ORDER] {price}원, 수량={volume_calc}")

            # 4. 매도=1, 매수=2 이하 → Case4
            elif len(sell_orders) == 1 and len(buy_orders) <= 2:
                print("[INFO] 매도=1, 매수≤2 → Case4")

                # 4-1. 기존 매도 주문 취소
                cancel_result = cancel_orders(uuids=[sell_orders[0]["uuid"]])
                print("[CANCEL SELL ORDER]", cancel_result)

                # 4-2. 보유 자산 조회
                account = get_account()
                xrp_info = next((a for a in account if a["currency"] == "XRP"), None)
                avg_price = float(xrp_info["avg_buy_price"])
                volume = float(xrp_info["balance"])
                print(f"[ACCOUNT] 평단가={avg_price}, 보유수량={volume}")

                # 4-3. 평균 단가 대비 +0.11%에 매도 주문 (먼저 매도)
                sell_price = round(avg_price * 1.0011)
                create_order(market, "ask", str(volume), str(sell_price), "limit")
                print(f"[SELL ORDER] {sell_price}원에 전량({volume}) 매도")

                # 4-4. 로직 실행 → 부족한 매수 주문 추가 (그 후 매수)
                buy_price_list = trading_logic(buy_orders=buy_orders)
                for price in buy_price_list:
                    volume_calc = round(unit_krw / price, 6)  # 수량 환산
                    create_order(market, "bid", str(volume_calc), str(price), "limit")
                    print(f"[BUY ORDER] {price}원, 수량={volume_calc}")

            else:
                print("[INFO] 조건 미충족 → 대기")

            time.sleep(2)

        except Exception as e:
            print("[ERROR]", e)
            time.sleep(2)


if __name__ == "__main__":
    main()
