import os
import time
import json
import sqlite3
import pandas as pd
import pyupbit
from datetime import datetime, timedelta
from typing import Dict, Any, Optional

from config import (
    get_upbit_client,
    MIN_ORDER_KRW,
    BTC_UNIT_KRW,
    BTC_DCA_CLOSING_CHECK_HOUR,
    BTC_DCA_CLOSING_CHECK_MINUTE,
    BTC_UPBIT_DCA_HOUR,
    BTC_UPBIT_DCA_MINUTE
)
from utils.db_logger import DB_PATH, log_real_trade, init_db

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BTC_STATE_FILE = os.path.join(BASE_DIR, "real_strategy_state_KRW_BTC.json")


def load_btc_state() -> Dict[str, Any]:
    """비트코인 계층형 적립 런타임 상태 파일 로드"""
    if os.path.exists(BTC_STATE_FILE):
        try:
            with open(BTC_STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "market": "KRW-BTC",
        "strategy": "BTC_TIERED_ACCUMULATOR",
        "active_mode": "TIERED_ACCUMULATOR_BUY_ONLY",
        "last_checked_time": None,
        "last_buy_date": None,
        "last_buy_amount": 0,
        "last_buy_tier": 0,
        "total_dca_buys_count": 0,
        "total_dca_invested_krw": 0.0,
        "today_status": "대기"
    }


def save_btc_state(state: Dict[str, Any]):
    """비트코인 적립 상태 파일 저장"""
    try:
        with open(BTC_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"[WARN] [KRW-BTC] 상태 파일 저장 실패: {e}")


_last_valid_btc_ma200 = 0.0
_last_btc_ma200_time = 0.0
_last_valid_btc_price = 0.0


def get_btc_200ma() -> float:
    """업비트 일봉 200일 이동평균선(200 SMA) 조회 및 계산 (결과 5분 캐싱 및 장애 시 이전값 활용)"""
    global _last_valid_btc_ma200, _last_btc_ma200_time
    now = time.time()
    if _last_valid_btc_ma200 > 0 and (now - _last_btc_ma200_time < 300):
        return _last_valid_btc_ma200

    try:
        df = pyupbit.get_ohlcv("KRW-BTC", interval="day", count=205)
        if df is not None and len(df) >= 200:
            ma200 = float(df["close"].rolling(200).mean().iloc[-1])
            if ma200 > 0:
                _last_valid_btc_ma200 = ma200
                _last_btc_ma200_time = now
                return ma200
    except Exception as e:
        print(f"[WARN] [KRW-BTC] 200일선 계산 실패: {e}")
    return _last_valid_btc_ma200


def check_btc_tiered_status(upbit_client=None) -> Dict[str, Any]:
    """
    현재 비트코인의 계층형 가중 적립 상태 및 조건 판정:
    - 현재가 vs 200일선 (시장 대세 하락 여부)
    - 현재가 vs 내 계좌 BTC 평단가 (계좌 마이너스 세일 여부)
    - 계층 판정:
      - Tier 0: 0원 추가 (맑음: 상승장 & 계좌 플러스)
      - Tier 1: 10,000원 추가 (흐림: 둘 중 하나 만족)
      - Tier 2: 20,000원 추가 (폭풍우: 둘 다 만족 = 역대급 바겐세일)
    """
    global _last_valid_btc_price
    if upbit_client is None:
        try:
            upbit_client = get_upbit_client()
        except Exception:
            pass

    cur_p = 0.0
    try:
        p = pyupbit.get_current_price("KRW-BTC")
        if p is not None and not isinstance(p, dict) and float(p) > 0:
            cur_p = float(p)
            _last_valid_btc_price = cur_p
        elif _last_valid_btc_price > 0:
            cur_p = _last_valid_btc_price
    except Exception:
        if _last_valid_btc_price > 0:
            cur_p = _last_valid_btc_price

    ma200 = get_btc_200ma()

    account_btc_bal = 0.0
    account_avg_p = 0.0
    if upbit_client is not None:
        try:
            bal_info = upbit_client.get_balance("BTC", verbose=True)
            if isinstance(bal_info, dict) and "avg_buy_price" in bal_info:
                account_btc_bal = float(bal_info.get("balance", 0.0)) + float(bal_info.get("locked", 0.0))
                account_avg_p = float(bal_info.get("avg_buy_price", 0.0))
        except Exception as e:
            print(f"[WARN] [KRW-BTC] 계좌 잔고 조회 실패: {e}")

    # [중요 안전장치] 시세(현재가 또는 200일선) 조회 실패 시 매수 판단 즉시 보류
    # (가격 0원을 폭락 바겐세일로 오인하여 2단계 매수가 오발주되는 사고 원천 차단)
    if cur_p <= 0.0 or ma200 <= 0.0:
        return {
            "market": "KRW-BTC",
            "current_price": cur_p,
            "ma200": ma200,
            "account_avg_price": account_avg_p,
            "account_btc_balance": account_btc_bal,
            "dist_avg_pct": 0.0,
            "dist_ma200_pct": 0.0,
            "is_below_avg": False,
            "is_below_ma200": False,
            "tier": -1,
            "tier_name": "⚠️ 시세 조회 실패 (매수 보류)",
            "tier_reason": "현재가 또는 200일선 조회 실패로 안전을 위해 매수를 중단합니다.",
            "buy_krw": 0,
            "already_bought_today": False,
            "is_valid": False,
            "last_buy_date": None,
            "total_dca_buys_count": 0,
            "total_dca_invested_krw": 0.0,
            "upbit_dca_schedule": f"매일 {BTC_UPBIT_DCA_HOUR:02d}:{BTC_UPBIT_DCA_MINUTE:02d} (10,000원)",
            "bot_dca_schedule": f"매일 {BTC_DCA_CLOSING_CHECK_HOUR:02d}:{BTC_DCA_CLOSING_CHECK_MINUTE:02d} (0~20,000원)"
        }

    # 조건 판정
    is_below_avg = (account_avg_p > 0) and (cur_p < account_avg_p)
    is_below_ma200 = (ma200 > 0) and (cur_p < ma200)

    dist_avg_pct = ((cur_p - account_avg_p) / account_avg_p * 100.0) if account_avg_p > 0 else 0.0
    dist_ma200_pct = ((cur_p - ma200) / ma200 * 100.0) if ma200 > 0 else 0.0

    if is_below_avg and is_below_ma200:
        tier = 2
        buy_krw = BTC_UNIT_KRW * 2  # 20,000원
        tier_name = "⛈️ 2단계 역대급 세일 (+20,000원)"
        tier_reason = "200일선 하회 + 계좌 평단가 하회 (대폭락 바겐세일)"
    elif is_below_avg or is_below_ma200:
        tier = 1
        buy_krw = BTC_UNIT_KRW      # 10,000원
        sub_desc = "계좌 평단가 하회" if is_below_avg else "200일선 하회"
        tier_name = f"⛅ 1단계 일반 세일 (+10,000원)"
        tier_reason = f"단기 세일 구간 ({sub_desc})"
    else:
        tier = 0
        buy_krw = 0
        tier_name = "☀️ 정상/상승 (0원 대기)"
        tier_reason = "상승장 & 수익 구간 (추가 매수 대기)"

    state = load_btc_state()
    kst_now = datetime.utcnow() + timedelta(hours=9)
    today_kst = kst_now.strftime("%Y-%m-%d")
    already_bought_today = (state.get("last_buy_date") == today_kst)

    return {
        "market": "KRW-BTC",
        "current_price": cur_p,
        "ma200": ma200,
        "account_avg_price": account_avg_p,
        "account_btc_balance": account_btc_bal,
        "dist_avg_pct": dist_avg_pct,
        "dist_ma200_pct": dist_ma200_pct,
        "is_below_avg": is_below_avg,
        "is_below_ma200": is_below_ma200,
        "tier": tier,
        "tier_name": tier_name,
        "tier_reason": tier_reason,
        "buy_krw": buy_krw,
        "already_bought_today": already_bought_today,
        "is_valid": True,
        "last_buy_date": state.get("last_buy_date"),
        "total_dca_buys_count": state.get("total_dca_buys_count", 0),
        "total_dca_invested_krw": state.get("total_dca_invested_krw", 0.0),
        "upbit_dca_schedule": f"매일 {BTC_UPBIT_DCA_HOUR:02d}:{BTC_UPBIT_DCA_MINUTE:02d} (10,000원)",
        "bot_dca_schedule": f"매일 {BTC_DCA_CLOSING_CHECK_HOUR:02d}:{BTC_DCA_CLOSING_CHECK_MINUTE:02d} (0~20,000원)"
    }


def execute_btc_tiered_buy_if_due(upbit_client) -> Optional[Dict[str, Any]]:
    """
    매일 아침 08:50 ~ 08:58 사이에 실행되어
    당일 미실행 시 계층형 가중 매수를 자동 집행합니다.
    """
    kst_now = datetime.utcnow() + timedelta(hours=9)
    hour = kst_now.hour
    minute = kst_now.minute
    today_kst = kst_now.strftime("%Y-%m-%d")

    # 점검 시각: 08:50 ~ 08:58
    if not (hour == BTC_DCA_CLOSING_CHECK_HOUR and 50 <= minute <= 58):
        return None

    state = load_btc_state()
    if state.get("last_buy_date") == today_kst:
        # 오늘 이미 처리 완료됨
        return None

    status = check_btc_tiered_status(upbit_client)
    buy_krw = status["buy_krw"]
    tier = status["tier"]
    reason = status["tier_reason"]

    # 0. 시세 조회 실패 또는 유효하지 않은 상태인 경우: 매수 판단 즉시 보류 (오발주 방지)
    if tier < 0 or not status.get("is_valid", True):
        print(f"[{kst_now.strftime('%H:%M:%S')}] [KRW-BTC] ⚠️ 시세 또는 200일선 조회 실패로 종가 매수 판단을 일시 보류합니다. (사유: {reason})")
        return None

    # 1. 추가 매수가 필요 없는 0단계 (상승 & 수익 중)
    if tier == 0 and buy_krw <= 0:
        state["last_checked_time"] = kst_now.strftime("%Y-%m-%d %H:%M:%S")
        state["last_buy_date"] = today_kst
        state["last_buy_amount"] = 0
        state["last_buy_tier"] = 0
        state["today_status"] = "☀️ 0원 (수익/상승장 추가매수 패스)"
        save_btc_state(state)
        print(f"[{kst_now.strftime('%H:%M:%S')}] [KRW-BTC] 종가 점검 완료: 0원 추가 (사유: {reason})")
        return {"action": "SKIP", "tier": 0, "amount": 0, "reason": reason}

    # 2. 추가 매수 집행 (1단계 10,000원 또는 2단계 20,000원)
    try:
        krw_bal = float(upbit_client.get_balance("KRW") or 0.0)
        if krw_bal < buy_krw:
            print(f"[{kst_now.strftime('%H:%M:%S')}] [KRW-BTC] [WARN] 예수금 부족으로 매수 불가 (필요: {buy_krw:,.0f}원, 잔고: {krw_bal:,.0f}원)")
            from main import send_telegram_alert
            send_telegram_alert(f"⚠️ <b>[비트코인 모으기 잔고 부족]</b>\n종가 가중 매수({buy_krw:,}원) 필요하나 예수금이 {krw_bal:,.0f}원입니다.")
            return None

        print(f"[{kst_now.strftime('%H:%M:%S')}] [KRW-BTC] 🟢 계층형 종가 매수 주문 집행: {buy_krw:,.0f}원 (Tier {tier}: {reason})")
        order_res = upbit_client.buy_market_order("KRW-BTC", buy_krw)
        time.sleep(1.5)

        if not (isinstance(order_res, dict) and "uuid" in order_res):
            print(f"[{kst_now.strftime('%H:%M:%S')}] [KRW-BTC] [ERROR] 비트코인 종가 매수 주문 실패: {order_res}")
            from main import send_telegram_alert
            send_telegram_alert(f"⚠️ <b>[비트코인 종가 매수 주문 실패]</b>\n시장가 매수 응답: {order_res}")
            return None

        # 체결 후 상태 갱신
        cur_p = pyupbit.get_current_price("KRW-BTC") or status["current_price"]
        bal_info = upbit_client.get_balance("BTC", verbose=True)
        new_bal = float(bal_info.get("balance", 0.0)) + float(bal_info.get("locked", 0.0))
        new_avg = float(bal_info.get("avg_buy_price", 0.0))

        # DB 체결 로그
        init_db()
        bought_vol = buy_krw / cur_p if cur_p > 0 else 0.0
        log_real_trade(
            market="KRW-BTC",
            ticker="BTC",
            side="bid",
            action=f"BTC_TIERED_BUY_T{tier}",
            price=cur_p,
            volume=bought_vol,
            cost_or_revenue=buy_krw,
            pnl=0.0,
            strategy="BTC_TIERED_ACCUMULATOR"
        )

        state["last_checked_time"] = kst_now.strftime("%Y-%m-%d %H:%M:%S")
        state["last_buy_date"] = today_kst
        state["last_buy_amount"] = buy_krw
        state["last_buy_tier"] = tier
        state["total_dca_buys_count"] = state.get("total_dca_buys_count", 0) + 1
        state["total_dca_invested_krw"] = state.get("total_dca_invested_krw", 0.0) + buy_krw
        state["today_status"] = f"🟢 +{buy_krw:,.0f}원 체결 (Tier {tier})"
        save_btc_state(state)

        # 텔레그램 체결 알림 발송
        from main import send_telegram_alert
        tier_badge = "🔥 2단계 역대급 세일" if tier == 2 else "✨ 1단계 단기 세일"
        msg = (
            f"🟢 <b>[비트코인 종가 추가매수 체결]</b> ({tier_badge})\n"
            f"• 추가 매수액: <b>{buy_krw:,.0f}원</b>\n"
            f"• 매수 사유: {reason}\n"
            f"• 체결 단가: {cur_p:,.0f}원\n"
            f"• 총 보유량: <b>{new_bal:.6f} BTC</b> (계좌 평단: {new_avg:,.0f}원)\n"
            f"• 모으기 누적: {state['total_dca_buys_count']}회차 ({state['total_dca_invested_krw']:,.0f}원)"
        )
        send_telegram_alert(msg)
        return {"action": "BOUGHT", "tier": tier, "amount": buy_krw, "reason": reason}

    except Exception as e:
        print(f"[ERROR] [KRW-BTC] 종가 매수 실행 실패: {e}")
        return None
