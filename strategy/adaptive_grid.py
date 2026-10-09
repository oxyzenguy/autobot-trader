"""
적응형 그리드 전략 모듈 (Adaptive Grid Strategy - C-E Model)
===========================================================
핵심 메커니즘 (C-E: Adaptive Buy + Dynamic Exit):
1. 200 MA 국면 판정:
   - 1시간봉 200 이동평균선 상회: BULL (상승 국면)
   - 1시간봉 200 이동평균선 이하: BEAR (하락 국면)

2. 30일 롤링 NATR (Normalized Average True Range):
   - 24시간 ATR을 현재가로 정규화하여 시장의 변동성을 백분율(NATR)로 측정
   - 과거 30일(720봉) 롤링 윈도우 기준 분위수(Q25/Q75) 자동 적응

3. Step Scale 1.25배 가속 매수 간격:
   - 기본 간격: base_drop = max(0.006, NATR * 0.9)
   - 차수가 깊어질수록 1.25^차수 비율로 간격 점진 확대 (1.0x -> 1.25x -> 1.56x...)
   - 1 -> 1 -> 2 -> 4 마틴게일 스쿼드 수량 투입

4. Regime Dynamic Take-Profit (국면 연동 동적 익절):
   - BULL (상승장): 바스켓 +1.20% (추세 파동 극대화) / 개별 차수 +4.0%
   - BEAR (하락장): 바스켓 +0.40% (수수료 제외 신속 탈출) / 개별 차수 +2.5%

5. Crisis Guard (재고 가중치 보존):
   - 비상 시 실탄 소진을 지연시키는 인벤토리 가중치 플러그인 훅 지원 (기본 OFF)
"""

from typing import List, Dict, Optional, Any, Tuple
import math
import numpy as np
import pandas as pd
from strategy.matingale2x_logic import (
    adjust_price_to_tick,
    get_upbit_tick_size,
    get_existing_stages_from_state,
    get_next_squad_stages,
)

# 기본 파라미터 상수 정의 (고원 테스트 중앙값 및 검증된 세팅)
DEFAULT_STEP_SCALE = 1.25
DEFAULT_MIN_DROP_PCT = 0.006      # 최소 간격 0.6%
DEFAULT_MAX_DROP_PCT = 0.050      # 최대 간격 5.0%
DEFAULT_BULL_BASKET_TP = 0.0120   # 상승장 바스켓 +1.20%
DEFAULT_BULL_TRANCHE_TP = 0.0400  # 상승장 개별 +4.00%
DEFAULT_BEAR_BASKET_TP = 0.0040   # 하락장 바스켓 +0.40%
DEFAULT_BEAR_TRANCHE_TP = 0.0250  # 하락장 개별 +2.50%
DEFAULT_SQUAD_MULTIPLIERS = [1, 1, 2, 4]


def calculate_natr_series(df: pd.DataFrame, window: int = 24) -> pd.Series:
    """
    캔들 데이터프레임에서 24시간 정규화 변동폭(NATR) 시계열을 계산합니다.
    NATR = ATR(window) / close
    """
    if df is None or len(df) < 2:
        return pd.Series(dtype=float)
        
    c = pd.to_numeric(df['close'], errors='coerce')
    h = pd.to_numeric(df['high'], errors='coerce')
    l = pd.to_numeric(df['low'], errors='coerce')
    
    if c.isna().any() or h.isna().any() or l.isna().any():
        return pd.Series(np.nan, index=df.index, dtype=float)

    c_prev = c.shift(1).fillna(c)
    tr = np.maximum(h - l, np.maximum((h - c_prev).abs(), (l - c_prev).abs()))
    atr = tr.rolling(window, min_periods=1).mean()
    natr = atr / c.replace(0, np.nan)
    return natr


def get_market_regime_and_natr(
    df: pd.DataFrame,
    rolling_window_days: int = 30,
    obs_time: Optional[Any] = None
) -> Dict[str, Any]:
    """
    최근 캔들 데이터를 바탕으로 200MA 국면(BULL/BEAR) 및 30일 롤링 NATR 분위수를 계산합니다.
    (룩어헤드 편향 방지: 확정봉 계약을 적용하여 미완성봉 제외 및 확정봉 보존)
    """
    try:
        from strategy.hybrid_regime import filter_confirmed_candles, validate_candles_numerical
        df = filter_confirmed_candles(df, obs_time=obs_time)
        validate_candles_numerical(df)
    except Exception as e:
        return {
            "candles_valid": False,
            "is_valid": False,
            "is_bull": None,
            "regime": "HOLD",
            "curr_price": 0.0,
            "ma200": 0.0,
            "curr_natr": None,
            "q25": None,
            "q50": None,
            "q75": None,
            "volatility_regime": "INVALID",
            "reason": f"확정봉 필터링 실패: {e}"
        }

    if df is None or len(df) < 20:
        return {
            "candles_valid": False,
            "is_valid": False,
            "is_bull": None,
            "regime": "HOLD",
            "curr_price": 0.0,
            "ma200": 0.0,
            "curr_natr": None,
            "q25": None,
            "q50": None,
            "q75": None,
            "volatility_regime": "INVALID",
            "reason": "데이터 부족으로 확정 캔들 검증 실패 (HOLD)"
        }
        
    c = df['close']
    curr_price = float(c.iloc[-1])
    if curr_price <= 0 or not np.isfinite(curr_price) or pd.isna(curr_price):
        return {
            "candles_valid": False,
            "is_valid": False,
            "is_bull": None,
            "regime": "HOLD",
            "curr_price": 0.0,
            "ma200": 0.0,
            "curr_natr": None,
            "q25": None,
            "q50": None,
            "q75": None,
            "volatility_regime": "INVALID",
            "reason": "현재가 수치 비정상 (0 이하 또는 NaN/inf)"
        }
    
    # 200 MA 계산
    ma200_series = c.rolling(200, min_periods=50).mean()
    curr_ma200 = float(ma200_series.iloc[-1]) if not pd.isna(ma200_series.iloc[-1]) else curr_price
    if curr_ma200 <= 0 or not np.isfinite(curr_ma200) or pd.isna(curr_ma200):
        return {
            "candles_valid": False,
            "is_valid": False,
            "is_bull": None,
            "regime": "HOLD",
            "curr_price": 0.0,
            "ma200": 0.0,
            "curr_natr": None,
            "q25": None,
            "q50": None,
            "q75": None,
            "volatility_regime": "INVALID",
            "reason": "이동평균 수치 비정상 (0 이하 또는 NaN/inf)"
        }
    is_bull = bool(curr_price > curr_ma200)
    regime = "BULL" if is_bull else "BEAR"
    
    # NATR 및 30일(720봉) 롤링 분위수 계산
    natr_series = calculate_natr_series(df, window=24)
    if natr_series.empty or len(natr_series) == 0:
        curr_natr = None
    else:
        curr_natr = float(natr_series.iloc[-1])
        
    if curr_natr is None or pd.isna(curr_natr) or not np.isfinite(curr_natr) or curr_natr < 0.0:
        return {
            "candles_valid": False,
            "is_valid": False,
            "is_bull": None,
            "regime": "HOLD",
            "curr_price": 0.0,
            "ma200": 0.0,
            "curr_natr": None,
            "q25": None,
            "q50": None,
            "q75": None,
            "volatility_regime": "INVALID",
            "reason": "NATR 지표 계산 실패 (결측/무한대/음수 변동성)"
        }
        
    rolling_bars = rolling_window_days * 24
    min_periods = min(len(natr_series), max(100, rolling_bars // 4))
    
    q25 = float(natr_series.rolling(rolling_bars, min_periods=min_periods).quantile(0.25).iloc[-1])
    q75 = float(natr_series.rolling(rolling_bars, min_periods=min_periods).quantile(0.75).iloc[-1])
    q50 = float(natr_series.rolling(rolling_bars, min_periods=min_periods).median().iloc[-1])
    
    # NaN 방어
    q25 = q25 if not pd.isna(q25) else curr_natr * 0.8
    q75 = q75 if not pd.isna(q75) else curr_natr * 1.2
    q50 = q50 if not pd.isna(q50) else curr_natr
    
    if curr_natr < q25:
        vol_regime = "LOW"
    elif curr_natr > q75:
        vol_regime = "HIGH"
    else:
        vol_regime = "NORMAL"
        
    return {
        "candles_valid": True,
        "is_valid": True,
        "is_bull": is_bull,
        "regime": regime,
        "curr_price": curr_price,
        "ma200": curr_ma200,
        "curr_natr": curr_natr,
        "q25": q25,
        "q50": q50,
        "q75": q75,
        "volatility_regime": vol_regime,
        "reason": f"200MA {regime} 국면 (현재가 {curr_price:,.0f}원 vs 200선 {curr_ma200:,.0f}원) | NATR {curr_natr*100:.2f}% ({vol_regime})"
    }


def get_dynamic_tp_margins(is_bull: bool) -> Tuple[float, float]:
    """
    국면(BULL / BEAR)에 따른 바스켓 및 개별 차수 동적 익절 목표 마진을 반환합니다.
    Returns:
        (basket_tp_margin, tranche_tp_margin)
    """
    if is_bull:
        # 상승장: 추세 파동 극대화
        return (1.0 + DEFAULT_BULL_BASKET_TP, 1.0 + DEFAULT_BULL_TRANCHE_TP)
    else:
        # 하락장: 수수료 제외 빠른 본전+알파 탈출
        return (1.0 + DEFAULT_BEAR_BASKET_TP, 1.0 + DEFAULT_BEAR_TRANCHE_TP)


def calculate_adaptive_buy_prices(
    base_price: float,
    natr: float,
    current_step: int = 0,
    num_orders: int = 3,
    max_steps: int = 16,
    step_scale: float = DEFAULT_STEP_SCALE,
    min_drop_pct: float = DEFAULT_MIN_DROP_PCT,
    max_drop_pct: float = DEFAULT_MAX_DROP_PCT,
    multipliers: Optional[List[int]] = None,
    unit_krw: float = 10_000.0,
    inventory_guard: bool = False,
    state: Optional[dict] = None,
    existing_stages: Optional[List[tuple]] = None,
    existing_orders: Optional[List[Any]] = None,
    **kwargs
) -> List[Dict[str, Any]]:
    """
    C-E 적응형 매수 호가 계산:
    - 공통 슬롯 allocator(get_next_squad_stages) 기반 스쿼드/스테이지 및 1-1-2-4 배수 유닛 배정
    - 보유 트랜치 및 미체결 봇 주문(bear_limit_buys)을 함께 고려하여 점유 슬롯 도출
    - 보충 시 기존 봇 최저 매수호가 아래로 가격 배치
    - 30일 롤링 NATR 기반 기본 간격 도출 및 Step Scale 1.25^차수 가속 적용
    - 업비트 공식 호가 단위 내림(floor) 보정
    """
    if existing_orders:
        valid_prices = []
        for o in existing_orders:
            if isinstance(o, dict) and float(o.get('price', 0)) > 0:
                valid_prices.append(float(o['price']))
            elif isinstance(o, (int, float)) and float(o) > 0:
                valid_prices.append(float(o))
        if valid_prices:
            base_price = min(valid_prices)

    if natr is None or pd.isna(natr) or not np.isfinite(natr) or natr < 0.0 or base_price <= 0 or num_orders <= 0:
        return []

    if multipliers is None:
        multipliers = DEFAULT_SQUAD_MULTIPLIERS

    legacy_count = 0
    if state and isinstance(state, dict):
        if existing_stages is None:
            existing_stages = get_existing_stages_from_state(state)
        if existing_stages is None:
            return []
        legacy_count = len([t for t in state.get("tranches", []) if not t.get("squad_id") or int(t.get("squad_id", 0)) <= 0])
    elif existing_stages is None:
        if current_step > 0:
            max_sid = (current_step - 1) // 4 + 1
            max_stg = (current_step - 1) % 4 + 1
            existing_stages = [(max_sid, max_stg)]
        else:
            existing_stages = []

    planned_stages = get_next_squad_stages(
        existing_stages=existing_stages,
        count=num_orders,
        max_steps=max_steps,
        legacy_count=legacy_count
    )
    if not planned_stages:
        return []

    orders = []
    base_drop = max(min_drop_pct, natr * 0.9)
    cum_price = base_price

    for s_info in planned_stages:
        s_id = s_info["squad_id"]
        s_stg = s_info["squad_stage"]
        units = s_info.get("units", multipliers[(s_stg - 1) % len(multipliers)])
        step_idx = legacy_count + (s_id - 1) * 4 + (s_stg - 1)

        # Step Scale 적용 (차수가 깊어질수록 1.25배씩 간격 확장)
        try:
            scale_factor = (step_scale ** step_idx) if step_scale > 1.0 else 1.0
        except OverflowError:
            scale_factor = float('inf')

        # Crisis Guard (비상용 재고 가중치 보존 훅)
        inv_factor = (1.0 + step_idx * 0.05) if inventory_guard else 1.0

        eff_drop = min(max_drop_pct, base_drop * scale_factor * inv_factor)
        cum_price = cum_price * (1.0 - eff_drop)

        adjusted_price = adjust_price_to_tick(cum_price, method="floor")
        if orders and adjusted_price >= orders[-1]["price"]:
            tick = get_upbit_tick_size(orders[-1]["price"])
            adjusted_price = adjust_price_to_tick(orders[-1]["price"] - tick, method="floor")

        order_krw = unit_krw * units

        orders.append({
            'price': adjusted_price,
            'units': units,
            'cost_krw': order_krw,
            'squad_id': s_id,
            'squad_stage': s_stg,
            'step_index': step_idx,
            'drop_pct': eff_drop
        })

    return orders


def calculate_basket_sell_price(
    avg_price: float,
    is_bull: bool,
    fee_rate: float = 0.0005
) -> float:
    """
    포지션 평균 단가와 국면을 바탕으로 업비트 호가 단위 올림(ceil)된 바스켓 익절가를 반환합니다.
    (왕복 수수료 0.10% 이상의 순수익 보장 방어선 포함)
    """
    if avg_price <= 0:
        return 0.0
        
    basket_margin, _ = get_dynamic_tp_margins(is_bull)
    
    # 왕복 수수료(0.10%) + 최소 순익(0.05%) 보장
    min_profitable_margin = 1.0 + (fee_rate * 2) + 0.0005
    target_margin = max(basket_margin, min_profitable_margin)
    
    raw_target = avg_price * target_margin
    return adjust_price_to_tick(raw_target, method="ceil")


def get_recent_candles(
    market: str,
    allow_network: bool = False,
    count: int = 200,
    obs_time: Optional[Any] = None
) -> Optional[pd.DataFrame]:
    """
    최근 확정 캔들 데이터를 반환합니다.
    1. hybrid_regime._CANDLE_CACHE 우선 활용 (불필요한 중복 API 호출 방지)
    2. allow_network=True일 때만 캐시 부재 시 pyupbit.get_ohlcv 호출 (테스트 환경 네트워크 호출 차단)
    3. 확정봉 계약(filter_confirmed_candles)을 적용하여 미완성 현재봉을 배제하고 이미 확정된 봉은 보존합니다.
    """
    try:
        from strategy.hybrid_regime import _CANDLE_CACHE, filter_confirmed_candles
        if market in _CANDLE_CACHE:
            cached = _CANDLE_CACHE[market].get("df")
            if cached is not None and len(cached) >= 20:
                return filter_confirmed_candles(cached, obs_time=obs_time)
    except Exception:
        return None
    if allow_network:
        try:
            import pyupbit
            from strategy.hybrid_regime import filter_confirmed_candles
            raw = pyupbit.get_ohlcv(market, interval="minute60", count=count)
            if raw is not None:
                return filter_confirmed_candles(raw, obs_time=obs_time)
            return None
        except Exception:
            return None
    return None


def format_shadow_mode_log(
    market: str,
    current_price: float,
    avg_price: float,
    current_step: int,
    legacy_buy_price: float,
    legacy_tp_price: float,
    df_candles: Optional[pd.DataFrame] = None
) -> str:
    """
    Shadow Mode 관측용 로그 문자열을 포맷팅합니다.
    실제 매매는 Legacy로 진행하되, 백그라운드 C-E 계산값과 나란히 대조 출력합니다.
    """
    if df_candles is None or len(df_candles) < 20:
        df_candles = get_recent_candles(market)

    if df_candles is None or len(df_candles) < 20:
        reg_info = {
            "candles_valid": False, "is_valid": False,
            "is_bull": None, "regime": "HOLD", "curr_natr": None,
            "q25": None, "q75": None, "volatility_regime": "INVALID"
        }
    else:
        reg_info = get_market_regime_and_natr(df_candles, rolling_window_days=30)
        
    is_bull = reg_info.get("is_bull")
    natr = reg_info.get("curr_natr")
    if natr is None:
        coin_symbol = market.split('-')[-1]
        return f"[SHADOW][{coin_symbol}] 확정 캔들 검증 실패로 C-E 관측 보류 (HOLD)"
    q25 = reg_info.get("q25", 0.0)
    q75 = reg_info.get("q75", 0.0)
    vol_regime = reg_info.get("volatility_regime", "NORMAL")
    regime = reg_info.get("regime", "HOLD")
    
    # Adaptive C-E 계산
    base_drop = max(DEFAULT_MIN_DROP_PCT, natr * 0.9)
    scale_factor = (DEFAULT_STEP_SCALE ** current_step) if DEFAULT_STEP_SCALE > 1.0 else 1.0
    effective_drop = min(DEFAULT_MAX_DROP_PCT, base_drop * scale_factor)

    adaptive_orders = calculate_adaptive_buy_prices(
        base_price=current_price if avg_price <= 0 else avg_price,
        natr=natr,
        current_step=current_step,
        num_orders=1,
        step_scale=DEFAULT_STEP_SCALE
    )
    adaptive_buy_p = adaptive_orders[0]['price'] if adaptive_orders else 0.0
    
    basket_margin, tranche_margin = get_dynamic_tp_margins(is_bull)
    basket_pct = (basket_margin - 1.0) * 100.0
    tranche_pct = (tranche_margin - 1.0) * 100.0
    
    coin_symbol = market.split('-')[-1]
    
    lines = [
        f"[SHADOW][{coin_symbol}]",
        f"Regime       : {regime}",
        f"NATR         : {natr*100:.2f}%",
        f"Q25/Q75      : {q25*100:.2f} / {q75*100:.2f}%",
        f"Volatility   : {vol_regime}",
        "",
        f"Step         : {current_step}",
        f"Base Drop    : {base_drop*100:.3f}%",
        f"Step Scale   : {DEFAULT_STEP_SCALE}^{current_step}",
        f"Effective    : {effective_drop*100:.2f}%",
        "",
        f"Legacy Buy   : {legacy_buy_price:,.0f}",
        f"Adaptive Buy : {adaptive_buy_p:,.0f}",
        "",
        f"Basket TP    : +{basket_pct:.2f}%",
        f"Tranche TP   : +{tranche_pct:.2f}%"
    ]
    return "\n".join(lines)

