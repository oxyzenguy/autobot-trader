import sys
import time
import math
import threading
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
        print(f"[{time.strftime('%H:%M:%S')}] [ERROR] [{ticker}] 코인 잔고 조회 오류: {e}")
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
            print(f"[{time.strftime('%H:%M:%S')}] [{market}] [ACTION] 미체결 주문 {len(open_orders)}건 전체 취소 완료.")
    except Exception as e:
        print(f"[{time.strftime('%H:%M:%S')}] [{market}] [ERROR] 미체결 주문 전체 취소 중 오류: {e}")


# --- 실전 매매 로직 (마켓별 독립 루프 지원) ---
def run_trading_strategy(market: str = "KRW-SOL"):
    """마틴게일 배수 물타기 및 리스크 관리 실전 자동매매 메인 루프"""
    ticker = market.split("-")[1]
    sell_profit_margin = get_profit_margin(market)
    unit_krw = INVESTMENTS.get(market, {}).get("unit", MIN_ORDER_KRW)

    try:
        upbit = get_upbit_client()
        print(f"[{market}] 업비트 클라이언트 인증 성공")
    except Exception as e:
        print(f"[{market}] [CRITICAL] 업비트 클라이언트 초기화 실패: {e}")
        return

    print("=" * 60)
    print(f"🚀 업비트 마틴게일 자동매매 시스템 가동: {market}")
    print(f" 대상 마켓: {market} ({ticker})")
    print(f" 1 Unit 금액: {unit_krw:,} KRW")
    print(f" 익절 목표: +{(sell_profit_margin - 1) * 100:.2f}%")
    print(f" 손절 기준: {STOP_LOSS_PERCENT * 100:.2f}% (Stop-Loss)")
    print("=" * 60)

    send_telegram_alert(
        f"🤖 [실전 봇 시작] {market}\n"
        f"1 Unit: {unit_krw:,}원\n"
        f"익절: +{(sell_profit_margin - 1) * 100:.2f}% | 손절: {STOP_LOSS_PERCENT * 100:.2f}%"
    )

    last_heartbeat_time = 0

    while True:
        try:
            # 1. 현재가 및 잔고 조회
            current_price = pyupbit.get_current_price(market)
            if current_price is None:
                print(f"[{time.strftime('%H:%M:%S')}] [{ticker}] [WARN] 현재가 조회 실패. 5초 후 재시도.")
                time.sleep(5)
                continue

            avg_price, quantity = get_my_balance(upbit, ticker)
            total_value = quantity * current_price

            # 2. 리스크 관리: Stop-Loss (손절) 감지
            if total_value >= MIN_ORDER_KRW and avg_price > 0:
                pnl_rate = (current_price - avg_price) / avg_price
                if pnl_rate <= STOP_LOSS_PERCENT:
                    msg = (
                        f"🚨 [STOP-LOSS 긴급 손절 발동] {market}\n"
                        f"현재가: {current_price:,.0f}원 | 평단가: {avg_price:,.0f}원\n"
                        f"수익률: {pnl_rate * 100:.2f}% (기준: {STOP_LOSS_PERCENT * 100:.2f}% 이하)\n"
                        f"보유 수량 {quantity} 전량 시장가 매도 진행."
                    )
                    print(f"\n[{market}] {msg}")
                    send_telegram_alert(msg)

                    cancel_all_orders(upbit, market)
                    time.sleep(0.5)
                    sell_res = upbit.sell_market_order(market, quantity)
                    print(f"[{time.strftime('%H:%M:%S')}] [{market}] [STOP-LOSS] 시장가 매도 결과: {sell_res}")

                    print(f"[{market}] [INFO] 손절 완료 후 5분간 휴식 대기합니다...")
                    time.sleep(300)
                    continue

            # 3. 미체결 주문 목록 조회
            open_orders = upbit.get_order(market, state="wait")
            if open_orders is None:
                print(f"[{time.strftime('%H:%M:%S')}] [{ticker}] [WARN] 미체결 주문 조회 실패. 5초 후 재시도.")
                time.sleep(5)
                continue

            sell_orders = [o for o in open_orders if o['side'] == 'ask']
            buy_orders = [o for o in open_orders if o['side'] == 'bid']
            num_sell, num_buy = len(sell_orders), len(buy_orders)

            # 3-1. 터미널 로깅: 30분 주기 생존 하트비트만 간결하게 출력 (체결 시에만 상세 출력)
            now_sec = time.time()
            if now_sec - last_heartbeat_time >= 1800:
                print(f"[{time.strftime('%H:%M:%S')}] [HEARTBEAT] {ticker}: {current_price:,.0f}원 | 감시 대기 중 (미체결: 매도 {num_sell}건, 매수 {num_buy}건 | 보유: {quantity:.4f})")
                last_heartbeat_time = now_sec

            # 4. 상태 머신 분기
            # Case 1 & 2: 정상 대기 상태 (매도 1건, 매수 3건)
            if num_sell == 1 and num_buy == 3:
                time.sleep(5)
                continue

            # Case 3: 매도 완료 (또는 최초 신규 진입) → 매도 주문 0건
            elif num_sell == 0:
                print(f"\n[{time.strftime('%H:%M:%S')}] [{market}] [ACTION] Case 3: 매도 주문 없음 감지. 사이클 시작/재진입.")

                for order in buy_orders:
                    upbit.cancel_order(order['uuid'])
                    time.sleep(0.1)

                avg_price, quantity = get_my_balance(upbit, ticker)
                if (quantity * current_price) < MIN_ORDER_KRW:
                    krw_balance = get_krw_balance(upbit)
                    if krw_balance < unit_krw:
                        print(f"[{market}] [WARN] 원화 잔고 부족 ({krw_balance:,.0f}원 < 필요: {unit_krw:,.0f}원). 10초 대기.")
                        time.sleep(10)
                        continue

                    print(f"[{market}]   - 1 Unit 시장가 매수 실행: {unit_krw:,} KRW")
                    upbit.buy_market_order(market, unit_krw)
                    time.sleep(1.5)

                    avg_price, quantity = get_my_balance(upbit, ticker)
                    if quantity <= 0 or avg_price <= 0:
                        print(f"[{market}] [WARN] 시장가 매수 후 잔고 반영 지연. 재확인 중...")
                        time.sleep(5)
                        continue

                print(f"[{market}]   - 현재 포지션: 평단가 {avg_price:,.0f}원, 보유수량 {quantity}")

                sell_price = adjust_price_to_tick(avg_price * sell_profit_margin, method="ceil")
                upbit.sell_limit_order(market, sell_price, quantity)
                print(f"[{market}]   - 익절 매도 주문: {sell_price:,.0f}원, 수량 {quantity} (목표: +{(sell_profit_margin - 1) * 100:.2f}%)")
                time.sleep(0.2)

                new_orders = calculate_new_buy_prices(avg_buy_price=avg_price, existing_orders=None)
                print(f"[{market}]   - 신규 마틴게일 매수 주문 계획 (3개): {new_orders}")

                for order in new_orders:
                    p = order['price']
                    u = order['units']
                    order_krw = unit_krw * u
                    volume = round(order_krw / p, 8)
                    upbit.buy_limit_order(market, p, volume)
                    print(f"[{market}]     - 지정가 매수 주문: {p:,.0f}원 | {u} Units ({order_krw:,}원) | 수량: {volume}")
                    time.sleep(0.2)

                send_telegram_alert(
                    f"✅ [사이클 시작] {market}\n"
                    f"진입가: {avg_price:,.0f}원 | 수량: {quantity}\n"
                    f"익절 매도가: {sell_price:,.0f}원\n"
                    f"물타기 3단계(2x, 3x, 6x) 주문 완료"
                )

            # Case 4: 부분 물타기 매수 체결 감지
            elif num_sell == 1 and num_buy <= 2:
                print(f"\n[{time.strftime('%H:%M:%S')}] [{market}] [ACTION] Case 4: 물타기 매수 체결 감지. 포지션 재조정.")

                for order in sell_orders:
                    upbit.cancel_order(order['uuid'])
                    time.sleep(0.1)

                time.sleep(1)
                avg_price, quantity = get_my_balance(upbit, ticker)
                print(f"[{market}]   - 갱신된 포지션: 새 평단가 {avg_price:,.0f}원, 총 보유수량 {quantity}")

                sell_price = adjust_price_to_tick(avg_price * sell_profit_margin, method="ceil")
                upbit.sell_limit_order(market, sell_price, quantity)
                print(f"[{market}]   - 새 익절 매도 주문: {sell_price:,.0f}원, 전량 {quantity}")
                time.sleep(0.2)

                buy_order_details = [{'price': float(o['price']), 'units': 1} for o in buy_orders]
                additional_orders = calculate_new_buy_prices(
                    avg_buy_price=avg_price,
                    existing_orders=buy_order_details
                )
                print(f"[{market}]   - 추가 물타기 매수 계획 ({len(additional_orders)}개): {additional_orders}")

                krw_balance = get_krw_balance(upbit)
                for order in additional_orders:
                    p = order['price']
                    u = order['units']
                    order_krw = unit_krw * u
                    if krw_balance < order_krw:
                        print(f"[{market}]     [WARN] 원화 잔고 부족으로 매수 스킵 ({krw_balance:,.0f}원 < {order_krw:,}원)")
                        continue
                    volume = round(order_krw / p, 8)
                    upbit.buy_limit_order(market, p, volume)
                    krw_balance -= order_krw
                    print(f"[{market}]     - 추가 매수 주문: {p:,.0f}원 | {u} Units ({order_krw:,}원) | 수량: {volume}")
                    time.sleep(0.2)

                send_telegram_alert(
                    f"💧 [물타기 체결 후 재조정] {market}\n"
                    f"새 평단가: {avg_price:,.0f}원 | 보유수량: {quantity}\n"
                    f"새 익절가: {sell_price:,.0f}원\n"
                    f"추가 매수 {len(additional_orders)}건 등록 완료"
                )

            else:
                print(f"[{time.strftime('%H:%M:%S')}] [{ticker}] [WARN] 예외적 주문 상태 (매도 {num_sell}건, 매수 {num_buy}건). 10초 대기.")
                time.sleep(10)

        except Exception as e:
            print(f"[{time.strftime('%H:%M:%S')}] [{market}] [CRITICAL] 메인 루프 에러: {e}")
            time.sleep(10)

        time.sleep(5)


# --- 봇 단일 실행 핸들러 ---
def start_bot(market: str):
    """모드(가상매매 또는 실전)에 따라 봇 실행"""
    if IS_PAPER_TRADING:
        from utils.paper_trading import run_paper_trading_loop
        run_paper_trading_loop(market)
    else:
        run_trading_strategy(market)


# --- 엔트리포인트 (멀티 코인 동시 실행 지원) ---
if __name__ == "__main__":
    # 1. 커맨드라인 인자 파싱 (쉼표로 구분하여 복수 종목 지정 가능: python main.py KRW-SOL,KRW-ETH)
    if len(sys.argv) > 1:
        target_markets = [m.strip() for m in sys.argv[1].split(",") if m.strip()]
    else:
        # 인자가 주어지지 않은 경우: .env의 INVEST_... 설정 종목(예: KRW-SOL, KRW-ETH) 모두 실행!
        target_markets = list(INVESTMENTS.keys())
        if not target_markets:
            target_markets = ["KRW-SOL", "KRW-ETH"]

    print("=" * 65)
    print(f"🚀 AutoBot Trader 다중 종목 자동매매 시스템 시작")
    print(f" - 가상매매(모의투자) 모드: {IS_PAPER_TRADING}")
    print(f" - 실행 대상 마켓: {', '.join(target_markets)}")
    for m in target_markets:
        info = INVESTMENTS.get(m, {})
        margin = (get_profit_margin(m) - 1) * 100
        print(f"   • {m}: 투자금 {info.get('total', 0):,}원 | 1Unit {info.get('unit', 0):,}원 | 익절 목표 +{margin:.2f}%")
    print("=" * 65)

    if len(target_markets) == 1:
        # 단일 종목 실행
        start_bot(target_markets[0])
    else:
        # 다중 종목(SOL, ETH 등) 병렬 스레드 실행
        threads = []
        for m in target_markets:
            t = threading.Thread(target=start_bot, args=(m,), daemon=True, name=f"BotThread-{m}")
            t.start()
            threads.append(t)
            time.sleep(1)  # 초기화 간격

        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\n[INFO] 자동매매 시스템 종료 요청 수신. 프로그램을 종료합니다.")
