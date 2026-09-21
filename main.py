import os
import sys
import time
import json
import math
import threading
import pyupbit
from datetime import datetime, timedelta
from config import (
    get_upbit_client,
    INVESTMENTS,
    MIN_ORDER_KRW,
    STOP_LOSS_PERCENT,
    MIN_KRW_ALERT_THRESHOLD,
    PROTECTED_BALANCES,
    get_profit_margin,
    USE_TRAILING_STOP,
    TRAILING_STOP_TRIGGER,
    TRAILING_STOP_DROP,
    USE_BULL_PYRAMID,
    PYRAMID_STEP_PCT,
    MAX_PYRAMID_STEPS,
    USE_BULL_CLOSING_BUY,
    USE_MAGIC_SPLIT_DEFENSE,
    MAGIC_SPLIT_TRANCHE_PROFIT,
    MAGIC_SPLIT_DOWN_PCT
)
from strategy.matingale2x_logic import calculate_new_buy_prices, adjust_price_to_tick
from strategy.hybrid_regime import get_hybrid_regime_and_signals, check_magic_split_exits, check_daily_closing_buy_condition
from utils.db_logger import log_real_trade, init_db

# 예수금 알림 쿨다운 관리
LAST_KRW_ALERT_TIME = 0
KRW_ALERT_COOLDOWN_SEC = 3600  # 1시간 쿨다운 (도배 방지)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


# --- 텔레그램 알림 헬퍼 ---
def send_telegram_alert(message: str):
    """텔레그램 알림을 발송합니다. (실패 시에도 프로그램은 정상 지속)"""
    try:
        from utils.bot import send_message
        send_message(message)
    except Exception as e:
        print(f"[{time.strftime('%H:%M:%S')}] [WARN] 텔레그램 알림 실패: {e}")


# --- 상태 파일 로드 및 저장 (실전 차수/트레일링 추적) ---
def get_state_file_path(market: str) -> str:
    return os.path.join(BASE_DIR, f"real_strategy_state_{market.replace('-', '_')}.json")


def load_strategy_state(market: str) -> dict:
    """실전 매매 차수(Tranches) 및 트레일링 스탑 상태 로드"""
    path = get_state_file_path(market)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                # 보호 수량 필드 보장
                if "protected_quantity" not in data:
                    data["protected_quantity"] = PROTECTED_BALANCES.get(market, 0.0)
                return data
        except Exception:
            pass
    return {
        "market": market,
        "current_regime": "BEAR",
        "active_mode": "MARTINGALE_MAGIC_SPLIT",
        "bot_quantity": 0.0,
        "bot_avg_price": 0.0,
        "protected_quantity": PROTECTED_BALANCES.get(market, 0.0),
        "initial_entry_done": False,
        "trend_peak_price": 0.0,
        "trailing_stop_active": False,
        "tranches": [],
        "completed_cycles": 0
    }


def save_strategy_state(market: str, state: dict):
    """실전 매매 상태 영속 저장"""
    path = get_state_file_path(market)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"[{market}] 상태 파일 저장 실패: {e}")


# --- 잔고 및 계좌 조회 (기존 자산 보호 & 봇 전용 포지션 관리) ---
def get_bot_balance(upbit_client, market: str, ticker: str, state: dict):
    """
    기존 보유 자산(PROTECTED_BALANCES)을 완전히 보호/격리하고,
    오직 봇이 신규 매수한 포지션의 평단가와 수량만을 안전하게 반환합니다.
    """
    bot_qty = float(state.get("bot_quantity", 0.0))
    bot_avg = float(state.get("bot_avg_price", 0.0))

    if bot_qty <= 0.0:
        return 0.0, 0.0

    try:
        balance_info = upbit_client.get_balance(ticker, verbose=True)
        if balance_info and 'balance' in balance_info:
            total_avail = float(balance_info['balance'])
            # 실제 업비트 가용 수량 내에서만 매도 가능하도록 초과분 캡핑
            safe_qty = min(bot_qty, total_avail)
            return bot_avg, safe_qty
    except Exception as e:
        print(f"[{time.strftime('%H:%M:%S')}] [ERROR] [{ticker}] 봇 잔고 확인 오류: {e}")

    return bot_avg, bot_qty


def get_my_balance(upbit_client, ticker: str):
    """(계좌 전체) 특정 코인의 보유 수량과 평단가를 조회합니다."""
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


def check_krw_balance_alert(upbit_client, context: str = "정기 감시") -> float:
    """
    예수금(주문가능 KRW)이 10만원 미만으로 떨어졌을 때 텔레그램 및 콘솔로 경고 발송
    """
    global LAST_KRW_ALERT_TIME
    krw = get_krw_balance(upbit_client)
    now = time.time()

    if krw < MIN_KRW_ALERT_THRESHOLD:
        if now - LAST_KRW_ALERT_TIME > KRW_ALERT_COOLDOWN_SEC:
            msg = (
                f"🚨 <b>[예수금 10만원 미만 긴급 알림]</b>\n\n"
                f"현재 주문 가능 예수금: <b>{krw:,.0f}원</b>\n"
                f"설정 기준치: <b>{MIN_KRW_ALERT_THRESHOLD:,.0f}원 미만</b>\n"
                f"감시 상황: {context}\n\n"
                f"⚠️ <i>예수금이 부족하여 추가 물타기 또는 신규 진입이 제한될 수 있습니다.\n"
                f"원화를 추가 입금하거나 다른 자산을 매도해 주세요.</i>"
            )
            print(f"\n[{time.strftime('%H:%M:%S')}] 🚨 [ALERT] 예수금 10만원 미만 감지 ({krw:,.0f}원)! 텔레그램 경고 발송.")
            send_telegram_alert(msg)
            LAST_KRW_ALERT_TIME = now
    return krw


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


# =============================================================================
# 실전 하이브리드 매매 로직 (상승장 추세 + 하락장 마틴게일 매직스플릿 이중익절)
# =============================================================================
def run_trading_strategy(market: str = "KRW-SOL"):
    """
    하이브리드 국면전환 실전 자동매매 메인 루프:
    - 상승장(BULL, 200 MA 상회): 5/20 MA 추세추종 & 트레일링 스탑
    - 하락장(BEAR, 200 MA 하회): 마틴게일 배수 진입 + 하이브리드 매직스플릿 이중익절 (개별 +3% OR 바스켓 익절)
    """
    init_db()
    ticker = market.split("-")[1]
    sell_profit_margin = get_profit_margin(market)
    unit_krw = INVESTMENTS.get(market, {}).get("unit", MIN_ORDER_KRW)

    try:
        upbit = get_upbit_client()
        print(f"[{market}] 업비트 클라이언트 인증 성공")
    except Exception as e:
        print(f"[{market}] [CRITICAL] 업비트 클라이언트 초기화 실패: {e}")
        return

    state = load_strategy_state(market)

    print("=" * 65)
    print(f"🚀 업비트 하이브리드 실전 자동매매 시스템 가동: {market}")
    print(f" 대상 마켓: {market} ({ticker})")
    print(f" 1 Unit 금액: {unit_krw:,} KRW")
    print(f" 상승 국면 (BULL): 5/20 MA 추세추종 + 트레일링 스탑 (+10% 도달 시 -3% 익절)")
    print(f" 하락 국면 (BEAR): 마틴게일 배수 진입 + 매직스플릿 이중익절 (개별 +3.0% OR 바스켓 +{(sell_profit_margin - 1) * 100:.2f}%)")
    print(f" 손절 기준: {STOP_LOSS_PERCENT * 100:.2f}% (Stop-Loss)")
    print(f" 예수금 알림 기준: {MIN_KRW_ALERT_THRESHOLD:,}원 미만 시 텔레그램 발송")
    print("=" * 65)

    check_krw_balance_alert(upbit, context=f"{market} 실전 봇 시작")

    send_telegram_alert(
        f"🤖 <b>[하이브리드 실전 매매 가동]</b>\n"
        f"종목: <b>{market}</b>\n"
        f"1 Unit: {unit_krw:,}원\n"
        f"하락장 방어: <b>마틴게일 배수 진입 + 매직스플릿 이중익절(개별+3%/바스켓)</b>\n"
        f"상승장 추세: 5/20 MA 추세추종 & 트레일링 스탑"
    )

    last_heartbeat_time = 0

    while True:
        try:
            # 1. 예수금 체크
            check_krw_balance_alert(upbit, context=f"{market} 실시간 감시")

            # 2. 실시간 시세 및 국면 판별
            current_price = pyupbit.get_current_price(market)
            if current_price is None:
                print(f"[{time.strftime('%H:%M:%S')}] [{ticker}] [WARN] 현재가 조회 실패. 5초 후 재시도.")
                time.sleep(5)
                continue

            regime_info = get_hybrid_regime_and_signals(market)
            is_bull = regime_info.get("is_bull", False)
            signal = regime_info.get("signal", "HOLD")
            regime_str = regime_info.get("regime_korean", "국면 분석 중")
            curr_ma5 = float(regime_info.get("ma5", 0.0))

            state["current_regime"] = "BULL" if is_bull else "BEAR"

            # 3. 봇 전용 잔고 조회 (기존 자산 완전 격리/보호)
            avg_price, quantity = get_bot_balance(upbit, market, ticker, state)
            total_value = quantity * current_price

            # 4. 리스크 관리: Stop-Loss (손절) 감지
            # 하락장(BEAR) 국면은 매직스플릿 방어 모듈(손절 없이 반등 시 +3% 익절 및 바스켓 탈출)을 적용하므로 손절 제외
            if is_bull and total_value >= MIN_ORDER_KRW and avg_price > 0:
                pnl_rate = (current_price - avg_price) / avg_price
                if pnl_rate <= STOP_LOSS_PERCENT:
                    loss_krw = (current_price - avg_price) * quantity
                    msg = (
                        f"🚨 <b>[STOP-LOSS 긴급 손절 발동]</b> {market}\n"
                        f"현재가: {current_price:,.0f}원 | 봇 평단가: {avg_price:,.0f}원\n"
                        f"수익률: {pnl_rate * 100:.2f}% (기준: {STOP_LOSS_PERCENT * 100:.2f}% 이하)\n"
                        f"봇 보유 수량 {quantity:.6f} 전량 시장가 매도 진행. (기존 보유 자산은 안전 보호)"
                    )
                    print(f"\n[{market}] {msg}")
                    send_telegram_alert(msg)

                    cancel_all_orders(upbit, market)
                    time.sleep(0.5)
                    sell_res = upbit.sell_market_order(market, quantity)
                    print(f"[{time.strftime('%H:%M:%S')}] [{market}] [STOP-LOSS] 매도 결과: {sell_res}")

                    log_real_trade(
                        market=market,
                        ticker=ticker,
                        side="ask",
                        action="STOP_LOSS",
                        price=current_price,
                        volume=quantity,
                        cost_or_revenue=quantity * current_price,
                        pnl=loss_krw,
                        strategy="HYBRID_MARTINGALE_MAGIC_SPLIT"
                    )

                    state["bot_quantity"] = 0.0
                    state["bot_avg_price"] = 0.0
                    state["tranches"] = []
                    state["trend_peak_price"] = 0.0
                    state["trailing_stop_active"] = False
                    state["initial_entry_done"] = True
                    save_strategy_state(market, state)

                    print(f"[{market}] [INFO] 손절 완료 후 5분간 휴식 대기...")
                    time.sleep(300)
                    continue

            # 5. 미체결 주문 목록 조회
            open_orders = upbit.get_order(market, state="wait")
            if open_orders is None:
                print(f"[{time.strftime('%H:%M:%S')}] [{ticker}] [WARN] 미체결 주문 조회 실패. 5초 후 재시도.")
                time.sleep(5)
                continue

            sell_orders = [o for o in open_orders if o['side'] == 'ask']
            buy_orders = [o for o in open_orders if o['side'] == 'bid']
            num_sell, num_buy = len(sell_orders), len(buy_orders)

            # 6. 하트비트 로깅 (30분 주기)
            now_sec = time.time()
            if now_sec - last_heartbeat_time >= 1800:
                krw_bal = get_krw_balance(upbit)
                mode_label = "상승 추세모드" if is_bull else "하락 마틴-매직스플릿 방어모드"
                prot_q = PROTECTED_BALANCES.get(market, 0.0)
                print(f"[{time.strftime('%H:%M:%S')}] [HEARTBEAT] {ticker}: {current_price:,.0f}원 | [{regime_str} / {mode_label}] | 예수금: {krw_bal:,.0f}원 (미체결: 매도 {num_sell}, 매수 {num_buy} | 봇 보유: {quantity:.6f} | 보호 자산: {prot_q:.4f})")
                last_heartbeat_time = now_sec

            # =========================================================================
            # [분기 A] 하락 국면 (BEAR): 마틴게일 배수 진입 + 하이브리드 매직스플릿 이중익절
            # =========================================================================
            if not is_bull:
                state["active_mode"] = "MARTINGALE_MAGIC_SPLIT"

                # A-1. 매직스플릿 이중익절(Dual Exit) 검사: 포지션 보유 중일 때
                if total_value >= MIN_ORDER_KRW and avg_price > 0:
                    exit_decision = check_magic_split_exits(
                        tranches=state.get("tranches", []),
                        current_price=current_price,
                        avg_buy_price=avg_price,
                        profit_margin=sell_profit_margin,
                        tranche_profit_pct=MAGIC_SPLIT_TRANCHE_PROFIT
                    )

                    # [Exit 1: 바스켓 전량 익절]
                    if exit_decision["exit_type"] == "BASKET":
                        print(f"\n[{market}] 🎯 [바스켓 익절] {exit_decision['reason']}")
                        cancel_all_orders(upbit, market)
                        time.sleep(0.5)

                        sell_res = upbit.sell_market_order(market, quantity)
                        real_pnl = (current_price - avg_price) * quantity

                        send_telegram_alert(
                            f"🎯 <b>[바스켓 전량 익절 완료]</b> {market}\n"
                            f"체결단가: {current_price:,.0f}원 | 평단가: {avg_price:,.0f}원\n"
                            f"수익률: {exit_decision['pnl_pct']:+.2f}%\n"
                            f"실현손익: <b>{real_pnl:+,.0f}원</b>"
                        )

                        log_real_trade(
                            market=market,
                            ticker=ticker,
                            side="ask",
                            action="BASKET_TAKE_PROFIT",
                            price=current_price,
                            volume=quantity,
                            cost_or_revenue=quantity * current_price,
                            pnl=real_pnl,
                            strategy="HYBRID_MARTINGALE_MAGIC_SPLIT"
                        )

                        state["bot_quantity"] = 0.0
                        state["bot_avg_price"] = 0.0
                        state["tranches"] = []
                        state["initial_entry_done"] = True
                        state["completed_cycles"] = state.get("completed_cycles", 0) + 1
                        save_strategy_state(market, state)
                        time.sleep(10)
                        continue

                    # [Exit 2: 개별 차수 매직스플릿 +3% 단독 익절 (합산 1회 매도, API 검증 및 평단가 재계산)]
                    elif exit_decision["exit_type"] == "TRANCHE" and USE_MAGIC_SPLIT_DEFENSE:
                        eligible = exit_decision["eligible_tranches"]
                        valid_tranches = [tr for tr in eligible if 0 < tr.get("volume", 0) <= quantity]

                        if valid_tranches:
                            tot_sell_vol = sum(tr["volume"] for tr in valid_tranches)
                            tot_sell_krw = tot_sell_vol * current_price

                            if tot_sell_krw >= MIN_ORDER_KRW:
                                sold_steps = [tr["step"] for tr in valid_tranches]
                                print(f"\n[{market}] 💧 [매직스플릿 차수 합산 익절] {sold_steps}차수 일괄 매도 실행: {tot_sell_vol:.6f} {ticker} ({tot_sell_krw:,.0f}원)")

                                # 1. 기존 바스켓 매도 주문 취소 (코인 잔고 잠금 해제)
                                for order in sell_orders:
                                    upbit.cancel_order(order['uuid'])
                                    time.sleep(0.1)

                                time.sleep(0.3)
                                sell_res = upbit.sell_market_order(market, tot_sell_vol)
                                time.sleep(0.5)

                                # 2. API 성공 여부 검증 (uuid 확인)
                                if isinstance(sell_res, dict) and "uuid" in sell_res:
                                    tot_pnl = sum((current_price - tr["buy_price"]) * tr["volume"] for tr in valid_tranches)

                                    for tr in valid_tranches:
                                        tr_pnl = (current_price - tr["buy_price"]) * tr["volume"]
                                        log_real_trade(
                                            market=market,
                                            ticker=ticker,
                                            side="ask",
                                            action=f"TRANCHE_TAKE_PROFIT_STEP_{tr['step']}",
                                            price=current_price,
                                            volume=tr["volume"],
                                            cost_or_revenue=tr["volume"] * current_price,
                                            pnl=tr_pnl,
                                            strategy="HYBRID_MARTINGALE_MAGIC_SPLIT"
                                        )

                                    # 3. 체결된 차수 장부에서 제거
                                    state["tranches"] = [t for t in state.get("tranches", []) if t.get("step") not in sold_steps]

                                    # 4. 남은 차수들을 기준으로 평단가 및 봇 수량 재계산
                                    remaining = state.get("tranches", [])
                                    if remaining:
                                        new_tot_vol = sum(t["volume"] for t in remaining)
                                        new_tot_cost = sum(t["volume"] * t["buy_price"] for t in remaining)
                                        new_avg = new_tot_cost / new_tot_vol if new_tot_vol > 0 else 0.0
                                        state["bot_avg_price"] = new_avg
                                        state["bot_quantity"] = new_tot_vol

                                        # 잔여 포지션 바스켓 매도 주문 즉시 갱신
                                        rem_sell_price = adjust_price_to_tick(new_avg * sell_profit_margin, method="ceil")
                                        upbit.sell_limit_order(market, rem_sell_price, new_tot_vol)
                                        print(f"[{market}]   - 잔여 포지션 바스켓 익절 매도 재등록: {rem_sell_price:,.0f}원, {new_tot_vol:.6f}")
                                    else:
                                        state["bot_avg_price"] = 0.0
                                        state["bot_quantity"] = 0.0

                                    save_strategy_state(market, state)

                                    steps_str = ", ".join([f"{s}차" for s in sold_steps])
                                    send_telegram_alert(
                                        f"💧 <b>[매직스플릿 차수 일괄 익절 완료]</b> {market}\n"
                                        f"익절 차수: {steps_str} (+3.0% 반등)\n"
                                        f"체결단가: {current_price:,.0f}원 | 합산수량: {tot_sell_vol:.6f}\n"
                                        f"실현손익: <b>{tot_pnl:+,.0f}원</b>\n"
                                        f"새 평단가: {state['bot_avg_price']:,.0f}원 | 잔여 차수: {len(state['tranches'])}개"
                                    )
                                    time.sleep(2)
                                    continue
                                else:
                                    err_msg = sell_res.get("error", {}).get("message", str(sell_res)) if isinstance(sell_res, dict) else str(sell_res)
                                    print(f"[{market}] ⚠️ [매직스플릿 매도 실패] 거래소 오류로 상태 보존: {err_msg}")
                                    send_telegram_alert(f"⚠️ <b>[매직스플릿 매도 실패]</b> {market}\n주문 실패 사유: {err_msg}\n상태를 보존하고 다음 루프에서 재시도합니다.")
                            else:
                                print(f"[{market}] ℹ️ [매직스플릿] 매도 대상 금액({tot_sell_krw:,.0f}원)이 최소주문금액(5,000원) 미만이므로 바스켓 익절 대기.")

                # A-2. 마틴게일 상태 머신 (주문 등록 및 물타기)
                # Case 1 & 2: 정상 대기 상태 (매도 1건, 매수 3건)
                if num_sell == 1 and num_buy == 3:
                    time.sleep(5)
                    continue

                # Case 3: 매도 완료 (또는 신규 진입) → 매도 주문 0건
                elif num_sell == 0:
                    print(f"\n[{time.strftime('%H:%M:%S')}] [{market}] [하락장 방어] Case 3: 매도 주문 없음 감지. 사이클 시작/재진입.")

                    for order in buy_orders:
                        upbit.cancel_order(order['uuid'])
                        time.sleep(0.1)

                    avg_price, quantity = get_bot_balance(upbit, market, ticker, state)
                    if (quantity * current_price) < MIN_ORDER_KRW:
                        krw_balance = check_krw_balance_alert(upbit, context=f"{market} 1차 진입 전")
                        if krw_balance < unit_krw:
                            print(f"[{market}] [WARN] 원화 잔고 부족 ({krw_balance:,.0f}원 < 필요: {unit_krw:,.0f}원). 10초 대기.")
                            time.sleep(10)
                            continue

                        print(f"[{market}]   - 1 Unit 시장가 매수 실행: {unit_krw:,} KRW")
                        prev_avail = float(upbit.get_balance(ticker) or 0.0)
                        upbit.buy_market_order(market, unit_krw)
                        time.sleep(1.5)

                        new_avail = float(upbit.get_balance(ticker) or 0.0)
                        bought_vol = max(0.0, new_avail - prev_avail)
                        if bought_vol <= 0:
                            bought_vol = round(unit_krw / current_price, 8)

                        quantity = bought_vol
                        avg_price = current_price

                        state["bot_quantity"] = quantity
                        state["bot_avg_price"] = avg_price
                        state["initial_entry_done"] = True

                        log_real_trade(
                            market=market,
                            ticker=ticker,
                            side="bid",
                            action="MARTINGALE_BUY_INITIAL",
                            price=avg_price,
                            volume=quantity,
                            cost_or_revenue=unit_krw,
                            strategy="HYBRID_MARTINGALE_MAGIC_SPLIT"
                        )

                    # 1차 차수 등록 (신규 진입으로 차수가 비어있을 때만)
                    if not state.get("tranches"):
                        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        state["tranches"] = [{
                            "step": 0,
                            "units": 1,
                            "buy_price": avg_price,
                            "volume": quantity,
                            "time": now_str
                        }]
                        state["bot_quantity"] = quantity
                        state["bot_avg_price"] = avg_price
                        save_strategy_state(market, state)

                    print(f"[{market}]   - 현재 포지션: 평단가 {avg_price:,.0f}원, 보유수량 {quantity}")

                    # 바스켓 익절 주문
                    sell_price = adjust_price_to_tick(avg_price * sell_profit_margin, method="ceil")
                    upbit.sell_limit_order(market, sell_price, quantity)
                    print(f"[{market}]   - 바스켓 익절 매도 주문: {sell_price:,.0f}원, 수량 {quantity}")
                    time.sleep(0.2)

                    # 마틴게일 3단계 매수 주문 계획 (-4% 2배, -8% 3배, -12% 6배)
                    new_orders = calculate_new_buy_prices(avg_buy_price=avg_price, existing_orders=None)
                    print(f"[{market}]   - 신규 마틴게일 매수 주문 계획 (3개): {new_orders}")

                    for order in new_orders:
                        p = order['price']
                        u = order['units']
                        order_krw = unit_krw * u
                        volume = round(order_krw / p, 8)
                        upbit.buy_limit_order(market, p, volume)
                        print(f"[{market}]     - 지정가 매수 주문: {p:,.0f}원 | {u} Units ({order_krw:,}원)")
                        time.sleep(0.2)

                    send_telegram_alert(
                        f"🛡️ <b>[하락장 마틴-매직스플릿 방어 시작]</b> {market}\n"
                        f"진입가: {avg_price:,.0f}원 | 수량: {quantity:.6f}\n"
                        f"바스켓 익절가: {sell_price:,.0f}원 (+{(sell_profit_margin - 1) * 100:.2f}%)\n"
                        f"개별 차수 목표: 매수가 대비 +{MAGIC_SPLIT_TRANCHE_PROFIT*100:.1f}%\n"
                        f"마틴게일 물타기 3단계(2x, 3x, 6x) 예약 완료"
                    )

                # Case 4: 물타기 매수 체결 감지
                elif num_sell == 1 and num_buy <= 2:
                    print(f"\n[{time.strftime('%H:%M:%S')}] [{market}] [하락장 방어] Case 4: 물타기 매수 체결 감지. 포지션 재조정.")

                    for order in sell_orders:
                        upbit.cancel_order(order['uuid'])
                        time.sleep(0.1)

                    time.sleep(1)
                    actual_avail = float(upbit.get_balance(ticker) or 0.0)
                    prot_qty = float(PROTECTED_BALANCES.get(market, 0.0))
                    bot_tot_qty = max(0.0, actual_avail - prot_qty)

                    prev_bot_qty = float(state.get("bot_quantity", 0.0))
                    prev_bot_avg = float(state.get("bot_avg_price", current_price))
                    bought_step_vol = max(0.0, bot_tot_qty - prev_bot_qty)
                    if bought_step_vol <= 0:
                        bought_step_vol = bot_tot_qty

                    # 봇 평단가 및 수량 갱신 (기존 자산 완전 격리)
                    new_tot_cost = (prev_bot_qty * prev_bot_avg) + (bought_step_vol * current_price)
                    avg_price = new_tot_cost / bot_tot_qty if bot_tot_qty > 0 else current_price
                    quantity = bot_tot_qty

                    state["bot_quantity"] = quantity
                    state["bot_avg_price"] = avg_price

                    print(f"[{market}]   - 갱신된 봇 포지션: 새 평단가 {avg_price:,.0f}원, 봇 보유수량 {quantity:.6f} (보호 자산: {prot_qty:.4f})")

                    # 새 바스켓 익절 매도 등록 (봇 수량만 등록)
                    sell_price = adjust_price_to_tick(avg_price * sell_profit_margin, method="ceil")
                    upbit.sell_limit_order(market, sell_price, quantity)
                    print(f"[{market}]   - 새 바스켓 익절 매도: {sell_price:,.0f}원, 전량 {quantity:.6f}")
                    time.sleep(0.2)

                    # 추가 차수 등록 (체결된 해당 차수의 순수 매수 수량 기록)
                    step_idx = len(state.get("tranches", []))
                    state.setdefault("tranches", []).append({
                        "step": step_idx,
                        "units": 2 if step_idx == 1 else (3 if step_idx == 2 else 6),
                        "buy_price": current_price,
                        "volume": bought_step_vol,
                        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    })
                    save_strategy_state(market, state)

                    # 추가 매수 주문 계획
                    buy_order_details = [{'price': float(o['price']), 'units': 1} for o in buy_orders]
                    additional_orders = calculate_new_buy_prices(
                        avg_buy_price=avg_price,
                        existing_orders=buy_order_details
                    )

                    krw_balance = check_krw_balance_alert(upbit, context=f"{market} 추가 물타기 매수")
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
                        print(f"[{market}]     - 추가 매수 주문: {p:,.0f}원 | {u} Units ({order_krw:,}원)")
                        time.sleep(0.2)

                    send_telegram_alert(
                        f"💧 <b>[물타기 체결 후 포지션 재조정]</b> {market}\n"
                        f"새 평단가: {avg_price:,.0f}원 | 총 보유수량: {quantity:.6f}\n"
                        f"새 바스켓 익절가: {sell_price:,.0f}원\n"
                        f"누적 차수: {len(state['tranches'])}개 | 추가 매수 {len(additional_orders)}건 등록"
                    )

            # =========================================================================
            # [분기 B] 상승 국면 (BULL): 5/20 MA 추세추종 & 트레일링 스탑 모드
            # =========================================================================
            else:
                state["active_mode"] = "TREND"

                # 포지션 보유 중: 트레일링 스탑, 추세 이탈 감시 및 피라미딩(불타기) 추가매수
                if total_value >= MIN_ORDER_KRW and avg_price > 0:
                    profit_rate = (current_price - avg_price) / avg_price

                    # 기존 포지션이 있으나 tranches가 비어있는 경우(이전 버전 호환) 1회차(step 0)로 상태 복구
                    if len(state.get("tranches", [])) == 0:
                        state["tranches"] = [{
                            "step": 0,
                            "units": 1,
                            "buy_price": avg_price,
                            "volume": quantity,
                            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        }]
                        save_strategy_state(market, state)

                    # B-1. 트레일링 스탑 가동 조건 (진입가 대비 +10% 도달)
                    if USE_TRAILING_STOP and profit_rate >= TRAILING_STOP_TRIGGER:
                        if not state.get("trailing_stop_active", False):
                            state["trailing_stop_active"] = True
                            state["trend_peak_price"] = current_price
                            send_telegram_alert(f"🎯 <b>[트레일링 스탑 가동]</b> {market} 수익률 {profit_rate*100:+.2f}% 도달! 고점 추적을 시작합니다.")

                    if state.get("trailing_stop_active", False):
                        state["trend_peak_price"] = max(state.get("trend_peak_price", current_price), current_price)
                        peak_p = state["trend_peak_price"]
                        drop_from_peak = (current_price - peak_p) / peak_p

                        # 고점 대비 -3% 하락 시 조기 익절 청산
                        if drop_from_peak <= -TRAILING_STOP_DROP:
                            real_pnl = (current_price - avg_price) * quantity
                            msg = (
                                f"🏆 <b>[트레일링 스탑 익절 청산]</b> {market}\n"
                                f"최고가: {peak_p:,.0f}원 -> 현재가: {current_price:,.0f}원 (고점 대비 {drop_from_peak*100:.2f}%)\n"
                                f"최종 수익률: {profit_rate*100:+.2f}% | 실현손익: {real_pnl:+,.0f}원"
                            )
                            print(f"\n[{market}] {msg}")
                            send_telegram_alert(msg)

                            cancel_all_orders(upbit, market)
                            time.sleep(0.5)
                            upbit.sell_market_order(market, quantity)

                            log_real_trade(
                                market=market,
                                ticker=ticker,
                                side="ask",
                                action="TRAILING_STOP_EXIT",
                                price=current_price,
                                volume=quantity,
                                cost_or_revenue=quantity * current_price,
                                pnl=real_pnl,
                                strategy="HYBRID_TREND"
                            )

                            state["bot_quantity"] = 0.0
                            state["bot_avg_price"] = 0.0
                            state["tranches"] = []
                            state["trailing_stop_active"] = False
                            state["trend_peak_price"] = 0.0
                            state["initial_entry_done"] = True
                            state["completed_cycles"] = state.get("completed_cycles", 0) + 1
                            save_strategy_state(market, state)
                            time.sleep(10)
                            continue

                    # B-2. 5/20 MA 데드크로스 신호 발생 시 추세 매도
                    if signal == "SELL":
                        real_pnl = (current_price - avg_price) * quantity
                        msg = (
                            f"🛑 <b>[5/20 MA 데드크로스 추세 청산]</b> {market}\n"
                            f"현재가: {current_price:,.0f}원 | 봇 평단가: {avg_price:,.0f}원\n"
                            f"수익률: {profit_rate*100:+.2f}% | 실현손익: {real_pnl:+,.0f}원\n"
                            f"봇 수량 {quantity:.6f} 매도 (기존 자산은 안전 보호)"
                        )
                        print(f"\n[{market}] {msg}")
                        send_telegram_alert(msg)

                        cancel_all_orders(upbit, market)
                        time.sleep(0.5)
                        upbit.sell_market_order(market, quantity)

                        log_real_trade(
                            market=market,
                            ticker=ticker,
                            side="ask",
                            action="TREND_DEAD_CROSS_SELL",
                            price=current_price,
                            volume=quantity,
                            cost_or_revenue=quantity * current_price,
                            pnl=real_pnl,
                            strategy="HYBRID_TREND"
                        )

                        state["bot_quantity"] = 0.0
                        state["bot_avg_price"] = 0.0
                        state["tranches"] = []
                        state["trailing_stop_active"] = False
                        state["trend_peak_price"] = 0.0
                        state["initial_entry_done"] = True
                        state["completed_cycles"] = state.get("completed_cycles", 0) + 1
                        save_strategy_state(market, state)
                        time.sleep(10)
                        continue

                    # B-3. 상승장 피라미딩(불타기) 분할 추가매수 (Option 2)
                    if USE_BULL_PYRAMID and not state.get("trailing_stop_active", False):
                        current_steps = len(state.get("tranches", []))
                        if current_steps < MAX_PYRAMID_STEPS:
                            # 직전 매수가 계산 (tranches의 마지막 매수가 또는 bot_avg_price)
                            last_buy_price = float(state["tranches"][-1].get("buy_price", avg_price)) if state["tranches"] else float(avg_price)
                            price_increase_ratio = (current_price - last_buy_price) / last_buy_price if last_buy_price > 0 else 0.0
                            ma5_support = (curr_ma5 <= 0 or current_price > curr_ma5)

                            if price_increase_ratio >= PYRAMID_STEP_PCT and ma5_support:
                                krw_balance = check_krw_balance_alert(upbit, context=f"{market} 상승장 불타기 {current_steps + 1}회차")
                                if krw_balance >= unit_krw:
                                    next_step = current_steps + 1
                                    print(
                                        f"\n[{market}] 🔥 [상승장 피라미딩 불타기 {next_step}/{MAX_PYRAMID_STEPS}회차 매수]\n"
                                        f"직전매수가: {last_buy_price:,.0f}원 -> 현재가: {current_price:,.0f}원 "
                                        f"(+{price_increase_ratio*100:.2f}% / 기준 +{PYRAMID_STEP_PCT*100:.1f}%)"
                                    )
                                    prev_avail = float(upbit.get_balance(ticker) or 0.0)
                                    upbit.buy_market_order(market, unit_krw)
                                    time.sleep(1.5)

                                    new_avail = float(upbit.get_balance(ticker) or 0.0)
                                    bought_vol = max(0.0, new_avail - prev_avail)
                                    if bought_vol <= 0:
                                        bought_vol = round(unit_krw / current_price, 8)

                                    # 누적 포지션 및 평단가 갱신
                                    old_q = quantity
                                    old_avg = avg_price
                                    new_q = old_q + bought_vol
                                    new_avg = ((old_avg * old_q) + (current_price * bought_vol)) / new_q if new_q > 0 else current_price

                                    state["bot_quantity"] = new_q
                                    state["bot_avg_price"] = new_avg
                                    state["trend_peak_price"] = max(state.get("trend_peak_price", current_price), current_price)
                                    state["last_buy_date"] = (datetime.utcnow() + timedelta(hours=9)).strftime("%Y-%m-%d")

                                    state["tranches"].append({
                                        "step": current_steps,
                                        "units": 1,
                                        "buy_price": current_price,
                                        "volume": bought_vol,
                                        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                                    })
                                    save_strategy_state(market, state)

                                    log_real_trade(
                                        market=market,
                                        ticker=ticker,
                                        side="bid",
                                        action=f"BULL_PYRAMID_STEP_{next_step}",
                                        price=current_price,
                                        volume=bought_vol,
                                        cost_or_revenue=unit_krw,
                                        pnl=0.0,
                                        strategy="HYBRID_TREND"
                                    )

                                    msg = (
                                        f"🔥 <b>[상승장 피라미딩 불타기 매수 체결]</b> {market}\n"
                                        f"차수: {next_step}/{MAX_PYRAMID_STEPS}회차 (+{price_increase_ratio*100:.2f}% 상승 돌파)\n"
                                        f"추가 매수액: {unit_krw:,.0f}원 (체결: {bought_vol:.6f} {ticker})\n"
                                        f"새 봇 평단가: {new_avg:,.0f}원 | 총 누적수량: {new_q:.6f} {ticker}\n"
                                        f"총 투입원금: {new_avg * new_q:,.0f}원\n"
                                        f"수익률: {((current_price - new_avg)/new_avg)*100:+.2f}%"
                                    )
                                    print(f"[{market}] {msg}")
                                    send_telegram_alert(msg)
                                    time.sleep(5)
                                    continue

                    # B-4. 상승장 일봉 양봉 종가매매 (08:50 ~ 09:00 KST, 당일 불타기 미체결 시)
                    if USE_BULL_CLOSING_BUY and not state.get("trailing_stop_active", False):
                        current_steps = len(state.get("tranches", []))
                        if current_steps < MAX_PYRAMID_STEPS:
                            now_kst = datetime.utcnow() + timedelta(hours=9)
                            today_str = now_kst.strftime("%Y-%m-%d")
                            is_closing_window = (now_kst.hour == 8 and now_kst.minute >= 50)

                            if is_closing_window and state.get("last_buy_date") != today_str:
                                closing_info = check_daily_closing_buy_condition(market, current_price)
                                if closing_info.get("can_buy", False):
                                    krw_balance = check_krw_balance_alert(upbit, context=f"{market} 상승장 일봉 종가매수 {current_steps + 1}회차")
                                    if krw_balance >= unit_krw:
                                        next_step = current_steps + 1
                                        print(
                                            f"\n[{market}] 🌅 [상승장 일봉 양봉 종가매수 {next_step}/{MAX_PYRAMID_STEPS}회차 매수]\n"
                                            f"사유: {closing_info.get('reason')} | 현재가: {current_price:,.0f}원"
                                        )
                                        prev_avail = float(upbit.get_balance(ticker) or 0.0)
                                        upbit.buy_market_order(market, unit_krw)
                                        time.sleep(1.5)

                                        new_avail = float(upbit.get_balance(ticker) or 0.0)
                                        bought_vol = max(0.0, new_avail - prev_avail)
                                        if bought_vol <= 0:
                                            bought_vol = round(unit_krw / current_price, 8)

                                        # 누적 포지션 및 평단가 갱신
                                        old_q = quantity
                                        old_avg = avg_price
                                        new_q = old_q + bought_vol
                                        new_avg = ((old_avg * old_q) + (current_price * bought_vol)) / new_q if new_q > 0 else current_price

                                        state["bot_quantity"] = new_q
                                        state["bot_avg_price"] = new_avg
                                        state["trend_peak_price"] = max(state.get("trend_peak_price", current_price), current_price)
                                        state["last_buy_date"] = today_str
                                        state["last_closing_buy_date"] = today_str

                                        state["tranches"].append({
                                            "step": current_steps,
                                            "units": 1,
                                            "buy_price": current_price,
                                            "volume": bought_vol,
                                            "time": now_kst.strftime("%Y-%m-%d %H:%M:%S"),
                                            "type": "CLOSING"
                                        })
                                        save_strategy_state(market, state)

                                        log_real_trade(
                                            market=market,
                                            ticker=ticker,
                                            side="bid",
                                            action=f"BULL_CLOSING_BUY_STEP_{next_step}",
                                            price=current_price,
                                            volume=bought_vol,
                                            cost_or_revenue=unit_krw,
                                            pnl=0.0,
                                            strategy="HYBRID_TREND"
                                        )

                                        msg = (
                                            f"🌅 <b>[상승장 일봉 양봉 종가매수 체결]</b> {market}\n"
                                            f"차수: {next_step}/{MAX_PYRAMID_STEPS}회차 (08:55 일봉 양봉 & 5일선 지지 확인)\n"
                                            f"추가 매수액: {unit_krw:,.0f}원 (체결: {bought_vol:.6f} {ticker})\n"
                                            f"새 봇 평단가: {new_avg:,.0f}원 | 총 누적수량: {new_q:.6f} {ticker}\n"
                                            f"총 투입원금: {new_avg * new_q:,.0f}원\n"
                                            f"수익률: {((current_price - new_avg)/new_avg)*100:+.2f}%"
                                        )
                                        print(f"[{market}] {msg}")
                                        send_telegram_alert(msg)
                                        time.sleep(5)
                                        continue

                # 포지션 미보유 시: 최초 가동 즉시 10,000원 진입 또는 5/20 MA 골든크로스 신호 감시
                else:
                    should_buy = False
                    buy_action = ""
                    buy_reason = ""

                    if not state.get("initial_entry_done", False):
                        # 안전 검사: 이미 계좌에 보호 수량을 초과하는 봇 수량이 존재하면 신규 매수 없이 즉시 동기화
                        actual_avail = float(upbit.get_balance(ticker) or 0.0)
                        prot_qty = PROTECTED_BALANCES.get(market, 0.0)
                        existing_diff = actual_avail - prot_qty
                        if existing_diff * current_price >= MIN_ORDER_KRW:
                            print(f"\n[{market}] 🔄 [상승 국면] 체결 완료된 봇 포지션({existing_diff:.6f} {ticker}) 감지. 상태 동기화 완료.")
                            state["bot_quantity"] = existing_diff
                            state["bot_avg_price"] = current_price
                            state["trend_peak_price"] = current_price
                            state["trailing_stop_active"] = False
                            state["initial_entry_done"] = True
                            state["tranches"] = [{
                                "step": 0,
                                "units": 1,
                                "buy_price": current_price,
                                "volume": existing_diff,
                                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                            }]
                            save_strategy_state(market, state)
                            continue

                        should_buy = True
                        buy_action = "INITIAL_10K_ENTRY"
                        buy_reason = "기존 보유자산 보호/격리 후 봇 1회차(10,000원) 신규 진입"
                    elif signal == "BUY":
                        should_buy = True
                        buy_action = "TREND_GOLDEN_CROSS_BUY"
                        buy_reason = "200선 상회 중 5/20 골든크로스 발생"

                    if should_buy:
                        krw_balance = check_krw_balance_alert(upbit, context=f"{market} 상승장 1회차 진입")
                        if krw_balance >= unit_krw:
                            print(f"\n[{market}] 🚀 [상승 국면] {buy_reason}! 1 Unit 시장가 매수 실행.")
                            prev_avail = float(upbit.get_balance(ticker) or 0.0)
                            upbit.buy_market_order(market, unit_krw)
                            time.sleep(1.5)

                            new_avail = float(upbit.get_balance(ticker) or 0.0)
                            bought_vol = max(0.0, new_avail - prev_avail)
                            if bought_vol <= 0:
                                bought_vol = round(unit_krw / current_price, 8)

                            avg_p = current_price
                            q = bought_vol

                            state["bot_quantity"] = q
                            state["bot_avg_price"] = avg_p
                            state["trend_peak_price"] = avg_p
                            state["trailing_stop_active"] = False
                            state["initial_entry_done"] = True
                            state["tranches"] = [{
                                "step": 0,
                                "units": 1,
                                "buy_price": avg_p,
                                "volume": q,
                                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                            }]
                            save_strategy_state(market, state)

                            log_real_trade(
                                market=market,
                                ticker=ticker,
                                side="bid",
                                action=buy_action,
                                price=avg_p,
                                volume=q,
                                cost_or_revenue=unit_krw,
                                strategy="HYBRID_TREND"
                            )

                            prot_q = PROTECTED_BALANCES.get(market, 0.0)
                            send_telegram_alert(
                                f"🚀 <b>[하이브리드 봇 1회차 신규 매수 체결]</b> {market}\n"
                                f"진입가: {avg_p:,.0f}원 | 수량: {q:.6f} {ticker}\n"
                                f"사유: {buy_reason}\n"
                                f"🔒 <b>기존 보유분({prot_q:,.4f} {ticker}) 안전 보호/격리 완료</b>"
                            )
                            time.sleep(5)
                            continue

            save_strategy_state(market, state)

        except Exception as e:
            print(f"[{time.strftime('%H:%M:%S')}] [{market}] [CRITICAL] 메인 루프 에러: {e}")
            time.sleep(10)

        time.sleep(5)


# --- 봇 단일 실행 핸들러 ---
def start_bot(market: str):
    """실전 매매 봇 실행"""
    run_trading_strategy(market)


# --- 엔트리포인트 (멀티 코인/전략 동시 실행 지원) ---
if __name__ == "__main__":
    if len(sys.argv) > 1:
        target_markets = [m.strip() for m in sys.argv[1].split(",") if m.strip()]
    else:
        target_markets = list(INVESTMENTS.keys())
        if not target_markets:
            target_markets = ["KRW-SOL", "KRW-ETH"]

    print("=" * 65)
    print(f"🚀 AutoBot Trader 하이브리드 실전 매매 시스템 가동")
    print(f" - 실행 대상 마켓: {', '.join(target_markets)}")
    print(f" - 하락장 방어: 마틴게일 배수 진입 + 매직스플릿 이중익절(개별+3%/바스켓)")
    print(f" - 상승장 추세: 5/20 MA 추세추종 & 트레일링 스탑")
    print(f" - 예수금 10만원 미만 경고 기준: {MIN_KRW_ALERT_THRESHOLD:,}원")
    for m in target_markets:
        info = INVESTMENTS.get(m, {})
        margin = (get_profit_margin(m) - 1) * 100
        print(f"   • {m}: 투자배정 {info.get('total', 0):,}원 | 1Unit {info.get('unit', 0):,}원 | 익절 목표 +{margin:.2f}%")
    print("=" * 65)

    if len(target_markets) == 1:
        start_bot(target_markets[0])
    else:
        threads = []
        for m in target_markets:
            t = threading.Thread(target=start_bot, args=(m,), daemon=True, name=f"BotThread-{m}")
            t.start()
            threads.append(t)
            time.sleep(1)

        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\n[INFO] 자동매매 시스템 종료 요청 수신. 프로그램을 안전하게 종료합니다.")
