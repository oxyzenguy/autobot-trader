import os
import json
import time
from datetime import datetime
from typing import Dict, Any, Optional
import pyupbit
from config import get_upbit_client

BASE_FILE = "real_account_base.json"


def init_or_load_base_snapshot() -> Dict[str, Any]:
    """업비트 실제 잔고의 시작 기준점을 로드하거나 없으면 현재 잔고로 초기화 생성"""
    if os.path.exists(BASE_FILE):
        try:
            with open(BASE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass

    # 새로 생성
    data = {
        "snapshot_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "coins": {}
    }
    try:
        upbit = get_upbit_client()
        balances = upbit.get_balances()
        for b in balances:
            cur = b.get("currency")
            if cur in ["SOL", "ETH"]:
                bal = float(b.get("balance", 0.0))
                avg_p = float(b.get("avg_buy_price", 0.0))
                market = f"KRW-{cur}"
                cur_p = pyupbit.get_current_price(market) or avg_p
                total_cost = bal * avg_p
                eval_amount = bal * cur_p
                pnl = eval_amount - total_cost
                pnl_pct = (pnl / total_cost * 100.0) if total_cost > 0 else 0.0
                data["coins"][market] = {
                    "currency": cur,
                    "base_balance": bal,
                    "base_avg_price": avg_p,
                    "base_total_cost": total_cost,
                    "base_price": cur_p,
                    "base_eval_amount": eval_amount,
                    "base_pnl_pct": pnl_pct
                }
        with open(BASE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"[WARN] 업비트 실계좌 베이스 스냅샷 생성 실패: {e}")

    return data


def get_real_coin_status(market: str) -> Dict[str, Any]:
    """
    업비트 실제 계좌의 현재 코인모으기 잔고 및 성과를 조회하고,
    시작 시점(Base) 대비 성장 추이를 계산합니다.
    """
    ticker = market.split("-")[1]
    base_data = init_or_load_base_snapshot()
    base_coin = base_data.get("coins", {}).get(market, {
        "base_balance": 0.0,
        "base_avg_price": 0.0,
        "base_total_cost": 0.0,
        "base_price": 0.0,
        "base_eval_amount": 0.0
    })

    current_price = pyupbit.get_current_price(market) or base_coin.get("base_price", 0.0)

    cur_bal = 0.0
    cur_avg_p = 0.0
    try:
        upbit = get_upbit_client()
        balance_info = upbit.get_balance(ticker, verbose=True)
        if balance_info and 'avg_buy_price' in balance_info:
            cur_bal = float(balance_info.get('balance', 0.0))
            cur_avg_p = float(balance_info.get('avg_buy_price', 0.0))
    except Exception as e:
        cur_bal = base_coin.get("base_balance", 0.0)
        cur_avg_p = base_coin.get("base_avg_price", 0.0)

    total_cost = cur_bal * cur_avg_p
    current_eval = cur_bal * current_price
    pnl = current_eval - total_cost
    pnl_pct = (pnl / total_cost * 100.0) if total_cost > 0 else 0.0

    # 시작 기준점 대비 코인모으기 순수익률 (기점 이후 성과 추적)
    base_eval = base_coin.get("base_eval_amount", current_eval)
    growth_since_base = current_eval - base_eval
    growth_pct_since_base = (growth_since_base / base_eval * 100.0) if base_eval > 0 else 0.0

    return {
        "market": market,
        "ticker": ticker,
        "current_price": current_price,
        # 현재 전체 실계좌 상태
        "current_balance": cur_bal,
        "current_avg_price": cur_avg_p,
        "total_cost": total_cost,
        "current_eval": current_eval,
        "total_pnl": pnl,
        "total_pnl_pct": pnl_pct,
        # 기점(2026-09-15) 기준점 정보
        "base_balance": base_coin.get("base_balance", cur_bal),
        "base_avg_price": base_coin.get("base_avg_price", cur_avg_p),
        "base_eval_amount": base_eval,
        "growth_since_base": growth_since_base,
        "growth_pct_since_base": growth_pct_since_base,
        "snapshot_time": base_data.get("snapshot_time", "-")
    }
