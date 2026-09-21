import os
import json
import sqlite3
import pandas as pd
import numpy as np
import pyupbit
from datetime import datetime
from typing import Dict, Any, List, Optional
from config import get_upbit_client, INVESTMENTS, MIN_KRW_ALERT_THRESHOLD, get_profit_margin, PROTECTED_BALANCES
from utils.db_logger import DB_PATH, init_db, log_total_account_snapshot, log_strategy_snapshot, get_strategy_history

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ACCOUNT_BASE_FILE = os.path.join(BASE_DIR, "real_account_base.json")


def safe_get_current_price(market: str, fallback: float = 0.0) -> float:
    """원화 마켓에 없거나 시세 조회 실패 시 예외 없이 fallback 반환"""
    try:
        p = pyupbit.get_current_price(market)
        return float(p) if p is not None else fallback
    except Exception:
        return fallback


def init_or_load_account_base() -> Dict[str, Any]:
    """실전 계좌의 기준점(Base Snapshot)을 로드하거나 없으면 현재 자산으로 초기화 생성"""
    if os.path.exists(ACCOUNT_BASE_FILE):
        try:
            with open(ACCOUNT_BASE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if "total_base_equity" in data:
                    return data
        except Exception:
            pass

    # 신규 전체 기준점 생성
    try:
        upbit = get_upbit_client()
        raw_b = upbit.get_balances()
        balances = raw_b if isinstance(raw_b, list) else []
        krw_bal = 0.0
        total_eval = 0.0
        coins_info = {}

        for b in balances:
            if not isinstance(b, dict):
                continue
            cur = b.get("currency")
            bal = float(b.get("balance", 0.0))
            locked = float(b.get("locked", 0.0))
            tot_bal = bal + locked
            avg_p = float(b.get("avg_buy_price", 0.0))

            if cur == "KRW":
                krw_bal = tot_bal
                total_eval += tot_bal
            else:
                market = f"KRW-{cur}"
                cur_p = safe_get_current_price(market, fallback=avg_p)
                eval_p = tot_bal * cur_p
                total_eval += eval_p
                coins_info[market] = {
                    "currency": cur,
                    "base_balance": tot_bal,
                    "base_avg_price": avg_p,
                    "base_price": cur_p,
                    "base_eval_amount": eval_p
                }

        base_data = {
            "snapshot_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "total_base_equity": total_eval,
            "base_krw": krw_bal,
            "coins": coins_info
        }

        with open(ACCOUNT_BASE_FILE, "w", encoding="utf-8") as f:
            json.dump(base_data, f, indent=2, ensure_ascii=False)
        return base_data
    except Exception as e:
        print(f"[WARN] 계좌 베이스 스냅샷 생성 실패: {e}")
        return {
            "snapshot_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "total_base_equity": 0.0,
            "base_krw": 0.0,
            "coins": {}
        }


def get_total_account_summary() -> Dict[str, Any]:
    """
    업비트 전체 계좌 현황 조회:
    - 총 평가금액 (KRW + 모든 코인 평가액)
    - 총 매수원가 (코인 매수금액 + KRW)
    - 총 평가손익 및 전체 계좌 수익률
    - 💵 예수금 (주문가능 KRW) 및 10만원 미만 경고 플래그
    - 보유 코인 목록 (비중, 수익률 등)
    """
    init_db()
    balances = []
    is_api_key_missing = False
    api_error_message = ""
    try:
        upbit = get_upbit_client()
        raw_balances = upbit.get_balances()
        if isinstance(raw_balances, list):
            balances = raw_balances
        elif isinstance(raw_balances, dict):
            # 업비트 API에서 에러 응답(예: IP 제한, 인증키 불일치 등)을 반환한 경우
            is_api_key_missing = True
            err_dict = raw_balances.get("error", {})
            err_name = err_dict.get("name", "")
            err_msg = err_dict.get("message", "")
            api_error_message = f"{err_name}: {err_msg}" if err_name or err_msg else str(raw_balances)
            print(f"[WARN] 업비트 API 오류 응답: {api_error_message}")
        else:
            is_api_key_missing = True
            api_error_message = f"응답 형식 오류 ({type(raw_balances)})"
            print(f"[WARN] 업비트 응답 형식 비정상: {raw_balances}")
    except Exception as e:
        is_api_key_missing = True
        api_error_message = str(e)
        print(f"[WARN] 업비트 클라이언트 초기화 실패 (API 키 확인 필요): {e}")

    krw_balance = 0.0
    krw_locked = 0.0
    coins_list = []
    total_coin_eval = 0.0
    total_coin_cost = 0.0

    # 1. 각 통화별 자산 계산
    for b in balances:
        if not isinstance(b, dict):
            continue
        cur = b.get("currency")
        bal = float(b.get("balance", 0.0))
        locked = float(b.get("locked", 0.0))
        tot_bal = bal + locked
        avg_p = float(b.get("avg_buy_price", 0.0))

        if cur == "KRW":
            krw_balance = bal
            krw_locked = locked
        else:
            if tot_bal <= 0:
                continue
            market = f"KRW-{cur}"
            cur_p = safe_get_current_price(market, fallback=avg_p)
            eval_amount = tot_bal * cur_p
            cost_amount = tot_bal * avg_p
            pnl = eval_amount - cost_amount
            pnl_pct = (pnl / cost_amount * 100.0) if cost_amount > 0 else 0.0

            total_coin_eval += eval_amount
            total_coin_cost += cost_amount

            coins_list.append({
                "currency": cur,
                "market": market,
                "balance": bal,
                "locked": locked,
                "total_balance": tot_bal,
                "avg_buy_price": avg_p,
                "current_price": cur_p,
                "eval_amount": eval_amount,
                "cost_amount": cost_amount,
                "pnl": pnl,
                "pnl_pct": pnl_pct
            })

    total_krw = krw_balance + krw_locked
    total_equity = total_krw + total_coin_eval
    total_invested = total_krw + total_coin_cost
    unrealized_pnl = total_coin_eval - total_coin_cost
    coin_pnl_pct = (unrealized_pnl / total_coin_cost * 100.0) if total_coin_cost > 0 else 0.0

    # 비중(weight) 계산
    for c in coins_list:
        c["weight_pct"] = (c["eval_amount"] / total_equity * 100.0) if total_equity > 0 else 0.0
    coins_list.sort(key=lambda x: x["eval_amount"], reverse=True)

    # 2. 기준점(Base) 대비 계좌 순수익률
    base_data = init_or_load_account_base()
    base_equity = base_data.get("total_base_equity", total_equity)
    if base_equity <= 0:
        base_equity = total_equity

    growth_amount = total_equity - base_equity
    growth_pct = (growth_amount / base_equity * 100.0) if base_equity > 0 else 0.0

    # 3. 예수금 10만원 이하 경고 플래그 (API 키 정상 연동 시에만)
    is_krw_warning = (krw_balance < MIN_KRW_ALERT_THRESHOLD) and (not is_api_key_missing)

    # 4. DB에 전체 계좌 스냅샷 기록 (시계열) - API 키가 정상일 때만 기록
    if not is_api_key_missing:
        try:
            log_total_account_snapshot(
                total_equity=total_equity,
                total_cost=total_invested,
                unrealized_pnl=unrealized_pnl,
                return_pct=growth_pct,
                krw_balance=krw_balance,
                coin_eval=total_coin_eval,
                coins_json=json.dumps([{"currency": c["currency"], "eval": c["eval_amount"]} for c in coins_list])
            )
        except Exception:
            pass

    return {
        "total_equity": total_equity,
        "total_invested": total_invested,
        "total_coin_eval": total_coin_eval,
        "total_coin_cost": total_coin_cost,
        "unrealized_pnl": unrealized_pnl,
        "coin_pnl_pct": coin_pnl_pct,
        "krw_balance": krw_balance,
        "krw_locked": krw_locked,
        "total_krw": total_krw,
        "is_krw_warning": is_krw_warning,
        "min_krw_threshold": MIN_KRW_ALERT_THRESHOLD,
        "base_equity": base_equity,
        "growth_amount": growth_amount,
        "growth_pct": growth_pct,
        "base_snapshot_time": base_data.get("snapshot_time", "-"),
        "coins": coins_list,
        "is_api_key_missing": is_api_key_missing,
        "api_error_message": api_error_message
    }


def get_strategy_performance(market: str, strategy_name: str = "마틴게일 2x 배수 물타기") -> Dict[str, Any]:
    """
    특정 마켓/전략의 실전 성과 분석:
    - 현재 포지션 (보유량, 평단가, 평가액, 미실현손익)
    - 미체결 주문 목록
    - 실거래 체결 통계 (실현손익, 승률, 손익비, 총 거래수, MDD)
    """
    init_db()
    ticker = market.split("-")[1]
    upbit = None
    try:
        upbit = get_upbit_client()
    except Exception:
        pass

    # 1. 현재 포지션 조회
    cur_bal = 0.0
    locked_bal = 0.0
    avg_price = 0.0
    current_price = safe_get_current_price(market, fallback=0.0)

    if upbit is not None:
        try:
            balance_info = upbit.get_balance(ticker, verbose=True)
            if isinstance(balance_info, dict) and 'avg_buy_price' in balance_info and 'error' not in balance_info:
                cur_bal = float(balance_info.get('balance', 0.0))
                locked_bal = float(balance_info.get('locked', 0.0))
                avg_price = float(balance_info.get('avg_buy_price', 0.0))
        except Exception as e:
            print(f"[{market}] 잔고 조회 실패: {e}")

    total_coin_balance = cur_bal + locked_bal
    eval_amount = total_coin_balance * current_price
    cost_amount = total_coin_balance * avg_price
    unrealized_pnl = eval_amount - cost_amount
    unrealized_pnl_pct = (unrealized_pnl / cost_amount * 100.0) if cost_amount > 0 else 0.0

    # 2. 미체결 주문 목록 조회
    open_orders = []
    if upbit is not None:
        try:
            orders = upbit.get_order(market, state="wait")
            if isinstance(orders, list):
                for o in orders:
                    if isinstance(o, dict) and "uuid" in o:
                        open_orders.append({
                            "uuid": o.get("uuid"),
                            "side": o.get("side"),
                            "price": float(o.get("price", 0.0)),
                            "volume": float(o.get("volume", 0.0)),
                            "created_at": o.get("created_at")
                        })
        except Exception as e:
            print(f"[{market}] 미체결 주문 조회 실패: {e}")

    # 2-1. 하이브리드 국면 및 런타임 상태 로드
    from strategy.hybrid_regime import get_hybrid_regime_and_signals
    regime_info = get_hybrid_regime_and_signals(market)

    state_file = os.path.join(BASE_DIR, f"real_strategy_state_{market.replace('-', '_')}.json")
    runtime_state = {}
    if os.path.exists(state_file):
        try:
            with open(state_file, "r", encoding="utf-8") as f:
                runtime_state = json.load(f)
        except Exception:
            pass

    # 2-2. 봇 전용 독립 포지션 계산 (기존 보유 자산 완전 격리/보호)
    protected_quantity = float(PROTECTED_BALANCES.get(market, 0.0))
    bot_quantity = float(runtime_state.get("bot_quantity", 0.0))
    bot_avg_price = float(runtime_state.get("bot_avg_price", 0.0))
    if current_price <= 0:
        current_price = bot_avg_price if bot_avg_price > 0 else avg_price
    bot_eval = bot_quantity * current_price
    bot_cost = bot_quantity * bot_avg_price
    bot_unrealized_pnl = bot_eval - bot_cost
    bot_pnl_pct = (bot_unrealized_pnl / bot_cost * 100.0) if bot_cost > 0 else 0.0

    # 3. 실거래 체결 이력 조회 (trades 테이블)
    trades_df = pd.DataFrame()
    try:
        conn = sqlite3.connect(DB_PATH)
        query = """
            SELECT id, timestamp, ticker, market, side, action, price, volume, cost_or_revenue, pnl, cycle, strategy
            FROM trades
            WHERE market = ? OR ticker = ?
            ORDER BY timestamp DESC
        """
        trades_df = pd.read_sql_query(query, conn, params=(market, ticker))
        conn.close()
    except Exception as e:
        print(f"[{market}] 체결 이력 조회 오류: {e}")

    # 4. 퀀트 지표 계산 (실현손익, 승률, 손익비 등)
    realized_pnl = 0.0
    wins = 0
    losses = 0
    win_pnl_sum = 0.0
    loss_pnl_sum = 0.0
    total_trades = len(trades_df)
    completed_cycles = 0

    if not trades_df.empty:
        # sell 거래 및 pnl이 계산된 거래 분석
        sell_trades = trades_df[trades_df["side"] == "ask"]
        for _, t in sell_trades.iterrows():
            p = float(t.get("pnl") or 0.0)
            realized_pnl += p
            if p > 0:
                wins += 1
                win_pnl_sum += p
            elif p < 0:
                losses += 1
                loss_pnl_sum += abs(p)

        completed_cycles = len(sell_trades)

    total_closed = wins + losses
    win_rate = (wins / total_closed * 100.0) if total_closed > 0 else 0.0
    profit_factor = (win_pnl_sum / loss_pnl_sum) if loss_pnl_sum > 0 else (99.0 if win_pnl_sum > 0 else 1.0)
    avg_win = (win_pnl_sum / wins) if wins > 0 else 0.0
    avg_loss = (loss_pnl_sum / losses) if losses > 0 else 0.0

    # 5. 전략 배정 원금 및 독립 봇 전략 수익률
    strat_cfg = INVESTMENTS.get(market, {"total": 1_000_000, "unit": 10_000})
    initial_capital = float(strat_cfg.get("total", 1_000_000))
    unit_krw = float(strat_cfg.get("unit", 10_000))
    profit_margin = get_profit_margin(market)

    # 봇의 총 손익 = 누적 실현손익 + 봇 포지션 미실현손익 (기존 자산의 손익은 배제)
    total_strat_pnl = realized_pnl + bot_unrealized_pnl
    strategy_return_pct = (total_strat_pnl / initial_capital * 100.0) if initial_capital > 0 else 0.0

    # 시계열 자산 추이 (스냅샷 테이블 또는 trades 누적)
    snapshots_df = pd.DataFrame()
    try:
        conn = sqlite3.connect(DB_PATH)
        q_snap = """
            SELECT timestamp, total_equity, return_pct, krw_balance
            FROM total_account_snapshots
            ORDER BY timestamp ASC
        """
        snapshots_df = pd.read_sql_query(q_snap, conn)
        conn.close()
    except Exception:
        pass

    return {
        "market": market,
        "ticker": ticker,
        "strategy_name": strategy_name,
        "initial_capital": initial_capital,
        "unit_krw": unit_krw,
        "profit_margin": profit_margin,
        "current_price": current_price,
        "coin_balance": total_coin_balance,
        "avg_buy_price": avg_price,
        "cost_amount": cost_amount,
        "eval_amount": eval_amount,
        "unrealized_pnl": unrealized_pnl,
        "unrealized_pnl_pct": unrealized_pnl_pct,
        "realized_pnl": realized_pnl,
        "total_strat_pnl": total_strat_pnl,
        "strategy_return_pct": strategy_return_pct,
        "total_trades": total_trades,
        "completed_cycles": completed_cycles,
        "wins": wins,
        "losses": losses,
        "win_rate": win_rate,
        "profit_factor": profit_factor,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "open_orders": open_orders,
        "trades_df": trades_df,
        "snapshots_df": snapshots_df,
        "regime_info": regime_info,
        "runtime_state": runtime_state,
        "bot_quantity": bot_quantity,
        "bot_avg_price": bot_avg_price,
        "bot_eval": bot_eval,
        "bot_cost": bot_cost,
        "bot_unrealized_pnl": bot_unrealized_pnl,
        "bot_pnl_pct": bot_pnl_pct,
        "protected_quantity": protected_quantity,
        "account_total_coin_balance": total_coin_balance,
        "account_avg_price": avg_price
    }


def get_all_active_strategies() -> List[Dict[str, Any]]:
    """
    현재 적용된 2개 이상의 모든 전략 성과 정보를 반환합니다.
    (기본 설정된 INVESTMENTS 종목들 및 추가된 전략)
    """
    configured_markets = list(INVESTMENTS.keys()) if INVESTMENTS else ["KRW-SOL", "KRW-ETH"]
    active_list = []

    # 전략별 맞춤 이름: 하이브리드 국면전환 (마틴-매직스플릿 이중익절)
    strat_names = {
        "KRW-SOL": "솔라나(SOL) 하이브리드 (마틴-매직스플릿 이중익절)",
        "KRW-ETH": "이더리움(ETH) 하이브리드 (마틴-매직스플릿 이중익절)",
        "KRW-BTC": "비트코인(BTC) 5/20 MA 추세추종",
        "KRW-XRP": "리플(XRP) 마틴게일 물타기"
    }

    for market in configured_markets:
        name = strat_names.get(market, f"{market} 하이브리드 전략")
        perf = get_strategy_performance(market, strategy_name=name)
        active_list.append(perf)

    return active_list
