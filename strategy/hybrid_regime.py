"""
하이브리드 국면전환 전략 모듈 (Hybrid Regime-Switching Strategy)

핵심 메커니즘:
1. 시장 국면 판별 (Regime Filter): 
   - 1시간봉 200 이평선(약 8.3일선) 기준
   - 현재가 > 200 MA: 상승 국면 (BULL)
   - 현재가 <= 200 MA: 하락 국면 (BEAR)

2. 상승 국면 (BULL) 매매 로직:
   - 5/20 MA 골든크로스 & 현재가 > 5선: 매수 (BUY)
   - 5/20 MA 데드크로스 & 현재가 < 5선: 전량 매도 (SELL)
   - 그 외: 보유 또는 관망 (HOLD)

3. 하락 국면 (BEAR) 매매 로직:
   - 추세 매수 전면 차단 (가짜 골든크로스 Whipsaw 회피)
   - 마틴게일 방어 모드 가동 (소액 물타기 1-2-3-6 Unit + 0.5% 기술적 반등 전량 익절)
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
                    # 실패 시 캐시가 있으면 사용, 없으면 빈 딕셔너리 리턴
                    if market in _CANDLE_CACHE:
                        df = _CANDLE_CACHE[market]["df"]
                    else:
                        return {
                            "market": market,
                            "regime": "BEAR",
                            "regime_korean": "하락 국면 (Bear - 데이터 조회 지연)",
                            "signal": "MARTINGALE_DEFENSE",
                            "curr_price": 0.0,
                            "ma5": 0.0, "ma20": 0.0, "ma200": 0.0,
                            "distance_ma200_pct": 0.0,
                            "reason": "시세 데이터 수집 지연으로 기본 방어 모드 유지"
                        }
            except Exception as e:
                return {
                    "market": market,
                    "regime": "BEAR",
                    "regime_korean": "하락 국면 (Bear - 조회 예외)",
                    "signal": "MARTINGALE_DEFENSE",
                    "curr_price": 0.0,
                    "ma5": 0.0, "ma20": 0.0, "ma200": 0.0,
                    "distance_ma200_pct": 0.0,
                    "reason": f"API 조회 오류({e})"
                }
    else:
        df = df.copy()

    # 2. 이동평균선 계산
    close = df["close"]
    ma5 = close.rolling(5).mean()
    ma20 = close.rolling(20).mean()
    ma200 = close.rolling(200).mean()

    curr_price = float(close.iloc[-1])
    curr_ma5 = float(ma5.iloc[-1])
    prev_ma5 = float(ma5.iloc[-2])
    curr_ma20 = float(ma20.iloc[-1])
    prev_ma20 = float(ma20.iloc[-2])
    curr_ma200 = float(ma200.iloc[-1])

    # 3. 시장 국면 판별 (200 MA 기준)
    is_bull = curr_price > curr_ma200
    regime = "BULL" if is_bull else "BEAR"
    regime_korean = "상승 국면 (Bull)" if is_bull else "하락 국면 (Bear)"
    distance_ma200_pct = ((curr_price / curr_ma200) - 1.0) * 100.0 if curr_ma200 > 0 else 0.0

    # 4. 신호 판별
    if is_bull:
        # 상승장 5/20 MA 추세 신호
        is_golden_cross = (prev_ma5 <= prev_ma20 and curr_ma5 > curr_ma20 and curr_price > curr_ma5)
        is_dead_cross = (prev_ma5 >= prev_ma20 and curr_ma5 < curr_ma20 and curr_price < curr_ma5)
        
        if is_golden_cross:
            signal = "BUY"
            reason = f"200 MA 상회 중 5/20 MA 골든크로스 발생 (5선 {curr_ma5:,.0f}원 > 20선 {curr_ma20:,.0f}원)"
        elif is_dead_cross:
            signal = "SELL"
            reason = f"5/20 MA 데드크로스 발생으로 추세 청산 (5선 {curr_ma5:,.0f}원 < 20선 {curr_ma20:,.0f}원)"
        else:
            signal = "HOLD"
            reason = f"200 MA 상회 중 추세 유지 (5선 {curr_ma5:,.0f}원, 20선 {curr_ma20:,.0f}원)"
    else:
        # 하락장 방어 신호
        signal = "MARTINGALE_DEFENSE"
        reason = f"현재가({curr_price:,.0f}원)가 200 MA({curr_ma200:,.0f}원) 이하: 마틴게일 소액 물타기 방어 가동"

    return {
        "market": market,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "curr_price": curr_price,
        "ma5": curr_ma5,
        "ma20": curr_ma20,
        "ma200": curr_ma200,
        "distance_ma200_pct": round(distance_ma200_pct, 2),
        "is_bull": is_bull,
        "regime": regime,
        "regime_korean": regime_korean,
        "signal": signal,
        "reason": reason
    }


if __name__ == "__main__":
    for m in ["KRW-SOL", "KRW-ETH"]:
        info = get_hybrid_regime_and_signals(m)
        print(f"[{m}] 국면: {info['regime_korean']} | 신호: {info['signal']}")
        print(f"       현재가: {info['curr_price']:,.0f}원 | 200 MA: {info['ma200']:,.0f}원 ({info['distance_ma200_pct']:+.2f}%)")
        print(f"       사유: {info['reason']}\n")
