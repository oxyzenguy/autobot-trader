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
import pyupbit
import pandas as pd
from typing import Dict, Any, Optional

# API 호출 과다 방지용 메모리 캐시 (종목별 60초 TTL)
_CANDLE_CACHE: Dict[str, Dict[str, Any]] = {}


def get_hybrid_regime_and_signals(market: str = "KRW-SOL", df: Optional[pd.DataFrame] = None) -> Dict[str, Any]:
    """
    특정 마켓의 최근 1시간봉 지표를 바탕으로 현재 시장 국면과 하이브리드 매매 신호를 반환합니다.
    
    Parameters
    ----------
    market : str
        대상 마켓 코드 (예: 'KRW-SOL', 'KRW-ETH')
    df : pd.DataFrame, optional
        이미 수집된 캔들 데이터프레임이 있을 경우 전달 (백테스트 및 테스트용)

    Returns
    -------
    dict
        시장 국면(regime), 신호(signal), 이평선(ma5, ma20, ma200), 이격도 등 상세 정보
    """
    now = time.time()
    
    # 1. 캔들 데이터 로드 (캐시 우선 확인)
    if df is None:
        if market in _CANDLE_CACHE and (now - _CANDLE_CACHE[market]["timestamp"]) < 50:
            df = _CANDLE_CACHE[market]["df"]
        else:
            try:
                # 200 MA 계산을 위해 최소 210개 이상의 1시간봉 필요
                candles = pyupbit.get_ohlcv(market, interval="minute60", count=220)
                if candles is not None and len(candles) >= 200:
                    df = candles.copy()
                    _CANDLE_CACHE[market] = {"df": df, "timestamp": now}
                else:
                    # 실패 시 이전 캐시가 있으면 재사용, 전혀 없으면 HOLD 리턴
                    if market in _CANDLE_CACHE:
                        df = _CANDLE_CACHE[market]["df"]
                    else:
                        return {
                            "market": market,
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
                    df = _CANDLE_CACHE[market]["df"]
                else:
                    return {
                        "market": market,
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
        df = df.copy()

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

    # 3. 시장 국면 판별 (200 MA 기준)
    is_bull = curr_price > curr_ma200
    regime = "BULL" if is_bull else "BEAR"
    regime_korean = "상승 국면 (Bull)" if is_bull else "하락 국면 (Bear)"
    distance_ma200_pct = ((curr_price / curr_ma200) - 1.0) * 100.0 if curr_ma200 > 0 else 0.0

    # 4. 신호 판별
    if is_bull:
        # 상승장 추세 신호: 5/20 MA 골든크로스 신규 진입 (추세 청산 없이 12h 정기적립 & -10% 긴급손절 및 트레일링 익절만 적용)
        is_golden_cross = (prev_ma5 <= prev_ma20 and curr_ma5 > curr_ma20 and curr_price > curr_ma5)
        
        if is_golden_cross:
            signal = "BUY"
            reason = f"200 MA 상회 중 5/20 MA 골든크로스 발생 (5선 {curr_ma5:,.0f}원 > 20선 {curr_ma20:,.0f}원)"
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
    하이브리드 매직스플릿 이중익절(Dual Exit) 판별:
    1. 바스켓 익절: 전체 평단가 대비 목표 마진(+0.5% 등) 도달 시 전량 청산
    2. 개별 차수 익절: 각 차수 매수가 대비 +3.0% 반등 시 해당 차수만 단독 익절
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

    # 2. 개별 차수 매직스플릿 익절 확인
    eligible_tranches = []
    for t in tranches:
        buy_p = float(t.get("buy_price", 0.0))
        target_p = buy_p * (1.0 + tranche_profit_pct)
        if buy_p > 0 and current_price >= target_p:
            pnl_pct = ((current_price - buy_p) / buy_p) * 100.0
            eligible_tranches.append({
                "step": t.get("step", 0),
                "buy_price": buy_p,
                "target_price": target_p,
                "volume": float(t.get("volume", 0.0)),
                "units": t.get("units", 1),
                "pnl_pct": pnl_pct
            })

    if eligible_tranches:
        return {
            "exit_type": "TRANCHE",
            "eligible_tranches": eligible_tranches,
            "current_price": current_price,
            "reason": f"개별 {len(eligible_tranches)}개 차수 +{tranche_profit_pct*100:.1f}% 반등 매직스플릿 익절 조건 달성"
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
            daily_ma5 = float(df_day['close'].rolling(5).mean().iloc[-1])

            is_bullish_candle = today_close > today_open
            is_above_ma5 = today_close > daily_ma5

            can_buy = is_bullish_candle and is_above_ma5
            reason = (
                f"일봉 양봉(시가 {today_open:,.0f}원 < 현재가 {today_close:,.0f}원) & 5일선({daily_ma5:,.0f}원) 지지 충족"
                if can_buy else
                f"일봉 조건 미충족 (양봉:{is_bullish_candle}, 5일선지지:{is_above_ma5})"
            )
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
