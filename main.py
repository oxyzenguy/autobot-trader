import sys
import time
import math
import pyupbit
from config import (
    get_upbit_client,
    INVESTMENTS,
    MIN_ORDER_KRW,
    STOP_LOSS_PERCENT,
    IS_PAPER_TRADING,
    get_profit_margin
)
from strategy.matingale2x_logic import calculate_new_buy_prices, adjust_price_to_tick

# --- 트레이딩 타깃 설정 (커맨드라인 인자로 지정 가능: python main.py KRW-ETH) ---
MARKET = sys.argv[1] if len(sys.argv) > 1 else "KRW-SOL"
TICKER = MARKET.split("-")[1]

# 종목 맞춤 익절 마진 (SOL 0.5%, ETH 0.8% 등)
SELL_PROFIT_MARGIN = get_profit_margin(MARKET)

# 1 Unit 매수 금액 (원화 기준)
UNIT_KRW = INVESTMENTS.get(MARKET, {}).get("unit", MIN_ORDER_KRW)


# --- 텔레그램 알림 헬퍼 ---
def send_telegram_alert(message: str):
    """텔레그램 알림을 발송합니다. (실패 시에도 프로그램은 정상 지속)"""
    try:
        from utils.bot import send_message
        send_message(message)
    except Exception as e:
        print(f"[{time.strftime('%H:%M:%S')}] [WARN] 텔레그램 알림 실패: {e}")


# --- 잔고 및 계좌 조회 ---
def get_my_balance(upbit_client, ticker: str):
    """특정 코인의 보유 수량과 평단가를 조회합니다."""
    try:
        balance_info = upbit_client.get_balance(ticker, verbose=True)
        if balance_info and 'avg_buy_price' in balance_info:
            avg_price = float(balance_info['avg_buy_price'])
            quantity = float(balance_info['balance'])
            return avg_price, quantity
    except Exception as e:
        print(f"[{time.strftime('%H:%M:%S')}] [ERROR] 코인 잔고 조회 오류: {e}")
    return 0.0, 0.0


def get_krw_balance(upbit_client):
    """현재 주문 가능한 원화(KRW) 잔고를 조회합니다."""
    try:
        return float(upbit_client.get_balance("KRW"))
    except Exception as e:
        print(f"[{time.strftime('%H:%M:%S')}] [ERROR] KRW 잔고 조회 오류: {e}")
        return 0.0


def cancel_all_orders(upbit_client, market: str):
    """해당 마켓의 모든 미체결 주문(매수/매도)을 취소합니다."""
    try:
        open_orders = upbit_client.get_order(market, state="wait")
        if open_orders:
            for order in open_orders:
                upbit_client.cancel_order(order['uuid'])
                time.sleep(0.1)
            print(f"[{time.strftime('%H:%M:%S')}] [ACTION] 미체결 주문 {len(open_orders)}건 전체 취소 완료.")
    except Exception as e:
        print(f"[{time.strftime('%H:%M:%S')}] [ERROR] 미체결 주문 전체 취소 중 오류: {e}")


# --- 메인 매매 로직 ---
def run_trading_strategy():
    """마틴게일 배수 물타기 및 리스크 관리 자동매매 메인 루프"""
    try:
        upbit = get_upbit_client()
        print(" 업비트 클라이언트 인증 성공")
    except Exception as e:
        print(f"[CRITICAL] 업비트 클라이언트 초기화 실패: {e}")
        return

    print("=" * 60)
    print("🚀 업비트 마틴게일 자동매매 시스템 가동")
    print(f" 대상 마켓: {MARKET}")
    print(f" 1 Unit 금액: {UNIT_KRW:,} KRW")
    print(f" 익절 목표: +{(SELL_PROFIT_MARGIN - 1) * 100:.2f}%")
    print(f" 손절 기준: {STOP_LOSS_PERCENT * 100:.2f}% (Stop-Loss)")
    print("=" * 60)

    send_telegram_alert(
        f"🤖 [봇 시작] {MARKET}\n"
        f"1 Unit: {UNIT_KRW:,}원\n"
        f"익절: +{(SELL_PROFIT_MARGIN - 1) * 100:.2f}% | 손절: {STOP_LOSS_PERCENT * 100:.2f}%"
    )

    while True:
        try:
            # 1. 현재가 및 잔고 조회
            current_price = pyupbit.get_current_price(MARKET)
            if current_price is None:
                print(f"[{time.strftime('%H:%M:%S')}] [WARN] 현재가 조회 실패. 5초 후 재시도.")
                time.sleep(5)
                continue

            avg_price, quantity = get_my_balance(upbit, TICKER)
            total_value = quantity * current_price

            # 2. 리스크 관리: Stop-Loss (손절) 감지
            # 보유 평가금액이 최소 주문금액(5,000원) 이상일 때만 손절 체크
            if total_value >= MIN_ORDER_KRW and avg_price > 0:
                pnl_rate = (current_price - avg_price) / avg_price
                if pnl_rate <= STOP_LOSS_PERCENT:
                    msg = (
                        f"🚨 [STOP-LOSS 긴급 손절 발동] {MARKET}\n"
                        f"현재가: {current_price:,.0f}원 | 평단가: {avg_price:,.0f}원\n"
                        f"수익률: {pnl_rate * 100:.2f}% (기준: {STOP_LOSS_PERCENT * 100:.2f}% 이하)\n"
                        f"보유 수량 {quantity} 전량 시장가 매도 진행."
                    )
                    print(f"\n{msg}")
                    send_telegram_alert(msg)

                    # 미체결 주문 전체 취소 후 전량 시장가 매도
                    cancel_all_orders(upbit, MARKET)
                    time.sleep(0.5)
                    sell_res = upbit.sell_market_order(MARKET, quantity)
                    print(f"[{time.strftime('%H:%M:%S')}] [STOP-LOSS] 시장가 매도 결과: {sell_res}")

                    # 손절 후 안전 대기 (급락 추가 피해 방지: 5분간 대기)
                    print("[INFO] 손절 완료 후 5분간 휴식 대기합니다...")
                    time.sleep(300)
                    continue

            # 3. 미체결 주문 목록 조회
            open_orders = upbit.get_order(MARKET, state="wait")
            if open_orders is None:
                print(f"[{time.strftime('%H:%M:%S')}] [WARN] 미체결 주문 조회 실패. 5초 후 재시도.")
                time.sleep(5)
                continue

            sell_orders = [o for o in open_orders if o['side'] == 'ask']
            buy_orders = [o for o in open_orders if o['side'] == 'bid']
            num_sell, num_buy = len(sell_orders), len(buy_orders)

            print(f"[{time.strftime('%H:%M:%S')}] [INFO] {TICKER} 현재가: {current_price:,.0f}원 | 미체결: 매도 {num_sell}건, 매수 {num_buy}건 (보유: {quantity:.4f})")

            # 4. 상태 머신 분기
            # -------------------------------------------------------------
            # Case 1 & 2: 정상 대기 상태 (매도 1건, 매수 3건)
            # -------------------------------------------------------------
            if num_sell == 1 and num_buy == 3:
                time.sleep(5)
                continue

            # -------------------------------------------------------------
            # Case 3: 매도 완료 (또는 최초 신규 진입) → 매도 주문 0건
            # -------------------------------------------------------------
            elif num_sell == 0:
                print(f"\n[{time.strftime('%H:%M:%S')}] [ACTION] Case 3: 매도 주문 없음 감지. 사이클 시작/재진입.")

                # 기존 잔여 매수 주문 취소
                for order in buy_orders:
                    upbit.cancel_order(order['uuid'])
                    time.sleep(0.1)

                # 만약 이미 코인을 충분히 보유 중이라면(예: 매도만 체결 안 걸린 상태) 신규 매수 건너뜀
                avg_price, quantity = get_my_balance(upbit, TICKER)
                if (quantity * current_price) < MIN_ORDER_KRW:
                    # 1 Unit 시장가 매수
                    krw_balance = get_krw_balance(upbit)
                    if krw_balance < UNIT_KRW:
                        print(f"[WARN] 원화 잔고 부족 ({krw_balance:,.0f}원 < 필요: {UNIT_KRW:,.0f}원). 10초 대기.")
                        time.sleep(10)
                        continue

                    print(f"  - 1 Unit 시장가 매수 실행: {UNIT_KRW:,} KRW")
                    upbit.buy_market_order(MARKET, UNIT_KRW)
                    time.sleep(1.5)  # 체결 대기

                    avg_price, quantity = get_my_balance(upbit, TICKER)
                    if quantity <= 0 or avg_price <= 0:
                        print("[WARN] 시장가 매수 후 잔고 반영 지연. 재확인 중...")
                        time.sleep(5)
                        continue

                print(f"  - 현재 포지션: 평단가 {avg_price:,.0f}원, 보유수량 {quantity}")

                # 평단가 대비 익절 매도 주문 (호가 단위 올림)
                sell_price = adjust_price_to_tick(avg_price * SELL_PROFIT_MARGIN, method="ceil")
                upbit.sell_limit_order(MARKET, sell_price, quantity)
                print(f"  - 익절 매도 주문: {sell_price:,.0f}원, 수량 {quantity} (목표: +{(SELL_PROFIT_MARGIN - 1) * 100:.2f}%)")
                time.sleep(0.2)

                # 신규 물타기 매수 주문 3건 생성 (마틴게일 2x, 3x, 6x Unit)
                new_orders = calculate_new_buy_prices(avg_buy_price=avg_price, existing_orders=None)
                print(f"  - 신규 마틴게일 매수 주문 계획 (3개): {new_orders}")

                for order in new_orders:
                    p = order['price']
                    u = order['units']
                    order_krw = UNIT_KRW * u
                    volume = round(order_krw / p, 8)
                    upbit.buy_limit_order(MARKET, p, volume)
                    print(f"    - 지정가 매수 주문: {p:,.0f}원 | {u} Units ({order_krw:,}원) | 수량: {volume}")
                    time.sleep(0.2)

                send_telegram_alert(
                    f"✅ [사이클 시작] {MARKET}\n"
                    f"진입가: {avg_price:,.0f}원 | 수량: {quantity}\n"
                    f"익절 매도가: {sell_price:,.0f}원\n"
                    f"물타기 3단계(2x, 3x, 6x) 주문 완료"
                )

            # -------------------------------------------------------------
            # Case 4: 부분 물타기 매수 체결 감지 (매도 1건, 매수 2건 이하)
            # -------------------------------------------------------------
            elif num_sell == 1 and num_buy <= 2:
                print(f"\n[{time.strftime('%H:%M:%S')}] [ACTION] Case 4: 물타기 매수 체결 감지. 포지션 재조정.")

                # 기존 매도 주문 취소
                for order in sell_orders:
                    upbit.cancel_order(order['uuid'])
                    time.sleep(0.1)

                time.sleep(1)  # 잔고 갱신 대기
                avg_price, quantity = get_my_balance(upbit, TICKER)
                print(f"  - 갱신된 포지션: 새 평단가 {avg_price:,.0f}원, 총 보유수량 {quantity}")

                # 새로운 평단가 기준 익절 매도 주문 전량 등록
                sell_price = adjust_price_to_tick(avg_price * SELL_PROFIT_MARGIN, method="ceil")
                upbit.sell_limit_order(MARKET, sell_price, quantity)
                print(f"  - 새 익절 매도 주문: {sell_price:,.0f}원, 전량 {quantity}")
                time.sleep(0.2)

                # 기존 미체결 매수 주문 파악 후 부족분 추가 주문
                buy_order_details = [{'price': float(o['price']), 'units': 1} for o in buy_orders]
                additional_orders = calculate_new_buy_prices(
                    avg_buy_price=avg_price,
                    existing_orders=buy_order_details
                )
                print(f"  - 추가 물타기 매수 계획 ({len(additional_orders)}개): {additional_orders}")

                krw_balance = get_krw_balance(upbit)
                for order in additional_orders:
                    p = order['price']
                    u = order['units']
                    order_krw = UNIT_KRW * u
                    if krw_balance < order_krw:
                        print(f"    [WARN] 원화 잔고 부족으로 매수 스킵 ({krw_balance:,.0f}원 < {order_krw:,}원)")
                        continue
                    volume = round(order_krw / p, 8)
                    upbit.buy_limit_order(MARKET, p, volume)
                    krw_balance -= order_krw
                    print(f"    - 추가 매수 주문: {p:,.0f}원 | {u} Units ({order_krw:,}원) | 수량: {volume}")
                    time.sleep(0.2)

                send_telegram_alert(
                    f"💧 [물타기 체결 후 재조정] {MARKET}\n"
                    f"새 평단가: {avg_price:,.0f}원 | 보유수량: {quantity}\n"
                    f"새 익절가: {sell_price:,.0f}원\n"
                    f"추가 매수 {len(additional_orders)}건 등록 완료"
                )

            # -------------------------------------------------------------
            # Case 5: 예외 상태 (매도 주문이 2개 이상 등)
            # -------------------------------------------------------------
            else:
                print(f"[{time.strftime('%H:%M:%S')}] [WARN] 예외적 주문 상태 (매도 {num_sell}건, 매수 {num_buy}건). 10초 대기.")
                time.sleep(10)

        except Exception as e:
            print(f"[{time.strftime('%H:%M:%S')}] [CRITICAL] 메인 루프 에러: {e}")
            time.sleep(10)

        time.sleep(5)


if __name__ == "__main__":
    if IS_PAPER_TRADING:
        from utils.paper_trading import run_paper_trading_loop
        run_paper_trading_loop(MARKET)
    else:
        run_trading_strategy()


