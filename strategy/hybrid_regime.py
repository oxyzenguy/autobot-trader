"""
하이브리드 국면전환 전략 모듈 (Hybrid Regime-Switching Strategy)

핵심 메커니즘:
1. 시장 국면 판별 (Regime Filter): 
   - 1시간봉 200 이평선(약 8.3일선) 기준
   - 현재가 > 200 MA: 상승 국면 (BULL)
   - 현재가 <= 200 MA: 하락 국면 (BEAR)

2. 상승 국면 (BULL) 매매 로직:
   - 5/20 MA 골든크로스 & 현재가 > 5선: 1차수 매수 (10,000원)
   - 12시간 정기 분할 적립(Time-DCA): 12시간 경과 시마다 10,000원씩 정기 적립 매수 (최대 20회)
   - 다이나믹 트레일링 스탑: 평단가 대비 +10% 도달 시 고점 추적 -> 최고점 대비 -3% 하락 시 일괄 익절
   - 20선 지지 이탈(-1.5%) 또는 평단가 대비 -3.0% 손절 시: 전량 청산 매도 (SELL / Stop-Loss)
   - 그 외: 보유 또는 관망 (HOLD)

3. 하락 국면 (BEAR) 매매 로직:
   - 추세 매수 전면 차단 (가짜 골든크로스 Whipsaw 회피)
   - ClucMay 과매도 낙주 필터: 볼린저 하단 1.5% 이탈 패닉 투매 발생 시에만 1차 진입
   - 마틴게일 매직스플릿 방어 모드 (1-1-2-4 Unit 배수 진입 + 개별 차수 +3% OR 바스켓 익절 이중 트랙)
   - 낙주 신호 미발생 시 100% 현금 보존 관망
"""

import time
import datetime
import pyupbit
import pandas as pd
import numpy as np
from typing import Dict, Any, Optional, Union
from config import REGIME_SWITCH_BUFFER_PCT, REGIME_SWITCH_CONFIRMATION_CANDLES

# API 호출 과다 방지용 메모리 캐시 (종목별 60초 TTL)
_CANDLE_CACHE: Dict[str, Dict[str, Any]] = {}
KST = datetime.timezone(datetime.timedelta(hours=9))


def validate_candles_numerical(df: Optional[pd.DataFrame]) -> None:
    """
    주문 판단에 필요한 캔들 수치의 유한성, 결측치(NaN), 양수 조건 및 OHLC 가격 관계를 공통 검증합니다.
    유효하지 않은 데이터가 감지되면 ValueError를 발생시켜 상위 호출자에서 신규 주문을 차단하도록 합니다.
    - 필수 수치 결측(NaN), 무한대(inf), 0 이하 비정상 가격 차단
    - 기본 가격 관계: high >= low, high >= open, high >= close, low <= open, low <= close
    - 거래량(volume): 음수, NaN, inf 차단
    - 정상적인 0변동성(flat price: open=high=low=close > 0)은 허용
    """
    if df is None or len(df) == 0:
        return

    # 1. 필수 가격 열 확인 (최소 close 필요)
    if 'close' not in df.columns:
        raise ValueError("캔들 데이터에 필수 가격 열('close')이 없습니다.")

    price_cols = [col for col in ['open', 'high', 'low', 'close'] if col in df.columns]

    # 2. 각 가격 열의 결측/무한대/비양수(<=0) 검증
    for col in price_cols:
        vals = pd.to_numeric(df[col], errors='coerce')
        if vals.isna().any():
            raise ValueError(f"가격 열 '{col}'에 결측치(NaN)가 포함되어 있습니다.")
        arr = vals.to_numpy()
        if not np.isfinite(arr).all():
            raise ValueError(f"가격 열 '{col}'에 무한대(inf) 값이 포함되어 있습니다.")
        if (arr <= 0).any():
            raise ValueError(f"가격 열 '{col}'에 0 이하의 비정상 가격이 포함되어 있습니다.")

    # 3. OHLC 기본 가격 관계 정합성 검증 (부동소수점 오차 허용 1e-9)
    if 'high' in df.columns and 'low' in df.columns:
        high = pd.to_numeric(df['high'], errors='coerce').to_numpy()
        low = pd.to_numeric(df['low'], errors='coerce').to_numpy()
        if (high < low - 1e-9).any():
            raise ValueError("고가(high)가 저가(low)보다 낮은 역전 현상이 발견되었습니다.")

        if 'open' in df.columns:
            open_p = pd.to_numeric(df['open'], errors='coerce').to_numpy()
            if (high < open_p - 1e-9).any():
                raise ValueError("고가(high)가 시가(open)보다 낮은 비정상 가격이 발견되었습니다.")
            if (low > open_p + 1e-9).any():
                raise ValueError("저가(low)가 시가(open)보다 높은 비정상 가격이 발견되었습니다.")

        if 'close' in df.columns:
            close_p = pd.to_numeric(df['close'], errors='coerce').to_numpy()
            if (high < close_p - 1e-9).any():
                raise ValueError("고가(high)가 종가(close)보다 낮은 비정상 가격이 발견되었습니다.")
            if (low > close_p + 1e-9).any():
                raise ValueError("저가(low)가 종가(close)보다 높은 비정상 가격이 발견되었습니다.")

    # 4. 거래량(volume) 검증 (존재하는 경우)
    if 'volume' in df.columns:
        vol = pd.to_numeric(df['volume'], errors='coerce')
        if vol.isna().any():
            raise ValueError("거래량(volume) 열에 결측치(NaN)가 포함되어 있습니다.")
        vol_arr = vol.to_numpy()
        if not np.isfinite(vol_arr).all():
            raise ValueError("거래량(volume) 열에 무한대(inf) 값이 포함되어 있습니다.")
        if (vol_arr < 0).any():
            raise ValueError("거래량(volume) 열에 음수 값이 포함되어 있습니다.")


def filter_confirmed_candles(
    df: Optional[pd.DataFrame],
    obs_time: Optional[Union[float, int, datetime.datetime, pd.Timestamp]] = None,
    interval_minutes: int = 60
) -> Optional[pd.DataFrame]:
    """
    봉의 시작/종료 시각과 관측 시각을 비교하여 확정된 캔들만 반환합니다.
    - 관측 시각 이전에 종료(close)된 봉만 확정봉으로 인정합니다 (candle_end <= obs_time).
    - 현재 형성 중인 미완성봉 및 미래 봉(obs_time < candle_end)은 전체 행에서 필터링하여 제외합니다.
    - 이미 확정된 데이터는 반복 필터링해도 마지막 봉이 중복 제거되지 않고 멱등성을 유지합니다.
    - Datetime 정보가 전혀 없는 합성/테스트 데이터(RangeIndex 등)는 이미 확정된 데이터로 취급하여 보존합니다.
    - 타임스탬프 정보가 명시되어 있으나 파싱 실패 또는 유효하지 않은 값이 포함된 경우 예외(ValueError)를 발생시킵니다.
    - 확정된 캔들의 OHLCV 수치 결측/NaN/무한대/역전 등 비정상 값을 공통 검증(validate_candles_numerical)합니다.
    """
    if df is None or len(df) == 0:
        return df

    # 관측 시각을 KST 기준 naive pd.Timestamp로 정규화
    if obs_time is None:
        obs_dt = pd.Timestamp(datetime.datetime.fromtimestamp(time.time(), tz=KST).replace(tzinfo=None))
    elif isinstance(obs_time, (int, float)):
        obs_dt = pd.Timestamp(datetime.datetime.fromtimestamp(obs_time, tz=KST).replace(tzinfo=None))
    elif isinstance(obs_time, (pd.Timestamp, datetime.datetime)):
        if obs_time.tzinfo is not None:
            obs_dt = pd.Timestamp(obs_time.astimezone(KST).replace(tzinfo=None))
        else:
            obs_dt = pd.Timestamp(obs_time)
    else:
        obs_dt = pd.to_datetime(obs_time)
        if getattr(obs_dt, 'tzinfo', None) is not None:
            obs_dt = pd.Timestamp(obs_dt.astimezone(KST).replace(tzinfo=None))
        else:
            obs_dt = pd.Timestamp(obs_dt)

    starts = None
    if isinstance(df.index, pd.DatetimeIndex):
        if df.index.tz is not None:
            starts = df.index.tz_convert(KST).tz_localize(None)
        else:
            starts = df.index
        if starts.isna().any():
            raise ValueError("DatetimeIndex에 유효하지 않은 타임스탬프(NaT)가 포함되어 있습니다.")
    else:
        ts_col = None
        for col in ['candle_date_time_kst', 'candle_date_time_utc', 'dt', 'datetime', 'date']:
            if col in df.columns:
                ts_col = col
                break
        if ts_col is not None:
            try:
                import warnings
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", UserWarning)
                    if ts_col == 'candle_date_time_utc':
                        # UTC 열은 UTC로 파싱 후 KST로 변환
                        parsed = pd.to_datetime(df[ts_col], utc=True, errors='coerce')
                        starts = parsed.dt.tz_convert(KST).dt.tz_localize(None)
                    else:
                        parsed = pd.to_datetime(df[ts_col], errors='coerce')
                        if getattr(parsed.dt, 'tz', None) is not None:
                            starts = parsed.dt.tz_convert(KST).dt.tz_localize(None)
                        else:
                            starts = parsed
            except Exception as e:
                raise ValueError(f"타임스탬프 열 '{ts_col}' 파싱 실패: {e}")
            if starts.isna().any():
                raise ValueError(f"타임스탬프 열 '{ts_col}'에 유효하지 않은 값(NaT)이 포함되어 있습니다.")
        else:
            # 타임스탬프 정보가 없는 합성/테스트 데이터는 이미 확정된 것으로 취급하여 보존
            confirmed = df.copy()
            validate_candles_numerical(confirmed)
            return confirmed

    duration = pd.Timedelta(minutes=interval_minutes)
    candle_ends = starts + duration

    # 전체 행 대상 마감 여부 마스크 (candle_end <= obs_dt)
    mask = (candle_ends <= obs_dt)
    if hasattr(mask, 'to_numpy'):
        mask = mask.to_numpy()
    elif hasattr(mask, 'values'):
        mask = mask.values

    confirmed = df[mask].copy()
    if len(confirmed) > 0:
        validate_candles_numerical(confirmed)
    return confirmed


def get_hybrid_regime_and_signals(
    market: str = "KRW-SOL",
    df: Optional[pd.DataFrame] = None,
    obs_time: Optional[Any] = None
) -> Dict[str, Any]:
    """
    특정 마켓의 최근 1시간봉 지표를 바탕으로 현재 시장 국면과 하이브리드 매매 신호를 반환합니다.
    
    Parameters
    ----------
    market : str
        대상 마켓 코드 (예: 'KRW-SOL', 'KRW-ETH')
    df : pd.DataFrame, optional
        이미 수집된 캔들 데이터프레임이 있을 경우 전달 (백테스트 및 테스트용)
    obs_time : float, int, datetime, pd.Timestamp, optional
        지표 판정 관측 기준 시각 (미지정 시 현재 시각)

    Returns
    -------
    dict
        시장 국면(regime), 신호(signal), 이평선(ma5, ma20, ma200), 이격도 등 상세 정보.
        실시간 조회 시 진행 중인 1시간봉은 국면 판정에서 제외한다.
    """
    now = time.time()
    obs_time_val = now if obs_time is None else obs_time
    
    # 1. 캔들 데이터 로드 (캐시 우선 확인)
    try:
        if df is None:
            if market in _CANDLE_CACHE and (now - _CANDLE_CACHE[market]["timestamp"]) < 50:
                df = filter_confirmed_candles(_CANDLE_CACHE[market]["df"], obs_time=obs_time_val)
            else:
                try:
                    candles = pyupbit.get_ohlcv(market, interval="minute60", count=220)
                    if candles is not None and len(candles) >= 200 + REGIME_SWITCH_CONFIRMATION_CANDLES:
                        _CANDLE_CACHE[market] = {"df": candles.copy(), "timestamp": now}
                        df = filter_confirmed_candles(candles, obs_time=obs_time_val)
                    else:
                        # 실패 시 이전 캐시가 있으면 재사용, 전혀 없으면 HOLD 리턴
                        if market in _CANDLE_CACHE:
                            df = filter_confirmed_candles(_CANDLE_CACHE[market]["df"], obs_time=obs_time_val)
                        else:
                            return {
                                "market": market,
                                "candles_valid": False,
                                "is_valid": False,
                                "regime": "HOLD",
                                "regime_korean": "데이터 수집 대기 (HOLD)",
                                "is_bull": None,
                                "signal": "HOLD",
                                "curr_price": 0.0,
                                "ma5": 0.0, "ma20": 0.0, "ma200": 0.0,
                                "distance_ma200_pct": 0.0,
                                "reason": "시세 데이터 수집 지연으로 기존 상태 유지 및 관망 (HOLD)"
                            }
                except Exception as e:
                    if market in _CANDLE_CACHE:
                        df = filter_confirmed_candles(_CANDLE_CACHE[market]["df"], obs_time=obs_time_val)
                    else:
                        return {
                            "market": market,
                            "candles_valid": False,
                            "is_valid": False,
                            "regime": "HOLD",
                            "regime_korean": "조회 예외 대기 (HOLD)",
                            "is_bull": None,
                            "signal": "HOLD",
                            "curr_price": 0.0,
                            "ma5": 0.0, "ma20": 0.0, "ma200": 0.0,
                            "distance_ma200_pct": 0.0,
                            "reason": f"API 조회 오류({e}) - 관망 유지"
                        }
        else:
            df = filter_confirmed_candles(df, obs_time=obs_time_val)
    except Exception as e:
        return {
            "market": market,
            "candles_valid": False,
            "is_valid": False,
            "regime": "HOLD",
            "regime_korean": "확정봉 필터링 실패 (HOLD)",
            "is_bull": None,
            "signal": "HOLD",
            "curr_price": 0.0,
            "ma5": 0.0, "ma20": 0.0, "ma200": 0.0,
            "distance_ma200_pct": 0.0,
            "reason": f"확정봉 필터링 실패({e}) - 신규 주문 방지"
        }

    minimum_candles = 200 + REGIME_SWITCH_CONFIRMATION_CANDLES - 1
    if df is None or len(df) < minimum_candles:
        return {
            "market": market,
            "candles_valid": False,
            "is_valid": False,
            "regime": "HOLD",
            "regime_korean": "데이터 수집 대기 (HOLD)",
            "is_bull": None,
            "signal": "HOLD",
            "curr_price": 0.0,
            "ma5": 0.0, "ma20": 0.0, "ma200": 0.0,
            "distance_ma200_pct": 0.0,
            "reason": "200 MA 국면 확인에 필요한 확정 1시간봉 데이터가 부족합니다"
        }

    # 2. 이동평균선 및 ClucMay 지표 계산
    close = df["close"]
    high = df["high"]
    low = df["low"]
    volume = df["volume"]

    ma5 = close.rolling(5).mean()
    ma20 = close.rolling(20).mean()
    ma200 = close.rolling(200).mean()

    # ClucMay 낙주 포착 지표 (볼린저 밴드 20, 2 / EMA 50 / 30봉 평균 거래량)
    typical_price = (high + low + close) / 3.0
    bb_mid = typical_price.rolling(20).mean()
    bb_std = typical_price.rolling(20).std()
    bb_lower = bb_mid - 2.0 * bb_std
    ema50 = close.ewm(span=50, adjust=False).mean()
    vol_mean30 = volume.rolling(30).mean()

    curr_price = float(close.iloc[-1])
    curr_ma5 = float(ma5.iloc[-1])
    prev_ma5 = float(ma5.iloc[-2])
    curr_ma20 = float(ma20.iloc[-1])
    prev_ma20 = float(ma20.iloc[-2])
    curr_ma200 = float(ma200.iloc[-1])

    if curr_price <= 0 or not np.isfinite(curr_price) or curr_ma200 <= 0 or not np.isfinite(curr_ma200):
        return {
            "market": market,
            "candles_valid": False,
            "is_valid": False,
            "regime": "HOLD",
            "regime_korean": "지표 수치 이상 (HOLD)",
            "is_bull": None,
            "signal": "HOLD",
            "curr_price": 0.0,
            "ma5": 0.0, "ma20": 0.0, "ma200": 0.0,
            "distance_ma200_pct": 0.0,
            "reason": "현재가 또는 이동평균 수치가 비정상(NaN/무한대/0 이하)입니다"
        }

    curr_bb_lower = float(bb_lower.iloc[-1]) if not pd.isna(bb_lower.iloc[-1]) else curr_price * 0.95
    curr_bb_mid = float(bb_mid.iloc[-1]) if not pd.isna(bb_mid.iloc[-1]) else curr_price
    curr_ema50 = float(ema50.iloc[-1]) if not pd.isna(ema50.iloc[-1]) else curr_price
    curr_vol = float(volume.iloc[-1])
    prev_vol_mean30 = float(vol_mean30.iloc[-2]) if len(vol_mean30) >= 2 and not pd.isna(vol_mean30.iloc[-2]) else float(volume.mean())

    # ClucMay 과매도 투매 조건: 종가 < EMA50 & 종가 < 볼린저하단*0.985 & 거래량 정상
    cluc_threshold = curr_bb_lower * 0.985
    is_cluc_dip = bool(
        (curr_price < curr_ema50) and
        (curr_price < cluc_threshold) and
        (curr_vol < prev_vol_mean30 * 20.0)
    )

    distance_ma200_pct = ((curr_price / curr_ma200) - 1.0) * 100.0 if curr_ma200 > 0 else 0.0
    recent_close = close.iloc[-REGIME_SWITCH_CONFIRMATION_CANDLES:]
    recent_ma200 = ma200.iloc[-REGIME_SWITCH_CONFIRMATION_CANDLES:]
    bull_confirmed = bool((recent_close > recent_ma200 * (1.0 + REGIME_SWITCH_BUFFER_PCT)).all())
    bear_confirmed = bool((recent_close < recent_ma200 * (1.0 - REGIME_SWITCH_BUFFER_PCT)).all())

    if bull_confirmed:
        is_bull = True
        regime = "BULL"
        regime_korean = "상승 국면 (Bull)"
    elif bear_confirmed:
        is_bull = False
        regime = "BEAR"
        regime_korean = "하락 국면 (Bear)"
    else:
        is_bull = None
        regime = "HOLD"
        regime_korean = "국면 전환 확인 대기 (HOLD)"

    # 4. 신호 판별
    if is_bull is None:
        signal = "HOLD"
        reason = (
            f"200 MA ±{REGIME_SWITCH_BUFFER_PCT * 100:.2f}% 완충 구간 또는 "
            f"{REGIME_SWITCH_CONFIRMATION_CANDLES}개 확정봉 연속 확인 대기"
        )
    elif is_bull:
        # 상승장 추세 신호: 5/20 MA 골든크로스 신규 진입 (추세 청산 없이 12h 정기적립 & -10% 긴급손절 및 트레일링 익절만 적용)
        is_golden_cross = (prev_ma5 <= prev_ma20 and curr_ma5 > curr_ma20 and curr_price > curr_ma5)
        
        if is_golden_cross:
            signal = "BUY"
            reason = f"200 MA 상회 중 5/20 MA 골든크로스 발생 (5선 {curr_ma5:,.0f}원 > 20선 {curr_ma20:,.0f}원)"
        elif curr_ma5 > curr_ma20:
            signal = "BUY_ALIGNED"
            reason = f"200 MA 상회 중 5/20 정배열 상승세 유지 (5선 {curr_ma5:,.0f}원 > 20선 {curr_ma20:,.0f}원)"
        else:
            signal = "HOLD"
            reason = f"200 MA 상회 상승 국면 유지 (12h 정기적립 & -10% 긴급손절 감시)"
    else:
        # 하락장 방어 모듈: 마틴게일 배수 진입 + 하이브리드 매직스플릿 이중익절 (개별 +3% OR 바스켓 익절)
        if is_cluc_dip:
            signal = "MARTINGALE_BUY_DIP"
            reason = (
                f"200 MA 하회 중 ClucMay 과매도 낙주 포착! "
                f"기준가({cluc_threshold:,.0f}원) 하회(현재가 {curr_price:,.0f}원): 마틴-매직스플릿 방어 진입"
            )
        else:
            signal = "MARTINGALE_MAGIC_SPLIT_DEFENSE"
            dist_ma = distance_ma200_pct
            reason = (
                f"200 MA 하회 하락 국면 (이격 {dist_ma:+.2f}%): "
                f"마틴게일 배수 진입 + 매직스플릿 이중익절(개별 +3% OR 바스켓 익절) 방어 모드 가동"
            )

    return {
        "market": market,
        "candles_valid": True,
        "is_valid": True,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "curr_price": curr_price,
        "ma5": curr_ma5,
        "ma20": curr_ma20,
        "ma200": curr_ma200,
        "bb_lower": curr_bb_lower,
        "bb_mid": curr_bb_mid,
        "ema50": curr_ema50,
        "cluc_threshold": cluc_threshold,
        "is_cluc_dip": is_cluc_dip,
        "distance_ma200_pct": round(distance_ma200_pct, 2),
        "is_bull": is_bull,
        "regime": regime,
        "regime_korean": regime_korean,
        "signal": signal,
        "reason": reason
    }


def check_magic_split_exits(
    tranches: list,
    current_price: float,
    avg_buy_price: float,
    profit_margin: float = 1.005,
    tranche_profit_pct: float = 0.03
) -> dict:
    """
    하이브리드 마틴게일 이중익절(Dual Exit) 판별:
    1. 바스켓 익절: 전체 평단가 대비 목표 마진(+0.8%/+0.5% 등) 도달 시 전량 청산
    2. 스쿼드별 익절: 각 스쿼드의 가중평균 매수가(VWAP) 대비 +3.0% 반등 시 해당 스쿼드만 단독 익절
       - 기존 개별 차수 +3% 익절을 스쿼드별 익절로 대체
       - 스쿼드 정보가 없는 레거시 차수(squad_id <= 0 or None)는 스쿼드 익절에서 제외(바스켓으로만 탈출)
    """
    # 1. 전체 바스켓 익절 우선 확인
    if avg_buy_price > 0 and current_price >= (avg_buy_price * profit_margin):
        pnl_pct = ((current_price - avg_buy_price) / avg_buy_price) * 100.0
        return {
            "exit_type": "BASKET",
            "target_price": avg_buy_price * profit_margin,
            "current_price": current_price,
            "pnl_pct": pnl_pct,
            "reason": f"전체 포지션 바스켓 익절 조건 달성 (평단가 {avg_buy_price:,.0f}원 대비 {pnl_pct:+.2f}%)"
        }

    # 2. 스쿼드별 가중평균 단가 기준 익절 (+3%)
    squad_map = {}
    for t in tranches:
        s_id = t.get("squad_id")
        if s_id is not None and int(s_id) > 0:
            squad_map.setdefault(int(s_id), []).append(t)

    eligible_squads = []
    for s_id, s_tranches in sorted(squad_map.items()):
        s_vol = sum(float(t.get("volume", 0.0)) for t in s_tranches)
        s_cost = sum(float(t.get("volume", 0.0)) * float(t.get("buy_price", 0.0)) for t in s_tranches)
        if s_vol <= 0 or s_cost <= 0:
            continue
        s_vwap = s_cost / s_vol
        target_p = s_vwap * (1.0 + tranche_profit_pct)
        if current_price >= target_p:
            pnl_pct = ((current_price - s_vwap) / s_vwap) * 100.0
            eligible_squads.append({
                "squad_id": s_id,
                "squad_vwap": s_vwap,
                "target_price": target_p,
                "volume": s_vol,
                "pnl_pct": pnl_pct,
                "tranche_ids": [t.get("tranche_id", t.get("step")) for t in s_tranches],
                "tranches": s_tranches
            })

    if eligible_squads:
        squad_ids = [sq["squad_id"] for sq in eligible_squads]
        return {
            "exit_type": "SQUAD",
            "eligible_squads": eligible_squads,
            "eligible_tranches": [tr for sq in eligible_squads for tr in sq["tranches"]],
            "current_price": current_price,
            "reason": f"스쿼드 {squad_ids} 가중평균가 대비 +{tranche_profit_pct*100:.1f}% 반등 익절 조건 달성"
        }

    return {
        "exit_type": "NONE",
        "reason": "익절 조건 미달성 (대기)"
    }


def check_daily_closing_buy_condition(market: str = "KRW-SOL", current_price: float = 0.0) -> Dict[str, Any]:
    """
    업비트 일봉 마감(08:50 ~ 09:00 KST) 시점에 일봉 양봉(종가 > 시가) 및 5일선 지지 여부를 검사합니다.
    """
    try:
        df_day = pyupbit.get_ohlcv(market, interval="day", count=10)
        if df_day is not None and len(df_day) >= 5:
            today_open = float(df_day['open'].iloc[-1])
            today_close = float(df_day['close'].iloc[-1]) if current_price <= 0 else current_price
            if current_price > 0:
                df_day.loc[df_day.index[-1], 'close'] = current_price
            daily_ma5 = float(df_day['close'].rolling(5).mean().iloc[-1])

            is_bullish_candle = today_close > today_open
            is_above_ma5 = today_close > daily_ma5
            can_buy = is_bullish_candle and is_above_ma5

            if can_buy:
                reason = f"일봉 양봉(시가 {today_open:,.0f}원 < 현재가 {today_close:,.0f}원) & 5일선({daily_ma5:,.0f}원) 지지 충족"
            elif not is_bullish_candle and not is_above_ma5:
                diff_krw = today_close - today_open
                diff_pct = (diff_krw / today_open) * 100.0
                reason = f"일봉 음봉 하락 마감(시가 대비 {diff_krw:+,.0f}원, {diff_pct:+.2f}%) 및 5일선({daily_ma5:,.0f}원) 하회"
            elif not is_bullish_candle:
                diff_krw = today_close - today_open
                diff_pct = (diff_krw / today_open) * 100.0
                reason = f"당일 일봉 음봉 하락 마감 (시가 {today_open:,.0f}원 대비 {diff_krw:+,.0f}원, {diff_pct:+.2f}%)"
            else:
                diff_ma = today_close - daily_ma5
                diff_ma_pct = (diff_ma / daily_ma5) * 100.0
                reason = f"일봉 5일선 지지 실패 (5일선 {daily_ma5:,.0f}원 대비 {diff_ma:+,.0f}원, {diff_ma_pct:+.2f}%)"

            return {
                "can_buy": can_buy,
                "today_open": today_open,
                "today_close": today_close,
                "daily_ma5": daily_ma5,
                "is_bullish_candle": is_bullish_candle,
                "is_above_ma5": is_above_ma5,
                "reason": reason
            }
    except Exception as e:
        return {"can_buy": False, "reason": f"일봉 조회 예외: {e}"}
    return {"can_buy": False, "reason": "일봉 데이터 부족"}


if __name__ == "__main__":
    for m in ["KRW-SOL", "KRW-ETH"]:
        info = get_hybrid_regime_and_signals(m)
        print(f"[{m}] 국면: {info['regime_korean']} | 신호: {info['signal']}")
        print(f"       현재가: {info['curr_price']:,.0f}원 | 200 MA: {info['ma200']:,.0f}원 ({info['distance_ma200_pct']:+.2f}%)")
        print(f"       사유: {info['reason']}\n")
