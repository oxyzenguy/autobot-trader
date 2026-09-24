import os
import sys
import time
import json
import math
import threading
import pyupbit
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List
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
    get_bull_trailing_stop_trigger,
    USE_BULL_TIME_DCA,
    BULL_TIME_DCA_INTERVAL_HOURS,
    MAX_BULL_DCA_STEPS,
    USE_BULL_PYRAMID,
    PYRAMID_STEP_PCT,
    MAX_PYRAMID_STEPS,
    USE_BULL_CLOSING_BUY,
    USE_MAGIC_SPLIT_DEFENSE,
    MAGIC_SPLIT_TRANCHE_PROFIT,
    MAGIC_SPLIT_DOWN_PCT,
    MARTINGALE_MULTIPLIERS,
    BULL_STOP_LOSS_PCT,
    REGIME_SWITCH_LIQUIDATION_PCT,
    MARTINGALE_SCHEDULE,
    MARTINGALE_MAX_STEPS,
    STOP_LOSS_COOLDOWN_HOURS
)
from strategy.matingale2x_logic import calculate_new_buy_prices, adjust_price_to_tick
from strategy.hybrid_regime import get_hybrid_regime_and_signals, check_magic_split_exits, check_daily_closing_buy_condition
from utils.db_logger import log_real_trade, init_db

# 예수금 알림 쿨다운 관리
LAST_KRW_ALERT_TIME = 0
KRW_ALERT_COOLDOWN_SEC = 3600  # 1시간 쿨다운 (도배 방지)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def trigger_instant_account_snapshot():
    """매매 체결(매수/매도/손절/익절) 직후 계좌 평가액 스냅샷 및 동기화 트리거 (호환용)"""
    try:
        from utils.sync_manager import trigger_snapshot_sync
        trigger_snapshot_sync()
    except Exception:
        pass


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


_last_saved_state_strings = {}

def save_strategy_state(market: str, state: dict):
    """실전 매매 상태 영속 저장 (내용이 실제로 변경되었을 때만 디스크 쓰기)"""
    path = get_state_file_path(market)
    try:
        new_content = json.dumps(state, indent=2, ensure_ascii=False)
        if _last_saved_state_strings.get(market) == new_content:
            return
        with open(path, "w", encoding="utf-8") as f:
            f.write(new_content)
        _last_saved_state_strings[market] = new_content
    except Exception as e:
        print(f"[{market}] 상태 파일 저장 실패: {e}")


def add_bot_tranche(state: dict, units: int, buy_price: float, volume: float, tranche_type: str = "NORMAL") -> dict:
    """
    고유 식별자(tranche_id)를 보장하여 차수를 추가합니다.
    차수 매도 후 번호 재사용으로 인한 장부 왜곡 및 중복 삭제를 원천 방지합니다.
    """
    tranches = state.setdefault("tranches", [])
    max_id = max([t.get("tranche_id", t.get("step", 0)) for t in tranches], default=0)
    next_id = max(state.get("next_tranche_id", 1), max_id + 1)
    state["next_tranche_id"] = next_id + 1

    step_num = len(tranches) + 1
    new_tr = {
        "tranche_id": next_id,
        "step": next_id,        # 고유 식별자 (삭제 시 정확한 대상 매칭용)
        "step_num": step_num,   # UI 및 순차 표시용 차수 번호
        "units": units,
        "buy_price": buy_price,
        "volume": volume,
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "type": tranche_type
    }
    tranches.append(new_tr)
    return new_tr


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
            avail = float(balance_info.get('balance', 0.0) or 0.0)
            locked = float(balance_info.get('locked', 0.0) or 0.0)
            total_held = avail + locked
            # 실제 업비트 총 보유 수량(가용+주문잠금) 내에서만 안전하게 캡핑
            # (바스켓 매도 주문으로 locked된 상태에서도 봇 보유량 및 매직스플릿 차수 익절 검사 정상 작동)
            safe_qty = min(bot_qty, total_held)
            return bot_avg, safe_qty
    except Exception as e:
        print(f"[{time.strftime('%H:%M:%S')}] [ERROR] [{ticker}] 봇 잔고 확인 오류: {e}")

    return bot_avg, bot_qty


def get_my_balance(upbit_client, ticker: str):
    """(계좌 전체) 특정 코인의 총 보유 수량(가용+잠금)과 평단가를 조회합니다."""
    try:
        balance_info = upbit_client.get_balance(ticker, verbose=True)
        if balance_info and 'avg_buy_price' in balance_info:
            avg_price = float(balance_info.get('avg_buy_price', 0.0) or 0.0)
            avail = float(balance_info.get('balance', 0.0) or 0.0)
            locked = float(balance_info.get('locked', 0.0) or 0.0)
            return avg_price, avail + locked
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
                f"⚠️ <b>[예수금 부족 경고]</b>\n"
                f"• 현재 주문가능 예수금: <b>{krw:,.0f}원</b> (기준: 10만 원 미만)\n"
                f"• 감시 상황: {context}"
            )
            print(f"\n[{time.strftime('%H:%M:%S')}] 🚨 [ALERT] 예수금 10만원 미만 감지 ({krw:,.0f}원)! 텔레그램 경고 발송.")
            send_telegram_alert(msg)
            LAST_KRW_ALERT_TIME = now
    return krw


def safe_get_current_price(market: str, retries: int = 3, delay: float = 0.5) -> Optional[float]:
    """현재가 안전 조회 (Upbit API 레이트리밋/일시 오류 시 KeyError(0) 방지 및 재시도)"""
    for i in range(retries):
        try:
            p = pyupbit.get_current_price(market)
            if p is not None and not isinstance(p, dict):
                p_float = float(p)
                if p_float > 0:
                    return p_float
        except Exception:
            pass
        if i < retries - 1:
            time.sleep(delay)
    return None


def cancel_all_orders(upbit_client, market: str):
    """해당 마켓의 모든 미체결 주문(매수/매도)을 취소합니다."""
    try:
        open_orders = upbit_client.get_order(market, state="wait")
        if open_orders and isinstance(open_orders, list):
            for order in open_orders:
                if isinstance(order, dict) and 'uuid' in order:
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

    send_telegram_alert(f"🚀 <b>[자동매매 가동]</b> {market} (1 Unit: {unit_krw:,}원)")

    last_heartbeat_time = 0

    while True:
        try:
            # 1. 예수금 체크
            check_krw_balance_alert(upbit, context=f"{market} 실시간 감시")

            # 2. 실시간 시세 및 국면 판별
            current_price = safe_get_current_price(market)
            if current_price is None or current_price <= 0:
                print(f"[{time.strftime('%H:%M:%S')}] [{ticker}] [WARN] 현재가 조회 실패 (API 응답 지연/제한). 5초 후 재시도.")
                time.sleep(5)
                continue

            regime_info = get_hybrid_regime_and_signals(market)
            if regime_info.get("is_bull") is None:
                # 캔들 데이터 수집 대기 중이므로 무리하게 매매하지 않고 안전 대기
                print(f"[{time.strftime('%H:%M:%S')}] [{ticker}] [WARN] 국면 분석 데이터 수집 대기 중. 5초 후 재시도.")
                time.sleep(5)
                continue

            is_bull = regime_info.get("is_bull", False)
            signal = regime_info.get("signal", "HOLD")
            regime_str = regime_info.get("regime_korean", "국면 분석 중")
            curr_ma5 = float(regime_info.get("ma5", 0.0))

            curr_regime = "BULL" if is_bull else "BEAR"
            prev_regime = state.get("current_regime", curr_regime)

            # 국면 전환 감지 (BULL ➔ BEAR 또는 BEAR ➔ BULL)
            if prev_regime == "BULL" and curr_regime == "BEAR":
                print(f"\n[{market}] ⚠️ [국면 전환 감지] 200 MA 하향 이탈: 상승장(BULL) ➔ 하락장(BEAR)")
                cancel_all_orders(upbit, market)
                time.sleep(0.5)

                avg_price, quantity = get_bot_balance(upbit, market, ticker, state)
                total_value = quantity * current_price

                if total_value >= MIN_ORDER_KRW and avg_price > 0:
                    liq_pct = REGIME_SWITCH_LIQUIDATION_PCT.get(market, 0.50)
                    sell_qty = round(quantity * liq_pct, 8)
                    sell_krw = sell_qty * current_price

                    # 업비트 최소주문금액(5,000원) 체크 및 보정
                    if sell_krw < MIN_ORDER_KRW:
                        if total_value >= MIN_ORDER_KRW:
                            min_qty = round((MIN_ORDER_KRW + 50) / current_price, 8)
                            if min_qty <= quantity:
                                sell_qty = min_qty
                            else:
                                sell_qty = quantity

                    if sell_qty > 0 and (sell_qty * current_price) >= MIN_ORDER_KRW:
                        pnl_krw = (current_price - avg_price) * sell_qty
                        pnl_rate = (current_price - avg_price) / avg_price
                        action_name = f"REGIME_SWITCH_PARTIAL_CUT_{int(liq_pct * 100)}PCT"

                        sell_res = upbit.sell_market_order(market, sell_qty)
                        print(f"[{time.strftime('%H:%M:%S')}] [{market}] [국면전환 부분손절] {int(liq_pct * 100)}% 매도 결과: {sell_res}")
                        time.sleep(1.0)

                        if not (isinstance(sell_res, dict) and "uuid" in sell_res):
                            print(f"[{time.strftime('%H:%M:%S')}] [{market}] [ERROR] 국면전환 부분손절 주문 실패: {sell_res}")
                            send_telegram_alert(f"⚠️ <b>[국면전환 부분손절 실패]</b> {market}\n시장가 매도 응답: {sell_res}")
                            time.sleep(5)
                            continue

                        log_real_trade(
                            market=market,
                            ticker=ticker,
                            side="ask",
                            action=action_name,
                            price=current_price,
                            volume=sell_qty,
                            cost_or_revenue=sell_qty * current_price,
                            pnl=pnl_krw,
                            strategy="HYBRID_REGIME_SWITCH"
                        )

                        actual_avail = float(upbit.get_balance(ticker) or 0.0)
                        prot_qty = float(PROTECTED_BALANCES.get(market, 0.0))
                        rem_qty = max(0.0, actual_avail - prot_qty)

                        msg = (
                            f"🔴 <b>[손절 체결]</b> {market} (하락장 전환 {int(liq_pct * 100)}% 부분손절)\n"
                            f"• 체결단가: {current_price:,.0f}원 (평단가: {avg_price:,.0f}원)\n"
                            f"• 손실률: {pnl_rate * 100:+.2f}%\n"
                            f"• 실현손익: <b>{pnl_krw:+,.0f}원</b> (잔여 {100 - int(liq_pct * 100)}% 하락방어 인계)"
                        )
                        print(f"[{market}] {msg}")
                        send_telegram_alert(msg)

                        state["bot_quantity"] = rem_qty
                        state["bot_avg_price"] = avg_price
                        if rem_qty * current_price >= MIN_ORDER_KRW:
                            state["tranches"] = [{
                                "step": 0,
                                "units": 1,
                                "buy_price": avg_price,
                                "volume": rem_qty,
                                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                "source": "REGIME_HANDOVER"
                            }]
                        else:
                            state["tranches"] = []
                    else:
                        print(f"[{market}] [INFO] 포지션 금액 부족으로 부분손절 생략 후 전량 하락장 모드로 인계.")
                else:
                    state["tranches"] = []

                state["trend_peak_price"] = 0.0
                state["trailing_stop_active"] = False
                state["last_dca_buy_time"] = None
                state["current_regime"] = "BEAR"
                state["active_mode"] = "MARTINGALE_MAGIC_SPLIT"
                save_strategy_state(market, state)
                time.sleep(2)
                continue

            elif prev_regime == "BEAR" and curr_regime == "BULL":
                print(f"\n[{market}] 🐂 [국면 전환 감지] 200 MA 상향 돌파: 하락장(BEAR) ➔ 상승장(BULL)")
                cancel_all_orders(upbit, market)
                time.sleep(0.5)

                avg_price, quantity = get_bot_balance(upbit, market, ticker, state)
                msg = (
                    f"🔄 <b>[국면 전환]</b> {market}: 🔴 하락장 ➔ 🟢 상승장 전환\n"
                    f"• 현재가: {current_price:,.0f}원 | 봇 보유: {quantity:.4f} {ticker}"
                )
                print(f"[{market}] {msg}")
                send_telegram_alert(msg)

                state["trend_peak_price"] = current_price
                state["trailing_stop_active"] = False
                state["last_dca_buy_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                state["current_regime"] = "BULL"
                state["active_mode"] = "TREND"
                save_strategy_state(market, state)
                time.sleep(2)
                continue

            state["current_regime"] = curr_regime


            # 3. 봇 전용 잔고 조회 (기존 자산 완전 격리/보호)
            avg_price, quantity = get_bot_balance(upbit, market, ticker, state)
            total_value = quantity * current_price

            # 4. 리스크 관리: Stop-Loss (긴급 손절) 감지
            # 하락장(BEAR) 국면은 매직스플릿 방어 모듈(손절 없이 반등 시 +3% 익절 및 바스켓 탈출)을 적용하므로 손절 제외
            # 상승장(BULL) 국면은 평단가 대비 BULL_STOP_LOSS_PCT(-10.0%) 도달 시에만 긴급 손절 (잔파동 털림 방지)
            if is_bull and total_value >= MIN_ORDER_KRW and avg_price > 0:
                pnl_rate = (current_price - avg_price) / avg_price
                if pnl_rate <= BULL_STOP_LOSS_PCT:
                    loss_krw = (current_price - avg_price) * quantity
                    cooldown_sec = STOP_LOSS_COOLDOWN_HOURS * 3600
                    cooldown_until_ts = time.time() + cooldown_sec
                    cooldown_str = datetime.fromtimestamp(cooldown_until_ts).strftime("%H:%M:%S")

                    msg = (
                        f"🔴 <b>[손절 체결]</b> {market} (상승장 -10% 긴급손절)\n"
                        f"• 체결가: {current_price:,.0f}원 (평단가: {avg_price:,.0f}원)\n"
                        f"• 손실률: {pnl_rate * 100:.2f}%\n"
                        f"• 실현손익: <b>{loss_krw:+,.0f}원</b>\n"
                        f"🛡️ <b>[재진입 차단]</b> 추가 급락 및 뇌동매매 방지를 위해 <b>{STOP_LOSS_COOLDOWN_HOURS}시간({cooldown_str}까지) 신규 진입을 전면 차단</b>합니다."
                    )

                    cancel_all_orders(upbit, market)
                    time.sleep(0.5)
                    sell_res = upbit.sell_market_order(market, quantity)
                    print(f"[{time.strftime('%H:%M:%S')}] [{market}] [STOP-LOSS] 매도 결과: {sell_res}")

                    if not (isinstance(sell_res, dict) and "uuid" in sell_res):
                        print(f"[{market}] [ERROR] 상승장 긴급손절 시장가 매도 주문 실패: {sell_res}")
                        send_telegram_alert(f"⚠️ <b>[손절 주문 실패]</b> {market}\n시장가 매도 주문 응답: {sell_res}")
                        time.sleep(5)
                        continue

                    print(f"\n[{market}] {msg}")
                    send_telegram_alert(msg)

                    log_real_trade(
                        market=market,
                        ticker=ticker,
                        side="ask",
                        action="BULL_STOP_LOSS_10PCT",
                        price=current_price,
                        volume=quantity,
                        cost_or_revenue=quantity * current_price,
                        pnl=loss_krw,
                        strategy="HYBRID_TREND"
                    )

                    state["bot_quantity"] = 0.0
                    state["bot_avg_price"] = 0.0
                    state["tranches"] = []
                    state["trend_peak_price"] = 0.0
                    state["trailing_stop_active"] = False
                    state["last_dca_buy_time"] = None
                    state["initial_entry_done"] = True
                    state["stop_loss_cooldown_until"] = cooldown_until_ts
                    save_strategy_state(market, state)

                    print(f"[{market}] [INFO] 손절 완료. {STOP_LOSS_COOLDOWN_HOURS}시간({cooldown_str}까지) 신규 진입 쿨다운 가동.")
                    time.sleep(300)
                    continue

            # 5. 미체결 주문 목록 조회
            open_orders = upbit.get_order(market, state="wait")
            if open_orders is None or not isinstance(open_orders, list):
                print(f"[{time.strftime('%H:%M:%S')}] [{ticker}] [WARN] 미체결 주문 조회 실패/지연 ({type(open_orders).__name__}). 5초 후 재시도.")
                time.sleep(5)
                continue

            sell_orders = [o for o in open_orders if isinstance(o, dict) and o.get('side') == 'ask']
            buy_orders = [o for o in open_orders if isinstance(o, dict) and o.get('side') == 'bid']
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
                        if not (isinstance(sell_res, dict) and "uuid" in sell_res):
                            print(f"[{market}] [ERROR] 바스켓 익절 시장가 매도 주문 실패: {sell_res}")
                            send_telegram_alert(f"⚠️ <b>[바스켓 익절 주문 실패]</b> {market}\n시장가 매도 주문 응답: {sell_res}")
                            time.sleep(5)
                            continue

                        real_pnl = (current_price - avg_price) * quantity

                        send_telegram_alert(
                            f"🟢 <b>[익절 완료]</b> {market} (바스켓 전량)\n"
                            f"• 체결단가: {current_price:,.0f}원 (평단가: {avg_price:,.0f}원)\n"
                            f"• 수익률: {exit_decision['pnl_pct']:+.2f}%\n"
                            f"• 실현손익: <b>{real_pnl:+,.0f}원</b>"
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
                                sold_steps = [tr.get("step_num", tr.get("step")) for tr in valid_tranches]
                                sold_ids = [tr.get("tranche_id", tr.get("step")) for tr in valid_tranches]
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
                                        st_num = tr.get("step_num", tr.get("step"))
                                        log_real_trade(
                                            market=market,
                                            ticker=ticker,
                                            side="ask",
                                            action=f"TRANCHE_TAKE_PROFIT_STEP_{st_num}",
                                            price=current_price,
                                            volume=tr["volume"],
                                            cost_or_revenue=tr["volume"] * current_price,
                                            pnl=tr_pnl,
                                            strategy="HYBRID_MARTINGALE_MAGIC_SPLIT"
                                        )

                                    # 3. 체결된 차수 장부에서 고유 식별자(tranche_id)로 정확히 제거 (미매도 차수 오삭제 원천 방지)
                                    state["tranches"] = [t for t in state.get("tranches", []) if t.get("tranche_id", t.get("step")) not in sold_ids]

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
                                        f"🟢 <b>[익절 완료]</b> {market} (매직스플릿 {steps_str})\n"
                                        f"• 체결단가: {current_price:,.0f}원 (+3.0% 반등)\n"
                                        f"• 실현손익: <b>{tot_pnl:+,.0f}원</b>\n"
                                        f"• 잔여 포지션: 평단 {state['bot_avg_price']:,.0f}원 ({len(state['tranches'])}차수 유지)"
                                    )
                                    time.sleep(2)
                                    continue
                                else:
                                    err_msg = sell_res.get("error", {}).get("message", str(sell_res)) if isinstance(sell_res, dict) else str(sell_res)
                                    print(f"[{market}] ⚠️ [매직스플릿 매도 실패] 거래소 오류: {err_msg}")
                                    send_telegram_alert(f"⚠️ <b>[매도 오류]</b> {market}: {err_msg} (다음 루프 재시도)")
                            else:
                                print(f"[{market}] ℹ️ [매직스플릿] 매도 대상 금액({tot_sell_krw:,.0f}원)이 최소주문금액(5,000원) 미만이므로 바스켓 익절 대기.")

                # A-2. 마틴게일 상태 머신 (주문 등록 및 물타기)
                martingale_sched = MARTINGALE_SCHEDULE.get(market, MARTINGALE_MULTIPLIERS)
                max_steps = MARTINGALE_MAX_STEPS.get(market, 16 if market == "KRW-SOL" else 9999)
                current_steps = len(state.get("tranches", []))
                expected_open_buys = max(0, min(3, max_steps - current_steps))

                # SOL 4개 스쿼드(16차수) 등 최대 차수 도달 시 잔여 매수 주문 자동 취소 및 홀딩 관리
                if current_steps >= max_steps and num_buy > 0:
                    print(f"[{market}] 🛑 마틴게일 최대 차수({max_steps}차수 / 4개 스쿼드 32U) 도달로 잔여 매수 주문 {num_buy}건 취소.")
                    for order in buy_orders:
                        upbit.cancel_order(order['uuid'])
                        time.sleep(0.1)
                    buy_orders = []
                    num_buy = 0

                # 정상 대기 상태:
                # 1) 매도 1건 등록되어 있고 매수 주문 수가 기대치와 일치할 때
                # 2) 또는 매수 주문 기대치가 0(최대 차수 도달 홀딩)이고 매도 주문 1건 등록되어 있을 때
                if num_sell == 1 and num_buy == expected_open_buys:
                    time.sleep(5)
                    continue
                elif num_sell == 0 and num_buy == expected_open_buys and num_buy > 0:
                    # 매도 주문 금액이 5,000원 미만이라 매도 등록 대기 중이고 매수 주문만 대기 중일 때
                    time.sleep(5)
                    continue

                # Case 3: 매도 완료 (또는 신규 진입) → 매도 주문 0건
                elif num_sell == 0:
                    for order in buy_orders:
                        upbit.cancel_order(order['uuid'])
                        time.sleep(0.1)

                    avg_price, quantity = get_bot_balance(upbit, market, ticker, state)
                    if (quantity * current_price) < MIN_ORDER_KRW:
                        # 1. 손절 재진입 쿨다운 검사
                        cooldown_until = float(state.get("stop_loss_cooldown_until", 0.0))
                        now_ts = time.time()
                        if now_ts < cooldown_until:
                            rem_min = int((cooldown_until - now_ts) / 60)
                            print(f"[{market}] ⏳ [하락장 방어] 손절 후 쿨다운 대기 중 ({rem_min}분 남음). 신규 진입 차단.")
                            time.sleep(10)
                            continue

                        # 2. 하락장 최초 1차 진입 조건: ClucMay 과매도 낙주(is_cluc_dip) 발생 시에만 진입 허용!
                        # 과매도 신호가 없으면 100% 현금 보존 관망 (하락장 속 떨어지는 칼날 묻지마 매수 원천 차단)
                        if signal != "MARTINGALE_BUY_DIP":
                            print(f"[{market}] 🛡️ [하락장 관망] 현재가 200 MA 하회 중. ClucMay 과매도 낙주 신호 대기 (100% 현금 보존 관망).")
                            time.sleep(10)
                            continue

                        krw_balance = check_krw_balance_alert(upbit, context=f"{market} 1차 진입 전")
                        if krw_balance < unit_krw:
                            print(f"[{market}] [WARN] 원화 잔고 부족 ({krw_balance:,.0f}원 < 필요: {unit_krw:,.0f}원). 10초 대기.")
                            time.sleep(10)
                            continue

                        print(f"\n[{market}] 💧 [하락장 과매도 포착] ClucMay 투매 신호 감지! 1 Unit 시장가 매수 실행: {unit_krw:,} KRW")
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
                        state["tranches"] = []
                        add_bot_tranche(
                            state=state,
                            units=martingale_sched[0],
                            buy_price=avg_price,
                            volume=quantity,
                            tranche_type="INITIAL"
                        )
                        state["bot_quantity"] = quantity
                        state["bot_avg_price"] = avg_price
                        save_strategy_state(market, state)

                    print(f"[{market}]   - 현재 포지션: 평단가 {avg_price:,.0f}원, 보유수량 {quantity}")

                    # 바스켓 익절 주문
                    sell_price = adjust_price_to_tick(avg_price * sell_profit_margin, method="ceil")
                    if (quantity * sell_price) >= MIN_ORDER_KRW:
                        upbit.sell_limit_order(market, sell_price, quantity)
                        print(f"[{market}]   - 바스켓 익절 매도 주문: {sell_price:,.0f}원, 수량 {quantity}")
                    else:
                        print(f"[{market}]   - [INFO] 바스켓 매도 평가액({quantity * sell_price:,.0f}원)이 최소주문(5,000원) 미만이므로 추가 물타기 체결 후 등록합니다.")
                    time.sleep(0.2)

                    # 마틴게일 매수 주문 계획
                    new_orders = calculate_new_buy_prices(
                        avg_buy_price=avg_price,
                        existing_orders=None,
                        max_steps=max_steps,
                        current_step=len(state.get("tranches", [])),
                        multipliers=martingale_sched
                    )
                    print(f"[{market}]   - 신규 마틴게일 매수 주문 계획 ({len(new_orders)}개): {new_orders}")

                    for order in new_orders:
                        p = order['price']
                        u = order['units']
                        order_krw = unit_krw * u
                        volume = round(order_krw / p, 8)
                        upbit.buy_limit_order(market, p, volume)
                        print(f"[{market}]     - 지정가 매수 주문: {p:,.0f}원 | {u} Units ({order_krw:,}원)")
                        time.sleep(0.2)

                    send_telegram_alert(
                        f"🔵 <b>[매수 체결]</b> {market} (하락 방어 1차수)\n"
                        f"• 체결가: {avg_price:,.0f}원 ({unit_krw:,}원)\n"
                        f"• 평단가: {avg_price:,.0f}원 | 수량: {quantity:.6f} {ticker}\n"
                        f"• 바스켓 목표: {sell_price:,.0f}원 (+{(sell_profit_margin - 1) * 100:.2f}%)"
                    )

                # Case 4: 물타기 매수 체결 감지
                elif num_sell == 1 and num_buy < expected_open_buys:
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
                    if (quantity * sell_price) >= MIN_ORDER_KRW:
                        upbit.sell_limit_order(market, sell_price, quantity)
                        print(f"[{market}]   - 새 바스켓 익절 매도: {sell_price:,.0f}원, 전량 {quantity:.6f}")
                    time.sleep(0.2)

                    # 추가 차수 등록 (체결된 해당 차수의 순수 매수 수량 기록, 고유 ID 보장)
                    step_seq = len(state.get("tranches", []))
                    assigned_units = martingale_sched[step_seq % len(martingale_sched)]
                    add_bot_tranche(
                        state=state,
                        units=assigned_units,
                        buy_price=current_price,
                        volume=bought_step_vol,
                        tranche_type="MARTINGALE"
                    )
                    save_strategy_state(market, state)

                    # 추가 매수 주문 계획 (최대 max_steps 차수 제한)
                    current_total_steps = len(state.get("tranches", [])) + len(buy_orders)
                    if current_total_steps >= max_steps:
                        print(f"[{market}]   - 마틴게일 최대 차수({max_steps}차수) 도달: 추가 물타기 매수 생략.")
                        additional_orders = []
                    else:
                        buy_order_details = [{'price': float(o['price']), 'units': 1} for o in buy_orders]
                        additional_orders = calculate_new_buy_prices(
                            avg_buy_price=avg_price,
                            existing_orders=buy_order_details,
                            max_steps=max_steps,
                            current_step=len(state.get("tranches", [])),
                            multipliers=martingale_sched
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

                    max_steps_str = f"{max_steps}차수" if max_steps < 900 else "무제한"
                    send_telegram_alert(
                        f"🔵 <b>[매수 체결]</b> {market} (하락 방어 {len(state['tranches'])}/{max_steps_str})\n"
                        f"• 체결가: {current_price:,.0f}원 ({unit_krw * assigned_units:,.0f}원)\n"
                        f"• 새 평단가: {avg_price:,.0f}원 | 총 보유: {quantity:.6f} {ticker}\n"
                        f"• 바스켓 목표: {sell_price:,.0f}원 (+{(sell_profit_margin - 1) * 100:.2f}%)"
                    )

            # =========================================================================
            # [분기 B] 상승 국면 (BULL): 5/20 MA 추세추종 & 트레일링 스탑 모드
            # =========================================================================
            else:
                state["active_mode"] = "TREND"

                # 포지션 보유 중: 트레일링 스탑, 추세 이탈 감시 및 피라미딩(불타기) 추가매수
                if total_value >= MIN_ORDER_KRW and avg_price > 0:
                    profit_rate = (current_price - avg_price) / avg_price

                    # 기존 포지션이 있으나 tranches가 비어있는 경우(이전 버전 호환) 1회차로 상태 복구
                    if len(state.get("tranches", [])) == 0:
                        state["tranches"] = []
                        add_bot_tranche(state, units=1, buy_price=avg_price, volume=quantity, tranche_type="RECOVERED")
                        save_strategy_state(market, state)

                    # B-1. 트레일링 스탑 가동 조건 (동적 목표가: 1~3회차 +10%, 4~6회차 +7%, 7~20회차 +5%)
                    current_steps = len(state.get("tranches", []))
                    active_ts_trigger = get_bull_trailing_stop_trigger(current_steps)
                    state["active_ts_trigger"] = active_ts_trigger

                    if USE_TRAILING_STOP and profit_rate >= active_ts_trigger:
                        if not state.get("trailing_stop_active", False):
                            state["trailing_stop_active"] = True
                            state["trend_peak_price"] = current_price
                            msg = (
                                f"🎯 <b>[트레일링 스탑 가동]</b> {market}\n"
                                f"• 수익률: {profit_rate*100:+.2f}% 도달 (목표 +{active_ts_trigger*100:.1f}% 돌파, 고점 추적 시작)"
                            )
                            print(f"\n[{market}] {msg}")
                            send_telegram_alert(msg)

                    if state.get("trailing_stop_active", False):
                        state["trend_peak_price"] = max(state.get("trend_peak_price", current_price), current_price)
                        peak_p = state["trend_peak_price"]
                        drop_from_peak = (current_price - peak_p) / peak_p

                        # 고점 대비 -3% 하락 시 조기 익절 청산
                        if drop_from_peak <= -TRAILING_STOP_DROP:
                            real_pnl = (current_price - avg_price) * quantity
                            cancel_all_orders(upbit, market)
                            time.sleep(0.5)
                            sell_res = upbit.sell_market_order(market, quantity)

                            if not (isinstance(sell_res, dict) and "uuid" in sell_res):
                                print(f"[{market}] [ERROR] 트레일링 스탑 시장가 매도 주문 실패: {sell_res}")
                                send_telegram_alert(f"⚠️ <b>[트레일링 스탑 주문 실패]</b> {market}\n시장가 매도 주문 응답: {sell_res}")
                                time.sleep(5)
                                continue

                            print(f"\n[{market}] {msg}")
                            send_telegram_alert(msg)

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
                            state["last_dca_buy_time"] = None
                            state["initial_entry_done"] = True
                            state["completed_cycles"] = state.get("completed_cycles", 0) + 1
                            save_strategy_state(market, state)
                            time.sleep(10)
                            continue

                    # (1차 이평선 추세청산 제거: 잔파동 털림을 방지하고 12시간 정기적립으로 물량을 모으며, +10% 트레일링 익절 및 -10% 긴급손절로만 관리)

                    # B-3. 상승장 12시간 정기 시간 분할 적립 (Time-DCA)
                    if USE_BULL_TIME_DCA and not state.get("trailing_stop_active", False):
                        current_steps = len(state.get("tranches", []))
                        if current_steps < MAX_BULL_DCA_STEPS:
                            last_buy_time_str = state.get("last_dca_buy_time")
                            if not last_buy_time_str and state.get("tranches"):
                                last_buy_time_str = state["tranches"][-1].get("time")

                            should_dca_buy = False
                            hours_elapsed = 0.0
                            now_dt = datetime.now()

                            if last_buy_time_str:
                                try:
                                    last_dt = datetime.strptime(str(last_buy_time_str)[:19], "%Y-%m-%d %H:%M:%S")
                                    hours_elapsed = (now_dt - last_dt).total_seconds() / 3600.0
                                    if hours_elapsed >= BULL_TIME_DCA_INTERVAL_HOURS:
                                        should_dca_buy = True
                                except Exception:
                                    should_dca_buy = False
                            else:
                                state["last_dca_buy_time"] = now_dt.strftime("%Y-%m-%d %H:%M:%S")
                                save_strategy_state(market, state)

                            if should_dca_buy:
                                krw_balance = check_krw_balance_alert(upbit, context=f"{market} 상승장 {BULL_TIME_DCA_INTERVAL_HOURS}시간 정기적립 {current_steps + 1}회차")
                                if krw_balance >= unit_krw:
                                    next_step = current_steps + 1
                                    print(
                                        f"\n[{market}] ⏰ [상승장 {BULL_TIME_DCA_INTERVAL_HOURS}시간 정기 분할적립 {next_step}/{MAX_BULL_DCA_STEPS}회차 매수]\n"
                                        f"직전 매수 후 {hours_elapsed:.1f}시간 경과 (기준: {BULL_TIME_DCA_INTERVAL_HOURS}시간) | 현재가: {current_price:,.0f}원"
                                    )
                                    prev_avail = float(upbit.get_balance(ticker) or 0.0)
                                    upbit.buy_market_order(market, unit_krw)
                                    time.sleep(1.5)

                                    new_avail = float(upbit.get_balance(ticker) or 0.0)
                                    bought_vol = max(0.0, new_avail - prev_avail)
                                    if bought_vol <= 0:
                                        bought_vol = round(unit_krw / current_price, 8)

                                    old_q = quantity
                                    old_avg = avg_price
                                    new_q = old_q + bought_vol
                                    new_avg = ((old_avg * old_q) + (current_price * bought_vol)) / new_q if new_q > 0 else current_price

                                    state["bot_quantity"] = new_q
                                    state["bot_avg_price"] = new_avg
                                    state["trend_peak_price"] = max(state.get("trend_peak_price", current_price), current_price)
                                    state["last_dca_buy_time"] = now_dt.strftime("%Y-%m-%d %H:%M:%S")

                                    add_bot_tranche(
                                        state=state,
                                        units=1,
                                        buy_price=current_price,
                                        volume=bought_vol,
                                        tranche_type="TIME_DCA"
                                    )
                                    save_strategy_state(market, state)

                                    log_real_trade(
                                        market=market,
                                        ticker=ticker,
                                        side="bid",
                                        action=f"BULL_TIME_DCA_STEP_{next_step}",
                                        price=current_price,
                                        volume=bought_vol,
                                        cost_or_revenue=unit_krw,
                                        pnl=0.0,
                                        strategy="HYBRID_TREND"
                                    )

                                    pnl_pct = ((current_price - new_avg) / new_avg) * 100
                                    msg = (
                                        f"🔵 <b>[매수 체결]</b> {market} (상승 적립 {next_step}/{MAX_BULL_DCA_STEPS}회차)\n"
                                        f"• 체결가: {current_price:,.0f}원 ({unit_krw:,}원)\n"
                                        f"• 새 평단가: {new_avg:,.0f}원 | 누적 수량: {new_q:.6f} {ticker}\n"
                                        f"• 현재 손익률: {pnl_pct:+.2f}%"
                                    )
                                    print(f"[{market}] {msg}")
                                    send_telegram_alert(msg)
                                    time.sleep(5)
                                    continue

                    # B-4. 상승장 피라미딩(불타기) 분할 추가매수 (Option 2 - 설정 시 동작)
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

                                    add_bot_tranche(
                                        state=state,
                                        units=1,
                                        buy_price=current_price,
                                        volume=bought_vol,
                                        tranche_type="PYRAMID"
                                    )
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

                                    pnl_pct = ((current_price - new_avg) / new_avg) * 100
                                    msg = (
                                        f"🔵 <b>[매수 체결]</b> {market} (불타기 {next_step}/{MAX_PYRAMID_STEPS}회차)\n"
                                        f"• 체결가: {current_price:,.0f}원 ({unit_krw:,}원)\n"
                                        f"• 새 평단가: {new_avg:,.0f}원 | 누적 수량: {new_q:.6f} {ticker}\n"
                                        f"• 현재 손익률: {pnl_pct:+.2f}%"
                                    )
                                    print(f"[{market}] {msg}")
                                    send_telegram_alert(msg)
                                    time.sleep(5)
                                    continue

                    # B-4. 상승장 일봉 양봉 종가매매 (08:50 ~ 09:00 KST, 당일 일봉 양봉 & 5일선 지지 시 1U 추가 매수)
                    if USE_BULL_CLOSING_BUY:
                        now_kst = datetime.utcnow() + timedelta(hours=9)
                        today_str = now_kst.strftime("%Y-%m-%d")
                        is_closing_window = (now_kst.hour == 8 and now_kst.minute >= 50)

                        if is_closing_window and state.get("last_closing_buy_date") != today_str:
                            current_steps = len(state.get("tranches", []))
                            if state.get("trailing_stop_active", False):
                                if now_kst.minute >= 55:
                                    state["last_closing_buy_date"] = today_str
                                    state["today_closing_status"] = "🎯 트레일링 익절 대기"
                                    save_strategy_state(market, state)
                                    skip_msg = (
                                        f"ℹ️ <b>[일봉 종가 매수 보류]</b> {market}\n"
                                        f"• 판정: <b>추가 매수 보류</b>\n"
                                        f"• 사유: 트레일링 스탑 가동 중 (수익 실현 감시 단계로 종가 추가 매수를 진행하지 않습니다.)"
                                    )
                                    print(f"[{market}] {skip_msg}")
                                    send_telegram_alert(skip_msg)
                            elif current_steps >= MAX_BULL_DCA_STEPS:
                                if now_kst.minute >= 55:
                                    state["last_closing_buy_date"] = today_str
                                    state["today_closing_status"] = f"☀️ 한도 완료 ({MAX_BULL_DCA_STEPS}/{MAX_BULL_DCA_STEPS}회차)"
                                    save_strategy_state(market, state)
                                    skip_msg = (
                                        f"ℹ️ <b>[일봉 종가 매수 완료]</b> {market}\n"
                                        f"• 판정: <b>추가 매수 종료</b>\n"
                                        f"• 사유: 상승장 최대 적립 한도({MAX_BULL_DCA_STEPS}회차)에 도달하여 추가 매수를 종료합니다."
                                    )
                                    print(f"[{market}] {skip_msg}")
                                    send_telegram_alert(skip_msg)
                            else:
                                closing_info = check_daily_closing_buy_condition(market, current_price)
                                if closing_info.get("can_buy", False):
                                    krw_balance = check_krw_balance_alert(upbit, context=f"{market} 상승장 일봉 종가매수 {current_steps + 1}회차")
                                    if krw_balance >= unit_krw:
                                        next_step = current_steps + 1
                                        print(
                                            f"\n[{market}] 🌅 [상승장 일봉 양봉 종가매수 {next_step}/{MAX_BULL_DCA_STEPS}회차 매수]\n"
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
                                        state["today_closing_status"] = f"🟢 체결 (+{unit_krw:,}원, {next_step}회차)"
                                        state["last_dca_buy_time"] = now_kst.strftime("%Y-%m-%d %H:%M:%S")

                                        add_bot_tranche(
                                            state=state,
                                            units=1,
                                            buy_price=current_price,
                                            volume=bought_vol,
                                            tranche_type="CLOSING"
                                        )
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

                                        pnl_pct = ((current_price - new_avg) / new_avg) * 100
                                        msg = (
                                            f"🔵 <b>[종가 매수 체결]</b> {market} (상승장 {next_step}/{MAX_BULL_DCA_STEPS}회차)\n"
                                            f"• 체결가: {current_price:,.0f}원 ({unit_krw:,}원)\n"
                                            f"• 새 평단가: {new_avg:,.0f}원 | 누적 수량: {new_q:.6f} {ticker}\n"
                                            f"• 현재 손익률: {pnl_pct:+.2f}%"
                                        )
                                        print(f"[{market}] {msg}")
                                        send_telegram_alert(msg)
                                        time.sleep(5)
                                        continue
                                    else:
                                        state["last_closing_buy_date"] = today_str
                                        state["today_closing_status"] = "⚠️ 예수금 부족"
                                        save_strategy_state(market, state)
                                        skip_msg = (
                                            f"⚠️ <b>[일봉 종가 매수 불가]</b> {market}\n"
                                            f"• 사유: 주문가능 예수금 부족 (필요: {unit_krw:,}원, 현재: {krw_balance:,.0f}원)"
                                        )
                                        print(f"[{market}] {skip_msg}")
                                        send_telegram_alert(skip_msg)
                                elif now_kst.minute >= 55:
                                    # 08:55 이후 마감 직전까지 조건 미충족 시: 당일 종가 매수 보류 판정 및 텔레그램 알림 발송
                                    reason_str = closing_info.get("reason", "일봉 조건 미충족")
                                    state["last_closing_buy_date"] = today_str
                                    state["today_closing_status"] = f"⚪ 보류 ({reason_str})"
                                    save_strategy_state(market, state)

                                    skip_msg = (
                                        f"ℹ️ <b>[상승장 일봉 종가 매수 보류]</b> {market}\n"
                                        f"• 판정: <b>매수 보류 (안전 관망)</b>\n"
                                        f"• 사유: <b>{reason_str}</b>\n"
                                        f"• 현재가: {current_price:,.0f}원 (시가: {closing_info.get('today_open', 0):,.0f}원)\n"
                                        f"• 5일 이평선: {closing_info.get('daily_ma5', 0):,.0f}원\n"
                                        f"🛡️ <b>[안전 규칙]</b> 하락 마감(음봉) 또는 5일선 하회 시에는 추가 하락 위험 방어를 위해 종가 매수를 진행하지 않습니다."
                                    )
                                    print(f"[{market}] {skip_msg}")
                                    send_telegram_alert(skip_msg)

                # 포지션 미보유 시: 최초 가동 즉시 10,000원 진입 또는 5/20 MA 골든크로스 신호 감시
                else:
                    # 1. 손절 재진입 쿨다운 검사
                    cooldown_until = float(state.get("stop_loss_cooldown_until", 0.0))
                    now_ts = time.time()
                    if now_ts < cooldown_until:
                        rem_min = int((cooldown_until - now_ts) / 60)
                        if now_sec - last_heartbeat_time >= 1800:
                            print(f"[{market}] ⏳ [상승 국면] 손절 후 쿨다운 대기 중 ({rem_min}분 남음). 신규 진입 차단.")
                        time.sleep(10)
                        continue

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
                            state["tranches"] = []
                            add_bot_tranche(
                                state=state,
                                units=1,
                                buy_price=current_price,
                                volume=existing_diff,
                                tranche_type="INITIAL_SYNC"
                            )
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
                            state["tranches"] = []
                            add_bot_tranche(
                                state=state,
                                units=1,
                                buy_price=avg_p,
                                volume=q,
                                tranche_type="INITIAL_ENTRY"
                            )
                            state["last_dca_buy_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
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

                            send_telegram_alert(
                                f"🔵 <b>[매수 체결]</b> {market} (상승 1회차 신규)\n"
                                f"• 체결가: {avg_p:,.0f}원 ({unit_krw:,}원)\n"
                                f"• 평단가: {avg_p:,.0f}원 | 수량: {q:.6f} {ticker}"
                            )
                            time.sleep(5)
                            continue

            save_strategy_state(market, state)

        except Exception as e:
            import traceback
            print(f"[{time.strftime('%H:%M:%S')}] [{market}] [CRITICAL] 메인 루프 에러: {repr(e)}\n{traceback.format_exc()}")
            time.sleep(10)

        time.sleep(5)


# --- 텔레그램 정기 현황 브리핑 (오전 8시 ~ 오후 8시, 1시간 간격) ---
def send_telegram_status_briefing():
    """
    현재 계좌 잔고 및 코인별 봇 포지션/진행상태를 텔레그램으로 깔끔하게 브리핑
    """
    try:
        from utils.analytics import get_total_account_summary
        from strategy.hybrid_regime import get_hybrid_regime_and_signals

        acc = get_total_account_summary()
        tot_equity = acc.get("total_equity", 0.0)
        krw_bal = acc.get("krw_balance", 0.0)
        unrealized_pnl = acc.get("unrealized_pnl", 0.0)
        coin_pnl_pct = acc.get("coin_pnl_pct", 0.0)

        lines = [
            "📊 <b>[정기 현황 브리핑]</b>",
            f"• 총 자산: <b>{tot_equity:,.0f}원</b> (평가손익 {unrealized_pnl:+,.0f}원 | {coin_pnl_pct:+.2f}%)",
            f"• 주문가능 예수금: <b>{krw_bal:,.0f}원</b>",
            ""
        ]

        markets = list(INVESTMENTS.keys())
        priority_map = {"KRW-BTC": 1, "KRW-ETH": 2, "KRW-SOL": 3}
        markets.sort(key=lambda x: priority_map.get(x, 99))

        for m in markets:
            if m == "KRW-BTC":
                try:
                    from strategy.btc_accumulator import check_btc_tiered_status, load_btc_state
                    btc_stat = check_btc_tiered_status(upbit_client=get_upbit_client())
                    btc_state = load_btc_state()
                    cur_p = btc_stat["current_price"]
                    avg_p = btc_stat["account_avg_price"]
                    tot_bal = btc_stat["account_btc_balance"]
                    pnl_pct = btc_stat["dist_avg_pct"]
                    ma200 = btc_stat["ma200"]
                    today_status = btc_state.get("today_status", "대기")
                    tier_label = btc_stat["tier_name"]

                    lines.append(f"<b>[KRW-BTC]</b> 🟡 계층형 가중 모으기 ({cur_p:,.0f}원)")
                    lines.append(f"• 총 보유: <b>{tot_bal:.6f} BTC</b> (계좌 평단 {avg_p:,.0f}원 | {pnl_pct:+.2f}%)")
                    lines.append(f"• 200일선: {ma200:,.0f}원 ({btc_stat['dist_ma200_pct']:+.2f}%) | {tier_label}")
                    lines.append(f"• 적립 상태: 15:05 업비트 1만 + 08:55 봇 ({today_status})")
                    lines.append("")
                except Exception as e_btc:
                    print(f"[WARN] BTC 브리핑 생성 오류: {e_btc}")
                continue

            ticker = m.split("-")[1]
            state = load_strategy_state(m)
            regime_info = get_hybrid_regime_and_signals(m)
            regime = regime_info.get("regime", "BULL")
            p = pyupbit.get_current_price(m) or 0.0
            q = state.get("bot_quantity", 0.0)
            avg = state.get("bot_avg_price", 0.0)
            tranches = state.get("tranches", [])
            pnl_pct = ((p - avg) / avg * 100.0) if avg > 0 else 0.0

            if regime == "BULL":
                regime_label = "🟢 상승 추세"
                prog_label = f"정기 적립 {len(tranches)}/{MAX_BULL_DCA_STEPS}회차" if q > 0 else "진입 대기 중"
            else:
                regime_label = "🔴 하락 방어"
                max_s = MARTINGALE_MAX_STEPS.get(m, 16 if m == "KRW-SOL" else 9999)
                max_str = f"{max_s}차" if max_s < 900 else "무제한"
                margin = (get_profit_margin(m) - 1) * 100
                prog_label = f"물타기 {len(tranches)}/{max_str} (바스켓 목표 +{margin:.2f}%)" if q > 0 else "진입 대기 중"

            lines.append(f"<b>[{m}]</b> {regime_label} ({p:,.0f}원)")
            if q > 0:
                lines.append(f"• 봇 보유: {q:.4f} {ticker} (평단 {avg:,.0f}원 | {pnl_pct:+.2f}%)")
                lines.append(f"• 진행 상태: {prog_label}")
                if regime == "BULL":
                    now_kst = datetime.utcnow() + timedelta(hours=9)
                    today_str = now_kst.strftime("%Y-%m-%d")
                    closing_st = state.get("today_closing_status")
                    if state.get("last_closing_buy_date") == today_str and closing_st:
                        lines.append(f"• 종가 매수: {closing_st}")
                    elif now_kst.hour < 8 or (now_kst.hour == 8 and now_kst.minute < 55):
                        lines.append("• 종가 매수: 08:55 판정 대기")
                    elif closing_st:
                        lines.append(f"• 종가 매수: {closing_st}")
            else:
                lines.append(f"• 봇 보유: 없음 ({prog_label})")
            lines.append("")

        send_telegram_alert("\n".join(lines).strip())
        print(f"[{time.strftime('%H:%M:%S')}] [BRIEFING] 텔레그램 정기 현황 브리핑 발송 완료.")
    except Exception as e:
        print(f"[WARN] 텔레그램 정기 브리핑 생성 오류: {e}")


def telegram_hourly_briefing_worker():
    """
    오전 8시부터 오후 8시까지(08:00 ~ 20:59) 1시간 간격으로
    정기 현황 브리핑을 텔레그램으로 발송하는 백그라운드 워커
    """
    print("[INFO] 텔레그램 정기 현황 브리핑 워커 가동 (08:00 ~ 20:00 KST 매시 1시간 간격)")
    last_sent_hour = None

    while True:
        try:
            kst_now = datetime.utcnow() + timedelta(hours=9)
            hour = kst_now.hour

            if 8 <= hour <= 20:
                if last_sent_hour != hour:
                    send_telegram_status_briefing()
                    last_sent_hour = hour
        except Exception as e:
            print(f"[WARN] 텔레그램 브리핑 루프 예외: {e}")

        time.sleep(30)


def telegram_command_listener_worker():
    """
    사용자가 텔레그램 채팅창에서 '/status', '/상태', '상태', '/현황' 등을 입력했을 때
    실시간으로 현재 상태 브리핑을 즉시 답장해주는 양방향 리스너 워커
    """
    import requests
    from utils.bot import TOKEN, CHAT_ID
    if not TOKEN or not CHAT_ID:
        return

    print("[INFO] 텔레그램 수동 명령어 리스너 가동 (/status, /상태, /현황, /잔고 등 지원)")
    offset = None

    while True:
        try:
            url = f"https://api.telegram.org/bot{TOKEN}/getUpdates"
            params = {"timeout": 15}
            if offset is not None:
                params["offset"] = offset

            resp = requests.get(url, params=params, timeout=20)
            if resp.status_code == 200:
                data = resp.json()
                if data.get("ok"):
                    for update in data.get("result", []):
                        offset = update["update_id"] + 1
                        msg = update.get("message", {})
                        from_chat_id = str(msg.get("chat", {}).get("id", ""))
                        text = msg.get("text", "").strip()

                        # 보안: 지정된 CHAT_ID에서 온 메시지만 처리
                        if from_chat_id != str(CHAT_ID):
                            continue

                        cmd = text.lower()
                        if cmd in ["/status", "/상태", "상태", "/현황", "현황", "/잔고", "잔고", "/check"]:
                            send_telegram_status_briefing()
                        elif cmd in ["/start", "/help", "도움말"]:
                            help_msg = (
                                "🤖 <b>[AutoBot 수동 확인 명령어]</b>\n\n"
                                "• <code>/상태</code> 또는 <code>/status</code>: 실시간 전체 계좌 및 코인별 봇 상태 즉시 확인\n"
                                "• 정기 브리핑: 오전 8시 ~ 오후 8시 매시 1시간 간격 자동 발송 중"
                            )
                            send_telegram_alert(help_msg)
        except Exception as e:
            time.sleep(3)

        time.sleep(1)


def run_btc_accumulator(market: str = "KRW-BTC"):
    """
    비트코인(KRW-BTC) 계층형 가중 적립 실전 봇 루프:
    - 손절/매도 원천 배제 (영구 수량 축적 Buy-Only)
    - 매일 오전 08:50 ~ 08:58 사이에 당일 1회 일봉 종가 조건 검사
    - 200일선 및 내 계좌 평단가 비교하여 0원 / 1만 원 / 2만 원 자동 매수 집행
    """
    from strategy.btc_accumulator import execute_btc_tiered_buy_if_due, check_btc_tiered_status
    print("=" * 65)
    print(f"🟡 업비트 비트코인 계층형 가중 적립 실전 봇 가동: {market}")
    print(f" 대상 마켓: {market} (기존 계좌 잔고 포함 모니터링)")
    print(f" 정기 모으기: 업비트 자체 매일 15:05 (10,000원 적립)")
    print(f" 스마트 적립: 봇 매일 08:55 일봉 종가 판정 (0원 / 1만 원 / 2만 원 추가 매수)")
    print(f" 리스크 관리: 무손절 장기 축적 (매도/청산 원천 배제)")
    print("=" * 65)

    try:
        upbit = get_upbit_client()
    except Exception as e:
        print(f"[{market}] [CRITICAL] 업비트 클라이언트 초기화 실패: {e}")
        return

    # 시작 시 현재 상태 점검 및 출력
    try:
        stat = check_btc_tiered_status(upbit)
        print(f"[{market}] 현재가: {stat['current_price']:,.0f}원 | 200일선: {stat['ma200']:,.0f}원 | 계좌평단: {stat['account_avg_price']:,.0f}원")
        print(f"[{market}] 현재 판정: {stat['tier_name']} ({stat['tier_reason']}) | 매수 예정액: {stat['buy_krw']:,}원")
    except Exception as e:
        print(f"[{market}] 초기 상태 조회 오류: {e}")

    send_telegram_alert(f"🟡 <b>[비트코인 모으기 봇 가동]</b> {market}\n• 무손절 계층형 적립 (매일 08:55 봇 스마트 가중 + 15:05 업비트)")

    last_heartbeat_hour = None

    while True:
        try:
            # 1. 08:50~08:58 종가 매수 검사 및 실행
            execute_btc_tiered_buy_if_due(upbit)

            # 2. 정시 하트비트 상태 출력
            kst_now = datetime.utcnow() + timedelta(hours=9)
            if kst_now.minute == 0 and last_heartbeat_hour != kst_now.hour:
                last_heartbeat_hour = kst_now.hour
                stat = check_btc_tiered_status(upbit)
                print(f"[{kst_now.strftime('%H:%M:%S')}] [{market}] 💓 하트비트 | 현재가: {stat['current_price']:,.0f}원 | {stat['tier_name']}")

        except Exception as e:
            print(f"[WARN] [{market}] 적립 루프 오류: {e}")

        time.sleep(10)


# --- 봇 단일 실행 핸들러 ---
def start_bot(market: str):
    """실전 매매 봇 실행 (BTC는 전용 무손절 적립 엔진, 알트는 하이브리드 엔진)"""
    if market == "KRW-BTC":
        run_btc_accumulator(market)
    else:
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
    print(f" - 상승장 추세: 5/20 MA 추세추종 & 12시간 정기 분할적립(Time-DCA)")
    print(f" - 예수금 10만원 미만 경고 기준: {MIN_KRW_ALERT_THRESHOLD:,}원")
    for m in target_markets:
        info = INVESTMENTS.get(m, {})
        margin = (get_profit_margin(m) - 1) * 100
        print(f"   • {m}: 투자배정 {info.get('total', 0):,}원 | 1Unit {info.get('unit', 0):,}원 | 익절 목표 +{margin:.2f}%")
    print("=" * 65)

    # 웹 대시보드(Streamlit Cloud)용 암호화 스냅샷 자동 동기화 스레드 가동
    try:
        from utils.sync_manager import start_background_sync_thread
        start_background_sync_thread(interval_sec=30)
    except Exception as e:
        print(f"[WARN] 클라우드 동기화 스레드 초기화 실패: {e}")

    # 텔레그램 정기 현황 브리핑 스레드 가동 (08:00 ~ 20:00 KST, 1시간 간격)
    try:
        t_brief = threading.Thread(target=telegram_hourly_briefing_worker, daemon=True, name="TelegramBriefingThread")
        t_brief.start()
    except Exception as e:
        print(f"[WARN] 텔레그램 정기 브리핑 스레드 초기화 실패: {e}")

    # 텔레그램 수동 명령어 리스너 스레드 가동 (/상태, /status 등 실시간 응답)
    try:
        t_cmd = threading.Thread(target=telegram_command_listener_worker, daemon=True, name="TelegramCommandThread")
        t_cmd.start()
    except Exception as e:
        print(f"[WARN] 텔레그램 명령어 리스너 스레드 초기화 실패: {e}")

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
