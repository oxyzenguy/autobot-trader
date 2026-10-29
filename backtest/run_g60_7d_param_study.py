#!/usr/bin/env python3
"""
========================================================================================
🔬 [G60 7-Day Parameter Study] Option A vs Option B Rigorous Backtest Simulation
========================================================================================
Dataset: 7 days of 1-minute candles (2026-10-22 19:43 ~ 2026-10-29 19:42 KST)
Assets: KRW-XRP, KRW-ETH, KRW-SOL
Capital per Coin: 300,000 KRW (Unit: 10,000 KRW, Max 30 levels)
Friction: Upbit Fee 0.05% each way + Slippage 0.02% each way (Round-trip ~0.14%)

Configurations tested:
1. Baseline (Current Live Settings):
   - XRP: Step 0.50%, TP 1.20% (Squad TP 1.20%)
   - ETH: Step 0.50%, TP 1.00% (Squad TP 1.00%)
   - SOL: Step 0.50%, TP 1.00% (Squad TP 1.00%)
2. Option A (Tighter TP / Rapid Capital Turnover):
   - A-1: Step 0.50%, TP 0.60% (Squad TP 0.60%)
   - A-2: Step 0.50%, TP 0.70% (Squad TP 0.70%)
   - A-3: Step 0.50%, TP 0.80% (Squad TP 0.80%)
3. Option B (Wider Step / Extended Drawdown Defense):
   - B-1: Step 0.80%, TP 1.00% (XRP 1.20%)
   - B-2: Step 1.00%, TP 1.00% (XRP 1.20%)
   - B-3: Step 0.80%, TP 0.80% (1:1 ratio)
   - B-4: Step 1.00%, TP 0.80%
========================================================================================
"""

import os
import sys
import math
import json
import numpy as np
import pandas as pd
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional, Tuple

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data" / "candles_1m_7d"

@dataclass
class SimulationConfig:
    name: str
    group: str
    step_pct: float
    profit_pct: float
    squad_tp_pct: float
    max_levels: int = 30
    unit_krw: float = 10_000.0
    budget_krw: float = 300_000.0
    squad_size: int = 5
    min_squad_tranches_for_tp: int = 2
    fee_rate: float = 0.0005     # 0.05% Upbit taker
    slippage: float = 0.0002     # 0.02% conservative slippage


def run_g60_simulation(df: pd.DataFrame, market: str, cfg: SimulationConfig) -> Dict[str, Any]:
    """
    Simulates G60 Pocket Grid Runner over high-resolution 1-minute bars.
    """
    cash = cfg.budget_krw
    unit_krw = cfg.unit_krw
    step_pct = cfg.step_pct
    profit_pct = cfg.profit_pct
    squad_tp_pct = cfg.squad_tp_pct
    max_levels = cfg.max_levels
    fee_rate = cfg.fee_rate
    slippage = cfg.slippage
    squad_size = cfg.squad_size
    min_squad_tranches = cfg.min_squad_tranches_for_tp

    base_p = 0.0
    active_grids: List[Dict[str, Any]] = []
    
    # Tracking
    trades: List[Dict[str, Any]] = []
    realized_pnl = 0.0
    total_fees = 0.0
    stat_buys = 0
    stat_sells_individual = 0
    stat_sells_squad = 0
    max_active_levels = 0
    peak_capital_used = 0.0
    
    hold_durations_min: List[float] = []
    
    equity_curve: List[float] = []
    peak_equity = cfg.budget_krw
    max_drawdown = 0.0

    n_bars = len(df)
    
    for i in range(n_bars):
        row = df.iloc[i]
        dt = row["dt"]
        open_p = float(row["opening_price"])
        high_p = float(row["high_price"])
        low_p = float(row["low_price"])
        close_p = float(row["trade_price"])

        # Determine intrabar order
        is_bullish = (close_p >= open_p)

        def check_sells(curr_high: float):
            nonlocal cash, realized_pnl, total_fees, stat_sells_individual, stat_sells_squad, active_grids, base_p
            if not active_grids:
                return

            exit_occurred = False
            
            # 1. Squad Basket Take-Profit
            squads_held = {}
            for g in active_grids:
                sq = g.get("squad", (g["lvl"] - 1) // squad_size + 1)
                squads_held.setdefault(sq, []).append(g)

            squads_to_exit = set()
            for sq, g_list in sorted(squads_held.items()):
                if len(g_list) >= min_squad_tranches:
                    sq_vol = sum(g["vol"] for g in g_list)
                    sq_cost = sum(g["cost"] for g in g_list)
                    if sq_vol > 0:
                        sq_vwap = sq_cost / sq_vol
                        sq_target = sq_vwap * (1.0 + squad_tp_pct)
                        if curr_high >= sq_target:
                            squads_to_exit.add(sq)

            if squads_to_exit:
                surviving = []
                for g in active_grids:
                    sq = g.get("squad", (g["lvl"] - 1) // squad_size + 1)
                    if sq in squads_to_exit:
                        g_list = squads_held[sq]
                        sq_vol = sum(x["vol"] for x in g_list)
                        sq_cost = sum(x["cost"] for x in g_list)
                        sq_vwap = sq_cost / sq_vol
                        sq_target = sq_vwap * (1.0 + squad_tp_pct)
                        
                        exit_p = sq_target * (1.0 - slippage)
                        gross_rev = exit_p * g["vol"]
                        sell_fee = gross_rev * fee_rate
                        net_rev = gross_rev - sell_fee
                        net_pnl = net_rev - g["cost"] - g["fee"]
                        
                        cash += net_rev
                        realized_pnl += net_pnl
                        total_fees += (g["fee"] + sell_fee)
                        stat_sells_squad += 1
                        exit_occurred = True
                        
                        # Calculate hold time
                        hold_min = i - g["bar_idx"]
                        hold_durations_min.append(hold_min)
                        
                        trades.append({
                            "dt": dt, "side": "ask", "type": f"SQUAD_{sq}_TP",
                            "lvl": g["lvl"], "price": exit_p, "vol": g["vol"],
                            "cost": g["cost"], "pnl": net_pnl, "hold_min": hold_min
                        })
                    else:
                        surviving.append(g)
                active_grids = surviving

            # 2. Individual Grid Take-Profit
            if active_grids:
                surviving = []
                for g in active_grids:
                    if curr_high >= g["tp_p"]:
                        exit_p = g["tp_p"] * (1.0 - slippage)
                        gross_rev = exit_p * g["vol"]
                        sell_fee = gross_rev * fee_rate
                        net_rev = gross_rev - sell_fee
                        net_pnl = net_rev - g["cost"] - g["fee"]
                        
                        cash += net_rev
                        realized_pnl += net_pnl
                        total_fees += (g["fee"] + sell_fee)
                        stat_sells_individual += 1
                        exit_occurred = True
                        
                        hold_min = i - g["bar_idx"]
                        hold_durations_min.append(hold_min)
                        
                        trades.append({
                            "dt": dt, "side": "ask", "type": f"INDIV_TP_LVL_{g['lvl']}",
                            "lvl": g["lvl"], "price": exit_p, "vol": g["vol"],
                            "cost": g["cost"], "pnl": net_pnl, "hold_min": hold_min
                        })
                    else:
                        surviving.append(g)
                active_grids = surviving

            # If all cleared, base_price resets
            if len(active_grids) == 0:
                base_p = 0.0

        def check_buys(curr_low: float, curr_open: float):
            nonlocal cash, stat_buys, active_grids, base_p, peak_capital_used, max_active_levels
            
            # Initial buy if empty
            if len(active_grids) == 0:
                base_p = curr_open
                buy_p = curr_open * (1.0 + slippage)
                buy_fee = unit_krw * fee_rate
                net_spend = unit_krw - buy_fee
                vol = net_spend / buy_p
                tp_p = buy_p * (1.0 + profit_pct)
                
                cash -= unit_krw
                stat_buys += 1
                active_grids.append({
                    "lvl": 1, "squad": 1, "buy_p": buy_p, "vol": vol,
                    "cost": unit_krw, "fee": buy_fee, "tp_p": tp_p, "bar_idx": i
                })
                trades.append({
                    "dt": dt, "side": "bid", "type": "BUY_LVL_1",
                    "lvl": 1, "price": buy_p, "vol": vol, "cost": unit_krw, "fee": buy_fee
                })

            # Subsequent dips
            existing_lvls = {g["lvl"] for g in active_grids}
            max_filled_lvl = max(existing_lvls) if existing_lvls else 0
            
            # Check sequentially if deeper levels were triggered
            while True:
                next_lvl = max_filled_lvl + 1
                if next_lvl > max_levels or next_lvl in existing_lvls:
                    break
                target_dip_p = base_p * (1.0 - (next_lvl - 1) * step_pct)
                if curr_low <= target_dip_p:
                    # Check capital constraint
                    curr_cost_sum = sum(g["cost"] for g in active_grids)
                    if curr_cost_sum + unit_krw > cfg.budget_krw or cash < unit_krw:
                        break  # Budget exhausted
                    
                    buy_p = target_dip_p * (1.0 + slippage)
                    buy_fee = unit_krw * fee_rate
                    net_spend = unit_krw - buy_fee
                    vol = net_spend / buy_p
                    tp_p = buy_p * (1.0 + profit_pct)
                    sq_id = (next_lvl - 1) // squad_size + 1
                    
                    cash -= unit_krw
                    stat_buys += 1
                    active_grids.append({
                        "lvl": next_lvl, "squad": sq_id, "buy_p": buy_p, "vol": vol,
                        "cost": unit_krw, "fee": buy_fee, "tp_p": tp_p, "bar_idx": i
                    })
                    existing_lvls.add(next_lvl)
                    max_filled_lvl = next_lvl
                    
                    trades.append({
                        "dt": dt, "side": "bid", "type": f"BUY_LVL_{next_lvl}",
                        "lvl": next_lvl, "price": buy_p, "vol": vol, "cost": unit_krw, "fee": buy_fee
                    })
                else:
                    break

            # Track peak utilization
            num_active = len(active_grids)
            if num_active > max_active_levels:
                max_active_levels = num_active
            active_cost = sum(g["cost"] for g in active_grids)
            if active_cost > peak_capital_used:
                peak_capital_used = active_cost

        # Execute in sequence based on candle polarity
        if is_bullish:
            check_buys(low_p, open_p)
            check_sells(high_p)
        else:
            check_sells(high_p)
            check_buys(low_p, open_p)

        # Mark-to-market valuation
        held_vol = sum(g["vol"] for g in active_grids)
        cur_equity = cash + (held_vol * close_p)
        equity_curve.append(cur_equity)
        if cur_equity > peak_equity:
            peak_equity = cur_equity
        dd = (peak_equity - cur_equity) / peak_equity * 100.0 if peak_equity > 0 else 0.0
        if dd > max_drawdown:
            max_drawdown = dd

    # Final valuation
    final_p = float(df["trade_price"].iloc[-1])
    ending_held_vol = sum(g["vol"] for g in active_grids)
    ending_held_cost = sum(g["cost"] for g in active_grids)
    ending_val = ending_held_vol * final_p
    ending_unrealized_pnl = ending_val - ending_held_cost
    ending_equity = cash + ending_val
    net_total_pnl = realized_pnl + ending_unrealized_pnl
    total_roi_pct = (ending_equity - cfg.budget_krw) / cfg.budget_krw * 100.0

    avg_hold_min = float(np.mean(hold_durations_min)) if hold_durations_min else 0.0
    total_wins = stat_sells_individual + stat_sells_squad

    return {
        "market": market,
        "config_name": cfg.name,
        "group": cfg.group,
        "step_pct": cfg.step_pct,
        "profit_pct": cfg.profit_pct,
        "squad_tp_pct": cfg.squad_tp_pct,
        "total_buys": stat_buys,
        "total_wins": total_wins,
        "indiv_wins": stat_sells_individual,
        "squad_wins": stat_sells_squad,
        "realized_pnl": realized_pnl,
        "total_fees": total_fees,
        "peak_capital_used": peak_capital_used,
        "max_active_levels": max_active_levels,
        "ending_levels": len(active_grids),
        "ending_held_cost": ending_held_cost,
        "ending_unrealized_pnl": ending_unrealized_pnl,
        "ending_equity": ending_equity,
        "net_total_pnl": net_total_pnl,
        "total_roi_pct": total_roi_pct,
        "max_drawdown_pct": max_drawdown,
        "avg_hold_min": avg_hold_min,
        "capital_turnover_ratio": (realized_pnl / peak_capital_used) if peak_capital_used > 0 else 0.0
    }


def main():
    markets = ["KRW-XRP", "KRW-ETH", "KRW-SOL"]
    
    # Load 7d data
    dfs = {}
    for m in markets:
        f = DATA_DIR / f"{m}_1m_7d.csv"
        df = pd.read_csv(f)
        # Align to start at 2026-10-22T19:43:00
        df = df[df["dt"] >= "2026-10-22T19:43:00"].reset_index(drop=True)
        dfs[m] = df
        print(f"Loaded {m}: {len(df):,} bars ({df['dt'].iloc[0]} ~ {df['dt'].iloc[-1]})")

    # Define test configs
    # 1. Baseline
    # 2. Option A (Tighter TP): TP 0.6%, TP 0.7%, TP 0.8%
    # 3. Option B (Wider Step): Step 0.8%, Step 1.0%
    # 4. Option C (Balanced): Step 0.8% / TP 0.8%, Step 1.0% / TP 0.8%
    
    results = []

    for m in markets:
        # Market-specific baseline TP (XRP 1.2%, ETH/SOL 1.0%)
        base_tp = 0.012 if m == "KRW-XRP" else 0.010
        
        configs = [
            # Baseline
            SimulationConfig(
                name="Baseline (현행)", group="Baseline",
                step_pct=0.005, profit_pct=base_tp, squad_tp_pct=base_tp
            ),
            # Option A (Tighter TP)
            SimulationConfig(
                name="Option A-1 (TP 0.6%)", group="Option A (익절축소)",
                step_pct=0.005, profit_pct=0.006, squad_tp_pct=0.006
            ),
            SimulationConfig(
                name="Option A-2 (TP 0.7%)", group="Option A (익절축소)",
                step_pct=0.005, profit_pct=0.007, squad_tp_pct=0.007
            ),
            SimulationConfig(
                name="Option A-3 (TP 0.8%)", group="Option A (익절축소)",
                step_pct=0.005, profit_pct=0.008, squad_tp_pct=0.008
            ),
            # Option B (Wider Step)
            SimulationConfig(
                name="Option B-1 (Step 0.8%)", group="Option B (간격확대)",
                step_pct=0.008, profit_pct=base_tp, squad_tp_pct=base_tp
            ),
            SimulationConfig(
                name="Option B-2 (Step 1.0%)", group="Option B (간격확대)",
                step_pct=0.010, profit_pct=base_tp, squad_tp_pct=base_tp
            ),
            # Option C (Balanced 1:1)
            SimulationConfig(
                name="Option C-1 (0.8% / 0.8%)", group="Option C (균형형)",
                step_pct=0.008, profit_pct=0.008, squad_tp_pct=0.008
            ),
            SimulationConfig(
                name="Option C-2 (1.0% / 0.8%)", group="Option C (균형형)",
                step_pct=0.010, profit_pct=0.008, squad_tp_pct=0.008
            ),
        ]

        for cfg in configs:
            res = run_g60_simulation(dfs[m], m, cfg)
            results.append(res)

    res_df = pd.DataFrame(results)
    
    # Save results
    out_json = BASE_DIR / "data" / "g60_7d_param_study_results.json"
    res_df.to_json(out_json, orient="records", indent=2)
    print(f"\n[SAVED] {out_json}")

    # Print summary tables
    print("\n" + "=" * 120)
    print("🏆 [G60 최근 7일 파라미터 백테스트 비교 결과 (2026-10-22 ~ 2026-10-29)]")
    print("=" * 120)
    
    for m in markets:
        sub = res_df[res_df["market"] == m]
        print(f"\n📊 마켓: {m} (예산: 300,000원, 1단=10,000원, 최대 30단)")
        print("-" * 120)
        fmt_str = "{:<25} | {:>6} | {:>9} | {:>9} | {:>9} | {:>7} | {:>7} | {:>9} | {:>8} | {:>8}"
        print(fmt_str.format("설정명", "익절수", "실현손익", "평가손익", "순손익합계", "최대단수", "잔여단수", "최대투입", "MDD", "평균보유"))
        print("-" * 120)
        for _, r in sub.iterrows():
            print(fmt_str.format(
                r["config_name"],
                f"{r['total_wins']}회",
                f"{r['realized_pnl']:+,.0f}원",
                f"{r['ending_unrealized_pnl']:+,.0f}원",
                f"{r['net_total_pnl']:+,.0f}원",
                f"{r['max_active_levels']}단",
                f"{r['ending_levels']}단",
                f"{r['peak_capital_used']:,.0f}원",
                f"{r['max_drawdown_pct']:.2f}%",
                f"{r['avg_hold_min']:.1f}분"
            ))

    # Portfolio combined aggregate
    print("\n" + "=" * 120)
    print("🌐 [포트폴리오 합산 결과 (XRP + ETH + SOL, 총 예산: 900,000원)]")
    print("=" * 120)
    
    grouped = res_df.groupby("config_name").agg({
        "total_wins": "sum",
        "realized_pnl": "sum",
        "total_fees": "sum",
        "ending_unrealized_pnl": "sum",
        "net_total_pnl": "sum",
        "peak_capital_used": "sum",
        "ending_levels": "sum",
        "avg_hold_min": "mean",
        "max_drawdown_pct": "mean"
    }).loc[[
        "Baseline (현행)",
        "Option A-1 (TP 0.6%)",
        "Option A-2 (TP 0.7%)",
        "Option A-3 (TP 0.8%)",
        "Option B-1 (Step 0.8%)",
        "Option B-2 (Step 1.0%)",
        "Option C-1 (0.8% / 0.8%)",
        "Option C-2 (1.0% / 0.8%)"
    ]]

    grouped["ROI(%)"] = (grouped["net_total_pnl"] / 900_000.0) * 100.0
    grouped["회전율(손익/투입)"] = (grouped["realized_pnl"] / grouped["peak_capital_used"]) * 100.0

    print(grouped[[
        "total_wins", "realized_pnl", "ending_unrealized_pnl", "net_total_pnl",
        "peak_capital_used", "ending_levels", "ROI(%)", "회전율(손익/투입)", "avg_hold_min"
    ]].to_string())
    print("=" * 120)


if __name__ == "__main__":
    main()
