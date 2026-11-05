#!/usr/bin/env python3
"""
strategy/keltner_trend.py
-------------------------
4H Keltner Channel Breakout Trend-Following Strategy:
- Timeframe: 4-Hour (240-minute) candles
- Macro Trend Filter: 4H Close > 200 EMA
- Channel: 20 EMA +/- 2.0 * ATR(14)
- Entry: Close crosses above Keltner Upper Band while Close > 200 EMA
- Exit: Close drops below 20 EMA (or Emergency Stop Loss)
"""

from typing import Dict, Any, Optional
import numpy as np
import pandas as pd
import requests


def calculate_keltner_channel(
    df: pd.DataFrame,
    ema_period: int = 20,
    atr_period: int = 14,
    multiplier: float = 2.0,
    macro_ema_period: int = 200
) -> pd.DataFrame:
    """
    Computes Keltner Channel indicators on 4H OHLCV dataframe.
    Required columns: 'close', 'high', 'low' (or 'trade_price', 'high_price', 'low_price').
    """
    out = df.copy()
    
    # Standardize column names
    c_col = 'close' if 'close' in out.columns else 'trade_price'
    h_col = 'high' if 'high' in out.columns else 'high_price'
    l_col = 'low' if 'low' in out.columns else 'low_price'
    
    close = out[c_col]
    high = out[h_col]
    low = out[l_col]
    
    # 1. EMAs
    out['ema20'] = close.ewm(span=ema_period, adjust=False).mean()
    out['ema200'] = close.ewm(span=macro_ema_period, adjust=False).mean()
    
    # 2. True Range & ATR(14)
    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    out['tr'] = tr
    out['atr14'] = tr.ewm(alpha=1.0 / atr_period, adjust=False).mean()
    
    # 3. Keltner Bands
    out['kc_upper'] = out['ema20'] + multiplier * out['atr14']
    out['kc_lower'] = out['ema20'] - multiplier * out['atr14']
    
    return out


def get_keltner_signals(df: pd.DataFrame) -> Dict[str, Any]:
    """
    Evaluates current and previous 4H bar to produce actionable trading signals:
    - 'BUY': Macro Bull (Close > 200 EMA) AND Breakout above Upper Band
    - 'EXIT': Close dropped below 20 EMA
    - 'HOLD': No state change
    """
    if len(df) < 200:
        return {
            "valid": False,
            "signal": "HOLD",
            "reason": f"데이터 부족 (최소 200봉 필요, 현재 {len(df)}봉)"
        }
        
    calc_df = calculate_keltner_channel(df)
    
    curr = calc_df.iloc[-1]
    prev = calc_df.iloc[-2]
    
    c_col = 'close' if 'close' in calc_df.columns else 'trade_price'
    curr_close = float(curr[c_col])
    prev_close = float(prev[c_col])
    
    curr_ema20 = float(curr['ema20'])
    curr_ema200 = float(curr['ema200'])
    curr_upper = float(curr['kc_upper'])
    curr_lower = float(curr['kc_lower'])
    curr_atr = float(curr['atr14'])
    
    prev_upper = float(prev['kc_upper'])
    
    is_macro_bull = bool(curr_close > curr_ema200)
    is_breakout = bool(curr_close > curr_upper)
    is_fresh_breakout = bool(prev_close <= prev_upper and curr_close > curr_upper)
    is_exit = bool(curr_close < curr_ema20)
    
    signal = "HOLD"
    reason = ""
    
    if is_exit:
        signal = "EXIT"
        reason = f"20 EMA 하향 이탈 ({curr_close:,.0f}원 < 20선 {curr_ema20:,.0f}원) ➔ 포지션 청산"
    elif is_macro_bull and is_breakout:
        signal = "BUY"
        if is_fresh_breakout:
            reason = f"대세 상승장(>200선) 4H 켈트너 상단 신규 돌파 ({curr_close:,.0f}원 > 상단 {curr_upper:,.0f}원)"
        else:
            reason = f"대세 상승장(>200선) 4H 켈트너 상단 상회 추세 지속 ({curr_close:,.0f}원 > 상단 {curr_upper:,.0f}원)"
    else:
        if not is_macro_bull:
            reason = f"200 EMA 하회 ({curr_close:,.0f}원 <= 200선 {curr_ema200:,.0f}원) ➔ 진입 차단"
        else:
            reason = f"켈트너 채널 내부 횡보 (상단 {curr_upper:,.0f}원 / 20선 {curr_ema20:,.0f}원)"
            
    return {
        "valid": True,
        "signal": signal,
        "reason": reason,
        "curr_price": curr_close,
        "ema20": curr_ema20,
        "ema200": curr_ema200,
        "upper_band": curr_upper,
        "lower_band": curr_lower,
        "atr14": curr_atr,
        "is_macro_bull": is_macro_bull,
        "is_breakout": is_breakout,
        "is_exit": is_exit
    }


def fetch_4h_candles_upbit(market: str = "KRW-ETH", count: int = 300) -> Optional[pd.DataFrame]:
    """
    Fetches the latest 4-hour (240m) candles from Upbit Public Quotation API.
    Supports multi-batch pagination if count > 200.
    """
    import time
    url = "https://api.upbit.com/v1/candles/minutes/240"
    headers = {"accept": "application/json"}
    all_data = []
    to_param = ""
    
    try:
        while len(all_data) < count:
            batch_count = min(200, count - len(all_data))
            params = {"market": market, "count": batch_count}
            if to_param:
                params["to"] = to_param
            r = requests.get(url, params=params, headers=headers, timeout=5)
            if r.status_code != 200:
                break
            batch = r.json()
            if not batch or not isinstance(batch, list):
                break
            all_data.extend(batch)
            if len(batch) < batch_count:
                break
            to_param = batch[-1]["candle_date_time_utc"]
            time.sleep(0.05)
            
        if not all_data:
            return None
            
        df = pd.DataFrame(all_data)
        # Rename to standard
        df.rename(columns={
            "candle_date_time_kst": "dt",
            "opening_price": "open",
            "high_price": "high",
            "low_price": "low",
            "trade_price": "close",
            "candle_acc_trade_volume": "volume"
        }, inplace=True)
        df['dt'] = pd.to_datetime(df['dt'])
        df.sort_values('dt', inplace=True)
        df.drop_duplicates(subset=['dt'], inplace=True)
        df.reset_index(drop=True, inplace=True)
        return df
    except Exception:
        return None
