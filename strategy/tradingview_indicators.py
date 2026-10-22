"""
트레이딩뷰(TradingView) 명품 검증 지표 모듈
- SuperTrend (Olivier Seban: ATR 기반 추세 추종 지표)
- WaveTrend (LazyBear: 오실레이터 과매도 반등 포착)
- TTM Squeeze (John Carter: 볼린저 밴드-켈트너 채널 변동성 응축 지표)
"""

from typing import Tuple
import numpy as np
import pandas as pd


def calculate_supertrend(
    df: pd.DataFrame,
    period: int = 10,
    multiplier: float = 3.0
) -> Tuple[pd.Series, pd.Series]:
    """
    트레이딩뷰 1위 추세 추종 지표: SuperTrend
    Returns:
        (supertrend_line, trend_direction)
        trend_direction: +1 (상승 추세 BULL), -1 (하락 추세 BEAR)
    """
    high = df['high'].values
    low = df['low'].values
    close = df['close'].values
    n = len(df)

    if n < period:
        zeros = pd.Series(0.0, index=df.index)
        ones = pd.Series(1, index=df.index)
        return zeros, ones

    # 1. True Range 계산
    tr = np.zeros(n)
    tr[0] = high[0] - low[0]
    for i in range(1, n):
        tr[i] = max(
            high[i] - low[i],
            abs(high[i] - close[i - 1]),
            abs(low[i] - close[i - 1])
        )

    # 2. Simple Moving Average ATR
    atr = pd.Series(tr).rolling(period).mean().values

    # 3. Basic Bands
    hl2 = (high + low) / 2.0
    basic_upper = hl2 + (multiplier * atr)
    basic_lower = hl2 - (multiplier * atr)

    final_upper = np.zeros(n)
    final_lower = np.zeros(n)
    trend = np.ones(n, dtype=int)

    for i in range(1, n):
        # ATR 계산 전 구간 스킵
        if np.isnan(atr[i]):
            continue

        # Final Upper
        if basic_upper[i] < final_upper[i - 1] or close[i - 1] > final_upper[i - 1]:
            final_upper[i] = basic_upper[i]
        else:
            final_upper[i] = final_upper[i - 1]

        # Final Lower
        if basic_lower[i] > final_lower[i - 1] or close[i - 1] < final_lower[i - 1]:
            final_lower[i] = basic_lower[i]
        else:
            final_lower[i] = final_lower[i - 1]

        # Direction
        if close[i] > final_upper[i - 1]:
            trend[i] = 1
        elif close[i] < final_lower[i - 1]:
            trend[i] = -1
        else:
            trend[i] = trend[i - 1]

    st_line = np.where(trend == 1, final_lower, final_upper)
    return pd.Series(st_line, index=df.index), pd.Series(trend, index=df.index)


def calculate_wavetrend(
    df: pd.DataFrame,
    n1: int = 10,
    n2: int = 21,
    oversold: float = -53.0
) -> Tuple[pd.Series, pd.Series, pd.Series]:
    """
    트레이딩뷰 대표 오실레이터: WaveTrend (by LazyBear)
    Returns:
        (wt1, wt2, is_oversold_cross)
        is_oversold_cross: 과매도(<= -53) 구간에서 골든크로스 발생 시 True (강력 반등 신호)
    """
    hlc3 = (df['high'] + df['low'] + df['close']) / 3.0
    esa = hlc3.ewm(span=n1, adjust=False).mean()
    d = (hlc3 - esa).abs().ewm(span=n1, adjust=False).mean()
    
    denom = 0.015 * d
    denom = denom.replace(0, 1e-6)
    ci = (hlc3 - esa) / denom

    wt1 = ci.ewm(span=n2, adjust=False).mean()
    wt2 = wt1.rolling(4).mean()

    prev_wt1 = wt1.shift(1)
    prev_wt2 = wt2.shift(1)
    
    # 과매도 구간(<= oversold)에서 wt1이 wt2를 상향 돌파
    oversold_cross = (prev_wt1 <= prev_wt2) & (wt1 > wt2) & (wt1 <= oversold)

    return wt1, wt2, oversold_cross


def calculate_ttm_squeeze(
    df: pd.DataFrame,
    period: int = 20,
    bb_mult: float = 2.0,
    kc_mult: float = 1.5
) -> pd.Series:
    """
    트레이딩뷰 존 카터 TTM Squeeze 지표:
    볼린저 밴드가 켈트너 채널 안으로 수축(응축)되었는지 여부
    """
    close = df['close']
    high = df['high']
    low = df['low']

    # 볼린저 밴드
    bb_mid = close.rolling(period).mean()
    bb_std = close.rolling(period).std()
    bb_upper = bb_mid + (bb_mult * bb_std)
    bb_lower = bb_mid - (bb_mult * bb_std)

    # 켈트너 채널
    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()
    atr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1).rolling(period).mean()

    kc_mid = close.rolling(period).mean()
    kc_upper = kc_mid + (kc_mult * atr)
    kc_lower = kc_mid - (kc_mult * atr)

    squeeze_on = (bb_lower > kc_lower) & (bb_upper < kc_upper)
    return squeeze_on


def add_tradingview_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """캔들 데이터프레임에 트레이딩뷰 핵심 지표 일괄 추가"""
    df = df.copy()
    st_line, st_dir = calculate_supertrend(df, period=10, multiplier=3.0)
    df['supertrend'] = st_line
    df['supertrend_dir'] = st_dir

    wt1, wt2, wt_cross = calculate_wavetrend(df, n1=10, n2=21, oversold=-53.0)
    df['wt1'] = wt1
    df['wt2'] = wt2
    df['is_wt_oversold_cross'] = wt_cross

    df['is_squeeze_on'] = calculate_ttm_squeeze(df, period=20, bb_mult=2.0, kc_mult=1.5)
    return df
