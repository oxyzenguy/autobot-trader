import os
import time
import json
import math
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
        "total_dca_volume": 0.0,
        "dca_avg_price": 0.0,
        "today_status": "대기",
        "last_sync_time": None
    }



def save_btc_state(state: Dict[str, Any]):
    """비트코인 적립 상태 파일 저장"""
    try:
        with open(BTC_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"[WARN] [KRW-BTC] 상태 파일 저장 실패: {e}")


def sync_upbit_btc_recurring_buys(upbit_client=None, start_date: str = "2026-09-24", orders: Optional[list] = None) -> Dict[str, Any]:
    """
    업비트 거래소의 비트코인 정기 '코인 모으기' 예약 매수 체결 내역을 동기화하여
    로컬 거래 원장(trade_history.db) 및 전략 상태(real_strategy_state_KRW_BTC.json)에 기록합니다.

    - 필터 조건:
      1) market == 'KRW-BTC'
      2) side == 'bid'
      3) state == 'done'
      4) time_in_force == 'ioc' (업비트 앱 '코인 모으기' 고유 주문 속성)
      5) created_at >= start_date (현재 적립 전략 개시일 이후)
    - 멱등성:
      이미 DB에 기록된 order_uuid는 건너뛰어 중복 기록을 원천 방지합니다.
    - 동기화 후:
      trade_history.db에 누적된 전체 BTC 매수 기록(봇 종가매수 + 업비트 모으기)을 바탕으로
      누적 매수 횟수, 총 매수금액, 총 매수수량, 정확한 적립 평단가(dca_avg_price)를 재산출하여 상태에 저장합니다.
    """
    if upbit_client is None:
        try:
            upbit_client = get_upbit_client()
        except Exception:
            pass

    if orders is None:
        from api.orders import get_closed_orders
        try:
            orders = get_closed_orders(market="KRW-BTC", state="done", limit=100)
        except Exception as e:
            print(f"[WARN] [KRW-BTC] 업비트 체결 주문 목록 조회 실패: {e}")

    if not isinstance(orders, list):
        return {"status": "ERROR", "message": "Failed to fetch closed orders", "synced_count": 0}

    init_db()
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT order_uuid FROM trades WHERE market = 'KRW-BTC' AND order_uuid IS NOT NULL")
    existing_uuids = {str(row[0]) for row in cur.fetchall()}

    newly_synced = 0
    # 오래된 주문부터 순서대로 반영
    for o in reversed(orders):
        if not isinstance(o, dict):
            continue
        if o.get("market") != "KRW-BTC":
            continue
        if o.get("side") != "bid":
            continue
        if o.get("state") != "done":
            continue
        if o.get("time_in_force") != "ioc":
            continue
        created_at_raw = o.get("created_at", "")
        if created_at_raw < start_date:
            continue

        order_uuid = o.get("uuid")
        if not order_uuid or str(order_uuid) in existing_uuids:
            continue

        # 상세 체결 내역 조회 (정확한 funds 및 체결단가 추출)
        order_detail = None
        if upbit_client is not None and hasattr(upbit_client, "get_order"):
            try:
                order_detail = upbit_client.get_order(order_uuid)
            except Exception:
                pass

        if isinstance(order_detail, dict) and order_detail.get("trades"):
            trades = order_detail.get("trades", [])
            total_funds = sum(float(t.get("funds", 0.0)) for t in trades)
            total_vol = float(order_detail.get("executed_volume", 0.0))
            cur_p = (total_funds / total_vol) if total_vol > 0 else float(order_detail.get("price", 0.0))
        elif o.get("trades"):
            trades = o.get("trades", [])
            total_funds = sum(float(t.get("funds", 0.0)) for t in trades)
            total_vol = float(o.get("executed_volume", 0.0))
            cur_p = (total_funds / total_vol) if total_vol > 0 else float(o.get("price", 0.0))
        else:
            total_vol = float(o.get("executed_volume", 0.0))
            order_p = float(o.get("price", 0.0))
            total_funds = total_vol * order_p
            cur_p = order_p

        if total_vol <= 0 or total_funds <= 0:
            continue

        try:
            ts_str = created_at_raw.replace("T", " ")[:19]
        except Exception:
            ts_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        log_real_trade(
            market="KRW-BTC",
            ticker="BTC",
            side="bid",
            action="BTC_UPBIT_DCA",
            price=cur_p,
            volume=total_vol,
            cost_or_revenue=total_funds,
            pnl=0.0,
            cycle=0,
            strategy="BTC_TIERED_ACCUMULATOR",
            strategy_id="BTC_TIERED_ACCUMULATOR",
            order_uuid=str(order_uuid),
            timestamp=ts_str
        )
        existing_uuids.add(str(order_uuid))
        newly_synced += 1

    # DB에 기록된 전체 BTC 매수 원장을 기반으로 정확한 누적 적립 지표 재산출
    cur.execute("""
        SELECT COUNT(*), COALESCE(SUM(cost_or_revenue), 0.0), COALESCE(SUM(volume), 0.0)
        FROM trades
        WHERE market = 'KRW-BTC' AND side = 'bid'
    """)
    row = cur.fetchone()
    conn.close()

    total_count = int(row[0]) if row else 0
    total_funds = float(row[1]) if row else 0.0
    total_vol = float(row[2]) if row else 0.0
    dca_avg_p = (total_funds / total_vol) if total_vol > 0 else 0.0

    state = load_btc_state()
    state["total_dca_buys_count"] = total_count
    state["total_dca_invested_krw"] = total_funds
    state["total_dca_volume"] = total_vol
    state["dca_avg_price"] = dca_avg_p
    state["last_sync_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    save_btc_state(state)

    return {
        "status": "SUCCESS",
        "synced_count": newly_synced,
        "total_dca_buys_count": total_count,
        "total_dca_invested_krw": total_funds,
        "total_dca_volume": total_vol,
        "dca_avg_price": dca_avg_p
    }



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
        "total_dca_volume": state.get("total_dca_volume", 0.0),
        "dca_avg_price": state.get("dca_avg_price", 0.0),
        "last_sync_time": state.get("last_sync_time"),
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

    state = load_btc_state()
    if state.get("pending_buy_order"):
        return _settle_btc_tiered_buy(upbit_client, state, kst_now)

    # 신규 주문은 08:50 ~ 08:58에만 실행하되, 미확정 주문은 창 밖에서도 확인한다.
    if not (hour == BTC_DCA_CLOSING_CHECK_HOUR and 50 <= minute <= 58):
        return None

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

        if not (isinstance(order_res, dict) and order_res.get("uuid")):
            print(f"[{kst_now.strftime('%H:%M:%S')}] [KRW-BTC] [ERROR] 비트코인 종가 매수 주문 실패: {order_res}")
            from main import send_telegram_alert
            send_telegram_alert(f"⚠️ <b>[비트코인 종가 매수 주문 실패]</b>\n시장가 매수 응답: {order_res}")
            return None

        # UUID는 접수 확인일 뿐이다. 조회 전에 저장해 다음 점검의 중복 주문을 막는다.
        state["pending_buy_order"] = {
            "uuid": order_res["uuid"], "tier": tier, "reason": reason,
            "credited_volume": 0.0, "credited_funds": 0.0,
        }
        save_btc_state(state)
        return _settle_btc_tiered_buy(upbit_client, state, kst_now)

    except Exception as e:
        print(f"[ERROR] [KRW-BTC] 종가 매수 실행 실패: {e}")
        return None


def _settle_btc_tiered_buy(upbit_client, state: Dict[str, Any], kst_now: datetime) -> Optional[Dict[str, Any]]:
    """미확정 주문은 유지하고, 종결된 주문의 실제 체결만 한 번 반영한다."""
    try:
        pending = state["pending_buy_order"]
        order_uuid = pending["uuid"]
        tier = pending["tier"]
        reason = pending["reason"]
        order = upbit_client.get_order(order_uuid)
        if not isinstance(order, dict) or order.get("uuid") != order_uuid:
            print(f"[WARN] [KRW-BTC] 매수 주문 조회 미확정: {order_uuid} ({order})")
            return None
        order_state = order.get("state")
        if order_state not in ("wait", "done", "cancel", "reject"):
            print(f"[KRW-BTC] 매수 주문 체결 대기: {order_uuid}")
            return None

        bought_vol = float(order["executed_volume"])
        buy_krw = sum(float(trade["funds"]) for trade in order.get("trades", []))
        credited_vol = float(pending.get("credited_volume", 0.0))
        credited_funds = float(pending.get("credited_funds", 0.0))
        delta_vol = bought_vol - credited_vol
        delta_funds = buy_krw - credited_funds
        if not all(math.isfinite(value) for value in (bought_vol, buy_krw, delta_vol, delta_funds)):
            print(f"[WARN] [KRW-BTC] 매수 주문 체결 내역 미확정: {order_uuid}")
            return None
        terminal = order_state in ("done", "cancel", "reject")
        if delta_vol == 0 and delta_funds == 0:
            if terminal:
                state.pop("pending_buy_order")
                if bought_vol > 0:
                    state["today_status"] = f"🟢 +{buy_krw:,.0f}원 체결 (Tier {tier})"
                else:
                    print(f"[KRW-BTC] 체결 없이 매수 주문 종결: {order_uuid}")
                save_btc_state(state)
            return None
        if not (delta_vol > 0 and delta_funds > 0):
            print(f"[WARN] [KRW-BTC] 매수 주문 체결 내역 미확정: {order_uuid}")
            return None
        cur_p = delta_funds / delta_vol

        # 계좌 전체 잔고는 알림에만 사용하고, 체결 금액/수량은 주문 조회값을 사용한다.
        bal_info = upbit_client.get_balance("BTC", verbose=True)
        new_bal = float(bal_info.get("balance", 0.0)) + float(bal_info.get("locked", 0.0))
        new_avg = float(bal_info.get("avg_buy_price", 0.0))

        # DB 체결 로그
        init_db()
        log_real_trade(
            market="KRW-BTC",
            ticker="BTC",
            side="bid",
            action=f"BTC_TIERED_BUY_T{tier}",
            price=cur_p,
            volume=delta_vol,
            cost_or_revenue=delta_funds,
            pnl=0.0,
            strategy="BTC_TIERED_ACCUMULATOR",
            order_uuid=order_uuid
        )

        state["last_checked_time"] = kst_now.strftime("%Y-%m-%d %H:%M:%S")
        state["last_buy_date"] = kst_now.strftime("%Y-%m-%d")
        state["last_buy_amount"] = buy_krw
        state["last_buy_tier"] = tier
        if credited_vol == 0:
            state["total_dca_buys_count"] = state.get("total_dca_buys_count", 0) + 1
        state["total_dca_invested_krw"] = state.get("total_dca_invested_krw", 0.0) + delta_funds
        state["total_dca_volume"] = state.get("total_dca_volume", 0.0) + delta_vol
        if state["total_dca_volume"] > 0:
            state["dca_avg_price"] = state["total_dca_invested_krw"] / state["total_dca_volume"]
        state["today_status"] = f"{'🟢' if terminal else '🟡'} +{buy_krw:,.0f}원 {'체결' if terminal else '부분체결'} (Tier {tier})"
        pending["credited_volume"] = bought_vol
        pending["credited_funds"] = buy_krw
        if terminal:
            state.pop("pending_buy_order")
        save_btc_state(state)

        # 텔레그램 체결 알림 발송
        from main import send_telegram_alert
        tier_badge = "🔥 2단계 역대급 세일" if tier == 2 else "✨ 1단계 단기 세일"
        dca_p_text = f", 평단: {state['dca_avg_price']:,.0f}원" if state.get("dca_avg_price", 0) > 0 else ""
        msg = (
            f"{'🟢' if terminal else '🟡'} <b>[비트코인 종가 추가매수 {'체결' if terminal else '부분체결'}]</b> ({tier_badge})\n"
            f"• 추가 매수액: <b>{delta_funds:,.0f}원</b>\n"
            f"• 매수 사유: {reason}\n"
            f"• 체결 단가: {cur_p:,.0f}원\n"
            f"• 총 보유량: <b>{new_bal:.6f} BTC</b> (계좌 평단: {new_avg:,.0f}원)\n"
            f"• 모으기 누적: {state['total_dca_buys_count']}회차 ({state['total_dca_invested_krw']:,.0f}원{dca_p_text})"
        )

        send_telegram_alert(msg)
        return {"action": "BOUGHT" if terminal else "PARTIAL", "tier": tier, "amount": delta_funds, "reason": reason}

    except Exception as e:
        print(f"[ERROR] [KRW-BTC] 종가 매수 실행 실패: {e}")
        return None
