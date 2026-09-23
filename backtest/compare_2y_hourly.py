"""Compare the live hybrid strategy with fixed-rule spot benchmarks.

This is an explicit research simulator, not an exchange-execution emulator.
It uses completed KRW hourly candles, 0.05% fees, and 0.05% market slippage.
"""
from __future__ import annotations

import json
import math
import time
from pathlib import Path
from typing import Callable
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "backtest" / "data"
OUT_DIR = ROOT / "backtest" / "results"
SYMBOLS = ("KRW-ETH", "KRW-SOL")
INITIAL_KRW = 1_000_000.0
UNIT_KRW = 10_000.0
MAX_EXPOSURE = 0.50
FEE = 0.0005
MARKET_SLIPPAGE = 0.0005
PERIODS_PER_YEAR = 24 * 365


def fetch_hourly(market: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = DATA_DIR / f"{market.lower().replace('-', '_')}_1h.csv"
    if path.exists():
        cached = pd.read_csv(path, index_col=0, parse_dates=True)
        if not cached.empty and cached.index.min() <= start and cached.index.max() >= end:
            return cached.loc[start:end]

    url = "https://api.upbit.com/v1/candles/minutes/60"
    cursor = end
    rows: list[dict] = []
    oldest_seen = None
    while cursor >= start:
        query = urlencode({"market": market, "count": 200, "to": cursor.strftime("%Y-%m-%dT%H:%M:%S")})
        request = Request(f"{url}?{query}", headers={"User-Agent": "AutoBotTrader-Backtest/1.0"})
        with urlopen(request, timeout=20) as response:
            page = json.loads(response.read().decode("utf-8"))
        if not page:
            break
        rows.extend(page)
        oldest = pd.Timestamp(page[-1]["candle_date_time_kst"])
        if oldest_seen is not None and oldest >= oldest_seen:
            raise RuntimeError(f"Pagination stopped advancing for {market} at {oldest}")
        oldest_seen = oldest
        cursor = oldest - pd.Timedelta(seconds=1)
        if oldest <= start:
            break
        time.sleep(0.13)

    if not rows:
        raise RuntimeError(f"No candles received for {market}")
    frame = pd.DataFrame(rows)
    frame["timestamp"] = pd.to_datetime(frame["candle_date_time_kst"])
    frame = frame.set_index("timestamp").sort_index()
    frame = frame.rename(columns={
        "opening_price": "open", "high_price": "high", "low_price": "low",
        "trade_price": "close", "candle_acc_trade_volume": "volume",
    })
    frame = frame[["open", "high", "low", "close", "volume"]]
    frame = frame[~frame.index.duplicated(keep="last")].loc[start:end]
    frame.to_csv(path)
    return frame


def indicators(df: pd.DataFrame) -> pd.DataFrame:
    x = df.copy()
    close = x.close
    typical = (x.high + x.low + close) / 3.0
    x["ma5"] = close.rolling(5).mean()
    x["ma20"] = close.rolling(20).mean()
    x["ma50"] = close.rolling(50).mean()
    x["ma100"] = close.rolling(100).mean()
    x["ma200"] = close.rolling(200).mean()
    x["ema50"] = close.ewm(span=50, adjust=False).mean()
    bb_mid = typical.rolling(20).mean()
    x["bb_lower"] = bb_mid - 2.0 * typical.rolling(20).std()
    x["bb_mid"] = bb_mid
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    x["rsi"] = 100.0 - 100.0 / (1.0 + gain / loss.replace(0, np.nan))
    x["vol_mean30"] = x.volume.rolling(30).mean()
    return x


def perf(name: str, market: str, equity: pd.Series, trades: list[dict], exposure: float) -> dict:
    equity = equity.dropna()
    if equity.empty:
        return {"market": market, "strategy": name}
    rets = equity.pct_change().dropna()
    years = max((equity.index[-1] - equity.index[0]).total_seconds() / (365.25 * 86400), 1e-9)
    total = equity.iloc[-1] / equity.iloc[0] - 1.0
    peak = equity.cummax()
    drawdown = equity / peak - 1.0
    vol = rets.std(ddof=1) * math.sqrt(PERIODS_PER_YEAR) if len(rets) > 1 else 0.0
    sharpe = rets.mean() / rets.std(ddof=1) * math.sqrt(PERIODS_PER_YEAR) if len(rets) > 1 and rets.std(ddof=1) > 0 else 0.0
    calmar = (total / years) / abs(drawdown.min()) if drawdown.min() < 0 else 0.0
    closed = [t["pnl"] for t in trades]
    wins = [p for p in closed if p > 0]
    losses = [p for p in closed if p < 0]
    return {
        "market": market,
        "strategy": name,
        "total_return_pct": round(total * 100, 2),
        "cagr_pct": round(((1 + total) ** (1 / years) - 1) * 100, 2) if total > -1 else -100.0,
        "max_drawdown_pct": round(drawdown.min() * 100, 2),
        "sharpe": round(sharpe, 2),
        "calmar": round(calmar, 2),
        "closed_trades": len(closed),
        "win_rate_pct": round(100 * len(wins) / len(closed), 2) if closed else None,
        "profit_factor": round(sum(wins) / abs(sum(losses)), 2) if losses else (None if not wins else "inf"),
        "exposure_pct_time": round(exposure * 100, 1),
        "final_equity_krw": round(float(equity.iloc[-1]), 2),
    }


def window_perf(name: str, market: str, label: str, equity: pd.Series) -> dict:
    """Return and drawdown for a fixed date window, measured from its first equity point."""
    equity = equity.dropna()
    rets = equity.pct_change().dropna()
    sharpe = rets.mean() / rets.std(ddof=1) * math.sqrt(PERIODS_PER_YEAR) if len(rets) > 1 and rets.std(ddof=1) > 0 else 0.0
    return {
        "market": market,
        "strategy": name,
        "period": label,
        "total_return_pct": round((equity.iloc[-1] / equity.iloc[0] - 1) * 100, 2),
        "max_drawdown_pct": round((equity / equity.cummax() - 1).min() * 100, 2),
        "sharpe": round(sharpe, 2),
    }


def simulate_hybrid(df: pd.DataFrame, market: str) -> tuple[pd.Series, list[dict], float]:
    """Approximate the live ETH/SOL hybrid rules on hourly OHLC candles."""
    x = indicators(df)
    cash = INITIAL_KRW
    tranches: list[dict] = []
    orders: list[dict] = []
    trades: list[dict] = []
    max_steps = 16 if market == "KRW-SOL" else 9999
    schedule = [1, 1, 2, 4] * 4 if market == "KRW-SOL" else [1, 1, 2, 4]
    margin = 1.005 if market == "KRW-SOL" else 1.008
    liquidation_pct = 0.70 if market == "KRW-SOL" else 0.50
    regime: str | None = None
    initial_entry_done = False
    last_dca: pd.Timestamp | None = None
    last_closing_date = None
    steps_used = 0
    trail_active = False
    peak = 0.0
    cooldown_until: pd.Timestamp | None = None
    equity_values: list[float] = []
    active_bar_count = 0
    x["session_key"] = (x.index - pd.Timedelta(hours=9)).normalize() + pd.Timedelta(hours=9)
    daily_open = x.groupby("session_key").open.first()
    daily_close_8 = x.loc[x.index.hour == 8].groupby("session_key").close.last()
    daily_ma5_at_8 = daily_close_8.rolling(5).mean()

    def qty() -> float:
        return sum(t["qty"] for t in tranches)

    def cost() -> float:
        return sum(t["cost"] for t in tranches)

    def average() -> float:
        q = qty()
        return cost() / q if q > 0 else 0.0

    def buy(notional: float, price: float, step: int, mode: str, ts: pd.Timestamp) -> bool:
        nonlocal cash
        if notional < 5000 or notional * (1 + FEE) > cash or cost() + notional > INITIAL_KRW * MAX_EXPOSURE:
            return False
        fill = price * (1 + MARKET_SLIPPAGE if mode == "market" else 1.0)
        volume = notional / fill
        cash -= notional * (1 + FEE)
        tranches.append({"qty": volume, "cost": notional * (1 + FEE), "price": fill, "step": step, "time": ts})
        return True

    def sell_parts(parts: list[dict], price: float, ts: pd.Timestamp) -> None:
        nonlocal cash, tranches, trail_active, peak, steps_used
        sold_qty = sum(t["qty"] for t in parts)
        revenue = sold_qty * price * (1 - FEE - MARKET_SLIPPAGE)
        sold_cost = sum(t["cost"] for t in parts)
        cash += revenue
        trades.append({"pnl": revenue - sold_cost, "time": ts})
        ids = {id(t) for t in parts}
        tranches = [t for t in tranches if id(t) not in ids]
        if not tranches:
            orders.clear()
            trail_active = False
            peak = 0.0
            steps_used = 0

    def replenish_orders(ts: pd.Timestamp) -> None:
        nonlocal orders
        existing = sorted(orders, key=lambda o: o["price"])
        allowed = max_steps - steps_used - len(existing)
        if allowed <= 0 or not tranches:
            return
        if not existing:
            n = min(3, allowed)
            for i in range(1, n + 1):
                step_idx = steps_used + i - 1
                orders.append({"price": average() * (0.96 ** i), "units": schedule[step_idx % len(schedule)]})
        elif len(existing) == 1:
            for j, factor in enumerate((0.96, 0.9216)):
                if j >= allowed:
                    break
                step_idx = steps_used + len(existing) + j
                orders.append({"price": existing[0]["price"] * factor, "units": schedule[step_idx % len(schedule)]})
        else:
            step_idx = steps_used + len(existing)
            orders.append({"price": existing[0]["price"] * 0.96, "units": schedule[step_idx % len(schedule)]})

    for i, (ts, bar) in enumerate(x.iterrows()):
        close, high, low, op = float(bar.close), float(bar.high), float(bar.low), float(bar.open)
        ready = pd.notna(bar.ma200) and pd.notna(bar.ma20)
        if not ready:
            equity_values.append(cash + qty() * close)
            continue
        curr_regime = "BULL" if close > bar.ma200 else "BEAR"
        transitioned = regime is not None and regime != curr_regime
        if regime and regime != curr_regime and regime == "BULL":
            if tranches:
                q_to_sell = qty() * liquidation_pct
                left = q_to_sell
                parts = []
                for t in tranches:
                    part_q = min(t["qty"], left)
                    if part_q > 0:
                        parts.append({**t, "qty": part_q, "cost": t["cost"] * part_q / t["qty"]})
                        t["qty"] -= part_q
                        t["cost"] *= (t["qty"] / (t["qty"] + part_q))
                        left -= part_q
                    if left <= 1e-12:
                        break
                # Replace records so sell_parts can remove partial objects and preserve remaining lots.
                sold_qty = sum(t["qty"] for t in parts)
                sold_cost = sum(t["cost"] for t in parts)
                cash += sold_qty * close * (1 - FEE - MARKET_SLIPPAGE)
                trades.append({"pnl": sold_qty * close * (1 - FEE - MARKET_SLIPPAGE) - sold_cost, "time": ts})
                tranches = [t for t in tranches if t["qty"] > 1e-10]
                if tranches:
                    q, c = qty(), cost()
                    tranches = [{"qty": q, "cost": c, "price": c / q, "step": 0, "time": ts}]
                    steps_used = 1
                else:
                    steps_used = 0
            orders.clear()
            trail_active = False
            peak = 0.0
            last_dca = None
        elif regime and regime != curr_regime and regime == "BEAR":
            orders.clear()
            last_dca = ts
        regime = curr_regime
        if transitioned:
            equity_values.append(cash + qty() * close)
            continue

        if regime == "BEAR":
            # Existing resting bids fill at their limit; budget and total exposure are capped at 50%.
            filled = [o for o in orders if low <= o["price"]]
            if filled:
                for order in filled:
                    notional = UNIT_KRW * order["units"]
                    if cost() + notional <= INITIAL_KRW * MAX_EXPOSURE and cash >= notional * (1 + FEE):
                        fill_price = min(op, order["price"])
                        if buy(notional, fill_price, steps_used, "limit", ts):
                            steps_used += 1
                    if order in orders:
                        orders.remove(order)
                replenish_orders(ts)

            if tranches:
                avg = average()
                if high >= avg * margin:
                    sell_parts(tranches.copy(), max(op, avg * margin), ts)
                else:
                    eligible = [t for t in tranches if high >= t["price"] * 1.03]
                    if eligible:
                        # basket target has priority, as in live check_magic_split_exits.
                        sell_parts(eligible, max(op, max(t["price"] * 1.03 for t in eligible)), ts)
                if tranches:
                    replenish_orders(ts)
            else:
                bb_low = bar.bb_lower
                prev_vol = x.volume.iloc[max(0, i - 30):i].mean() if i else np.nan
                cluc = (close < bar.ema50 and close < bb_low * 0.985 and pd.notna(prev_vol) and bar.volume < prev_vol * 20)
                if cluc:
                    cooldown_ok = cooldown_until is None or ts >= cooldown_until
                    if cooldown_ok:
                        initial_entry_done = buy(UNIT_KRW, close, steps_used, "market", ts)
                    if tranches:
                        steps_used += 1
                        replenish_orders(ts)
        else:
            if tranches:
                avg = average()
                pnl_rate = close / avg - 1.0
                if low <= avg * (1 + (-0.10)):
                    fill = min(op, avg * 0.90)
                    sell_parts(tranches.copy(), fill, ts)
                    cooldown_until = ts + pd.Timedelta(hours=2)
                    initial_entry_done = True
                else:
                    steps = len(tranches)
                    trigger = 0.10 if steps <= 3 else 0.07 if steps <= 6 else 0.05
                    if pnl_rate >= trigger:
                        trail_active = True
                    if trail_active:
                        peak = max(peak, high)
                        stop = peak * 0.97
                        if low <= stop:
                            sell_parts(tranches.copy(), min(op, stop), ts)
                            initial_entry_done = True
                    dca_bought = False
                    if tranches and len(tranches) < 20 and last_dca is not None and ts - last_dca >= pd.Timedelta(hours=12) and not trail_active:
                        if buy(UNIT_KRW, close, len(tranches), "market", ts):
                            last_dca = ts
                            dca_bought = True
                    if tranches and not dca_bought and ts.hour == 8 and not trail_active and len(tranches) < 20 and last_closing_date != ts.date():
                        session = bar.session_key
                        day_open = daily_open.get(session, np.nan)
                        day_ma5 = daily_ma5_at_8.get(session, np.nan)
                        if pd.notna(day_open) and pd.notna(day_ma5) and close > day_open and close > day_ma5:
                            if buy(UNIT_KRW, close, len(tranches), "market", ts):
                                last_dca = ts
                                last_closing_date = ts.date()
            else:
                golden = (i > 0 and pd.notna(x.ma5.iloc[i - 1]) and pd.notna(x.ma20.iloc[i - 1])
                          and x.ma5.iloc[i - 1] <= x.ma20.iloc[i - 1] and bar.ma5 > bar.ma20 and close > bar.ma5)
                cooldown_ok = cooldown_until is None or ts >= cooldown_until
                if cooldown_ok and (not initial_entry_done or golden) and cash >= UNIT_KRW * (1 + FEE) and cost() + UNIT_KRW <= INITIAL_KRW * MAX_EXPOSURE:
                    if buy(UNIT_KRW, close, steps_used, "market", ts):
                        initial_entry_done = True
                        last_dca = ts
        if qty() > 0:
            active_bar_count += 1
        equity_values.append(cash + qty() * close)

    equity = pd.Series(equity_values, index=x.index)
    return equity, trades, active_bar_count / max(len(equity), 1)


def simulate_signal_strategy(df: pd.DataFrame, name: str, market: str) -> tuple[pd.Series, list[dict], float]:
    """Fixed 50%-allocation benchmarks; completed-bar signals fill at next bar open."""
    x = indicators(df)
    cash = INITIAL_KRW
    qty = 0.0
    entry = 0.0
    trades: list[dict] = []
    values: list[float] = []
    active = 0
    for i, (ts, bar) in enumerate(x.iterrows()):
        close = float(bar.close)
        fill_price = float(bar.open)
        buy_signal = sell_signal = False
        if i > 1:
            signal_bar = x.iloc[i - 1]
            prev = x.iloc[i - 2]
            if name == "200h Trend":
                buy_signal = pd.notna(signal_bar.ma200) and prev.close <= prev.ma200 and signal_bar.close > signal_bar.ma200
                sell_signal = pd.notna(signal_bar.ma200) and signal_bar.close < signal_bar.ma200
            elif name == "20/100 Cross":
                buy_signal = pd.notna(signal_bar.ma100) and prev.ma20 <= prev.ma100 and signal_bar.ma20 > signal_bar.ma100
                sell_signal = pd.notna(signal_bar.ma100) and prev.ma20 >= prev.ma100 and signal_bar.ma20 < signal_bar.ma100
            elif name == "RSI-Bollinger":
                buy_signal = pd.notna(signal_bar.bb_lower) and signal_bar.rsi < 30 and signal_bar.close < signal_bar.bb_lower
                sell_signal = (pd.notna(signal_bar.bb_mid) and signal_bar.close > signal_bar.bb_mid) or (pd.notna(signal_bar.rsi) and signal_bar.rsi > 55) or (qty > 0 and signal_bar.close <= entry * 0.92)
        if qty == 0 and buy_signal:
            notional = min(INITIAL_KRW * MAX_EXPOSURE, cash / (1 + FEE))
            fill = fill_price * (1 + MARKET_SLIPPAGE)
            qty = notional / fill
            cash -= notional * (1 + FEE)
            entry = fill
        elif qty > 0 and sell_signal:
            revenue = qty * fill_price * (1 - FEE - MARKET_SLIPPAGE)
            trades.append({"pnl": revenue - qty * entry, "time": ts})
            cash += revenue
            qty = 0.0
            entry = 0.0
        if qty > 0:
            active += 1
        values.append(cash + qty * close)
    return pd.Series(values, index=x.index), trades, active / max(len(x), 1)


def simulate_buy_hold(df: pd.DataFrame) -> tuple[pd.Series, list[dict], float]:
    close = df.close
    entry = float(close.iloc[0]) * (1 + MARKET_SLIPPAGE)
    notional = INITIAL_KRW * MAX_EXPOSURE / (1 + FEE)
    qty = notional / entry
    cash = INITIAL_KRW - notional * (1 + FEE)
    equity = cash + qty * close
    return equity, [], 1.0


def main() -> None:
    end = pd.Timestamp.now(tz="Asia/Seoul").tz_localize(None).floor("h") - pd.Timedelta(hours=1)
    start = end - pd.DateOffset(years=2)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    all_results: list[dict] = []
    split_results: list[dict] = []
    for market in SYMBOLS:
        raw = fetch_hourly(market, start, end)
        if len(raw) < 17_000:
            raise RuntimeError(f"Insufficient 2-year hourly data for {market}: {len(raw)} bars")
        raw = raw.loc[start:end]
        sims: list[tuple[str, Callable]] = [
            ("Current Hybrid (approx.)", lambda d, m=market: simulate_hybrid(d, m)),
            ("Buy & Hold (50%)", lambda d: simulate_buy_hold(d)),
            ("200h Trend", lambda d, m=market: simulate_signal_strategy(d, "200h Trend", m)),
            ("20/100 Cross", lambda d, m=market: simulate_signal_strategy(d, "20/100 Cross", m)),
            ("RSI-Bollinger", lambda d, m=market: simulate_signal_strategy(d, "RSI-Bollinger", m)),
        ]
        for name, fn in sims:
            equity, trades, exposure = fn(raw)
            all_results.append(perf(name, market, equity, trades, exposure))
            split_at = raw.index[0] + pd.DateOffset(years=1)
            windows = [("Year 1", raw.index[0], split_at), ("Year 2", split_at, raw.index[-1])]
            for label, left, right in windows:
                segment = equity.loc[(equity.index >= left) & (equity.index <= right)]
                if len(segment) > 1:
                    split_results.append(window_perf(name, market, label, segment))
        print(f"{market}: {len(raw):,} candles, {raw.index.min()} to {raw.index.max()}")
    result_df = pd.DataFrame(all_results)
    result_df.to_csv(OUT_DIR / "strategy_comparison_2y_1h.csv", index=False)
    (OUT_DIR / "strategy_comparison_2y_1h.json").write_text(result_df.to_json(orient="records", indent=2), encoding="utf-8")
    pd.DataFrame(split_results).to_csv(OUT_DIR / "strategy_comparison_2y_1h_split.csv", index=False)
    (OUT_DIR / "strategy_comparison_2y_1h_split.json").write_text(json.dumps(split_results, indent=2), encoding="utf-8")
    print(result_df.to_string(index=False))
    print("\nYearly segments:")
    print(pd.DataFrame(split_results).to_string(index=False))


if __name__ == "__main__":
    main()
