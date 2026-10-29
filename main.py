import os
import sys

import signal
import time

# 터미널 창 닫힘 및 세션 종료(SIGHUP) 시 프로세스 강제 종료 원천 방지
try:
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
except Exception:
    pass

import json
import math
from decimal import Decimal, ROUND_DOWN
import threading
import fcntl
import tempfile
import sqlite3
from contextlib import closing
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
    MIN_BULL_BUY_GAP_HOURS,
    USE_MAGIC_SPLIT_DEFENSE,
    MAGIC_SPLIT_TRANCHE_PROFIT,
    MAGIC_SPLIT_DOWN_PCT,
    MARTINGALE_MULTIPLIERS,
    BULL_STOP_LOSS_PCT,
    REGIME_SWITCH_LIQUIDATION_PCT,
    MARTINGALE_SCHEDULE,
    MARTINGALE_MAX_STEPS,
    get_martingale_max_steps,
    STOP_LOSS_COOLDOWN_HOURS,
    REGIME_SWITCH_COOLDOWN_HOURS,
    TELEGRAM_BRIEFING_SCHEDULE,
    MARKET_STRATEGIES,
    get_market_strategy,
    get_magic_split_down_pct,
    get_magic_split_tranche_profit,
    USE_ADAPTIVE_GRID,
    ENABLE_ADAPTIVE_GRID_SHADOW,
    is_adaptive_grid_enabled,
    is_adaptive_grid_shadow_enabled,
)
from strategy.matingale2x_logic import calculate_new_buy_prices, adjust_price_to_tick, get_existing_stages_from_state
from strategy.adaptive_grid import (
    calculate_adaptive_buy_prices,
    calculate_basket_sell_price,
    format_shadow_mode_log,
    get_market_regime_and_natr,
    DEFAULT_STEP_SCALE,
)
from strategy.hybrid_regime import get_hybrid_regime_and_signals, check_magic_split_exits, check_daily_closing_buy_condition
from utils.db_logger import log_real_trade, init_db
from utils import db_logger

# 예수금 알림 쿨다운 관리
LAST_KRW_ALERT_TIME = 0
KRW_ALERT_COOLDOWN_SEC = 3600  # 1시간 쿨다운 (도배 방지)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ORDER_SETTLEMENT_ATTEMPTS = 5
ORDER_SETTLEMENT_DELAY_SEC = 0.5


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
        except Exception as e:
            print(f"[CRITICAL] [{market}] 상태 JSON 파싱 오류로 거래 중단 격리: {e}")
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
                "completed_cycles": 0,
                "trading_halted": True,
                "halt_reason": f"상태 JSON 파일 손상 ({e})",
            }
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
        "completed_cycles": 0,
        "trading_halted": False
    }


_last_saved_state_strings = {}

def save_strategy_state(market: str, state: dict):
    """실전 매매 상태 영속 저장 (내용이 실제로 변경되었을 때만 디스크 쓰기)"""
    path = get_state_file_path(market)
    temp_path = None
    try:
        new_content = json.dumps(state, indent=2, ensure_ascii=False)
        if _last_saved_state_strings.get(market) == new_content:
            return True
        fd, temp_path = tempfile.mkstemp(
            dir=BASE_DIR,
            prefix=f".{market.replace('-', '_')}_",
            suffix=".json.tmp"
        )
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(new_content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, path)
        temp_path = None
        _last_saved_state_strings[market] = new_content
        return True
    except Exception as e:
        print(f"[{market}] 상태 파일 저장 실패: {e}")
        return False
    finally:
        if temp_path and os.path.exists(temp_path):
            os.unlink(temp_path)


def add_bot_tranche(
    state: dict,
    units: int,
    buy_price: float,
    volume: float,
    tranche_type: str = "NORMAL",
    squad_id: Optional[int] = None,
    squad_stage: Optional[int] = None
) -> dict:
    """
    고유 식별자(tranche_id)를 보장하여 차수를 추가합니다.
    차수 매도 후 번호 재사용으로 인한 장부 왜곡 및 중복 삭제를 원천 방지합니다.
    스쿼드(squad_id, squad_stage) 정보를 영속 저장합니다.
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
        "type": tranche_type,
        "squad_id": squad_id,
        "squad_stage": squad_stage
    }
    tranches.append(new_tr)
    return new_tr


def get_hours_since_last_buy(state: dict, now_dt: datetime) -> float:
    """
    최근 매수(초기 진입, 12시간 정기적립, 일봉 종가매수 등) 시점으로부터 경과한 시간(시간 단위)을 반환합니다.
    last_dca_buy_time과 tranches의 최신 매수 시각 중 가장 최근 시각을 기준으로 계산합니다.
    """
    candidates = []
    last_dca = state.get("last_dca_buy_time")
    if last_dca:
        try:
            candidates.append(datetime.strptime(str(last_dca)[:19], "%Y-%m-%d %H:%M:%S"))
        except Exception:
            pass
    if state.get("tranches"):
        last_tr_time = state["tranches"][-1].get("time")
        if last_tr_time:
            try:
                candidates.append(datetime.strptime(str(last_tr_time)[:19], "%Y-%m-%d %H:%M:%S"))
            except Exception:
                pass
    if not candidates:
        return 999.0  # 과거 매수 이력 없음: 충분히 경과한 것으로 취급
    latest_dt = max(candidates)
    return max(0.0, (now_dt - latest_dt).total_seconds() / 3600.0)


# --- 잔고 및 계좌 조회 (기존 자산 보호 & 봇 전용 포지션 관리) ---
def get_bot_balance(upbit_client, market: str, ticker: str, state: dict):
    """
    기존 보유 자산(PROTECTED_BALANCES)을 완전히 보호/격리하고,
    오직 봇이 신규 매수한 포지션의 평단가와 수량만을 안전하게 반환합니다.
    """
    bot_qty = float(state.get("bot_quantity", 0.0))
    bot_avg = float(state.get("bot_avg_price", 0.0))

    try:
        balance_info = None
        try:
            balance_info = upbit_client.get_balance(ticker, verbose=True)
        except TypeError:
            balance_info = upbit_client.get_balance(ticker)

        if isinstance(balance_info, dict) and 'balance' in balance_info:
            raw_avail = balance_info['balance']
            raw_locked = balance_info.get('locked', 0.0) or 0.0
            if isinstance(raw_avail, bool) or isinstance(raw_locked, bool) or raw_avail is None:
                raise ValueError("invalid exchange balance type")
            avail = float(raw_avail)
            locked = float(raw_locked)
        elif isinstance(balance_info, (int, float, str)) and not isinstance(balance_info, bool):
            avail = float(balance_info)
            locked = 0.0
        else:
            raise ValueError("invalid exchange balance structure")

        if not math.isfinite(avail) or not math.isfinite(locked) or avail < 0 or locked < 0:
            raise ValueError("invalid exchange balance")
        total_held = avail + locked
        if not math.isfinite(total_held):
            raise ValueError("invalid exchange balance")
        state.pop("balance_query_failed", None)
        if bot_qty <= 0.0:
            return 0.0, 0.0
        # 실제 업비트 총 보유 수량(가용+주문잠금) 내에서만 안전하게 캡핑
        # Decimal 계산 및 내림(ROUND_DOWN)으로 IEEE 754 부동소수점 오차 및 상향 라운딩 방지
        d_zero = Decimal("0")
        d_avail = Decimal(str(avail))
        d_locked = Decimal(str(locked))
        d_prot = Decimal(str(PROTECTED_BALANCES.get(market, 0.0) or 0.0))
        d_total = d_avail + d_locked
        d_max_unprotected = max(d_zero, d_total - d_prot)
        d_bot_qty = Decimal(str(bot_qty))
        safe_d = max(d_zero, min(d_bot_qty, d_max_unprotected))
        safe_qty = float(safe_d.quantize(Decimal("1e-8"), rounding=ROUND_DOWN))
        return bot_avg, safe_qty
    except Exception as e:
        print(f"[{time.strftime('%H:%M:%S')}] [ERROR] [{ticker}] 봇 잔고 확인 오류: {e}")

    state["balance_query_failed"] = True
    return 0.0, 0.0


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


def get_order_fill_price(order: dict, executed_volume: float) -> float:
    trades = order.get("trades", []) if isinstance(order, dict) else []
    if not isinstance(trades, list) or executed_volume <= 0:
        return 0.0
    total_funds = 0.0
    for trade in trades:
        if not isinstance(trade, dict):
            continue
        try:
            total_funds += float(trade.get("funds", 0.0) or 0.0)
        except (TypeError, ValueError):
            continue
    return total_funds / executed_volume if total_funds > 0 else 0.0


def submit_market_order_and_wait(upbit_client, market: str, side: str, amount: float, on_accepted=None) -> dict:
    try:
        if side == "bid":
            response = upbit_client.buy_market_order(market, amount)
        else:
            response = upbit_client.sell_market_order(market, amount)
    except Exception as e:
        return {"status": "REJECTED", "reason": f"주문 제출 예외: {e}"}

    if not isinstance(response, dict) or not response.get("uuid"):
        return {"status": "REJECTED", "reason": f"주문 접수 실패: {response}"}

    order_uuid = response["uuid"]
    if on_accepted is not None:
        on_accepted(order_uuid)
    for attempt in range(ORDER_SETTLEMENT_ATTEMPTS):
        try:
            order = upbit_client.get_order(order_uuid)
        except Exception as e:
            order = None
            last_error = f"체결 조회 예외: {e}"
        else:
            last_error = "체결 대기 중"

        if isinstance(order, dict):
            resp_uuid = order.get("uuid")
            if not resp_uuid or resp_uuid != order_uuid:
                return {"status": "PENDING", "uuid": order_uuid, "reason": "주문 UUID 조회 불일치"}
            state = order.get("state")
            if state in ("done", "cancel", "reject"):
                try:
                    executed_volume = float(order.get("executed_volume", 0.0) or 0.0)
                except (TypeError, ValueError):
                    executed_volume = 0.0
                fill_price = get_order_fill_price(order, executed_volume)
                if executed_volume > 0 and fill_price > 0:
                    if side == "ask" and executed_volume < amount * (1.0 - 0.000001):
                        return {
                            "status": "PARTIAL",
                            "uuid": order_uuid,
                            "volume": executed_volume,
                            "price": fill_price,
                            "reason": "시장가 매도 부분체결"
                        }
                    return {
                        "status": "FILLED",
                        "uuid": order_uuid,
                        "volume": executed_volume,
                        "price": fill_price,
                        "fee": float(order.get("paid_fee", 0.0) or 0.0)
                    }
                return {"status": "REJECTED", "uuid": order_uuid, "reason": f"주문 종결 상태: {state}" if executed_volume == 0 else "체결 수량 또는 체결가 없음"}


        if attempt < ORDER_SETTLEMENT_ATTEMPTS - 1:
            time.sleep(ORDER_SETTLEMENT_DELAY_SEC)

    return {"status": "PENDING", "uuid": order_uuid, "reason": last_error}


def get_trailing_exit_recovery_path(market: str) -> str:
    return get_state_file_path(market) + ".trailing-exit.json"


def save_trailing_exit_recovery(market: str, recovery: dict) -> None:
    """Checkpoint the exit before an order can be sent or its UUID acknowledged."""
    path = get_trailing_exit_recovery_path(market)
    directory = os.path.dirname(path)
    fd, temporary = tempfile.mkstemp(dir=directory, prefix=".trailing-exit-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(recovery, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def load_trailing_exit_recovery(market: str) -> Optional[dict]:
    path = get_trailing_exit_recovery_path(market)
    if not os.path.lexists(path):
        return None
    with open(path, encoding="utf-8") as handle:
        recovery = json.load(handle)
    if (
        not isinstance(recovery, dict)
        or recovery.get("market") != market
        or recovery.get("kind") not in ("TRAILING_STOP_EXIT", "STOP_LOSS_EXIT")
        or not isinstance(recovery.get("uuid"), (str, type(None)))
        or type(recovery.get("volume")) not in (int, float)
        or not math.isfinite(recovery["volume"])
        or recovery["volume"] <= 0
        or type(recovery.get("avg_price")) not in (int, float)
        or not math.isfinite(recovery["avg_price"])
        or recovery["avg_price"] <= 0
        or type(recovery.get("cycles")) is not int
        or recovery["cycles"] < 0
        or (recovery["kind"] == "STOP_LOSS_EXIT" and (
            type(recovery.get("cooldown_until")) not in (int, float)
            or not math.isfinite(recovery["cooldown_until"])
            or recovery["cooldown_until"] <= 0
        ))
    ):
        raise ValueError("손상된 trailing 청산 recovery 정보")
    return recovery


def clear_trailing_exit_recovery(market: str) -> None:
    path = get_trailing_exit_recovery_path(market)
    os.unlink(path)
    directory_fd = os.open(os.path.dirname(path), os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def reconcile_trailing_exit(upbit_client, market: str, state: dict, recovery: dict, fresh_result: Optional[dict] = None) -> bool:
    """Only release the halt after the exact fill, trade row, and final JSON are durable."""
    is_stop_loss = recovery["kind"] == "STOP_LOSS_EXIT"
    action = "BULL_STOP_LOSS_10PCT" if is_stop_loss else "TRAILING_STOP_EXIT"
    order_uuid = recovery["uuid"]
    if not order_uuid:
        return False
    if fresh_result is None:
        order = upbit_client.get_order(order_uuid)
        if not isinstance(order, dict) or order.get("uuid") != order_uuid:
            return False
        if order.get("state") in ("cancel", "reject"):
            if float(order.get("executed_volume", 0) or 0) != 0:
                return False
            if is_stop_loss:
                return False
            clear_trailing_exit_recovery(market)
            return True
        if order.get("state") != "done":
            return False
        volume = float(order.get("executed_volume", 0) or 0)
        price = get_order_fill_price(order, volume)
        result = {"uuid": order_uuid, "volume": volume, "price": price}
    else:
        result = fresh_result
    volume = float(result["volume"])
    price = float(result["price"])
    if (
        result["uuid"] != order_uuid or not math.isfinite(price) or not math.isfinite(volume)
        or price <= 0 or volume < recovery["volume"] * (1 - 0.000001)
    ):
        return False

    if fresh_result is None:
        with closing(sqlite3.connect(db_logger.DB_PATH)) as connection:
            recorded = connection.execute(
                "SELECT 1 FROM trades WHERE order_uuid = ? AND action = ?",
                (order_uuid, action),
            ).fetchone() is not None
    else:
        recorded = False
    if not recorded:
        log_real_trade(
            market=market, ticker=market.split("-")[1], side="ask",
            action=action, price=price, volume=volume,
            cost_or_revenue=volume * price,
            pnl=(price - recovery["avg_price"]) * volume,
            strategy="HYBRID_TREND", order_uuid=order_uuid,
        )

    if state.get("completed_cycles", 0) == recovery["cycles"] and state.get("bot_quantity", 0) == recovery["volume"]:
        state["bot_quantity"] = 0.0
        state["bot_avg_price"] = 0.0
        state["tranches"] = []
        state["trailing_stop_active"] = False
        state["trend_peak_price"] = 0.0
        state["last_dca_buy_time"] = None
        state["initial_entry_done"] = True
        state["need_fresh_golden_cross"] = True
        if is_stop_loss:
            state["stop_loss_cooldown_until"] = recovery["cooldown_until"]
        else:
            state["completed_cycles"] = recovery["cycles"] + 1
    elif (state.get("completed_cycles", 0) != recovery["cycles"] + (0 if is_stop_loss else 1)
          or state.get("bot_quantity") != 0.0
          or (is_stop_loss and state.get("stop_loss_cooldown_until") != recovery["cooldown_until"])):
        return False
    if save_strategy_state(market, state) is not True:
        return False
    directory_fd = os.open(os.path.dirname(get_state_file_path(market)), os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    clear_trailing_exit_recovery(market)
    return True


def submit_limit_order(upbit_client, market: str, side: str, price: float, volume: float) -> Optional[dict]:
    try:
        if side == "bid":
            response = upbit_client.buy_limit_order(market, price, volume)
        else:
            response = upbit_client.sell_limit_order(market, price, volume)
    except Exception as e:
        print(f"[{market}] [ERROR] 지정가 주문 제출 예외: {e}")
        return None
    if not isinstance(response, dict) or not response.get("uuid"):
        print(f"[{market}] [ERROR] 지정가 주문 접수 실패: {response}")
        return None
    return response


def track_bear_limit_buy(
    market: str,
    state: dict,
    order: dict,
    units: int,
    squad_id: Optional[int] = None,
    squad_stage: Optional[int] = None
):
    """Persist ownership before the next tick; acceptance is not execution."""
    state.setdefault("bear_limit_buys", {})[order["uuid"]] = {
        "units": units,
        "squad_id": squad_id,
        "squad_stage": squad_stage,
        "state": "wait",
        "executed_volume": 0.0,
        "executed_funds": 0.0,
    }
    saved = save_strategy_state(market, state)
    if saved is False:
        halt_for_order_reconciliation(market, state, {"reason": "지정가 매수 주문 상태 저장 실패", "uuid": order["uuid"]})


def _get_db_cumulative_trades(order_uuid: str, side: str, market: str) -> Optional[tuple[float, float]]:
    """DB에서 해당 UUID 및 side의 누적 체결량(volume, cost_or_revenue)을 조회.
    조회 실패 시 None을 반환하여 호출자가 0으로 추정하지 않고 즉시 안전 차단(fail-closed)하도록 한다.
    """
    try:
        with closing(sqlite3.connect(db_logger.DB_PATH)) as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(volume), 0), COALESCE(SUM(cost_or_revenue), 0) FROM trades WHERE order_uuid = ? AND side = ?",
                (order_uuid, side)
            ).fetchone()
            if row:
                return float(row[0] or 0.0), float(row[1] or 0.0)
            return 0.0, 0.0
    except sqlite3.OperationalError as oe:
        if "no such table" in str(oe).lower():
            try:
                db_logger.init_db()
                with closing(sqlite3.connect(db_logger.DB_PATH)) as conn:
                    row = conn.execute(
                        "SELECT COALESCE(SUM(volume), 0), COALESCE(SUM(cost_or_revenue), 0) FROM trades WHERE order_uuid = ? AND side = ?",
                        (order_uuid, side)
                    ).fetchone()
                    if row:
                        return float(row[0] or 0.0), float(row[1] or 0.0)
                    return 0.0, 0.0
            except Exception as ie:
                print(f"[{market}] [ERROR] 누적 체결 DB 조회 실패 ({order_uuid}, {side}): {ie}")
                return None
        print(f"[{market}] [ERROR] 누적 체결 DB 조회 실패 ({order_uuid}, {side}): {oe}")
        return None
    except Exception as e:
        print(f"[{market}] [ERROR] 누적 체결 DB 조회 실패 ({order_uuid}, {side}): {e}")
        return None


def reconcile_bear_limit_buys(upbit_client, market: str, state: dict, *, notify: bool = False) -> bool:
    """Credit only new cumulative UUID fills, including still-open partials."""
    for order_uuid, recorded in state.get("bear_limit_buys", {}).items():
        if recorded["state"] in ("done", "cancel"):
            continue
        try:
            order = upbit_client.get_order(order_uuid)
            if (not isinstance(order, dict) or order.get("uuid") != order_uuid
                    or order.get("state") not in ("wait", "done", "cancel")):
                raise ValueError("unconfirmed limit-buy status")
            executed = float(order["executed_volume"])
            funds = get_order_fill_price(order, executed) * executed
            delta_volume = executed - recorded["executed_volume"]
            delta_funds = funds - recorded["executed_funds"]
            if (not math.isfinite(executed) or not math.isfinite(funds)
                    or delta_volume < 0 or delta_funds < 0
                    or (delta_volume > 0 and delta_funds <= 0)):
                raise ValueError("invalid cumulative limit-buy execution")
        except Exception as e:
            halt_for_order_reconciliation(market, state, {"uuid": order_uuid, "reason": str(e)})
            return False

        if delta_volume > 0:
            db_res = _get_db_cumulative_trades(order_uuid, "bid", market)
            if db_res is None:
                return False
            db_vol, db_funds = db_res

            db_delta_vol = max(0.0, round(executed - db_vol, 8))
            db_delta_funds = max(0.0, round(funds - db_funds, 4))
            if db_delta_vol > 1e-12:
                try:
                    fill_price = db_delta_funds / db_delta_vol if db_delta_funds > 0 else (delta_funds / delta_volume)
                    s_id = recorded.get("squad_id", 1)
                    s_stage = recorded.get("squad_stage", 1)
                    m_strat = get_market_strategy(market)
                    strat_name = "PURE_MARTINGALE" if m_strat == "PURE_MARTINGALE" else "HYBRID_MARTINGALE_MAGIC_SPLIT"
                    log_real_trade(
                        market=market,
                        ticker=market.split("-")[1],
                        side="bid",
                        action=f"MARTINGALE_LIMIT_BUY_SQUAD_{s_id}_STAGE_{s_stage}",
                        price=fill_price,
                        volume=db_delta_vol,
                        cost_or_revenue=db_delta_funds,
                        pnl=0.0,
                        cycle=state.get("completed_cycles", 0),
                        strategy=strat_name,
                        order_uuid=order_uuid
                    )
                except Exception as le:
                    print(f"[{market}] [ERROR] 지정가 매수 체결 DB 기록 오류: {le}")
                    return False

            old_quantity = float(state.get("bot_quantity", 0.0))
            old_cost = old_quantity * float(state.get("bot_avg_price", 0.0))
            state["bot_quantity"] = round(old_quantity + delta_volume, 8)
            state["bot_avg_price"] = (old_cost + delta_funds) / state["bot_quantity"]
            tranche = next((tr for tr in state.get("tranches", [])
                            if tr.get("order_uuid") == order_uuid), None)
            if tranche is None:
                tranche = add_bot_tranche(
                    state, recorded["units"], delta_funds / delta_volume,
                    delta_volume, tranche_type="MARTINGALE",
                    squad_id=recorded.get("squad_id"),
                    squad_stage=recorded.get("squad_stage")
                )
                tranche["order_uuid"] = order_uuid
            else:
                cost = tranche["volume"] * tranche["buy_price"] + delta_funds
                tranche["volume"] = round(tranche["volume"] + delta_volume, 8)
                tranche["buy_price"] = cost / tranche["volume"]
            state["bear_buy_rebalance_pending"] = True

        if delta_volume > 0 or recorded["state"] != order["state"]:
            recorded.update(state=order["state"], executed_volume=executed, executed_funds=funds)
            # The position and cumulative checkpoint share one atomic state snapshot.
            # Terminal records survive tranche sales/restarts to prevent re-crediting.
            if save_strategy_state(market, state) is False:
                return False
            if notify and delta_volume > 0 and db_delta_vol > 1e-12:
                send_telegram_alert(
                    f"🔵 <b>[매수 체결]</b> {market} (지정가 체결 확인)\n"
                    f"• 이번 체결: {db_delta_vol:.8f} {market.split('-')[1]} | 체결대금: {db_delta_funds:,.2f}원\n"
                    f"• 체결단가: {db_delta_funds / db_delta_vol:,.0f}원 | 누적 체결: {executed:.8f}\n"
                    f"• 봇 평단가: {state['bot_avg_price']:,.0f}원 | 봇 보유: {state['bot_quantity']:.8f}\n"
                    f"• 주문 UUID: {order_uuid}"
                )
    return True


def _reconcile_ask_order_fill(upbit_client, market: str, state: dict, order: dict, executed_volume: float, action: Optional[str] = None) -> bool:
    if not isinstance(order, dict) or executed_volume <= 0:
        return False
    order_uuid = order.get("uuid")
    if not order_uuid:
        return False
    order_state = order.get("state")
    ticker = market.split("-")[1]

    trades = order.get("trades", []) if isinstance(order, dict) else []
    exec_funds = 0.0
    if isinstance(trades, list):
        for t in trades:
            if isinstance(t, dict):
                try:
                    tf = float(t.get("funds", 0.0) or 0.0)
                    if tf <= 0:
                        tf = float(t.get("price", 0.0) or 0.0) * float(t.get("volume", 0.0) or 0.0)
                    exec_funds += tf
                except (TypeError, ValueError):
                    pass

    ask_info = state.get("basket_limit_ask") if isinstance(state.get("basket_limit_ask"), dict) else {}
    order_p = 0.0
    try:
        order_p = float(order.get("price", 0.0) or 0.0)
    except (TypeError, ValueError):
        order_p = 0.0
    if order_p <= 0:
        try:
            order_p = float(ask_info.get("price", 0.0) or 0.0)
        except (TypeError, ValueError):
            order_p = 0.0

    if executed_volume > 0 and exec_funds > 0:
        total_funds = exec_funds
        avg_price = exec_funds / executed_volume
    elif executed_volume > 0 and order_p > 0:
        total_funds = executed_volume * order_p
        avg_price = order_p
    else:
        total_funds = 0.0
        avg_price = order_p

    rec = state.setdefault("_reconciled_asks", {}).get(order_uuid)
    rec_vol = 0.0
    rec_funds = 0.0
    if isinstance(rec, dict):
        rec_vol = float(rec.get("volume", 0.0) or 0.0)
        rec_funds = float(rec.get("funds", 0.0) or 0.0)
    elif isinstance(rec, (int, float)):
        rec_vol = float(rec or 0.0)
        rec_funds = 0.0

    db_res = _get_db_cumulative_trades(order_uuid, "ask", market)
    if db_res is None:
        return False
    db_vol, db_funds = db_res

    if rec_funds <= 0.0 and rec_vol > 0.0:
        if executed_volume > 0 and total_funds > 0:
            rec_funds = rec_vol * (total_funds / executed_volume)
        elif avg_price > 0:
            rec_funds = rec_vol * avg_price

    if db_funds <= 0.0 and db_vol > 0.0:
        if executed_volume > 0 and total_funds > 0:
            db_funds = db_vol * (total_funds / executed_volume)
        elif avg_price > 0:
            db_funds = db_vol * avg_price

    ledger_delta_vol = max(0.0, round(executed_volume - rec_vol, 8))
    ledger_delta_funds = max(0.0, round(total_funds - rec_funds, 4))
    if ledger_delta_vol > 1e-12 and ledger_delta_funds <= 0.0:
        ledger_delta_funds = round(ledger_delta_vol * avg_price, 4) if avg_price > 0 else 0.0

    db_delta_vol = max(0.0, round(executed_volume - db_vol, 8))
    db_delta_funds = max(0.0, round(total_funds - db_funds, 4))
    if db_delta_vol > 1e-12 and db_delta_funds <= 0.0:
        db_delta_funds = round(db_delta_vol * avg_price, 4) if avg_price > 0 else 0.0

    if ledger_delta_vol <= 1e-12 and db_delta_vol <= 1e-12:
        if not isinstance(state.setdefault("_reconciled_asks", {}).get(order_uuid), dict):
            state["_reconciled_asks"][order_uuid] = {
                "volume": executed_volume,
                "funds": total_funds
            }
        return True

    tot_pnl = 0.0
    remaining_tranches = None
    new_bot_qty = None
    new_bot_avg = None
    if ledger_delta_vol > 1e-12:
        rem_to_remove = ledger_delta_vol
        remaining_tranches = []
        sold_cost = 0.0
        for tr in state.get("tranches", []):
            tr_vol = float(tr.get("volume", 0.0))
            if rem_to_remove >= tr_vol - 1e-12:
                rem_to_remove -= tr_vol
                sold_cost += tr_vol * float(tr.get("buy_price", 0.0))
            elif rem_to_remove > 1e-12:
                tr_copy = dict(tr)
                tr_copy["volume"] = round(tr_vol - rem_to_remove, 8)
                sold_cost += rem_to_remove * float(tr.get("buy_price", 0.0))
                rem_to_remove = 0.0
                remaining_tranches.append(tr_copy)
            else:
                remaining_tranches.append(tr)

        if rem_to_remove > 1e-12:
            sold_cost += rem_to_remove * float(state.get("bot_avg_price", 0.0))
            rem_to_remove = 0.0

        cost_basis = sold_cost if sold_cost > 0 else (ledger_delta_vol * float(state.get("bot_avg_price", 0.0)))
        tot_pnl = ledger_delta_funds - cost_basis

        computed_qty = round(sum(float(tr.get("volume", 0.0)) for tr in remaining_tranches), 8)
        if not remaining_tranches and computed_qty <= 1e-8:
            old_qty = float(state.get("bot_quantity", 0.0))
            computed_qty = max(0.0, round(old_qty - ledger_delta_vol, 8))

        if computed_qty <= 1e-8:
            new_bot_qty = 0.0
            new_bot_avg = 0.0
            remaining_tranches = []
        else:
            new_bot_qty = computed_qty
            if remaining_tranches:
                new_cost = sum(float(tr.get("volume", 0.0)) * float(tr.get("buy_price", 0.0)) for tr in remaining_tranches)
                new_bot_avg = new_cost / new_bot_qty if new_bot_qty > 0 else 0.0
            else:
                new_bot_avg = float(state.get("bot_avg_price", 0.0))

    if db_delta_vol > 1e-12:
        trade_action = action
        if not trade_action:
            trade_action = "BASKET_TAKE_PROFIT_LIMIT" if order_state == "done" else "LIMIT_ASK_CANCEL_FILL"

        m_strat = get_market_strategy(market)
        strat_name = "PURE_MARTINGALE" if m_strat == "PURE_MARTINGALE" else "HYBRID_MARTINGALE_MAGIC_SPLIT"
        db_delta_price = round(db_delta_funds / db_delta_vol, 4) if db_delta_funds > 0 else avg_price

        try:
            log_real_trade(
                market=market,
                ticker=ticker,
                side="ask",
                action=trade_action,
                price=db_delta_price,
                volume=db_delta_vol,
                cost_or_revenue=db_delta_funds,
                pnl=tot_pnl,
                cycle=state.get("completed_cycles", 0),
                strategy=strat_name,
                order_uuid=order_uuid
            )
        except Exception as le:
            print(f"[{market}] [ERROR] 매도 체결 DB 기록 오류: {le}")
            return False

    if new_bot_qty is not None:
        state["bot_quantity"] = new_bot_qty
        state["bot_avg_price"] = new_bot_avg
        state["tranches"] = remaining_tranches

    state.setdefault("_reconciled_asks", {})[order_uuid] = {
        "volume": executed_volume,
        "funds": total_funds
    }
    if save_strategy_state(market, state) is False:
        return False
    return True


def _reconcile_cancelled_order_fill(upbit_client, market: str, state: dict, order: dict, executed_volume: float) -> bool:
    order_uuid = order.get("uuid")
    side = order.get("side")
    ticker = market.split("-")[1]
    fill_price = get_order_fill_price(order, executed_volume)
    funds = fill_price * executed_volume

    if side == "ask":
        order_st = order.get("state")
        action = "BASKET_TAKE_PROFIT_LIMIT" if order_st == "done" else "LIMIT_ASK_CANCEL_FILL"
        return _reconcile_ask_order_fill(upbit_client, market, state, order, executed_volume, action=action)

    elif side == "bid":
        recorded = state.setdefault("bear_limit_buys", {}).setdefault(order_uuid, {
            "units": 1,
            "state": "wait",
            "executed_volume": 0.0,
            "executed_funds": 0.0
        })
        delta_vol = max(0.0, round(executed_volume - recorded.get("executed_volume", 0.0), 8))
        delta_funds = max(0.0, round(funds - recorded.get("executed_funds", 0.0), 4))

        if delta_vol <= 1e-12:
            recorded["state"] = order.get("state", "cancel")
            return True

        db_res = _get_db_cumulative_trades(order_uuid, "bid", market)
        if db_res is None:
            return False
        db_vol, db_funds = db_res

        db_delta_vol = max(0.0, round(executed_volume - db_vol, 8))
        db_delta_funds = max(0.0, round(funds - db_funds, 4))
        if db_delta_vol > 1e-12:
            try:
                p = db_delta_funds / db_delta_vol if db_delta_funds > 0 else (delta_funds / delta_vol if delta_vol > 0 else fill_price)
                s_id = recorded.get("squad_id", 1)
                s_stage = recorded.get("squad_stage", 1)
                m_strat = get_market_strategy(market)
                strat_name = "PURE_MARTINGALE" if m_strat == "PURE_MARTINGALE" else "HYBRID_MARTINGALE_MAGIC_SPLIT"
                log_real_trade(
                    market=market,
                    ticker=ticker,
                    side="bid",
                    action=f"MARTINGALE_LIMIT_BUY_SQUAD_{s_id}_STAGE_{s_stage}",
                    price=p,
                    volume=db_delta_vol,
                    cost_or_revenue=db_delta_funds,
                    pnl=0.0,
                    cycle=state.get("completed_cycles", 0),
                    strategy=strat_name,
                    order_uuid=order_uuid
                )
            except Exception as le:
                print(f"[{market}] [ERROR] 취소 매수 주문 체결 DB 기록 오류: {le}")
                return False

        if delta_vol > 1e-12:
            old_qty = float(state.get("bot_quantity", 0.0))
            old_cost = old_qty * float(state.get("bot_avg_price", 0.0))
            state["bot_quantity"] = round(old_qty + delta_vol, 8)
            state["bot_avg_price"] = (old_cost + delta_funds) / state["bot_quantity"]
            tranche = next((tr for tr in state.get("tranches", [])
                            if tr.get("order_uuid") == order_uuid), None)
            if tranche is None:
                tranche = add_bot_tranche(
                    state, recorded.get("units", 1), delta_funds / delta_vol,
                    delta_vol, tranche_type="MARTINGALE",
                    squad_id=recorded.get("squad_id"),
                    squad_stage=recorded.get("squad_stage")
                )
                tranche["order_uuid"] = order_uuid
            else:
                cost = tranche["volume"] * tranche["buy_price"] + delta_funds
                tranche["volume"] = round(tranche["volume"] + delta_vol, 8)
                tranche["buy_price"] = cost / tranche["volume"]
            state["bear_buy_rebalance_pending"] = True

        recorded.update(
            state="cancel" if order.get("state") == "cancel" else order.get("state", "cancel"),
            executed_volume=executed_volume,
            executed_funds=funds
        )
        if save_strategy_state(market, state) is False:
            return False
        return True
    return False


def get_bot_owned_order_uuids(state: Optional[dict]) -> Optional[set]:
    """저장된 state에 기록된 봇 소유 주문 UUID 집합. state가 없으면 None(소유권 미확정)."""
    if not isinstance(state, dict):
        return None
    owned = set()
    basket_info = state.get("basket_limit_ask")
    if isinstance(basket_info, dict) and basket_info.get("uuid"):
        owned.add(basket_info["uuid"])
    bear_buys = state.get("bear_limit_buys")
    if isinstance(bear_buys, dict):
        owned.update(k for k in bear_buys.keys() if k)
    return owned


def partition_bot_open_orders(open_orders, state: Optional[dict]):
    """미체결 주문 목록에서 봇 소유 (매도, 매수) 주문만 분리한다.

    UUID 기반으로만 판별하며 가격·수량·방향으로 소유권을 추정하지 않는다.
    소유권을 확정할 수 없으면(state 없음, UUID 없는 항목 등) None을 반환한다.
    """
    owned = get_bot_owned_order_uuids(state)
    if owned is None or not isinstance(open_orders, list):
        return None
    sell_orders, buy_orders = [], []
    for o in open_orders:
        if not isinstance(o, dict) or not o.get("uuid"):
            return None
        if o["uuid"] not in owned:
            continue
        if o.get("side") == "ask":
            sell_orders.append(o)
        elif o.get("side") == "bid":
            buy_orders.append(o)
        else:
            return None
    return sell_orders, buy_orders


def cancel_order_and_wait(upbit_client, order_uuid: str, market: Optional[str] = None, state: Optional[dict] = None) -> bool:
    owned_uuids = get_bot_owned_order_uuids(state)
    if market is None or owned_uuids is None or not order_uuid or order_uuid not in owned_uuids:
        print(f"[ERROR] 주문 취소 거부: 봇 소유 UUID로 확인되지 않음 ({order_uuid})")
        return False
    try:
        response = upbit_client.cancel_order(order_uuid)
    except Exception as e:
        print(f"[ERROR] 주문 취소 제출 예외 ({order_uuid}): {e}")
        return False
    if not isinstance(response, dict) or not response.get("uuid"):
        print(f"[ERROR] 주문 취소 접수 실패 ({order_uuid}): {response}")
        return False
    for attempt in range(ORDER_SETTLEMENT_ATTEMPTS):
        try:
            order = upbit_client.get_order(order_uuid)
        except Exception as e:
            print(f"[ERROR] 취소 주문 상태 조회 예외 ({order_uuid}): {e}")
            order = None
        if isinstance(order, dict):
            if not order.get("uuid") or order.get("uuid") != order_uuid:
                continue
            order_state = order.get("state")
            if order_state in ("cancel", "done"):
                try:
                    executed_volume = float(order.get("executed_volume", 0.0) or 0.0)
                except (TypeError, ValueError):
                    executed_volume = 0.0

                if market is not None and state is not None and executed_volume > 0:
                    if not _reconcile_cancelled_order_fill(upbit_client, market, state, order, executed_volume):
                        return False

                # 바스켓 매도 주문 취소/완료 확인 시 활성 주문에서 즉시 제거 및 체크포인트 영속화
                if (market is not None and state is not None and
                        isinstance(state.get("basket_limit_ask"), dict) and
                        state["basket_limit_ask"].get("uuid") == order_uuid):
                    if order_state == "done":
                        rec_dict = state.setdefault("_reconciled_asks", {}).get(order_uuid)
                        is_counted = isinstance(rec_dict, dict) and rec_dict.get("cycle_counted")
                        if not is_counted:
                            state["completed_cycles"] = state.get("completed_cycles", 0) + 1
                            if isinstance(rec_dict, dict):
                                rec_dict["cycle_counted"] = True
                    else:
                        rec_entry = state.setdefault("_reconciled_asks", {}).get(order_uuid)
                        if not isinstance(rec_entry, dict):
                            state["_reconciled_asks"][order_uuid] = {
                                "volume": 0.0,
                                "funds": 0.0
                            }
                    state.pop("basket_limit_ask", None)
                    if save_strategy_state(market, state) is False:
                        return False

                if order_state == "cancel":
                    return True
                if order_state == "done":
                    return True
        if attempt < ORDER_SETTLEMENT_ATTEMPTS - 1:
            time.sleep(ORDER_SETTLEMENT_DELAY_SEC)
    print(f"[ERROR] 주문 취소 미확정 ({order_uuid})")
    return False


def halt_for_order_reconciliation(market: str, state: dict, order_result: dict):
    state["trading_halted"] = True
    state["halt_reason"] = order_result.get("reason", "주문 체결 상태 미확정")
    state["pending_order_uuid"] = order_result.get("uuid")
    saved = save_strategy_state(market, state)
    state["_halt_persisted"] = (saved is not False)
    send_telegram_alert(
        f"⚠️ <b>[거래 중단]</b> {market}\n"
        f"• 사유: {state['halt_reason']}\n"
        f"• 주문 UUID: {state.get('pending_order_uuid') or '없음'}\n"
        f"• 거래소 체결·잔고를 확인한 뒤 상태를 재조정하세요."
    )


def cancel_all_orders(upbit_client, market: str, state: Optional[dict] = None) -> bool:
    if state is None:
        print(f"[{market}] [ERROR] 주문 취소 거부: state 없음(소유 UUID 미확정)")
        return False
    try:
        open_orders = upbit_client.get_order(market, state="wait")
        partitioned = partition_bot_open_orders(open_orders, state)
        if partitioned is None:
            return False
        sell_orders, buy_orders = partitioned
        orders_to_cancel = sell_orders + buy_orders
        for order in orders_to_cancel:
            if not cancel_order_and_wait(upbit_client, order['uuid'], market=market, state=state):
                return False
        if orders_to_cancel:
            print(f"[{time.strftime('%H:%M:%S')}] [{market}] [ACTION] 봇 미체결 주문 {len(orders_to_cancel)}건 전체 취소 완료.")
        return True
    except Exception as e:
        print(f"[{time.strftime('%H:%M:%S')}] [{market}] [ERROR] 미체결 주문 전체 취소 중 오류: {e}")
        return False


def reconcile_and_settle_engine_transition_orders(upbit_client, market: str, state: dict) -> bool:
    """전략 엔진 전환 시 미체결/체결 중인 주문을 직접 조회하고 체결 차액을 정산한 후 안전하게 취소한다.

    1) 미반영 매수 주문 체결 정산 (reconcile_bear_limit_buys)
    2) 추적 중인 바스켓 매도 주문 상태 조회 및 정산
       - done: 포지션 전량/부분 차감, 실현손익 및 trade 기록, cycle 증가, basket_limit_ask 제거
       - wait/cancel (executed_volume > 0): 부분체결 차액 정산
    3) 잔여 봇 소유 미체결 주문 취소 (cancel_all_orders)
    4) 취소 중 발생한 추가 체결분 재정산 (reconcile_bear_limit_buys 및 basket ask 재확인)
    5) wait 상태로 남은 bear_limit_buys 주문의 거래소 상태 재확인 (cancel/done 확인 시 갱신)
    6) 상태 저장 성공 여부 검증
    """
    ticker = market.split("-")[1]

    # 1. 전환 전 미반영 매수 체결 선행 정산
    if not reconcile_bear_limit_buys(upbit_client, market, state):
        return False

    # 2. 바스켓 매도 주문 상태 확인 및 선행 정산
    ask_info = state.get("basket_limit_ask")
    if isinstance(ask_info, dict) and ask_info.get("uuid"):
        ask_uuid = ask_info["uuid"]
        try:
            order = upbit_client.get_order(ask_uuid)
            if (not isinstance(order, dict) or order.get("uuid") != ask_uuid
                    or order.get("state") not in ("wait", "done", "cancel")):
                return False
        except Exception as e:
            print(f"[{market}] [ERROR] 엔진 전환 중 매도 주문 조회 실패 ({ask_uuid}): {e}")
            return False

        order_state = order.get("state")
        try:
            executed_vol = float(order.get("executed_volume", 0.0) or 0.0)
        except (TypeError, ValueError):
            executed_vol = 0.0

        if order_state == "done" and executed_vol <= 0:
            return False

        if executed_vol > 0:
            action = "BASKET_TAKE_PROFIT_LIMIT" if order_state == "done" else "LIMIT_ASK_CANCEL_FILL"
            if not _reconcile_ask_order_fill(upbit_client, market, state, order, executed_vol, action=action):
                return False

        if order_state == "done":
            rec_dict = state.setdefault("_reconciled_asks", {}).get(ask_uuid)
            is_counted = isinstance(rec_dict, dict) and rec_dict.get("cycle_counted")
            if not is_counted and state.get("basket_limit_ask") and state["basket_limit_ask"].get("uuid") == ask_uuid:
                state["completed_cycles"] = state.get("completed_cycles", 0) + 1
                if isinstance(rec_dict, dict):
                    rec_dict["cycle_counted"] = True
            state.pop("basket_limit_ask", None)
            if save_strategy_state(market, state) is False:
                return False
        elif order_state == "cancel":
            state.pop("basket_limit_ask", None)
            if save_strategy_state(market, state) is False:
                return False

    # 3. 봇 소유 미체결 주문 전체 취소
    if not cancel_all_orders(upbit_client, market, state=state):
        return False

    # 4. 취소 과정에서 발생한 매수 추가 체결 정산
    if not reconcile_bear_limit_buys(upbit_client, market, state):
        return False

    # 5. 매도 주문 잔여 확인 및 정리
    ask_info_after = state.get("basket_limit_ask")
    if isinstance(ask_info_after, dict) and ask_info_after.get("uuid"):
        ask_uuid = ask_info_after["uuid"]
        try:
            order = upbit_client.get_order(ask_uuid)
            if not isinstance(order, dict) or order.get("uuid") != ask_uuid:
                return False
            order_st = order.get("state")
            if order_st not in ("cancel", "done"):
                return False
            exec_vol = float(order.get("executed_volume", 0.0) or 0.0)
            if exec_vol > 0:
                action = "BASKET_TAKE_PROFIT_LIMIT" if order_st == "done" else "LIMIT_ASK_CANCEL_FILL"
                if not _reconcile_ask_order_fill(upbit_client, market, state, order, exec_vol, action=action):
                    return False
            if order_st == "done":
                rec_dict = state.setdefault("_reconciled_asks", {}).get(ask_uuid)
                is_counted = isinstance(rec_dict, dict) and rec_dict.get("cycle_counted")
                if not is_counted and state.get("basket_limit_ask") and state["basket_limit_ask"].get("uuid") == ask_uuid:
                    state["completed_cycles"] = state.get("completed_cycles", 0) + 1
                    if isinstance(rec_dict, dict):
                        rec_dict["cycle_counted"] = True
            state.pop("basket_limit_ask", None)
        except Exception as e:
            print(f"[{market}] [ERROR] 취소 후 매도 주문 재확인 실패 ({ask_uuid}): {e}")
            return False

    # 6. 여전히 wait로 남아있는 bear_limit_buys가 있다면 거래소 상태 확인
    for b_uuid, b_rec in list(state.get("bear_limit_buys", {}).items()):
        if isinstance(b_rec, dict) and b_rec.get("state") == "wait":
            try:
                b_order = upbit_client.get_order(b_uuid)
                if not isinstance(b_order, dict) or b_order.get("uuid") != b_uuid:
                    return False
                b_st = b_order.get("state")
                if b_st not in ("cancel", "done"):
                    return False
                b_exec = float(b_order.get("executed_volume", 0.0) or 0.0)
                if b_exec > float(b_rec.get("executed_volume", 0.0) or 0.0):
                    if not _reconcile_cancelled_order_fill(upbit_client, market, state, b_order, b_exec):
                        return False
                b_rec["state"] = b_st
            except Exception as e:
                print(f"[{market}] [ERROR] 취소 후 매수 주문 상태 확인 실패 ({b_uuid}): {e}")
                return False

    if save_strategy_state(market, state) is False:
        return False

    return True


def get_safe_sell_quantity(upbit_client, market: str, ticker: str, desired_qty: float, state: dict) -> float:
    """
    안전 매도 가능 수량을 계산합니다.
    매도 수량은 요청량(desired_qty), 봇 장부량(ledger_qty), 가용량(avail),
    max(0, avail + locked - protected_qty)의 최솟값으로 엄격히 제한합니다.
    조회 실패 및 비정상 응답(None, 음수, NaN, 무한대)은 fail-closed(-1.0 반환 및 balance_query_failed 플래그)를 유지합니다.
    """
    avail = None
    locked = None
    try:
        bal_info = None
        try:
            bal_info = upbit_client.get_balance(ticker, verbose=True)
        except TypeError:
            bal_info = upbit_client.get_balance(ticker)

        if isinstance(bal_info, dict):
            raw_avail = bal_info.get("balance")
            raw_locked = bal_info.get("locked", 0.0)
            if raw_avail is None or isinstance(raw_avail, bool) or isinstance(raw_locked, bool):
                state["balance_query_failed"] = True
                return -1.0
            avail = float(raw_avail)
            locked = float(raw_locked or 0.0)
        elif isinstance(bal_info, (int, float, str)) and not isinstance(bal_info, bool):
            avail = float(bal_info)
            locked = 0.0
        elif bal_info is None:
            state["balance_query_failed"] = True
            return -1.0
        else:
            state["balance_query_failed"] = True
            return -1.0
    except Exception:
        state["balance_query_failed"] = True
        return -1.0

    if (avail is None or locked is None or
            not math.isfinite(avail) or not math.isfinite(locked) or
            avail < 0.0 or locked < 0.0):
        state["balance_query_failed"] = True
        return -1.0

    state.pop("balance_query_failed", None)

    protected_qty = float(PROTECTED_BALANCES.get(market, 0.0) or 0.0)
    if not math.isfinite(protected_qty) or protected_qty < 0.0:
        protected_qty = 0.0

    ledger_qty = float(state.get("bot_quantity", 0.0) or 0.0)
    if not math.isfinite(ledger_qty) or ledger_qty < 0.0:
        ledger_qty = 0.0

    req_qty = float(desired_qty or 0.0)
    if not math.isfinite(req_qty) or req_qty < 0.0:
        req_qty = 0.0

    d_zero = Decimal("0")
    d_avail = Decimal(str(avail))
    d_locked = Decimal(str(locked))
    d_prot = Decimal(str(protected_qty))
    d_total = d_avail + d_locked
    d_max_unprotected = max(d_zero, d_total - d_prot)
    d_ledger = Decimal(str(ledger_qty))
    d_req = Decimal(str(req_qty))

    # 매도 수량은 요청량, 봇 장부량, 가용량, max(0, balance + locked - protected_qty)의 최솟값
    # Decimal 계산 및 내림(ROUND_DOWN)으로 4가지 상한선을 절대 초과하지 않도록 엄격히 보장
    safe_d = max(d_zero, min(d_req, d_ledger, d_avail, d_max_unprotected))
    return float(safe_d.quantize(Decimal("1e-8"), rounding=ROUND_DOWN))


def acquire_market_process_lock(market: str):
    lock_path = os.path.join(tempfile.gettempdir(), f"autotrade_{market}.lock")
    lock_file = open(lock_path, "w")
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_file.close()
        return None
    return lock_file


# =============================================================================
# 실전 하이브리드 매매 로직 (상승장 추세 + 하락장 마틴게일 매직스플릿 이중익절)
# =============================================================================
def run_trading_strategy(market: str = "KRW-SOL"):
    """
    하이브리드 국면전환 실전 자동매매 메인 루프:
    - 상승장(BULL, 200 MA 상회): 5/20 MA 추세추종 & 트레일링 스탑
    - 하락장(BEAR, 200 MA 하회): 마틴게일 배수 진입 + 하이브리드 매직스플릿 이중익절 (개별 +3% OR 바스켓 익절)
    """
    market_process_lock = acquire_market_process_lock(market)
    if market_process_lock is None:
        print(f"[{market}] [CRITICAL] 동일 마켓 봇이 이미 실행 중입니다. 중복 주문 방지를 위해 이번 실행을 중단합니다.")
        return

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
    if is_adaptive_grid_enabled(market):
        print(f" 전략 엔진: 🟢 ACTIVE (Adaptive Grid C-E: NATR x Step Scale 1.25 + Dynamic TP)")
    elif is_adaptive_grid_shadow_enabled(market):
        print(f" 전략 엔진: 🟡 LEGACY [Adaptive C-E: SHADOW 관측중 (실주문 미반영)]")
    else:
        print(f" 전략 엔진: ⚪ LEGACY")
    print(f" 상승 국면 (BULL): 5/20 MA 추세추종 + 트레일링 스탑 (+10% 도달 시 -3% 익절)")
    print(f" 하락 국면 (BEAR): 마틴게일 배수 진입 + 매직스플릿 이중익절 (개별 +3.0% OR 바스켓 +{(sell_profit_margin - 1) * 100:.2f}%)")
    print(f" 손절 기준: {STOP_LOSS_PERCENT * 100:.2f}% (Stop-Loss)")
    print(f" 예수금 알림 기준: {MIN_KRW_ALERT_THRESHOLD:,}원 미만 시 텔레그램 발송")
    print("=" * 65)

    check_krw_balance_alert(upbit, context=f"{market} 실전 봇 시작")

    send_telegram_alert(f"🚀 <b>[자동매매 가동]</b> {market} (1 Unit: {unit_krw:,}원)")

    # 전략 엔진 전환(Legacy <-> Adaptive C-E) 감지 시 기존 미체결 주문 안전 재배치
    current_engine = "ADAPTIVE_GRID_C_E" if is_adaptive_grid_enabled(market) else "LEGACY"
    engine_marker_missing = state.get("strategy_engine") is None
    prev_engine = state.get("strategy_engine") or "LEGACY"
    if prev_engine != current_engine:
        print(f"\n[{market}] 🔄 [전략 엔진 전환 감지] {prev_engine} ➔ {current_engine}")
        print(f"[{market}]   - 새 엔진 기준 호가 재배치를 위해 기존 주문을 정산 및 안전하게 취소합니다.")
        if reconcile_and_settle_engine_transition_orders(upbit, market, state):
            state["bear_buy_rebalance_pending"] = False
            state["strategy_engine"] = current_engine
            if save_strategy_state(market, state) is False:
                if engine_marker_missing:
                    state.pop("strategy_engine", None)
                else:
                    state["strategy_engine"] = prev_engine
                print(f"[{market}] [ERROR] 전략 엔진 전환 마커 저장 실패 ({current_engine}); 신규 주문을 중단합니다.")
                halt_for_order_reconciliation(market, state, {"reason": f"전략 엔진 전환 마커 저장 실패 ({current_engine})"})
            else:
                send_telegram_alert(
                    f"🔄 <b>[전략 엔진 전환]</b> {market}\n"
                    f"• 이전: {prev_engine} ➔ 현재: <b>{current_engine}</b>\n"
                    f"• 기존 주문 정산 및 취소 완료 후 새 호가로 자동 재등록합니다."
                )
        else:
            print(f"[{market}] [ERROR] 전략 엔진 전환 주문 정산/취소 미확정; 신규 주문을 중단합니다.")
            halt_for_order_reconciliation(market, state, {"reason": "전략 엔진 전환 주문 정산 및 취소 미확정"})
    else:
        state["strategy_engine"] = current_engine
        if engine_marker_missing and save_strategy_state(market, state) is False:
            state.pop("strategy_engine", None)
            halt_for_order_reconciliation(market, state, {
                "reason": f"전략 엔진 최초 마커 저장 실패 ({current_engine})"
            })

    last_heartbeat_time = 0

    while True:
        try:
            try:
                recovery = load_trailing_exit_recovery(market)
                if recovery is not None:
                    if not reconcile_trailing_exit(upbit, market, state, recovery):
                        print(f"[{market}] [CRITICAL] trailing 청산 recovery 미확정; 자동 주문 중단")
                    time.sleep(10)
                    continue
            except (OSError, ValueError, TypeError, KeyError, sqlite3.Error) as error:
                print(f"[{market}] [CRITICAL] trailing 청산 recovery 실패: {error}; 자동 주문 중단")
                time.sleep(10)
                continue
            if state.get("trading_halted", False):
                if not state.get("_halt_persisted", True):
                    if save_strategy_state(market, state) is not False:
                        state["_halt_persisted"] = True
                    print(f"[{market}] [CRITICAL] 주문 체결 상태 미확정으로 거래 중단 (디스크 상태 저장 미확정): {state.get('halt_reason', '')}")
                    time.sleep(30)
                    continue
                fresh_state = load_strategy_state(market)
                if fresh_state and not fresh_state.get("trading_halted", False):
                    state = fresh_state
                    print(f"[{market}] 🔄 [원장 재조정 확인] 상태 정상화 감지. 자동매매를 재개합니다.")
                else:
                    print(f"[{market}] [CRITICAL] 주문 체결 상태 미확정으로 거래 중단: {state.get('halt_reason', '')}")
                    time.sleep(30)
                    continue


            # 1. 예수금 체크
            check_krw_balance_alert(upbit, context=f"{market} 실시간 감시")

            # 2. 실시간 시세 및 국면 판별
            current_price = safe_get_current_price(market)
            if current_price is None or current_price <= 0:
                print(f"[{time.strftime('%H:%M:%S')}] [{ticker}] [WARN] 현재가 조회 실패 (API 응답 지연/제한). 5초 후 재시도.")
                time.sleep(5)
                continue

            market_strat = get_market_strategy(market)
            is_exit_only = (market_strat == "EXIT_ONLY" or state.get("exit_only", False))
            sell_profit_margin = get_profit_margin(market)
            regime_info = get_hybrid_regime_and_signals(market)
            confirmed_is_bull = regime_info.get("is_bull")
            candles_valid = bool(regime_info.get("candles_valid", False))
            prev_regime = state.get("current_regime", "BEAR")
            if is_exit_only:
                # EXIT_ONLY 모드: 추가 매수 전면 차단, 오직 기존 보유 물량 익절만 대기 (포켓 이전용)
                if state.get("current_regime") == "BULL":
                    state["current_regime"] = "BEAR"
                    state["active_mode"] = "MARTINGALE_MAGIC_SPLIT"
                    state["trailing_stop_active"] = False
                    save_strategy_state(market, state)
                is_bull = False
                signal = "HOLD"
                regime_str = "익절 정리 대기 (EXIT_ONLY)"
                curr_regime = "BEAR"
                prev_regime = "BEAR"
            elif market_strat == "PURE_MARTINGALE":
                # PURE_MARTINGALE 모드에서는 상승장(BULL) 경로 및 손절/국면전환 청산을 전면 배제합니다.
                # 만약 이전 저장 상태가 BULL이었다면, 즉시 BEAR 마틴게일 바스켓으로 무손절 안전 인계 마이그레이션 실행
                if state.get("current_regime") == "BULL":
                    print(f"[{market}] 🔄 [순수 마틴게일 전환] 이전 BULL 상태 감지 ➔ BEAR 마틴게일 바스켓으로 안전 인계 마이그레이션 실행")
                    state["current_regime"] = "BEAR"
                    state["active_mode"] = "MARTINGALE_MAGIC_SPLIT"
                    state["trailing_stop_active"] = False
                    state["trend_peak_price"] = 0.0
                    bot_q = float(state.get("bot_quantity", 0.0))
                    bot_a = float(state.get("bot_avg_price", 0.0))
                    if bot_q > 0 and not state.get("tranches"):
                        state["tranches"] = [{
                            "step": 0,
                            "units": 1,
                            "buy_price": bot_a,
                            "volume": bot_q,
                            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                            "source": "MIGRATION_FROM_BULL"
                        }]
                    save_strategy_state(market, state)

                is_bull = False
                signal = "HOLD"
                regime_str = "순수 마틴게일"
                curr_regime = "BEAR"
                prev_regime = "BEAR"
            elif confirmed_is_bull is None:
                is_bull = prev_regime == "BULL"
                signal = "HOLD"
                regime_str = regime_info.get("regime_korean", "국면 전환 확인 대기")
                curr_regime = prev_regime
            else:
                is_bull = confirmed_is_bull
                signal = regime_info.get("signal", "HOLD")
                regime_str = regime_info.get("regime_korean", "국면 분석 중")
                curr_regime = "BULL" if is_bull else "BEAR"
            curr_ma5 = float(regime_info.get("ma5", 0.0))
            curr_ma20 = float(regime_info.get("ma20", 0.0))
            curr_ma200 = float(regime_info.get("ma200", 0.0))

            # 국면 전환 감지 (BULL ➔ BEAR 또는 BEAR ➔ BULL)
            if market_strat != "PURE_MARTINGALE" and prev_regime == "BULL" and curr_regime == "BEAR":
                # 🛡️ [연쇄 손절 방지] 쿨다운 중이면 부분손절을 반복 실행하지 않음
                # 200 MA 경계에서 BULL↔BEAR 진동 시 50%→25%→12.5%... 연쇄 매도 원천 차단
                existing_cooldown = float(state.get("stop_loss_cooldown_until", 0.0))
                if time.time() < existing_cooldown:
                    rem_min = int((existing_cooldown - time.time()) / 60)
                    print(f"[{market}] 🛡️ [연쇄 손절 방지] 국면전환 쿨다운 중 ({rem_min}분 남음). 부분손절 생략, BEAR 모드 유지.")
                    state["current_regime"] = "BEAR"
                    state["active_mode"] = "MARTINGALE_MAGIC_SPLIT"
                    save_strategy_state(market, state)
                    time.sleep(5)
                    continue

                print(f"\n[{market}] ⚠️ [국면 전환 감지] 200 MA 하향 이탈: 상승장(BULL) ➔ 하락장(BEAR)")
                if not cancel_all_orders(upbit, market, state=state):
                    halt_for_order_reconciliation(market, state, {"reason": "국면 전환 전 주문 취소 미확정"})
                    continue

                avg_price, quantity = get_bot_balance(upbit, market, ticker, state)
                if state.pop("balance_query_failed", False):
                    halt_for_order_reconciliation(market, state, {"reason": "국면 전환 중 잔고 조회 미확정"})
                    continue
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

                    sell_qty = get_safe_sell_quantity(upbit, market, ticker, sell_qty, state)
                    if sell_qty < 0 or state.pop("balance_query_failed", False):
                        halt_for_order_reconciliation(market, state, {"reason": "국면 전환 중 잔고 조회 실패"})
                        continue

                    if sell_qty > 0 and (sell_qty * current_price) >= MIN_ORDER_KRW:
                        action_name = f"REGIME_SWITCH_PARTIAL_CUT_{int(liq_pct * 100)}PCT"
                        sell_result = submit_market_order_and_wait(upbit, market, "ask", sell_qty)
                        if sell_result.get("status") != "FILLED":
                            halt_for_order_reconciliation(market, state, sell_result)
                            continue

                        filled_volume = float(sell_result["volume"])
                        filled_price = float(sell_result["price"])
                        pnl_krw = (filled_price - avg_price) * filled_volume
                        pnl_rate = (filled_price - avg_price) / avg_price

                        log_real_trade(
                            market=market,
                            ticker=ticker,
                            side="ask",
                            action=action_name,
                            price=filled_price,
                            volume=filled_volume,
                            cost_or_revenue=filled_price * filled_volume,
                            pnl=pnl_krw,
                            strategy="HYBRID_REGIME_SWITCH",
                            order_uuid=sell_result["uuid"]
                        )

                        actual_avail = float(upbit.get_balance(ticker) or 0.0)
                        prot_qty = float(PROTECTED_BALANCES.get(market, 0.0))
                        rem_qty = max(0.0, actual_avail - prot_qty)

                        msg = (
                            f"🔴 <b>[손절 체결]</b> {market} (하락장 전환 {int(liq_pct * 100)}% 부분손절)\n"
                            f"• 체결단가: {filled_price:,.0f}원 (평단가: {avg_price:,.0f}원)\n"
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

                # 국면전환 휩쏘 방지: 부분손절 후 재진입 쿨다운 설정
                regime_cooldown_sec = REGIME_SWITCH_COOLDOWN_HOURS * 3600
                regime_cooldown_until_ts = time.time() + regime_cooldown_sec
                regime_cooldown_str = datetime.fromtimestamp(regime_cooldown_until_ts).strftime("%H:%M:%S")
                state["stop_loss_cooldown_until"] = regime_cooldown_until_ts
                print(f"[{market}] 🛡️ [휩쏘 방지] 국면전환 부분손절 완료. {REGIME_SWITCH_COOLDOWN_HOURS}시간({regime_cooldown_str}까지) 신규 진입 쿨다운 가동.")

                cooldown_msg = (
                    f"🛡️ <b>[재진입 차단]</b> {market} 국면전환 부분손절 후 "
                    f"<b>{REGIME_SWITCH_COOLDOWN_HOURS}시간({regime_cooldown_str}까지) 신규 진입을 전면 차단</b>합니다.\n"
                    f"• 200 MA 경계선 휩쏘(매도→즉시 재매수) 방지"
                )
                send_telegram_alert(cooldown_msg)

                save_strategy_state(market, state)
                time.sleep(2)
                continue

            elif prev_regime == "BEAR" and curr_regime == "BULL":
                # 🛡️ [국면 진동 방지] 쿨다운 중에는 BULL 전환을 무시하여 BEAR 유지
                # 200 MA 경계 진동 시 BEAR→BULL→BEAR→BULL... 반복으로 인한 연쇄 손절 원천 차단
                existing_cooldown = float(state.get("stop_loss_cooldown_until", 0.0))
                if time.time() < existing_cooldown:
                    rem_min = int((existing_cooldown - time.time()) / 60)
                    print(f"[{market}] 🛡️ [국면 진동 방지] 쿨다운 중 ({rem_min}분 남음). BULL 전환 무시, BEAR 모드 유지.")
                    state["current_regime"] = "BEAR"
                    save_strategy_state(market, state)
                    time.sleep(5)
                    continue

                print(f"\n[{market}] 🐂 [국면 전환 감지] 200 MA 상향 돌파: 하락장(BEAR) ➔ 상승장(BULL)")
                if not cancel_all_orders(upbit, market, state=state):
                    halt_for_order_reconciliation(market, state, {"reason": "국면 전환 전 주문 취소 미확정"})
                    continue

                avg_price, quantity = get_bot_balance(upbit, market, ticker, state)
                if state.pop("balance_query_failed", False):
                    halt_for_order_reconciliation(market, state, {"reason": "국면 전환 중 잔고 조회 미확정"})
                    continue
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
                state["need_fresh_golden_cross"] = False
                save_strategy_state(market, state)
                time.sleep(2)
                continue

            state["current_regime"] = curr_regime


            # 3. 봇 전용 잔고 조회 (기존 자산 완전 격리/보호)
            avg_price, quantity = get_bot_balance(upbit, market, ticker, state)
            if state.pop("balance_query_failed", False):
                halt_for_order_reconciliation(market, state, {"reason": "거래 전 잔고 조회 미확정"})
                continue
            total_value = quantity * current_price

            # 4. 리스크 관리: Stop-Loss (긴급 손절) 감지
            # 하락장(BEAR) 및 PURE_MARTINGALE 국면은 매직스플릿 방어/바스켓 순환 모듈을 적용하므로 손절 제외
            # 상승장(BULL) 국면은 평단가 대비 BULL_STOP_LOSS_PCT(-10.0%) 도달 시에만 긴급 손절 (잔파동 털림 방지)
            if is_bull and market_strat != "PURE_MARTINGALE" and total_value >= MIN_ORDER_KRW and avg_price > 0:
                pnl_rate = (current_price - avg_price) / avg_price
                if pnl_rate <= BULL_STOP_LOSS_PCT:
                    cooldown_sec = STOP_LOSS_COOLDOWN_HOURS * 3600
                    cooldown_until_ts = time.time() + cooldown_sec
                    cooldown_str = datetime.fromtimestamp(cooldown_until_ts).strftime("%H:%M:%S")

                    msg = (
                        f"🔴 <b>[손절 체결]</b> {market} (상승장 -10% 긴급손절)\n"
                        f"• 평단가: {avg_price:,.0f}원\n"
                        f"🛡️ <b>[재진입 차단]</b> 추가 급락 및 뇌동매매 방지를 위해 <b>{STOP_LOSS_COOLDOWN_HOURS}시간({cooldown_str}까지) 신규 진입을 전면 차단</b>합니다."
                    )

                    if not cancel_all_orders(upbit, market, state=state):
                        halt_for_order_reconciliation(market, state, {"reason": "손절 전 주문 취소 미확정"})
                        continue
                    avg_price, quantity = get_bot_balance(upbit, market, ticker, state)
                    sell_qty = get_safe_sell_quantity(upbit, market, ticker, quantity, state)
                    if sell_qty < 0 or state.pop("balance_query_failed", False):
                        halt_for_order_reconciliation(market, state, {"reason": "손절 전 잔고 조회 미확정"})
                        continue
                    if sell_qty <= 0 or (sell_qty * current_price) < MIN_ORDER_KRW:
                        print(f"[{market}] 손절 대상 수량 부족 또는 최소 주문금액 미달 (safe_qty={sell_qty})")
                        continue
                    recovery = {
                        "kind": "STOP_LOSS_EXIT",
                        "market": market,
                        "uuid": None,
                        "volume": sell_qty,
                        "avg_price": avg_price,
                        "cycles": state.get("completed_cycles", 0),
                        "cooldown_until": cooldown_until_ts,
                    }
                    save_trailing_exit_recovery(market, recovery)

                    def checkpoint_stop_loss_uuid(order_uuid: str) -> None:
                        recovery["uuid"] = order_uuid
                        save_trailing_exit_recovery(market, recovery)

                    sell_result = submit_market_order_and_wait(
                        upbit, market, "ask", sell_qty, on_accepted=checkpoint_stop_loss_uuid
                    )
                    if sell_result.get("status") != "FILLED":
                        print(f"[{market}] [CRITICAL] 손절 주문 미확정; recovery 유지")
                        continue

                    filled_volume = float(sell_result["volume"])
                    filled_price = float(sell_result["price"])
                    loss_krw = (filled_price - avg_price) * filled_volume
                    pnl_rate = (filled_price - avg_price) / avg_price
                    msg = (
                        f"🔴 <b>[손절 체결]</b> {market} (상승장 -10% 긴급손절)\n"
                        f"• 체결가: {filled_price:,.0f}원 (평단가: {avg_price:,.0f}원)\n"
                        f"• 손실률: {pnl_rate * 100:.2f}%\n"
                        f"• 실현손익: <b>{loss_krw:+,.0f}원</b>\n"
                        f"🛡️ <b>[재진입 차단]</b> 추가 급락 및 뇌동매매 방지를 위해 <b>{STOP_LOSS_COOLDOWN_HOURS}시간({cooldown_str}까지) 신규 진입을 전면 차단</b>합니다."
                    )

                    print(f"\n[{market}] {msg}")
                    send_telegram_alert(msg)

                    if not reconcile_trailing_exit(upbit, market, state, recovery, sell_result):
                        print(f"[{market}] [CRITICAL] 손절 최종 저장 실패; recovery 유지")
                        continue

                    print(f"[{market}] [INFO] 손절 완료. {STOP_LOSS_COOLDOWN_HOURS}시간({cooldown_str}까지) 신규 진입 쿨다운 가동.")
                    time.sleep(300)
                    continue

            # 5. 미체결 주문 목록 조회
            open_orders = upbit.get_order(market, state="wait")
            if open_orders is None or not isinstance(open_orders, list):
                print(f"[{time.strftime('%H:%M:%S')}] [{ticker}] [WARN] 미체결 주문 조회 실패/지연 ({type(open_orders).__name__}). 5초 후 재시도.")
                time.sleep(5)
                continue

            # 봇 소유 UUID(state)로만 매도/매수 대기 주문을 분리한다. 수동 주문은 제외.
            partitioned_orders = partition_bot_open_orders(open_orders, state)
            if partitioned_orders is None:
                print(f"[{time.strftime('%H:%M:%S')}] [{ticker}] [WARN] 미체결 주문 소유권 미확정(UUID 누락 등). 신규 주문 보류, 5초 후 재시도.")
                time.sleep(5)
                continue
            sell_orders, buy_orders = partitioned_orders
            num_sell, num_buy = len(sell_orders), len(buy_orders)

            # 6. 하트비트 로깅 (30분 주기)
            now_sec = time.time()
            if now_sec - last_heartbeat_time >= 1800:
                krw_bal = get_krw_balance(upbit)
                mode_label = "상승 추세모드" if is_bull else ("익절 정리 모드 (EXIT_ONLY)" if is_exit_only else ("순수 마틴게일 순환모드" if market_strat == "PURE_MARTINGALE" else "하락 마틴-매직스플릿 방어모드"))
                prot_q = PROTECTED_BALANCES.get(market, 0.0)
                print(f"[{time.strftime('%H:%M:%S')}] [HEARTBEAT] {ticker}: {current_price:,.0f}원 | [{regime_str} / {mode_label}] | 예수금: {krw_bal:,.0f}원 (미체결: 매도 {num_sell}, 매수 {num_buy} | 봇 보유: {quantity:.6f} | 보호 자산: {prot_q:.4f})")
                last_heartbeat_time = now_sec

                if is_adaptive_grid_shadow_enabled(market) and not is_adaptive_grid_enabled(market):
                    try:
                        cur_step = len(state.get("tranches", []))
                        leg_buy_p = buy_orders[0].get("price", 0.0) if buy_orders else (avg_price * (1.0 - get_magic_split_down_pct(market)) if avg_price > 0 else current_price * (1.0 - get_magic_split_down_pct(market)))
                        leg_tp_p = sell_orders[0].get("price", 0.0) if sell_orders else (avg_price * sell_profit_margin if avg_price > 0 else 0.0)
                        shadow_log = format_shadow_mode_log(
                            market=market,
                            current_price=current_price,
                            avg_price=avg_price,
                            current_step=cur_step,
                            legacy_buy_price=float(leg_buy_p),
                            legacy_tp_price=float(leg_tp_p)
                        )
                        print(shadow_log)
                    except Exception as s_err:
                        print(f"[{market}] [WARN] 섀도우 모드 로그 포맷 오류: {s_err}")

            # =========================================================================
            # [분기 A] 하락 국면 (BEAR): 마틴게일 배수 진입 + 하이브리드 매직스플릿 이중익절
            # =========================================================================
            if not is_bull:
                state["active_mode"] = "MARTINGALE_MAGIC_SPLIT"

                # Reconcile even when a partial bid still counts as an open order.
                if state.get("bear_limit_buys"):
                    if not reconcile_bear_limit_buys(upbit, market, state, notify=True):
                        continue
                    avg_price, quantity = get_bot_balance(upbit, market, ticker, state)
                    if state.pop("balance_query_failed", False):
                        halt_for_order_reconciliation(market, state, {"reason": "지정가 매수 체결 후 잔고 조회 미확정"})
                        continue
                    total_value = quantity * current_price

                # A-1. 매직스플릿 이중익절(Dual Exit) 검사: 포지션 보유 중일 때
                if total_value >= MIN_ORDER_KRW and avg_price > 0:
                    if is_adaptive_grid_enabled(market):
                        from strategy.adaptive_grid import get_dynamic_tp_margins
                        dyn_basket_margin, dyn_tranche_margin = get_dynamic_tp_margins(is_bull=is_bull)
                        active_exit_margin = dyn_basket_margin
                        active_tranche_margin = dyn_tranche_margin - 1.0
                    else:
                        active_exit_margin = sell_profit_margin
                        active_tranche_margin = get_magic_split_tranche_profit(market)

                    exit_decision = check_magic_split_exits(
                        tranches=state.get("tranches", []),
                        current_price=current_price,
                        avg_buy_price=avg_price,
                        profit_margin=active_exit_margin,
                        tranche_profit_pct=active_tranche_margin
                    )

                    # [Exit 1: 바스켓 전량 익절]
                    if exit_decision["exit_type"] == "BASKET":
                        print(f"\n[{market}] 🎯 [바스켓 익절] {exit_decision['reason']}")
                        if not cancel_all_orders(upbit, market, state):
                            halt_for_order_reconciliation(market, state, {"reason": "바스켓 익절 전 주문 취소 미확정"})
                            continue

                        # Cancel fill could have executed volume! Calculate safe sell volume:
                        sell_qty = get_safe_sell_quantity(upbit, market, ticker, float(state.get("bot_quantity", quantity)), state)
                        if sell_qty < 0 or state.pop("balance_query_failed", False):
                            halt_for_order_reconciliation(market, state, {"reason": "바스켓 익절 전 잔고 조회 미확정"})
                            continue

                        if sell_qty > 0 and (sell_qty * current_price) >= MIN_ORDER_KRW:
                            sell_result = submit_market_order_and_wait(upbit, market, "ask", sell_qty)
                            if sell_result.get("status") != "FILLED":
                                halt_for_order_reconciliation(market, state, sell_result)
                                continue

                            filled_volume = float(sell_result["volume"])
                            filled_price = float(sell_result["price"])
                            real_pnl = (filled_price - avg_price) * filled_volume

                            send_telegram_alert(
                                f"🟢 <b>[익절 완료]</b> {market} (바스켓 전량)\n"
                                f"• 체결단가: {filled_price:,.0f}원 (평단가: {avg_price:,.0f}원)\n"
                                f"• 수익률: {exit_decision['pnl_pct']:+.2f}%\n"
                                f"• 실현손익: <b>{real_pnl:+,.0f}원</b>"
                            )

                            log_real_trade(
                                market=market,
                                ticker=ticker,
                                side="ask",
                                action="BASKET_TAKE_PROFIT",
                                price=filled_price,
                                volume=filled_volume,
                                cost_or_revenue=filled_volume * filled_price,
                                pnl=real_pnl,
                                strategy="HYBRID_MARTINGALE_MAGIC_SPLIT",
                                order_uuid=sell_result["uuid"]
                            )

                            state["bot_quantity"] = 0.0
                            state["bot_avg_price"] = 0.0
                            state["tranches"] = []
                            state["initial_entry_done"] = True
                            state["completed_cycles"] = state.get("completed_cycles", 0) + 1
                            state.pop("basket_limit_ask", None)
                            for rec in state.get("bear_limit_buys", {}).values():
                                if rec.get("state") == "wait":
                                    rec["state"] = "cancel"
                            save_strategy_state(market, state)
                            time.sleep(10)
                            continue
                        elif sell_qty <= 0 and float(state.get("bot_quantity", 0.0)) > 1e-8:
                            halt_for_order_reconciliation(market, state, {"reason": "바스켓 익절 대상 잔고 불일치(보호잔고 초과분 없음)"})
                            continue

                    # [Exit 2: 스쿼드별 가중평균 매수가 대비 +3% 단독 익절]
                    elif exit_decision["exit_type"] == "SQUAD" and USE_MAGIC_SPLIT_DEFENSE:
                        eligible_squads = exit_decision.get("eligible_squads", [])
                        if eligible_squads:
                            sq = eligible_squads[0]
                            sq_id = sq["squad_id"]
                            squad_tranches = sq["tranches"]
                            valid_squad_tranches = []
                            sellable_remaining = quantity
                            for tr in squad_tranches:
                                vol = float(tr.get("volume", 0.0))
                                if 0 < vol <= sellable_remaining + 1e-12:
                                    valid_squad_tranches.append(tr)
                                    sellable_remaining -= vol

                            if valid_squad_tranches:
                                tot_sell_vol = round(sum(tr["volume"] for tr in valid_squad_tranches), 8)
                                tot_sell_krw = tot_sell_vol * current_price

                                if tot_sell_krw >= MIN_ORDER_KRW and tot_sell_vol > 0:
                                    sold_ids = set(tr.get("tranche_id", tr.get("step")) for tr in valid_squad_tranches)
                                    print(f"\n[{market}] 💧 [스쿼드 {sq_id} 익절] 일괄 매도 실행: {tot_sell_vol:.6f} {ticker} ({tot_sell_krw:,.0f}원)")

                                    # 1. 기존 매도 주문(바스켓 매도) 취소
                                    cancelled = all(cancel_order_and_wait(upbit, order['uuid'], market=market, state=state) for order in sell_orders)
                                    if not cancelled:
                                        halt_for_order_reconciliation(market, state, {"reason": "기존 매도 주문 취소 미확정"})
                                        continue

                                    # 2. 해당 스쿼드의 미체결 매수 주문 취소 + 보유 트랜치가 없는 더 깊은 스쿼드의 고립 주문도 함께 취소
                                    held_squad_ids = set(
                                        tr.get("squad_id") for tr in state.get("tranches", [])
                                        if tr.get("squad_id") is not None and tr.get("squad_id") != sq_id
                                    )
                                    squad_buys_to_cancel = [
                                        uuid for uuid, rec in state.get("bear_limit_buys", {}).items()
                                        if rec.get("state") == "wait" and (
                                            rec.get("squad_id") == sq_id
                                            or (rec.get("squad_id") is not None and rec.get("squad_id") > sq_id and rec.get("squad_id") not in held_squad_ids)
                                        )
                                    ]
                                    cancel_buys_ok = True
                                    for b_uuid in squad_buys_to_cancel:
                                        if cancel_order_and_wait(upbit, b_uuid, market=market, state=state):
                                            if state.get("bear_limit_buys", {}).get(b_uuid, {}).get("state") != "done":
                                                state["bear_limit_buys"][b_uuid]["state"] = "cancel"
                                            cancelled_sq = state.get("bear_limit_buys", {}).get(b_uuid, {}).get("squad_id", sq_id)
                                            print(f"[{market}]   - 스쿼드 {cancelled_sq} 미체결 매수 주문 취소 완료: {b_uuid}")
                                        else:
                                            cancel_buys_ok = False
                                    if not cancel_buys_ok:
                                        halt_for_order_reconciliation(market, state, {"reason": f"스쿼드 {sq_id} 관련 매수 주문 취소 미확정"})
                                        continue

                                    # Refresh bot balance after cancellations to reflect any cancel-fills
                                    avg_price, quantity = get_bot_balance(upbit, market, ticker, state)

                                    # Safe sell quantity cap against protected balances!
                                    tot_sell_vol = get_safe_sell_quantity(upbit, market, ticker, tot_sell_vol, state)
                                    if tot_sell_vol < 0 or state.pop("balance_query_failed", False):
                                        halt_for_order_reconciliation(market, state, {"reason": f"스쿼드 {sq_id} 익절 전 잔고 조회 미확정"})
                                        continue
                                    if tot_sell_vol <= 0 or (tot_sell_vol * current_price) < MIN_ORDER_KRW:
                                        print(f"[{market}] ℹ️ [스쿼드 익절] 취소 후 매도 가능 수량 부족 또는 최소주문금액 미만; 잔여 바스켓 매도 복구 시도")
                                        state.pop("basket_limit_ask", None)
                                        state["bear_buy_rebalance_pending"] = True
                                        rem_qty = float(state.get("bot_quantity", quantity) or 0.0)
                                        rem_avg = float(state.get("bot_avg_price", avg_price) or 0.0)
                                        if rem_qty > 0 and rem_avg > 0:
                                            if is_adaptive_grid_enabled(market):
                                                basket_p = calculate_basket_sell_price(rem_avg, is_bull=is_bull)
                                            else:
                                                basket_p = adjust_price_to_tick(rem_avg * sell_profit_margin, method="ceil")
                                            safe_basket_qty = get_safe_sell_quantity(upbit, market, ticker, rem_qty, state)
                                            if safe_basket_qty < 0 or state.pop("balance_query_failed", False):
                                                halt_for_order_reconciliation(market, state, {"reason": "스쿼드 익절 스킵 후 바스켓 복구 전 잔고 조회 미확정"})
                                                continue
                                            if safe_basket_qty > 0 and (safe_basket_qty * basket_p) >= MIN_ORDER_KRW:
                                                restored_ask = submit_limit_order(upbit, market, "ask", basket_p, safe_basket_qty)
                                                if restored_ask and restored_ask.get("uuid"):
                                                    state["basket_limit_ask"] = {
                                                        "uuid": restored_ask["uuid"],
                                                        "price": basket_p,
                                                        "volume": safe_basket_qty
                                                    }
                                                    print(f"[{market}]   - [복구] 바스켓 매도 재등록 완료: {basket_p:,.0f}원, {safe_basket_qty:.6f}")
                                                else:
                                                    halt_for_order_reconciliation(market, state, {"reason": "스쿼드 익절 스킵 후 바스켓 복구 주문 등록 실패"})
                                                    continue
                                            else:
                                                state.pop("basket_limit_ask", None)
                                        if save_strategy_state(market, state) is False:
                                            halt_for_order_reconciliation(market, state, {"reason": "바스켓 복구 상태 저장 실패"})
                                        continue

                                    # 3. 스쿼드 보유 물량 시장가 매도
                                    sell_result = submit_market_order_and_wait(upbit, market, "ask", tot_sell_vol)
                                    status = sell_result.get("status")
                                    if status not in ("FILLED", "PARTIAL"):
                                        halt_for_order_reconciliation(market, state, sell_result)
                                        continue

                                    filled_price = float(sell_result.get("price", 0.0) or 0.0)
                                    filled_vol = float(sell_result.get("volume", 0.0) or 0.0)
                                    if filled_vol <= 0:
                                        halt_for_order_reconciliation(market, state, sell_result)
                                        continue

                                    rem_to_deduct = filled_vol
                                    tot_pnl = 0.0
                                    for tr in list(valid_squad_tranches):
                                        if rem_to_deduct <= 0:
                                            break
                                        tr_vol = tr.get("volume", 0.0)
                                        deduct_vol = min(tr_vol, rem_to_deduct)
                                        tr_pnl = (filled_price - tr["buy_price"]) * deduct_vol
                                        tot_pnl += tr_pnl
                                        log_real_trade(
                                            market=market,
                                            ticker=ticker,
                                            side="ask",
                                            action=f"SQUAD_{sq_id}_TAKE_PROFIT" if status == "FILLED" else f"SQUAD_{sq_id}_PARTIAL_TAKE_PROFIT",
                                            price=filled_price,
                                            volume=deduct_vol,
                                            cost_or_revenue=deduct_vol * filled_price,
                                            pnl=tr_pnl,
                                            strategy="HYBRID_MARTINGALE_SQUAD_EXIT",
                                            order_uuid=sell_result["uuid"]
                                        )
                                        rem_to_deduct -= deduct_vol
                                        tr["volume"] = round(tr["volume"] - deduct_vol, 8)

                                    # 4. 체결된 수량 반영 후 0 이하가 된 차수만 장부에서 제거
                                    state["tranches"] = [t for t in state.get("tranches", []) if round(t.get("volume", 0.0), 8) > 0]

                                    # 5. 남은 차수들을 기준으로 평단가 및 봇 수량 재계산 (다른 스쿼드는 보존)
                                    remaining = state.get("tranches", [])
                                    remaining_sellable = max(0.0, quantity - filled_vol)
                                    if remaining and remaining_sellable > 0:
                                        new_tot_vol = sum(t["volume"] for t in remaining)
                                        new_tot_cost = sum(t["volume"] * t["buy_price"] for t in remaining)
                                        new_avg = new_tot_cost / new_tot_vol if new_tot_vol > 0 else 0.0
                                        state["bot_avg_price"] = new_avg
                                        state["bot_quantity"] = min(round(new_tot_vol, 8), round(remaining_sellable, 8))

                                        # 잔여 포지션 바스켓 매도 주문 즉시 갱신
                                        if is_adaptive_grid_enabled(market):
                                            rem_sell_price = calculate_basket_sell_price(new_avg, is_bull=is_bull)
                                        else:
                                            rem_sell_price = adjust_price_to_tick(new_avg * sell_profit_margin, method="ceil")
                                        safe_basket_qty = get_safe_sell_quantity(upbit, market, ticker, state["bot_quantity"], state)
                                        if safe_basket_qty < 0 or state.pop("balance_query_failed", False):
                                            halt_for_order_reconciliation(market, state, {"reason": "스쿼드 매도 후 잔여 바스켓 매도 전 잔고 조회 미확정"})
                                            continue
                                        if safe_basket_qty > 0 and (safe_basket_qty * rem_sell_price) >= MIN_ORDER_KRW:
                                            ask_order = submit_limit_order(upbit, market, "ask", rem_sell_price, safe_basket_qty)
                                            if ask_order and ask_order.get("uuid"):
                                                state["basket_limit_ask"] = {"uuid": ask_order["uuid"], "price": rem_sell_price, "volume": safe_basket_qty}
                                                print(f"[{market}]   - 잔여 포지션 바스켓 익절 매도 재등록: {rem_sell_price:,.0f}원, {safe_basket_qty:.6f}")
                                            else:
                                                halt_for_order_reconciliation(market, state, {"reason": "스쿼드 매도 후 잔여 바스켓 매도 주문 등록 실패"})
                                                continue
                                        else:
                                            state.pop("basket_limit_ask", None)
                                    else:
                                        state["tranches"] = []
                                        state["bot_avg_price"] = 0.0
                                        state["bot_quantity"] = 0.0
                                    # 스쿼드 익절 후 잔여 매수 호가 재정렬 및 박스권 재진입 트리거
                                    state["bear_buy_rebalance_pending"] = True

                                    if save_strategy_state(market, state) is False:
                                        halt_for_order_reconciliation(market, state, {"reason": "스쿼드 익절 후 상태 저장 실패"})
                                        continue

                                    send_telegram_alert(
                                        f"🟢 <b>[익절 완료]</b> {market} (스쿼드 {sq_id} {'부분' if status == 'PARTIAL' else ''}익절)\n"
                                        f"• 체결단가: {filled_price:,.0f}원 (스쿼드 평단: {sq['squad_vwap']:,.0f}원)\n"
                                        f"• 실현손익: <b>{tot_pnl:+,.0f}원</b> ({sq['pnl_pct']:+.2f}%)\n"
                                        f"• 잔여 포지션: 평단 {state['bot_avg_price']:,.0f}원 ({len(state['tranches'])}차수 유지)"
                                    )
                                    time.sleep(2)
                                    continue
                                else:
                                    print(f"[{market}] ℹ️ [스쿼드 익절] 매도 대상 금액({tot_sell_krw:,.0f}원)이 최소주문금액(5,000원) 미만이므로 바스켓 익절 대기.")
                            else:
                                print(f"[{market}] ℹ️ [스쿼드 익절] 유효한 스쿼드 차수가 없음")

                    # [Exit 3: 개별 차수 매직스플릿 +3% 단독 익절 (레거시 하위호환용)]
                    elif exit_decision["exit_type"] == "TRANCHE" and USE_MAGIC_SPLIT_DEFENSE:
                        eligible = exit_decision["eligible_tranches"]
                        valid_tranches = []
                        sellable_remaining = quantity
                        for tr in eligible:
                            volume = tr.get("volume", 0)
                            if 0 < volume <= sellable_remaining + 1e-12:
                                valid_tranches.append(tr)
                                sellable_remaining -= volume

                        if valid_tranches:
                            tot_sell_vol = round(sum(tr["volume"] for tr in valid_tranches), 8)
                            tot_sell_krw = tot_sell_vol * current_price

                            if tot_sell_krw >= MIN_ORDER_KRW and tot_sell_vol > 0:
                                sold_steps = [tr.get("step_num", tr.get("step")) for tr in valid_tranches]
                                sold_ids = [tr.get("tranche_id", tr.get("step")) for tr in valid_tranches]
                                print(f"\n[{market}] 💧 [매직스플릿 차수 합산 익절] {sold_steps}차수 일괄 매도 실행: {tot_sell_vol:.6f} {ticker} ({tot_sell_krw:,.0f}원)")

                                cancelled = all(cancel_order_and_wait(upbit, order['uuid'], market=market, state=state) for order in sell_orders)
                                if not cancelled:
                                    halt_for_order_reconciliation(market, state, {"reason": "기존 매도 주문 취소 미확정"})
                                    continue

                                # Refresh bot balance after cancellations to reflect any cancel-fills
                                avg_price, quantity = get_bot_balance(upbit, market, ticker, state)

                                tot_sell_vol = get_safe_sell_quantity(upbit, market, ticker, tot_sell_vol, state)
                                if tot_sell_vol < 0 or state.pop("balance_query_failed", False):
                                    halt_for_order_reconciliation(market, state, {"reason": "차수 익절 전 잔고 조회 미확정"})
                                    continue
                                if tot_sell_vol <= 0 or (tot_sell_vol * current_price) < MIN_ORDER_KRW:
                                    print(f"[{market}] ℹ️ [매직스플릿] 취소 후 매도 가능 수량 부족 또는 최소주문금액 미만; 잔여 바스켓 매도 복구 시도")
                                    state.pop("basket_limit_ask", None)
                                    rem_qty = float(state.get("bot_quantity", quantity) or 0.0)
                                    rem_avg = float(state.get("bot_avg_price", avg_price) or 0.0)
                                    if rem_qty > 0 and rem_avg > 0:
                                        if is_adaptive_grid_enabled(market):
                                            basket_p = calculate_basket_sell_price(rem_avg, is_bull=is_bull)
                                        else:
                                            basket_p = adjust_price_to_tick(rem_avg * sell_profit_margin, method="ceil")
                                        safe_basket_qty = get_safe_sell_quantity(upbit, market, ticker, rem_qty, state)
                                        if safe_basket_qty < 0 or state.pop("balance_query_failed", False):
                                            halt_for_order_reconciliation(market, state, {"reason": "차수 익절 스킵 후 바스켓 복구 전 잔고 조회 미확정"})
                                            continue
                                        if safe_basket_qty > 0 and (safe_basket_qty * basket_p) >= MIN_ORDER_KRW:
                                            restored_ask = submit_limit_order(upbit, market, "ask", basket_p, safe_basket_qty)
                                            if restored_ask and restored_ask.get("uuid"):
                                                state["basket_limit_ask"] = {
                                                    "uuid": restored_ask["uuid"],
                                                    "price": basket_p,
                                                    "volume": safe_basket_qty
                                                }
                                                print(f"[{market}]   - [복구] 바스켓 매도 재등록 완료: {basket_p:,.0f}원, {safe_basket_qty:.6f}")
                                            else:
                                                halt_for_order_reconciliation(market, state, {"reason": "차수 익절 스킵 후 바스켓 복구 주문 등록 실패"})
                                                continue
                                        else:
                                            state.pop("basket_limit_ask", None)
                                    if save_strategy_state(market, state) is False:
                                        halt_for_order_reconciliation(market, state, {"reason": "바스켓 복구 상태 저장 실패"})
                                    continue

                                sell_result = submit_market_order_and_wait(upbit, market, "ask", tot_sell_vol)
                                status = sell_result.get("status")
                                if status not in ("FILLED", "PARTIAL"):
                                    halt_for_order_reconciliation(market, state, sell_result)
                                    continue

                                filled_price = float(sell_result.get("price", 0.0) or 0.0)
                                filled_vol = float(sell_result.get("volume", 0.0) or 0.0)
                                if filled_vol <= 0:
                                    halt_for_order_reconciliation(market, state, sell_result)
                                    continue

                                rem_to_deduct = filled_vol
                                tot_pnl = 0.0
                                for tr in list(valid_tranches):
                                    if rem_to_deduct <= 0:
                                        break
                                    tr_vol = tr.get("volume", 0.0)
                                    deduct_vol = min(tr_vol, rem_to_deduct)
                                    tr_pnl = (filled_price - tr["buy_price"]) * deduct_vol
                                    tot_pnl += tr_pnl
                                    st_num = tr.get("step_num", tr.get("step"))
                                    log_real_trade(
                                        market=market,
                                        ticker=ticker,
                                        side="ask",
                                        action=f"TRANCHE_TAKE_PROFIT_STEP_{st_num}",
                                        price=filled_price,
                                        volume=deduct_vol,
                                        cost_or_revenue=deduct_vol * filled_price,
                                        pnl=tr_pnl,
                                        strategy="HYBRID_MARTINGALE_MAGIC_SPLIT",
                                        order_uuid=sell_result["uuid"]
                                    )
                                    rem_to_deduct -= deduct_vol
                                    tr["volume"] = round(tr["volume"] - deduct_vol, 8)

                                # 3. 체결된 수량 반영 후 0 이하가 된 차수만 장부에서 제거
                                state["tranches"] = [t for t in state.get("tranches", []) if round(t.get("volume", 0.0), 8) > 0]

                                # 4. 남은 차수들을 기준으로 평단가 및 봇 수량 재계산
                                remaining = state.get("tranches", [])
                                remaining_sellable = max(0.0, quantity - filled_vol)
                                if remaining and remaining_sellable > 0:
                                    new_tot_vol = sum(t["volume"] for t in remaining)
                                    new_tot_cost = sum(t["volume"] * t["buy_price"] for t in remaining)
                                    new_avg = new_tot_cost / new_tot_vol if new_tot_vol > 0 else 0.0
                                    state["bot_avg_price"] = new_avg
                                    state["bot_quantity"] = min(round(new_tot_vol, 8), round(remaining_sellable, 8))

                                    # 잔여 포지션 바스켓 매도 주문 즉시 갱신
                                    if is_adaptive_grid_enabled(market):
                                        rem_sell_price = calculate_basket_sell_price(new_avg, is_bull=is_bull)
                                    else:
                                        rem_sell_price = adjust_price_to_tick(new_avg * sell_profit_margin, method="ceil")
                                    safe_basket_qty = get_safe_sell_quantity(upbit, market, ticker, state["bot_quantity"], state)
                                    if safe_basket_qty < 0 or state.pop("balance_query_failed", False):
                                        halt_for_order_reconciliation(market, state, {"reason": "차수 매도 후 잔여 바스켓 매도 전 잔고 조회 미확정"})
                                        continue
                                    if safe_basket_qty > 0 and (safe_basket_qty * rem_sell_price) >= MIN_ORDER_KRW:
                                        ask_order = submit_limit_order(upbit, market, "ask", rem_sell_price, safe_basket_qty)
                                        if ask_order and ask_order.get("uuid"):
                                            state["basket_limit_ask"] = {"uuid": ask_order["uuid"], "price": rem_sell_price, "volume": safe_basket_qty}
                                            print(f"[{market}]   - 잔여 포지션 바스켓 익절 매도 재등록: {rem_sell_price:,.0f}원, {safe_basket_qty:.6f}")
                                        else:
                                            halt_for_order_reconciliation(market, state, {"reason": "차수 매도 후 잔여 바스켓 매도 주문 등록 실패"})
                                            continue
                                    else:
                                        state.pop("basket_limit_ask", None)
                                else:
                                    state["tranches"] = []
                                    state["bot_avg_price"] = 0.0
                                    state["bot_quantity"] = 0.0
                                    state.pop("basket_limit_ask", None)

                                if save_strategy_state(market, state) is False:
                                    halt_for_order_reconciliation(market, state, {"reason": "차수 익절 후 상태 저장 실패"})
                                    continue

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
                                print(f"[{market}] ℹ️ [매직스플릿] 매도 대상 금액({tot_sell_krw:,.0f}원)이 최소주문금액(5,000원) 미만이므로 바스켓 익절 대기.")

                # A-2. 마틴게일 상태 머신 (주문 등록 및 물타기)
                martingale_sched = MARTINGALE_SCHEDULE.get(market, MARTINGALE_MULTIPLIERS)
                max_steps = get_martingale_max_steps(market)
                if is_exit_only:
                    max_steps = min(max_steps, len(state.get("tranches", [])))
                active_stages = get_existing_stages_from_state(state)
                if active_stages is None:
                    print(f"[{market}] [CRITICAL] 미체결 주문/트랜치 슬롯 메타데이터 불일치 또는 미확정; 거래를 중단합니다.")
                    halt_for_order_reconciliation(market, state, {"reason": "슬롯 메타데이터 불일치 또는 식별 불가"})
                    continue
                max_squad_cap_reached = (market == "KRW-SOL" and any(sid >= 4 and stg >= 4 for sid, stg in active_stages))
                current_steps = len(state.get("tranches", []))
                expected_open_buys = 0 if (max_squad_cap_reached or is_exit_only) else max(0, min(3, max_steps - len(active_stages)))

                # 그리드 이탈 방지: 시장가가 최고 매수가보다 크게 상승하여 박스권을 이탈한 경우 고립 주문 취소 및 재배치
                if buy_orders and not max_squad_cap_reached:
                    max_bid_price = max(float(o['price']) for o in buy_orders)
                    drift_threshold = 0.04
                    if is_adaptive_grid_enabled(market):
                        try:
                            from strategy.adaptive_grid import get_recent_candles
                            df_c = get_recent_candles(market)
                            reg_data_tmp = get_market_regime_and_natr(df_c)
                            c_natr = reg_data_tmp.get("curr_natr")
                            if c_natr:
                                drift_threshold = max(0.04, c_natr * 2.5)
                        except Exception:
                            drift_threshold = 0.04
                    if current_price > max_bid_price * (1.0 + drift_threshold):
                        held_squad_ids = set(t.get("squad_id") for t in state.get("tranches", []) if t.get("squad_id") is not None)
                        orphan_orders_to_cancel = [
                            o for o in buy_orders
                            if state.get("bear_limit_buys", {}).get(o['uuid'], {}).get("squad_id") not in held_squad_ids
                        ]
                        if orphan_orders_to_cancel:
                            print(f"[{market}] 🔄 [그리드 이탈 감지] 현재가({current_price:,.0f}원)가 최고 매수가({max_bid_price:,.0f}원) 대비 +{((current_price / max_bid_price) - 1.0)*100:.1f}% 괴리 (임계치 {drift_threshold*100:.1f}% 초과).")
                            print(f"[{market}]   - 고립된 미체결 매수 주문 {len(orphan_orders_to_cancel)}건 취소 및 박스권 재정렬 착수.")
                            cancel_drift_ok = True
                            for o in orphan_orders_to_cancel:
                                if cancel_order_and_wait(upbit, o['uuid'], market=market, state=state):
                                    if o['uuid'] in state.get("bear_limit_buys", {}):
                                        state["bear_limit_buys"][o['uuid']]["state"] = "cancel"
                                else:
                                    cancel_drift_ok = False
                            if not cancel_drift_ok:
                                halt_for_order_reconciliation(market, state, {"reason": "그리드 이탈 매수 주문 취소 미확정"})
                                continue
                            state["bear_buy_rebalance_pending"] = True
                            save_strategy_state(market, state)
                            buy_orders = [o for o in buy_orders if o['uuid'] not in [x['uuid'] for x in orphan_orders_to_cancel]]
                            num_buy = len(buy_orders)

                # SOL 4개 스쿼드(16차수) 등 최대 차수 도달 또는 EXIT_ONLY 모드 시 잔여 매수 주문 자동 취소 및 홀딩 관리
                if (current_steps >= max_steps or max_squad_cap_reached or is_exit_only) and num_buy > 0:
                    reason_desc = "익절 정리(EXIT_ONLY)" if is_exit_only else f"최대 차수({max_steps}차수) 도달"
                    print(f"[{market}] 🛑 마틴게일 {reason_desc}로 잔여 매수 주문 {num_buy}건 취소.")
                    cancel_ok = True
                    for order in buy_orders:
                        if cancel_order_and_wait(upbit, order['uuid'], market=market, state=state):
                            if order['uuid'] in state.get("bear_limit_buys", {}):
                                state["bear_limit_buys"][order['uuid']]["state"] = "cancel"
                        else:
                            cancel_ok = False
                    if not cancel_ok:
                        halt_for_order_reconciliation(market, state, {"reason": "마틴게일 매수 주문 취소 미확정"})
                        continue
                    save_strategy_state(market, state)
                    buy_orders = []
                    num_buy = 0

                # Case 2: 지정가 매도 주문 체결 확인 (F03)
                # 만약 등록된 매도 주문이 없고 basket_limit_ask가 등록되어 있다면,
                # 거래소 잔고와 무관하게(동시 체결된 매수분 등으로 잔고가 0이 아니어도)
                # 해당 매도 주문이 체결(done)되었는지 UUID로 직접 확인하여 정산한다.
                ask_info = state.get("basket_limit_ask")
                if num_sell == 0 and ask_info and ask_info.get("uuid"):
                    ask_uuid = ask_info["uuid"]
                    order = None
                    try:
                        order = upbit.get_order(ask_uuid)
                    except Exception as e:
                        print(f"[{market}] 지정가 매도 주문 조회 예외: {e}")
                    if not isinstance(order, dict) or order.get("uuid") != ask_uuid:
                        halt_for_order_reconciliation(market, state, {"reason": "지정가 매도 주문 상태 미확정", "uuid": ask_uuid})
                        continue
                    order_st = order.get("state")
                    if order_st not in ("done", "cancel"):
                        halt_for_order_reconciliation(market, state, {"reason": f"지정가 매도 미종결 상태({order_st})", "uuid": ask_uuid})
                        continue

                    try:
                        exec_vol = float(order.get("executed_volume", 0.0) or 0.0)
                    except (TypeError, ValueError):
                        exec_vol = 0.0

                    if order_st == "done" and exec_vol <= 0:
                        halt_for_order_reconciliation(market, state, {"reason": "지정가 매도 체결 수량 0", "uuid": ask_uuid})
                        continue

                    if order_st == "cancel":
                        if exec_vol > 0:
                            if not _reconcile_ask_order_fill(upbit, market, state, order, exec_vol, action="LIMIT_ASK_CANCEL_FILL"):
                                halt_for_order_reconciliation(market, state, {"reason": "취소 매도 주문 부분체결 정산 실패", "uuid": ask_uuid})
                                continue
                        else:
                            state.setdefault("_reconciled_asks", {})[ask_uuid] = {"volume": 0.0, "funds": 0.0}

                        state.pop("basket_limit_ask", None)
                        if save_strategy_state(market, state) is False:
                            halt_for_order_reconciliation(market, state, {"reason": "취소 매도 주문 상태 저장 실패", "uuid": ask_uuid})
                            continue

                        print(f"\n[{market}] ℹ️ [지정가 매도 취소 확인] UUID {ask_uuid} 취소 종결 (체결수량: {exec_vol}). 잔여 바스켓 재등록 준비.")
                        continue

                    trades = order.get("trades", [])
                    exec_funds = sum(float(t.get("funds", 0.0) or 0.0) for t in trades) if trades else 0.0
                    sold_p = (exec_funds / exec_vol) if (exec_vol > 0 and exec_funds > 0) else float(order.get("price", 0.0) or current_price)
                    if sold_p <= 0:
                        sold_p = float(ask_info.get("price", current_price))

                    print(f"\n[{market}] 🎯 [지정가 바스켓 매도 체결 감지] 포지션 매도 완료 (체결수량: {exec_vol}).")
                    cancel_all_ok = True
                    for b_order in buy_orders:
                        ok = cancel_order_and_wait(upbit, b_order['uuid'], market=market, state=state)
                        if not ok:
                            cancel_all_ok = False

                    if not cancel_all_ok:
                        halt_for_order_reconciliation(market, state, {"reason": "지정가 매도 후 잔여 매수 주문 취소 미확정", "uuid": ask_uuid})
                        continue

                    rem_to_remove = exec_vol
                    remaining_tranches = []
                    sold_cost = 0.0
                    for tr in state.get("tranches", []):
                        tr_vol = float(tr.get("volume", 0.0))
                        if rem_to_remove >= tr_vol - 1e-12:
                            rem_to_remove -= tr_vol
                            sold_cost += tr_vol * float(tr.get("buy_price", 0.0))
                        elif rem_to_remove > 1e-12:
                            tr_copy = dict(tr)
                            tr_copy["volume"] = round(tr_vol - rem_to_remove, 8)
                            sold_cost += rem_to_remove * float(tr.get("buy_price", 0.0))
                            rem_to_remove = 0.0
                            remaining_tranches.append(tr_copy)
                        else:
                            remaining_tranches.append(tr)

                    cost_basis = sold_cost if sold_cost > 0 else (exec_vol * float(state.get("bot_avg_price", 0.0)))
                    tot_revenue = exec_funds if exec_funds > 0 else (exec_vol * sold_p)
                    tot_pnl = tot_revenue - cost_basis

                    log_real_trade(
                        market=market,
                        ticker=ticker,
                        side="ask",
                        action="BASKET_TAKE_PROFIT_LIMIT",
                        price=sold_p,
                        volume=exec_vol,
                        cost_or_revenue=tot_revenue,
                        pnl=tot_pnl,
                        strategy="HYBRID_MARTINGALE_MAGIC_SPLIT",
                        order_uuid=ask_uuid
                    )

                    state["completed_cycles"] = state.get("completed_cycles", 0) + 1
                    state.pop("basket_limit_ask", None)
                    for rec in state.get("bear_limit_buys", {}).values():
                        if rec.get("state") == "wait":
                            rec["state"] = "cancel"

                    new_bot_qty = round(sum(float(tr.get("volume", 0.0)) for tr in remaining_tranches), 8)
                    if new_bot_qty <= 1e-8:
                        state["bot_quantity"] = 0.0
                        state["bot_avg_price"] = 0.0
                        state["tranches"] = []
                    else:
                        state["bot_quantity"] = new_bot_qty
                        new_cost = sum(float(tr.get("volume", 0.0)) * float(tr.get("buy_price", 0.0)) for tr in remaining_tranches)
                        state["bot_avg_price"] = new_cost / new_bot_qty if new_bot_qty > 0 else 0.0
                        state["tranches"] = remaining_tranches

                    save_strategy_state(market, state)
                    send_telegram_alert(
                        f"🟢 <b>[지정가 익절 완료]</b> {market} (바스켓 체결: {exec_vol})\n"
                        f"• 체결단가: {sold_p:,.0f}원 (원가: {cost_basis:,.0f}원)\n"
                        f"• 실현손익: <b>{tot_pnl:+,.0f}원</b>"
                    )
                    time.sleep(2)
                    continue

                elif num_sell == 0 and float(state.get("bot_quantity", 0.0)) > 1e-8:
                    avg_p, held_q = get_bot_balance(upbit, market, ticker, state)
                    if state.pop("balance_query_failed", False):
                        halt_for_order_reconciliation(market, state, {"reason": "지정가 매도 체결 확인 중 잔고 조회 미확정"})
                        continue
                    if held_q <= 1e-8:
                        halt_for_order_reconciliation(market, state, {"reason": "지정가 매도 주문 정보(UUID) 부재"})
                        continue

                # 정상 대기 상태:
                # 1) 매도 1건 등록되어 있고 매수 주문 수가 기대치와 일치할 때
                # 2) 또는 매수 주문 기대치가 0(최대 차수 도달 홀딩)이고 매도 주문 1건 등록되어 있을 때
                if num_sell == 1 and num_buy == expected_open_buys:
                    time.sleep(5)
                    continue
                elif num_sell == 0 and num_buy == expected_open_buys and num_buy > 0 and (quantity * current_price) < MIN_ORDER_KRW:
                    # 매도 주문 금액이 5,000원 미만이라 매도 등록 대기 중이고 매수 주문만 대기 중일 때
                    time.sleep(5)
                    continue

                # Case 3: 매도 완료 (또는 신규 진입) → 매도 주문 0건
                elif num_sell == 0:
                    initial_buy_result = None
                    if not all(cancel_order_and_wait(upbit, order['uuid'], market=market, state=state) for order in buy_orders):
                        halt_for_order_reconciliation(market, state, {"reason": "기존 매수 주문 취소 미확정"})
                        continue

                    avg_price, quantity = get_bot_balance(upbit, market, ticker, state)
                    if state.pop("balance_query_failed", False):
                        halt_for_order_reconciliation(market, state, {"reason": "하락장 진입 전 잔고 조회 미확정"})
                        continue
                    if (quantity * current_price) < MIN_ORDER_KRW:
                        if float(state.get("bot_quantity", 0.0)) > quantity + 1e-10:
                            halt_for_order_reconciliation(market, state, {"reason": "기존 봇 수량과 거래소 잔고 불일치"})
                            continue
                        # 1. 손절 재진입 쿨다운 검사
                        cooldown_until = float(state.get("stop_loss_cooldown_until", 0.0))
                        now_ts = time.time()
                        if now_ts < cooldown_until:
                            rem_min = int((cooldown_until - now_ts) / 60)
                            print(f"[{market}] ⏳ [하락장 방어] 손절 후 쿨다운 대기 중 ({rem_min}분 남음). 신규 진입 차단.")
                            time.sleep(10)
                            continue

                        if not candles_valid:
                            print(f"[{market}] ⚠️ [캔들 데이터 검증 대기] 확정 캔들 검증 실패로 최초 1차 진입 보류 (신규 매수 0건).")
                            time.sleep(5)
                            continue

                        # 2. 최초 1차 진입 조건:
                        # - EXIT_ONLY: 보유 물량 청산 완료 후 신규 진입 전면 차단 (포켓 이전 대기)
                        # - PURE_MARTINGALE: 상시 1-1-2-4 순환 모드로 잔여 포지션 청산 완료 시 즉시 1차수 시장가 진입
                        # - HYBRID: ClucMay 과매도 낙주(is_cluc_dip) 발생 시에만 진입 허용 (과매도 신호 없으면 현금 보존 관망)
                        if is_exit_only:
                            now_sec = time.time()
                            last_log = getattr(run_trading_strategy, f"_last_exit_log_{ticker}", 0)
                            if now_sec - last_log >= 300:
                                setattr(run_trading_strategy, f"_last_exit_log_{ticker}", now_sec)
                                print(f"[{market}] 🏁 [종료 대기 (EXIT_ONLY)] 봇 보유 물량 전량 정리 완료. 신규 매수 없이 대기합니다.")
                            time.sleep(10)
                            continue

                        if market_strat != "PURE_MARTINGALE" and signal != "MARTINGALE_BUY_DIP":
                            print(f"[{market}] 🛡️ [하락장 관망] 현재가 200 MA 하회 중. ClucMay 과매도 낙주 신호 대기 (100% 현금 보존 관망).")
                            time.sleep(10)
                            continue

                        krw_balance = check_krw_balance_alert(upbit, context=f"{market} 1차 진입 전")
                        if krw_balance < unit_krw:
                            print(f"[{market}] [WARN] 원화 잔고 부족 ({krw_balance:,.0f}원 < 필요: {unit_krw:,.0f}원). 10초 대기.")
                            time.sleep(10)
                            continue

                        entry_desc = "순수 마틴게일 1차수 신규 진입" if market_strat == "PURE_MARTINGALE" else "하락장 과매도 포착: ClucMay 투매 신호 감지"
                        print(f"\n[{market}] 💧 [{entry_desc}] 1 Unit 시장가 매수 실행: {unit_krw:,} KRW")
                        buy_result = submit_market_order_and_wait(upbit, market, "bid", unit_krw)
                        if buy_result.get("status") != "FILLED":
                            halt_for_order_reconciliation(market, state, buy_result)
                            continue

                        initial_buy_result = buy_result
                        filled_quantity = float(buy_result["volume"])
                        filled_price = float(buy_result["price"])
                        previous_quantity = quantity
                        quantity += filled_quantity
                        avg_price = (
                            previous_quantity * avg_price + filled_quantity * filled_price
                        ) / quantity

                        state["bot_quantity"] = quantity
                        state["bot_avg_price"] = avg_price
                        state["initial_entry_done"] = True
                        if previous_quantity > 0 and state.get("tranches"):
                            add_bot_tranche(
                                state, units=martingale_sched[0], buy_price=filled_price,
                                volume=filled_quantity, tranche_type="INITIAL",
                                squad_id=1, squad_stage=1
                            )

                        log_real_trade(
                            market=market,
                            ticker=ticker,
                            side="bid",
                            action="MARTINGALE_BUY_INITIAL",
                            price=filled_price,
                            volume=filled_quantity,
                            cost_or_revenue=filled_quantity * filled_price,
                            strategy="HYBRID_MARTINGALE_MAGIC_SPLIT",
                            order_uuid=buy_result["uuid"]
                        )

                    # 1차 차수 등록 (신규 진입으로 차수가 비어있을 때만)
                    if not state.get("tranches"):
                        state["tranches"] = []
                        add_bot_tranche(
                            state=state,
                            units=martingale_sched[0],
                            buy_price=avg_price,
                            volume=quantity,
                            tranche_type="INITIAL",
                            squad_id=1,
                            squad_stage=1
                        )
                        state["bot_quantity"] = quantity
                        state["bot_avg_price"] = avg_price
                        save_strategy_state(market, state)

                    print(f"[{market}]   - 현재 포지션: 평단가 {avg_price:,.0f}원, 보유수량 {quantity}")

                    # 바스켓 익절 주문
                    if is_adaptive_grid_enabled(market):
                        sell_price = calculate_basket_sell_price(avg_price, is_bull=is_bull)
                    else:
                        sell_price = adjust_price_to_tick(avg_price * sell_profit_margin, method="ceil")
                    safe_basket_qty = get_safe_sell_quantity(upbit, market, ticker, quantity, state)
                    if safe_basket_qty < 0 or state.pop("balance_query_failed", False):
                        print(f"[{market}] [CRITICAL] 잔고 조회 미확정으로 바스켓 매도 수량 계산 불가; 신규 주문 차단 및 거래 중단")
                        halt_for_order_reconciliation(market, state, {"reason": "바스켓 매도 전 잔고 조회 미확정"})
                        continue
                    if safe_basket_qty > 0 and (safe_basket_qty * sell_price) >= MIN_ORDER_KRW:
                        ask_order = submit_limit_order(upbit, market, "ask", sell_price, safe_basket_qty)
                        if ask_order and ask_order.get("uuid"):
                            state["basket_limit_ask"] = {"uuid": ask_order["uuid"], "price": sell_price, "volume": safe_basket_qty}
                            if save_strategy_state(market, state) is False:
                                halt_for_order_reconciliation(market, state, {"reason": "바스켓 매도 상태 저장 실패", "uuid": ask_order["uuid"]})
                                continue
                            print(f"[{market}]   - 바스켓 익절 매도 주문: {sell_price:,.0f}원, 수량 {safe_basket_qty}")
                        else:
                            print(f"[{market}] [CRITICAL] 바스켓 매도 주문 등록 실패; 신규 매수 차단 및 거래 중단")
                            halt_for_order_reconciliation(market, state, {"reason": "바스켓 매도 주문 등록 실패"})
                            continue
                    else:
                        state.pop("basket_limit_ask", None)
                        if save_strategy_state(market, state) is False:
                            halt_for_order_reconciliation(market, state, {"reason": "바스켓 매도 정리 상태 저장 실패"})
                            continue
                        print(f"[{market}]   - [INFO] 바스켓 매도 평가액({quantity * sell_price:,.0f}원)이 최소주문(5,000원) 미만이거나 가용수량 부족으로 추가 물타기 체결 후 등록합니다.")
                    time.sleep(0.2)

                    # 마틴게일 매수 주문 계획
                    if get_existing_stages_from_state(state) is None:
                        print(f"[{market}] [CRITICAL] 신규 주문 계산 중 슬롯 메타데이터 불일치 또는 미확정; 거래를 중단합니다.")
                        halt_for_order_reconciliation(market, state, {"reason": "신규 주문 슬롯 메타데이터 불일치 또는 식별 불가"})
                        continue
                    reg_data = {}
                    if not candles_valid:
                        print(f"[{market}] ⚠️ [캔들 데이터 검증 대기] 확정 캔들 검증 실패로 신규 매수 주문 등록 보류 (신규 매수 0건).")
                        new_orders = []
                    elif is_adaptive_grid_enabled(market):
                        from strategy.adaptive_grid import get_recent_candles
                        df_c = get_recent_candles(market)
                        reg_data = get_market_regime_and_natr(df_c)
                        cur_natr = reg_data.get("curr_natr")

                        if cur_natr is None or not reg_data.get("candles_valid", False):
                            print(f"[{market}] ⚠️ [캔들 데이터 검증 대기] 적응형 지표 확정 캔들 검증 실패로 신규 매수 주문 등록 보류 (신규 매수 0건).")
                            new_orders = []
                        else:
                            base_p = min(current_price, avg_price) if avg_price > 0 else current_price
                            new_orders = calculate_adaptive_buy_prices(
                                base_price=base_p,
                                natr=cur_natr,
                                current_step=len(state.get("tranches", [])),
                                num_orders=3,
                                max_steps=max_steps,
                                step_scale=DEFAULT_STEP_SCALE,
                                multipliers=martingale_sched,
                                unit_krw=unit_krw,
                                state=state
                            )
                    else:
                        new_orders = calculate_new_buy_prices(
                            avg_buy_price=avg_price,
                            existing_orders=None,
                            max_steps=max_steps,
                            current_step=len(state.get("tranches", [])),
                            multipliers=martingale_sched,
                            down_pct=get_magic_split_down_pct(market),
                            state=state
                        )
                    print(f"[{market}]   - 신규 마틴게일 매수 주문 계획 ({len(new_orders)}개): {new_orders}")

                    for order in new_orders:
                        p = order['price']
                        u = order['units']
                        s_id = order.get('squad_id', 1)
                        s_stage = order.get('squad_stage', 1)
                        order_krw = unit_krw * u
                        volume = round(order_krw / p, 8)
                        buy_order = submit_limit_order(upbit, market, "bid", p, volume)
                        if buy_order:
                            track_bear_limit_buy(market, state, buy_order, u, squad_id=s_id, squad_stage=s_stage)
                            print(f"[{market}]     - 지정가 매수 주문: {p:,.0f}원 | Squad {s_id} Stage {s_stage} | {u} Units ({order_krw:,}원)")
                        time.sleep(0.2)

                    if bool(new_orders) or (candles_valid and (not is_adaptive_grid_enabled(market) or reg_data.get("candles_valid", False))):
                        state["bear_buy_rebalance_pending"] = False
                    else:
                        state["bear_buy_rebalance_pending"] = True
                    state_saved = save_strategy_state(market, state)
                    label_desc = "순수 마틴게일 1차수" if market_strat == "PURE_MARTINGALE" else "하락 방어 1차수"
                    if initial_buy_result is not None and state_saved is not False:
                        send_telegram_alert(
                            f"🔵 <b>[매수 체결]</b> {market} ({label_desc})\n"
                            f"• 체결가: {filled_price:,.0f}원 | 이번 체결: {filled_quantity:.8f} {ticker}\n"
                            f"• 평단가: {avg_price:,.0f}원 | 수량: {quantity:.6f} {ticker}\n"
                            f"• 바스켓 목표: {sell_price:,.0f}원 (+{(sell_price / avg_price - 1) * 100:.2f}%)\n"
                            f"• 주문 UUID: {initial_buy_result['uuid']}"
                        )

                # Case 4: 물타기 매수 체결 감지
                elif num_sell == 1 and num_buy < expected_open_buys:
                    if not state.get("bear_buy_rebalance_pending", False):
                        time.sleep(5)
                        continue
                    print(f"\n[{time.strftime('%H:%M:%S')}] [{market}] [하락장 방어] Case 4: 대기 주문 재배치. 신규 체결 여부는 UUID 정산으로만 판정.")

                    if not all(cancel_order_and_wait(upbit, order['uuid'], market=market, state=state) for order in sell_orders):
                        halt_for_order_reconciliation(market, state, {"reason": "기존 매도 주문 취소 미확정"})
                        continue

                    # UUID execution already updated the ledger; balances only cap sellable volume.
                    avg_price, quantity = get_bot_balance(upbit, market, ticker, state)
                    if state.pop("balance_query_failed", False):
                        halt_for_order_reconciliation(market, state, {"reason": "물타기 체결 후 잔고 조회 미확정"})
                        continue
                    print(f"[{market}]   - 갱신된 봇 포지션: 새 평단가 {avg_price:,.0f}원, 봇 보유수량 {quantity:.6f}")

                    # 새 바스켓 익절 매도 등록 (봇 수량만 등록)
                    if is_adaptive_grid_enabled(market):
                        sell_price = calculate_basket_sell_price(avg_price, is_bull=is_bull)
                    else:
                        sell_price = adjust_price_to_tick(avg_price * sell_profit_margin, method="ceil")
                    safe_basket_qty = get_safe_sell_quantity(upbit, market, ticker, quantity, state)
                    if safe_basket_qty < 0 or state.pop("balance_query_failed", False):
                        print(f"[{market}] [CRITICAL] 잔고 조회 미확정으로 새 바스켓 매도 수량 계산 불가; 추가 물타기 차단 및 거래 중단")
                        halt_for_order_reconciliation(market, state, {"reason": "새 바스켓 매도 전 잔고 조회 미확정"})
                        continue
                    if safe_basket_qty > 0 and (safe_basket_qty * sell_price) >= MIN_ORDER_KRW:
                        ask_order = submit_limit_order(upbit, market, "ask", sell_price, safe_basket_qty)
                        if ask_order and ask_order.get("uuid"):
                            state["basket_limit_ask"] = {"uuid": ask_order["uuid"], "price": sell_price, "volume": safe_basket_qty}
                            if save_strategy_state(market, state) is False:
                                halt_for_order_reconciliation(market, state, {"reason": "새 바스켓 매도 상태 저장 실패", "uuid": ask_order["uuid"]})
                                continue
                            print(f"[{market}]   - 새 바스켓 익절 매도: {sell_price:,.0f}원, 전량 {safe_basket_qty:.6f}")
                        else:
                            print(f"[{market}] [CRITICAL] 새 바스켓 매도 주문 등록 실패; 추가 물타기 차단 및 거래 중단")
                            halt_for_order_reconciliation(market, state, {"reason": "새 바스켓 매도 주문 등록 실패"})
                            continue
                    else:
                        state.pop("basket_limit_ask", None)
                        if save_strategy_state(market, state) is False:
                            halt_for_order_reconciliation(market, state, {"reason": "새 바스켓 매도 정리 상태 저장 실패"})
                            continue
                    time.sleep(0.2)

                    # 추가 매수 주문 계획 (최대 max_steps 차수 제한)
                    active_stages = get_existing_stages_from_state(state)
                    if active_stages is None:
                        print(f"[{market}] [CRITICAL] 추가 물타기 계산 중 슬롯 메타데이터 불일치 또는 미확정; 거래를 중단합니다.")
                        halt_for_order_reconciliation(market, state, {"reason": "추가 물타기 슬롯 메타데이터 불일치 또는 식별 불가"})
                        continue
                    reg_data = {}
                    max_squad_cap_reached = (market == "KRW-SOL" and any(sid >= 4 and stg >= 4 for sid, stg in active_stages))
                    if max_squad_cap_reached or len(active_stages) >= max_steps or is_exit_only:
                        print(f"[{market}]   - 마틴게일 최대 차수({max_steps}차수) 도달 또는 익절 정리(EXIT_ONLY) 모드: 추가 물타기 매수 생략.")
                        additional_orders = []
                    elif not candles_valid:
                        print(f"[{market}] ⚠️ [캔들 데이터 검증 대기] 확정 캔들 검증 실패로 추가 물타기 매수 보류 (신규 매수 0건).")
                        additional_orders = []
                    elif is_adaptive_grid_enabled(market):
                        from strategy.adaptive_grid import get_recent_candles
                        df_c = get_recent_candles(market)
                        reg_data = get_market_regime_and_natr(df_c)
                        cur_natr = reg_data.get("curr_natr")

                        if cur_natr is None or not reg_data.get("candles_valid", False):
                            print(f"[{market}] ⚠️ [캔들 데이터 검증 대기] 적응형 지표 확정 캔들 검증 실패로 추가 물타기 매수 보류 (신규 매수 0건).")
                            additional_orders = []
                        else:
                            remaining_orders = max(0, 3 - len(buy_orders))
                            lowest_bid = min(float(o['price']) for o in buy_orders) if buy_orders else (min(current_price, avg_price) if avg_price > 0 else current_price)
                            additional_orders = calculate_adaptive_buy_prices(
                                base_price=lowest_bid,
                                natr=cur_natr,
                                current_step=len(state.get("tranches", [])),
                                num_orders=remaining_orders,
                                max_steps=max_steps,
                                step_scale=DEFAULT_STEP_SCALE,
                                multipliers=martingale_sched,
                                unit_krw=unit_krw,
                                state=state,
                                existing_orders=buy_orders
                            )
                    else:
                        buy_order_details = []
                        for o in buy_orders:
                            uuid = o.get('uuid')
                            rec = state.get("bear_limit_buys", {}).get(uuid, {})
                            buy_order_details.append({
                                'price': float(o['price']),
                                'units': rec.get('units', 1),
                                'squad_id': rec.get('squad_id'),
                                'squad_stage': rec.get('squad_stage'),
                                'uuid': uuid
                            })
                        additional_orders = calculate_new_buy_prices(
                            avg_buy_price=avg_price,
                            existing_orders=buy_order_details,
                            max_steps=max_steps,
                            current_step=len(state.get("tranches", [])),
                            multipliers=martingale_sched,
                            down_pct=get_magic_split_down_pct(market),
                            state=state
                        )

                    krw_balance = check_krw_balance_alert(upbit, context=f"{market} 추가 물타기 매수")
                    for order in additional_orders:
                        p = order['price']
                        u = order['units']
                        s_id = order.get('squad_id')
                        s_stage = order.get('squad_stage')
                        order_krw = unit_krw * u
                        if krw_balance < order_krw:
                            print(f"[{market}]     [WARN] 원화 잔고 부족으로 매수 스킵 ({krw_balance:,.0f}원 < {order_krw:,}원)")
                            continue
                        volume = round(order_krw / p, 8)
                        buy_order = submit_limit_order(upbit, market, "bid", p, volume)
                        if buy_order:
                            track_bear_limit_buy(market, state, buy_order, u, squad_id=s_id, squad_stage=s_stage)
                            krw_balance -= order_krw
                            print(f"[{market}]     - 추가 매수 주문: {p:,.0f}원 | Squad {s_id} Stage {s_stage} | {u} Units ({order_krw:,}원)")
                        time.sleep(0.2)

                    if max_squad_cap_reached or len(active_stages) >= max_steps:
                        state["bear_buy_rebalance_pending"] = False
                    elif bool(additional_orders) or (candles_valid and (not is_adaptive_grid_enabled(market) or reg_data.get("candles_valid", False))):
                        state["bear_buy_rebalance_pending"] = False
                    else:
                        state["bear_buy_rebalance_pending"] = True
                    save_strategy_state(market, state)

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
                            if not cancel_all_orders(upbit, market, state=state):
                                halt_for_order_reconciliation(market, state, {"reason": "트레일링 익절 전 주문 취소 미확정"})
                                continue
                            avg_price, quantity = get_bot_balance(upbit, market, ticker, state)
                            sell_qty = get_safe_sell_quantity(upbit, market, ticker, quantity, state)
                            if sell_qty < 0 or state.pop("balance_query_failed", False):
                                halt_for_order_reconciliation(market, state, {"reason": "트레일링 익절 전 잔고 조회 미확정"})
                                continue
                            if sell_qty <= 0 or (sell_qty * current_price) < MIN_ORDER_KRW:
                                print(f"[{market}] 트레일링 익절 대상 수량 부족 또는 최소 주문금액 미달 (safe_qty={sell_qty})")
                                continue
                            recovery = {
                                "kind": "TRAILING_STOP_EXIT",
                                "market": market,
                                "uuid": None,
                                "volume": sell_qty,
                                "avg_price": avg_price,
                                "cycles": state.get("completed_cycles", 0),
                            }
                            save_trailing_exit_recovery(market, recovery)

                            def checkpoint_exit_uuid(order_uuid: str) -> None:
                                recovery["uuid"] = order_uuid
                                save_trailing_exit_recovery(market, recovery)

                            sell_result = submit_market_order_and_wait(
                                upbit, market, "ask", sell_qty, on_accepted=checkpoint_exit_uuid
                            )
                            if sell_result.get("status") != "FILLED":
                                print(f"[{market}] [CRITICAL] trailing 청산 주문 미확정; recovery 유지")
                                continue

                            filled_volume = float(sell_result["volume"])
                            filled_price = float(sell_result["price"])
                            real_pnl = (filled_price - avg_price) * filled_volume

                            msg = (
                                f"🟢 <b>[트레일링 익절 체결]</b> {market}\n"
                                f"• 체결단가: {filled_price:,.0f}원\n"
                                f"• 실현손익: <b>{real_pnl:+,.0f}원</b>"
                            )
                            print(f"\n[{market}] {msg}")
                            send_telegram_alert(msg)
                            if not reconcile_trailing_exit(upbit, market, state, recovery, sell_result):
                                print(f"[{market}] [CRITICAL] trailing 청산 최종 저장 실패; recovery 유지")
                                continue
                            time.sleep(10)
                            continue

                    # (1차 이평선 추세청산 제거: 잔파동 털림을 방지하고 12시간 정기적립으로 물량을 모으며, +10% 트레일링 익절 및 -10% 긴급손절로만 관리)

                    # B-3. 상승장 12시간 정기 시간 분할 적립 (Time-DCA)
                    if USE_BULL_TIME_DCA and not state.get("trailing_stop_active", False):
                        current_steps = len(state.get("tranches", []))
                        if current_steps < MAX_BULL_DCA_STEPS:
                            now_kst = datetime.utcnow() + timedelta(hours=9)
                            today_str = now_kst.strftime("%Y-%m-%d")

                            # 💡 [중복 매수 방지 Guard 1]: 오전 일봉 종가 매수(08:50) 우선권 보장
                            # 07:00 ~ 09:05 KST 사이에는 정기 적립 매수를 일시 대기(양보)하여,
                            # 08:50 일봉 종가 매수와 불과 몇 초/분 간격으로 중복 매수되는 현상을 원천 방지합니다.
                            # - 08:50 종가 매수 조건(양봉 & 5일선 지지) 충족 시 종가 매수가 체결되고 타이머가 리셋되어 저녁 20:50으로 넘어갑니다.
                            # - 음봉/5일선 이탈 등으로 종가 매수가 보류될 경우, 09:05 이후 12시간 정기 적립이 정상 실행되어 정기 모으기를 유지합니다.
                            in_morning_closing_window = (
                                USE_BULL_CLOSING_BUY
                                and (now_kst.hour in (7, 8) or (now_kst.hour == 9 and now_kst.minute < 5))
                            )

                            hours_elapsed = get_hours_since_last_buy(state, now_kst)

                            should_dca_buy = False
                            if not in_morning_closing_window:
                                if hours_elapsed >= BULL_TIME_DCA_INTERVAL_HOURS:
                                    should_dca_buy = True

                            # 최초 기록이 전혀 없는 경우 현재 시각으로 초기화
                            if not state.get("last_dca_buy_time") and not state.get("tranches"):
                                state["last_dca_buy_time"] = now_kst.strftime("%Y-%m-%d %H:%M:%S")
                                save_strategy_state(market, state)

                            if should_dca_buy:
                                krw_balance = check_krw_balance_alert(upbit, context=f"{market} 상승장 {BULL_TIME_DCA_INTERVAL_HOURS}시간 정기적립 {current_steps + 1}회차")
                                if krw_balance >= unit_krw:
                                    next_step = current_steps + 1
                                    print(
                                        f"\n[{market}] ⏰ [상승장 {BULL_TIME_DCA_INTERVAL_HOURS}시간 정기 분할적립 {next_step}/{MAX_BULL_DCA_STEPS}회차 매수]\n"
                                        f"직전 매수 후 {hours_elapsed:.1f}시간 경과 (기준: {BULL_TIME_DCA_INTERVAL_HOURS}시간) | 현재가: {current_price:,.0f}원"
                                    )
                                    buy_result = submit_market_order_and_wait(upbit, market, "bid", unit_krw)
                                    if buy_result.get("status") != "FILLED":
                                        halt_for_order_reconciliation(market, state, buy_result)
                                        continue
                                    bought_vol = float(buy_result["volume"])
                                    filled_price = float(buy_result["price"])

                                    old_q = quantity
                                    old_avg = avg_price
                                    new_q = old_q + bought_vol
                                    new_avg = ((old_avg * old_q) + (filled_price * bought_vol)) / new_q if new_q > 0 else filled_price

                                    state["bot_quantity"] = new_q
                                    state["bot_avg_price"] = new_avg
                                    state["trend_peak_price"] = max(state.get("trend_peak_price", filled_price), filled_price)
                                    state["last_dca_buy_time"] = now_kst.strftime("%Y-%m-%d %H:%M:%S")

                                    add_bot_tranche(
                                        state=state,
                                        units=1,
                                        buy_price=filled_price,
                                        volume=bought_vol,
                                        tranche_type="TIME_DCA"
                                    )
                                    save_strategy_state(market, state)

                                    log_real_trade(
                                        market=market,
                                        ticker=ticker,
                                        side="bid",
                                        action=f"BULL_TIME_DCA_STEP_{next_step}",
                                        price=filled_price,
                                        volume=bought_vol,
                                        cost_or_revenue=filled_price * bought_vol,
                                        pnl=0.0,
                                        strategy="HYBRID_TREND",
                                        order_uuid=buy_result["uuid"]
                                    )

                                    pnl_pct = ((current_price - new_avg) / new_avg) * 100
                                    msg = (
                                        f"🔵 <b>[매수 체결]</b> {market} (상승 적립 {next_step}/{MAX_BULL_DCA_STEPS}회차)\n"
                                        f"• 체결가: {filled_price:,.0f}원 ({filled_price * bought_vol:,.0f}원)\n"
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
                                    buy_result = submit_market_order_and_wait(upbit, market, "bid", unit_krw)
                                    if buy_result.get("status") != "FILLED":
                                        halt_for_order_reconciliation(market, state, buy_result)
                                        continue
                                    bought_vol = float(buy_result["volume"])
                                    filled_price = float(buy_result["price"])

                                    # 누적 포지션 및 평단가 갱신
                                    old_q = quantity
                                    old_avg = avg_price
                                    new_q = old_q + bought_vol
                                    new_avg = ((old_avg * old_q) + (filled_price * bought_vol)) / new_q if new_q > 0 else filled_price

                                    state["bot_quantity"] = new_q
                                    state["bot_avg_price"] = new_avg
                                    state["trend_peak_price"] = max(state.get("trend_peak_price", filled_price), filled_price)
                                    state["last_buy_date"] = (datetime.utcnow() + timedelta(hours=9)).strftime("%Y-%m-%d")

                                    add_bot_tranche(
                                        state=state,
                                        units=1,
                                        buy_price=filled_price,
                                        volume=bought_vol,
                                        tranche_type="PYRAMID"
                                    )
                                    save_strategy_state(market, state)

                                    log_real_trade(
                                        market=market,
                                        ticker=ticker,
                                        side="bid",
                                        action=f"BULL_PYRAMID_STEP_{next_step}",
                                        price=filled_price,
                                        volume=bought_vol,
                                        cost_or_revenue=filled_price * bought_vol,
                                        pnl=0.0,
                                        strategy="HYBRID_TREND",
                                        order_uuid=buy_result["uuid"]
                                    )

                                    pnl_pct = ((current_price - new_avg) / new_avg) * 100
                                    msg = (
                                        f"🔵 <b>[매수 체결]</b> {market} (불타기 {next_step}/{MAX_PYRAMID_STEPS}회차)\n"
                                        f"• 체결가: {filled_price:,.0f}원 ({filled_price * bought_vol:,.0f}원)\n"
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
                            hours_since_last_buy = get_hours_since_last_buy(state, now_kst)

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
                            # 💡 [중복 방지 Guard 2]: 최근 매수 후 최소 간격(MIN_BULL_BUY_GAP_HOURS) 미달 시 종가 매수 건너뛰기
                            elif hours_since_last_buy < MIN_BULL_BUY_GAP_HOURS:
                                state["last_closing_buy_date"] = today_str
                                state["today_closing_status"] = f"⚪ 정기적립 근접 스킵 ({hours_since_last_buy:.1f}h 전 매수)"
                                save_strategy_state(market, state)
                                skip_msg = (
                                    f"ℹ️ <b>[일봉 종가 매수 보류]</b> {market}\n"
                                    f"• 판정: <b>중복 매수 방지 (보류)</b>\n"
                                    f"• 사유: 최근 매수({hours_since_last_buy:.1f}시간 전) 후 최소 보호 간격({MIN_BULL_BUY_GAP_HOURS}시간)이 경과하지 않아 동일 시간대 중복 매수를 건너뜁니다."
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
                                        buy_result = submit_market_order_and_wait(upbit, market, "bid", unit_krw)
                                        if buy_result.get("status") != "FILLED":
                                            halt_for_order_reconciliation(market, state, buy_result)
                                            continue
                                        bought_vol = float(buy_result["volume"])
                                        filled_price = float(buy_result["price"])

                                        # 누적 포지션 및 평단가 갱신
                                        old_q = quantity
                                        old_avg = avg_price
                                        new_q = old_q + bought_vol
                                        new_avg = ((old_avg * old_q) + (filled_price * bought_vol)) / new_q if new_q > 0 else filled_price

                                        state["bot_quantity"] = new_q
                                        state["bot_avg_price"] = new_avg
                                        state["trend_peak_price"] = max(state.get("trend_peak_price", filled_price), filled_price)
                                        state["last_buy_date"] = today_str
                                        state["last_closing_buy_date"] = today_str
                                        state["today_closing_status"] = f"🟢 체결 (+{unit_krw:,}원, {next_step}회차)"
                                        state["last_dca_buy_time"] = now_kst.strftime("%Y-%m-%d %H:%M:%S")

                                        add_bot_tranche(
                                            state=state,
                                            units=1,
                                            buy_price=filled_price,
                                            volume=bought_vol,
                                            tranche_type="CLOSING"
                                        )
                                        save_strategy_state(market, state)

                                        log_real_trade(
                                            market=market,
                                            ticker=ticker,
                                            side="bid",
                                            action=f"BULL_CLOSING_BUY_STEP_{next_step}",
                                            price=filled_price,
                                            volume=bought_vol,
                                            cost_or_revenue=filled_price * bought_vol,
                                            pnl=0.0,
                                            strategy="HYBRID_TREND",
                                            order_uuid=buy_result["uuid"]
                                        )

                                        pnl_pct = ((current_price - new_avg) / new_avg) * 100
                                        msg = (
                                            f"🔵 <b>[종가 매수 체결]</b> {market} (상승장 {next_step}/{MAX_BULL_DCA_STEPS}회차)\n"
                                            f"• 체결가: {filled_price:,.0f}원 ({filled_price * bought_vol:,.0f}원)\n"
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
                    if float(state.get("bot_quantity", 0.0)) > quantity + 1e-10:
                        halt_for_order_reconciliation(market, state, {"reason": "기존 봇 수량과 거래소 잔고 불일치"})
                        continue

                    # 1. 손절 재진입 쿨다운 검사
                    cooldown_until = float(state.get("stop_loss_cooldown_until", 0.0))
                    now_ts = time.time()
                    if now_ts < cooldown_until:
                        rem_min = int((cooldown_until - now_ts) / 60)
                        if now_sec - last_heartbeat_time >= 1800:
                            print(f"[{market}] ⏳ [상승 국면] 손절 후 쿨다운 대기 중 ({rem_min}분 남음). 신규 진입 차단.")
                        time.sleep(10)
                        continue

                    if not candles_valid:
                        print(f"[{market}] ⚠️ [캔들 데이터 검증 대기] 확정 캔들 검증 실패로 상승장 신규 진입 보류 (신규 매수 0건).")
                        time.sleep(5)
                        continue

                    should_buy = False
                    buy_action = ""
                    buy_reason = ""

                    need_fresh_gc = state.get("need_fresh_golden_cross", False)
                    if not state.get("initial_entry_done", False):
                        # 안전 검사: 이미 계좌에 보호 수량을 초과하는 봇 수량이 존재하면 신규 매수 없이 즉시 동기화
                        try:
                            available = upbit.get_balance(ticker)
                            actual_avail = float(available)
                            if not math.isfinite(actual_avail) or actual_avail < 0:
                                raise ValueError("invalid exchange balance")
                        except Exception as error:
                            halt_for_order_reconciliation(
                                market, state, {"reason": f"상승장 최초 진입 잔고 조회 미확정: {error}"}
                            )
                            continue
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
                        state["need_fresh_golden_cross"] = False
                    elif signal == "BUY_ALIGNED" and not need_fresh_gc:
                        should_buy = True
                        buy_action = "BULL_REGIME_ALIGNED_ENTRY"
                        buy_reason = f"200선 상회 및 5/20 정배열(5선 {curr_ma5:,.0f} > 20선 {curr_ma20:,.0f}) 1회차 신규 진입"

                    if should_buy:
                        krw_balance = check_krw_balance_alert(upbit, context=f"{market} 상승장 1회차 진입")
                        if krw_balance >= unit_krw:
                            print(f"\n[{market}] 🚀 [상승 국면] {buy_reason}! 1 Unit 시장가 매수 실행.")
                            buy_result = submit_market_order_and_wait(upbit, market, "bid", unit_krw)
                            if buy_result.get("status") != "FILLED":
                                halt_for_order_reconciliation(market, state, buy_result)
                                continue

                            avg_p = float(buy_result["price"])
                            q = float(buy_result["volume"])

                            previous_quantity = quantity
                            total_quantity = previous_quantity + q
                            state["bot_quantity"] = total_quantity
                            state["bot_avg_price"] = (
                                (avg_price * previous_quantity + avg_p * q) / total_quantity
                            )
                            state["trend_peak_price"] = max(state.get("trend_peak_price", 0.0), avg_p)
                            state["trailing_stop_active"] = False
                            state["initial_entry_done"] = True
                            state["need_fresh_golden_cross"] = False
                            if previous_quantity > 0 and not state.get("tranches"):
                                add_bot_tranche(
                                    state=state, units=1, buy_price=avg_price,
                                    volume=previous_quantity, tranche_type="RECOVERED"
                                )
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
                                cost_or_revenue=avg_p * q,
                                strategy="HYBRID_TREND",
                                order_uuid=buy_result["uuid"]
                            )

                            send_telegram_alert(
                                f"🔵 <b>[매수 체결]</b> {market} (상승 1회차 신규)\n"
                                f"• 체결가: {avg_p:,.0f}원 ({avg_p * q:,.0f}원)\n"
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


# --- 텔레그램 브리핑 및 현황 조회 핸들러 ---
def send_telegram_status_briefing():
    """현재 전 계좌 및 코인별 봇 상태를 텔레그램으로 깔끔하게 통합 브리핑"""
    try:
        from utils.telegram_briefing import build_status_briefing
        msg = build_status_briefing()
        send_telegram_alert(msg)
        print(f"[{time.strftime('%H:%M:%S')}] [BRIEFING] 텔레그램 통합 현황 브리핑 발송 완료.")
    except Exception as e:
        print(f"[WARN] 텔레그램 정기 브리핑 생성 오류: {e}")
        send_telegram_alert(f"⚠️ 통합 브리핑 생성 오류: {e}")


def send_telegram_main_bot_briefing():
    """메인 봇 실거래 상세 현황 브리핑 (/메인 명령어)"""
    try:
        from utils.telegram_briefing import build_main_bot_briefing
        msg = build_main_bot_briefing()
        send_telegram_alert(msg)
        print(f"[{time.strftime('%H:%M:%S')}] [BRIEFING] 텔레그램 메인봇 상세 브리핑 발송 완료.")
    except Exception as e:
        send_telegram_alert(f"⚠️ 메인봇 브리핑 생성 오류: {e}")


def send_telegram_cascade_briefing():
    """Cascade 서브포켓 실거래 상세 현황 브리핑 (/cascade, /포켓2 명령어)"""
    try:
        from utils.telegram_briefing import build_cascade_briefing
        msg = build_cascade_briefing()
        send_telegram_alert(msg)
        print(f"[{time.strftime('%H:%M:%S')}] [BRIEFING] 텔레그램 Cascade 브리핑 발송 완료.")
    except Exception as e:
        send_telegram_alert(f"⚠️ Cascade 브리핑 생성 오류: {e}")


def send_telegram_balance_briefing():
    """전 계좌 자산 및 잔고 총정리 브리핑 (/잔고 명령어)"""
    try:
        from utils.telegram_briefing import build_balance_briefing
        msg = build_balance_briefing()
        send_telegram_alert(msg)
        print(f"[{time.strftime('%H:%M:%S')}] [BRIEFING] 텔레그램 잔고 브리핑 발송 완료.")
    except Exception as e:
        send_telegram_alert(f"⚠️ 잔고 브리핑 생성 오류: {e}")


def send_telegram_recent_trades():
    """최근 체결 내역 통합 조회 브리핑 (/체결 명령어)"""
    try:
        from utils.telegram_briefing import build_recent_trades_briefing
        msg = build_recent_trades_briefing()
        send_telegram_alert(msg)
        print(f"[{time.strftime('%H:%M:%S')}] [BRIEFING] 텔레그램 최근 체결 브리핑 발송 완료.")
    except Exception as e:
        send_telegram_alert(f"⚠️ 체결 내역 조회 오류: {e}")


def send_telegram_help_briefing():
    """텔레그램 컨트롤 명령어 도움말 안내 (/도움말 명령어)"""
    try:
        from utils.telegram_briefing import build_help_briefing
        msg = build_help_briefing()
        send_telegram_alert(msg)
        print(f"[{time.strftime('%H:%M:%S')}] [BRIEFING] 텔레그램 도움말 발송 완료.")
    except Exception as e:
        send_telegram_alert(f"⚠️ 도움말 생성 오류: {e}")


def send_telegram_g60_briefing(filter_market: Optional[str] = None):
    """
    G60 서브포켓 스쿼드 그리드 3종(이더리움 · 솔라나 · 리플) 실시간 포지션 및 매매 현황을 텔레그램으로 브리핑 (/60 명령어)
    """
    try:
        from utils.pocket_api import UpbitPocketClient

        pocket_client = UpbitPocketClient()
        try:
            krw_bal = pocket_client.get_balance("KRW")
        except Exception:
            krw_bal = 0.0

        pocket_db = os.path.join(BASE_DIR, "pocket_trade_history.db")
        markets_cfg = [
            {"market": "KRW-ETH", "ticker": "ETH", "name": "이더리움", "icon": "💎"},
            {"market": "KRW-SOL", "ticker": "SOL", "name": "솔라나", "icon": "🟣"},
            {"market": "KRW-XRP", "ticker": "XRP", "name": "리플", "icon": "🌊"}
        ]
        if filter_market:
            filter_up = filter_market.upper()
            if not filter_up.startswith("KRW-"):
                filter_up = f"KRW-{filter_up}"
            matched = [m for m in markets_cfg if m["market"] == filter_up]
            if matched:
                markets_cfg = matched

        # 최근 체결 내역 조회
        recent_trades = {}
        if os.path.exists(pocket_db):
            try:
                conn = sqlite3.connect(pocket_db, timeout=2.0)
                conn.row_factory = sqlite3.Row
                c = conn.cursor()
                for m in markets_cfg:
                    c.execute(
                        "SELECT * FROM pocket_trades WHERE market = ? ORDER BY id DESC LIMIT 1",
                        (m["market"],)
                    )
                    row = c.fetchone()
                    if row:
                        recent_trades[m["market"]] = dict(row)
                conn.close()
            except Exception as e_db:
                print(f"[WARN] G60 DB 조회 실패: {e_db}")

        tot_budget = 300_000.0 * len(markets_cfg)
        tot_pnl = 0.0
        tot_fees = 0.0
        tot_wins = 0
        tot_trades = 0
        tot_cost = 0.0
        tot_eval = 0.0

        market_blocks = []
        for m in markets_cfg:
            m_key = m["market"]
            ticker = m["ticker"]
            state_file = os.path.join(BASE_DIR, f"pocket_grid_state_{m_key.replace('-', '_')}.json")
            state = {}
            if os.path.exists(state_file):
                try:
                    with open(state_file, "r", encoding="utf-8") as f:
                        state = json.load(f)
                except Exception:
                    pass

            curr_p = pyupbit.get_current_price(m_key) or 0.0
            base_p = float(state.get("base_price", 0.0) or 0.0)
            pnl = float(state.get("realized_pnl", 0.0) or 0.0)
            fees = float(state.get("total_fees", 0.0) or 0.0)
            wins = int(state.get("win_count", 0) or 0)
            trades = int(state.get("total_trades", 0) or 0)
            active_grids = state.get("active_grids", [])

            vol = sum(float(g.get("vol", 0.0) or 0.0) for g in active_grids)
            cost = sum(float(g.get("cost", 0.0) or 0.0) for g in active_grids)
            eval_val = vol * curr_p
            unreal_pnl = eval_val - cost
            unreal_pct = (unreal_pnl / cost * 100.0) if cost > 0 else 0.0
            dist_base = ((curr_p - base_p) / base_p * 100.0) if base_p > 0 else 0.0

            tot_pnl += pnl
            tot_fees += fees
            tot_wins += wins
            tot_trades += trades
            tot_cost += cost
            tot_eval += eval_val

            curr_p_str = f"{curr_p:,.0f}원" if curr_p >= 100 else f"{curr_p:,.2f}원"
            base_p_str = f"{base_p:,.0f}원" if base_p >= 100 else f"{base_p:,.2f}원"

            b_lines = [
                f"{m['icon']} <b>{m['name']} ({ticker})</b>",
                f"• 현재가: <b>{curr_p_str}</b> (기준가 {base_p_str} | {dist_base:+.2f}%)",
                f"• 보유 그리드: <b>{len(active_grids)}/30단</b> ({vol:.4f} {ticker})",
                f"• 투입원가: {cost:,.0f}원 | 평가금액: {eval_val:,.0f}원 ({unreal_pnl:+,.0f}원, {unreal_pct:+.2f}%)",
                f"• 누적 실현손익: <b>{pnl:+,.1f}원</b> ({wins}회 익절 | 수수료 {fees:,.1f}원)"
            ]

            # 최근 체결 내역
            r_tr = recent_trades.get(m_key)
            if r_tr:
                side_tag = "🔵 매수" if r_tr["side"] == "bid" else "🟢 익절"
                tr_time = r_tr["timestamp"][11:19]
                if r_tr["side"] == "ask":
                    b_lines.append(f"• 최근 체결: {side_tag} Lvl {r_tr['level']} (손익: {r_tr['pnl']:+,.1f}원 | {tr_time})")
                else:
                    tr_p = float(r_tr['price'])
                    tr_p_str = f"{tr_p:,.0f}원" if tr_p >= 100 else f"{tr_p:,.2f}원"
                    b_lines.append(f"• 최근 체결: {side_tag} Lvl {r_tr['level']} ({tr_p_str} | {tr_time})")

            market_blocks.append("\n".join(b_lines))

        tot_unreal = tot_eval - tot_cost
        tot_unreal_pct = (tot_unreal / tot_cost * 100.0) if tot_cost > 0 else 0.0

        header_lines = [
            "⚡ <b>[G60 서브포켓 스쿼드 그리드 실거래 현황]</b>",
            f"• 서브포켓 예수금: <b>{krw_bal:,.0f}원</b>",
            f"• 총 배정예산: <b>{tot_budget:,.0f}원</b> (마켓당 30만 원)",
            f"• 총 실현손익: <b>{tot_pnl:+,.1f}원</b> ({tot_wins}회 익절 | 수수료 {tot_fees:,.1f}원)",
            f"• 총 투입원가: <b>{tot_cost:,.0f}원</b> | 평가손익: <b>{tot_unreal:+,.0f}원</b> ({tot_unreal_pct:+.2f}%)",
            ""
        ]

        full_msg = "\n".join(header_lines) + "\n\n".join(market_blocks)
        send_telegram_alert(full_msg.strip())
        print(f"[{time.strftime('%H:%M:%S')}] [G60] 텔레그램 G60 현황 브리핑 발송 완료.")
    except Exception as e:
        print(f"[WARN] 텔레그램 G60 현황 브리핑 오류: {e}")
        send_telegram_alert(f"⚠️ G60 브리핑 생성 오류: {e}")


def telegram_hourly_briefing_worker():
    """
    지정된 정기 시각(08:30, 18:00 KST, 하루 2회)에
    계좌 및 전략 현황 브리핑을 텔레그램으로 자동 발송하는 백그라운드 워커
    """
    schedule_str = ", ".join(TELEGRAM_BRIEFING_SCHEDULE)
    print(f"[INFO] 텔레그램 정기 현황 브리핑 워커 가동 (KST 하루 {len(TELEGRAM_BRIEFING_SCHEDULE)}회: {schedule_str})")
    last_sent_slot = None

    while True:
        try:
            kst_now = datetime.utcnow() + timedelta(hours=9)
            hm_str = kst_now.strftime("%H:%M")
            today_str = kst_now.strftime("%Y-%m-%d")

            if hm_str in TELEGRAM_BRIEFING_SCHEDULE:
                slot_key = f"{today_str}_{hm_str}"
                if last_sent_slot != slot_key:
                    send_telegram_status_briefing()
                    last_sent_slot = slot_key
        except Exception as e:
            print(f"[WARN] 텔레그램 브리핑 루프 예외: {e}")

        time.sleep(15)


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

                        if not text:
                            continue
                        cmd = text.lower()

                        if cmd in ["/status", "/상태", "상태", "/현황", "현황", "/요약", "요약", "/check", "/all"]:
                            send_telegram_status_briefing()
                        elif cmd in ["/메인", "메인", "/main", "main", "/메인봇", "메인봇"]:
                            send_telegram_main_bot_briefing()
                        elif cmd in ["/cascade", "cascade", "/포켓2", "포켓2", "/pocket2", "pocket2", "/캐스케이드", "캐스케이드", "/c2", "c2"]:
                            send_telegram_cascade_briefing()
                        elif cmd in ["/잔고", "잔고", "/balance", "balance", "/자산", "자산", "/예수금", "예수금"]:
                            send_telegram_balance_briefing()
                        elif cmd in ["/체결", "체결", "/체결내역", "체결내역", "/trades", "trades", "/최근체결", "최근체결"]:
                            send_telegram_recent_trades()
                        elif (
                            cmd in ["/60", "60", "/g60", "g60", "/포켓", "포켓", "/서브포켓", "서브포켓"] or
                            cmd.startswith("/60 ") or cmd.startswith("60 ") or
                            cmd.startswith("/g60 ") or cmd.startswith("g60 ")
                        ):
                            filter_m = None
                            parts = text.strip().split()
                            if len(parts) >= 2:
                                filter_m = parts[1].strip()
                            send_telegram_g60_briefing(filter_market=filter_m)
                        elif cmd in ["/start", "/help", "help", "/도움말", "도움말", "/명령어", "명령어"]:
                            send_telegram_help_briefing()
                        elif cmd in ["/tqqq", "tqqq", "/미국주식", "미국주식", "/주식", "주식"]:
                            try:
                                import subprocess
                                tqqq_python = "/Users/xyz/Documents/TQQQ/.venv/bin/python"
                                tqqq_helper = "/Users/xyz/Documents/TQQQ/NH/TelegramHelper.py"
                                out = subprocess.check_output([tqqq_python, tqqq_helper], timeout=15)
                                msg = out.decode("utf-8").strip()
                                send_telegram_alert(msg)
                            except Exception as te:
                                send_telegram_alert(f"⚠️ TQQQ 브리핑 생성 실패: {te}")
                        elif cmd in ["/국장", "국장", "/국내주식", "국내주식"]:
                            try:
                                import subprocess
                                kr_python = "/Users/xyz/Documents/TQQQ/.venv/bin/python"
                                kr_bot = "/Users/xyz/Documents/TQQQ/NH/KR_Stock_Sell_Bot.py"
                                out = subprocess.check_output([kr_python, kr_bot], timeout=25)
                                msg = out.decode("utf-8").strip()
                                if "📊" in msg:
                                    msg = msg[msg.find("📊"):]
                                send_telegram_alert(msg)
                            except Exception as ke:
                                send_telegram_alert(f"⚠️ 국내주식 브리핑 생성 실패: {ke}")
                        elif (
                            cmd in ["/재기동", "재기동", "/restart", "restart", "/reboot", "reboot", "/재시작", "재시작"] or
                            cmd.startswith("/재기동 ") or cmd.startswith("재기동 ") or
                            cmd.startswith("/restart ") or cmd.startswith("restart ") or
                            cmd.startswith("/reboot ") or cmd.startswith("reboot ")
                        ):
                            try:
                                from utils.process_manager import handle_restart_command
                                handle_restart_command(
                                    text=text,
                                    token=TOKEN,
                                    chat_id=CHAT_ID,
                                    offset=offset,
                                    send_alert_fn=send_telegram_alert
                                )
                            except Exception as re:
                                send_telegram_alert(f"⚠️ 재기동 명령 처리 실패: {re}")
                        elif cmd in ["/전략", "/전략검토", "전략", "전략검토"]:
                            send_telegram_alert("🧭 <b>[전략 봇]</b> 최근 시장 변동성 및 백테스트 최적화 탐색을 시작합니다. (약 10~15초 소요)")
                            try:
                                from strategy.strategy_advisor import run_daily_strategy_review
                                run_daily_strategy_review(send_telegram=True)
                            except Exception as se:
                                send_telegram_alert(f"⚠️ 전략 검토 실행 실패: {se}")
                        elif cmd.startswith("/전략승인") or cmd.startswith("전략승인") or cmd.startswith("/approve") or cmd.startswith("approve"):
                            parts = text.split()
                            if len(parts) >= 2:
                                target_market = parts[1].strip().upper()
                                try:
                                    from strategy.strategy_advisor import apply_strategy_approval
                                    ok, res_msg = apply_strategy_approval(target_market)
                                    send_telegram_alert(res_msg)
                                except Exception as ae:
                                    send_telegram_alert(f"⚠️ 전략 승인 적용 실패: {ae}")
                            else:
                                send_telegram_alert("⚠️ 사용법: <code>/전략승인 SOL</code> 또는 <code>/전략승인 ETH</code>")
                        elif cmd in ["/감사", "/audit", "감사"]:
                            send_telegram_alert("🛡️ <b>[감사 봇]</b> 실시간 시스템 무결성 정밀 감사를 실행합니다...")
                            try:
                                from utils.system_auditor import run_full_system_audit
                                run_full_system_audit(auto_heal_enabled=True, send_telegram=True, audit_label="텔레그램 수동 감사")
                            except Exception as ae:
                                send_telegram_alert(f"⚠️ 시스템 감사 실행 실패: {ae}")
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
    from strategy.btc_accumulator import (
        execute_btc_tiered_buy_if_due,
        check_btc_tiered_status,
        sync_upbit_btc_recurring_buys
    )
    market_process_lock = acquire_market_process_lock(market)
    if market_process_lock is None:
        print(f"[{market}] [CRITICAL] 동일 마켓 봇이 이미 실행 중입니다. 중복 주문 방지를 위해 이번 실행을 중단합니다.")
        return

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

    # 시작 시 업비트 모으기 체결 내역 동기화 (원장 및 평단가 갱신)
    try:
        sync_res = sync_upbit_btc_recurring_buys(upbit)
        if sync_res.get("synced_count", 0) > 0:
            print(f"[{market}] 🔄 업비트 모으기 체결 {sync_res['synced_count']}건 신규 동기화 완료 (누적 {sync_res['total_dca_buys_count']}회, 적립평단: {sync_res['dca_avg_price']:,.0f}원)")
    except Exception as e:
        print(f"[{market}] 업비트 모으기 초기 동기화 오류: {e}")

    # 시작 시 현재 상태 점검 및 출력
    try:
        stat = check_btc_tiered_status(upbit)
        dca_text = f" | 적립평단: {stat['dca_avg_price']:,.0f}원 ({stat['total_dca_buys_count']}회차)" if stat.get('dca_avg_price', 0) > 0 else ""
        print(f"[{market}] 현재가: {stat['current_price']:,.0f}원 | 200일선: {stat['ma200']:,.0f}원 | 계좌평단: {stat['account_avg_price']:,.0f}원{dca_text}")
        print(f"[{market}] 현재 판정: {stat['tier_name']} ({stat['tier_reason']}) | 매수 예정액: {stat['buy_krw']:,}원")
    except Exception as e:
        print(f"[{market}] 초기 상태 조회 오류: {e}")

    send_telegram_alert(f"🟡 <b>[비트코인 모으기 봇 가동]</b> {market}\n• 무손절 계층형 적립 (매일 08:55 봇 스마트 가중 + 15:05 업비트)")

    last_heartbeat_hour = None
    last_sync_minute = None

    while True:
        try:
            # 1. 08:50~08:58 종가 매수 검사 및 실행
            execute_btc_tiered_buy_if_due(upbit)

            # 2. 주기적 업비트 앱 모으기 체결 동기화 (매 10분마다 실행)
            kst_now = datetime.utcnow() + timedelta(hours=9)
            if kst_now.minute in (0, 10, 20, 30, 40, 50) and last_sync_minute != kst_now.minute:
                last_sync_minute = kst_now.minute
                try:
                    sync_res = sync_upbit_btc_recurring_buys(upbit)
                    if sync_res.get("synced_count", 0) > 0:
                        print(f"[{kst_now.strftime('%H:%M:%S')}] [{market}] 🔄 업비트 모으기 체결 {sync_res['synced_count']}건 신규 동기화 (적립평단: {sync_res['dca_avg_price']:,.0f}원)")
                except Exception as e:
                    print(f"[{market}] 업비트 모으기 주기적 동기화 오류: {e}")

            # 3. 정시 하트비트 상태 출력
            if kst_now.minute == 0 and last_heartbeat_hour != kst_now.hour:
                last_heartbeat_hour = kst_now.hour
                stat = check_btc_tiered_status(upbit)
                dca_info = f" | 적립평단: {stat['dca_avg_price']:,.0f}원 ({stat['total_dca_buys_count']}회차)" if stat.get('dca_avg_price', 0) > 0 else ""
                print(f"[{kst_now.strftime('%H:%M:%S')}] [{market}] 💓 하트비트 | 현재가: {stat['current_price']:,.0f}원{dca_info} | {stat['tier_name']}")

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


MAIN_GLOBAL_LOCK_FILE = None


def acquire_main_global_lock() -> bool:
    """main.py 전체의 단일 실행을 보장하는 글로벌 파일 락입니다."""
    global MAIN_GLOBAL_LOCK_FILE
    lock_path = os.path.join(tempfile.gettempdir(), "autotrade_main_global.lock")
    try:
        MAIN_GLOBAL_LOCK_FILE = open(lock_path, "w")
        fcntl.flock(MAIN_GLOBAL_LOCK_FILE.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        MAIN_GLOBAL_LOCK_FILE.write(f"{os.getpid()}\n")
        MAIN_GLOBAL_LOCK_FILE.flush()
        return True
    except (BlockingIOError, OSError):
        if MAIN_GLOBAL_LOCK_FILE:
            try:
                MAIN_GLOBAL_LOCK_FILE.close()
            except Exception:
                pass
            MAIN_GLOBAL_LOCK_FILE = None
        return False


# --- 엔트리포인트 (멀티 코인/전략 동시 실행 지원) ---
if __name__ == "__main__":
    if not acquire_main_global_lock():
        print(f"[{datetime.now().strftime('%H:%M:%S')}] [WARN] main.py 프로세스가 이미 실행 중입니다. 중복 기동을 방지하고 안전하게 종료합니다.")
        sys.exit(0)

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
    print(" -------------------------------------------------------------")
    print(" [STRATEGY ENGINE CONFIGURATION]")
    for m in target_markets:
        info = INVESTMENTS.get(m, {})
        margin = (get_profit_margin(m) - 1) * 100
        if is_adaptive_grid_enabled(m):
            eng_status = "🟢 ACTIVE (Adaptive Grid C-E)"
        elif is_adaptive_grid_shadow_enabled(m):
            eng_status = "🟡 SHADOW (Adaptive C-E 관측중)"
        else:
            eng_status = "⚪ LEGACY"
        print(f"   • {m}: 엔진 [{eng_status}] | 투자배정 {info.get('total', 0):,}원 | 1Unit {info.get('unit', 0):,}원 | 기본 익절 +{margin:.2f}%")
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

    try:
        send_telegram_alert(
            f"🚀 <b>[AutoBot 실거래 엔진 가동]</b> PID: <code>{os.getpid()}</code>\n"
            f"• 대상 마켓: {', '.join(target_markets)}\n"
            f"• 텔레그램 리스너: 활성화 (<code>/상태</code>, <code>/재기동</code> 지원)\n"
            f"• 정상 동기화 완료"
        )
    except Exception as e:
        print(f"[WARN] 텔레그램 가동 알림 전송 실패: {e}")

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
