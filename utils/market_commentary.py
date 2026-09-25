"""
시장 시황 분석 및 Gemini AI 마켓 코멘터리 생성 모듈 (Market Commentary)

주요 기능:
1. 3대 코인(BTC, ETH, SOL) 실시간 1시간봉/일봉 기술적 지표(RSI, MA200 이격도, 5/20 MA 크로스) 산출
2. 코인별 1줄 핵심 기술 시황 텍스트 생성 (옵션 1)
3. Google Gemini AI 기반 2~3문장 종합 마켓 코멘터리 자동 생성 (옵션 3, 장애 시 룰 기반 자동 폴백)
"""

import os
import time
import requests
import pyupbit
import pandas as pd
from typing import Dict, Any, Optional
from dotenv import load_dotenv

load_dotenv()

# Google Gemini API 설정
_GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY", "").strip('"').strip("'")
_PREFERRED_GEMINI_MODELS = ["gemini-3.5-flash-lite", "gemini-flash-latest", "gemini-3.1-flash-lite"]


def calculate_rsi(series: pd.Series, period: int = 14) -> float:
    """14기간 상대강도지수(RSI) 계산"""
    if series is None or len(series) < period + 1:
        return 50.0
    try:
        delta = series.diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)
        avg_gain = gain.rolling(window=period, min_periods=period).mean()
        avg_loss = loss.rolling(window=period, min_periods=period).mean()
        rs = avg_gain / avg_loss.replace(0, 1e-9)
        rsi = 100 - (100 / (1 + rs))
        val = float(rsi.iloc[-1])
        return round(val, 1) if not pd.isna(val) else 50.0
    except Exception:
        return 50.0


def get_rsi_desc(rsi: float) -> str:
    """RSI 수치 기반 심리 상태 문자열 반환"""
    if rsi >= 70:
        return "과매수"
    elif rsi <= 30:
        return "과매도"
    elif rsi >= 55:
        return "매수우위"
    elif rsi <= 45:
        return "매도우위"
    else:
        return "중립"


def get_all_markets_technical_summary() -> Dict[str, Dict[str, Any]]:
    """
    3대 코인(BTC, ETH, SOL)의 실시간 시세 및 기술적 지표를 집계하여
    코인별 1줄 기술 시황 및 AI 분석용 원천 데이터를 반환합니다.
    """
    results = {}

    # 1. KRW-BTC
    try:
        from strategy.btc_accumulator import check_btc_tiered_status
        btc_stat = check_btc_tiered_status()
        cur_p = btc_stat.get("current_price", 0.0)
        ma200 = btc_stat.get("ma200", 0.0)
        avg_p = btc_stat.get("account_avg_price", 0.0)
        dist_ma = btc_stat.get("dist_ma200_pct", 0.0)
        dist_avg = btc_stat.get("dist_avg_pct", 0.0)

        df_h = pyupbit.get_ohlcv("KRW-BTC", interval="minute60", count=30)
        rsi_1h = calculate_rsi(df_h["close"]) if df_h is not None else 50.0
        rsi_desc = get_rsi_desc(rsi_1h)

        ma_label = f"200일선({dist_ma:+.1f}%) 위 중기 상승 지지" if dist_ma >= 0 else f"200일선({dist_ma:+.1f}%) 하회(단기 하락)"
        avg_label = f"평단 대비 {dist_avg:+.1f}% 세일 구간" if dist_avg < 0 else f"평단 대비 {dist_avg:+.1f}% 수익 구간"
        rsi_label = f"RSI {rsi_1h:.0f}({rsi_desc})"

        summary_line = f"{ma_label} | {avg_label} | {rsi_label}"
        results["KRW-BTC"] = {
            "current_price": cur_p,
            "ma200": ma200,
            "dist_ma200_pct": dist_ma,
            "dist_avg_pct": dist_avg,
            "rsi_1h": rsi_1h,
            "rsi_desc": rsi_desc,
            "summary_line": summary_line
        }
    except Exception as e:
        results["KRW-BTC"] = {"summary_line": "기술 지표 분석 대기 중", "error": str(e)}

    # 2. KRW-ETH & KRW-SOL
    for m in ["KRW-ETH", "KRW-SOL"]:
        try:
            from strategy.hybrid_regime import get_hybrid_regime_and_signals
            reg_info = get_hybrid_regime_and_signals(m)
            cur_p = reg_info.get("curr_price", 0.0)
            if cur_p <= 0:
                cur_p = pyupbit.get_current_price(m) or 0.0
            ma200 = reg_info.get("ma200", 0.0)
            ma5 = reg_info.get("ma5", 0.0)
            ma20 = reg_info.get("ma20", 0.0)
            dist_ma = reg_info.get("distance_ma200_pct", 0.0)
            is_bull = reg_info.get("is_bull", True)

            df_h = pyupbit.get_ohlcv(m, interval="minute60", count=30)
            rsi_1h = calculate_rsi(df_h["close"]) if df_h is not None else 50.0
            rsi_desc = get_rsi_desc(rsi_1h)

            reg_label = f"200시간선({dist_ma:+.1f}%) 상회(Bull)" if is_bull else f"200시간선({dist_ma:+.1f}%) 하회(Bear)"
            ma_cross = "5/20 MA 정배열(상승)" if ma5 >= ma20 else "5/20 MA 역배열(단기조정)"
            rsi_label = f"RSI {rsi_1h:.0f}({rsi_desc})"

            summary_line = f"{reg_label} | {ma_cross} | {rsi_label}"
            results[m] = {
                "current_price": cur_p,
                "ma200": ma200,
                "dist_ma200_pct": dist_ma,
                "is_bull": is_bull,
                "ma5": ma5,
                "ma20": ma20,
                "rsi_1h": rsi_1h,
                "rsi_desc": rsi_desc,
                "summary_line": summary_line
            }
        except Exception as e:
            results[m] = {"summary_line": "기술 지표 분석 대기 중", "error": str(e)}

    return results


def _build_rule_based_fallback_commentary(tech_data: Dict[str, Dict[str, Any]]) -> str:
    """Gemini API 호출 불가 시 실시간 지표 기반 룰 기반 자동 완성 시황"""
    btc = tech_data.get("KRW-BTC", {})
    eth = tech_data.get("KRW-ETH", {})
    sol = tech_data.get("KRW-SOL", {})

    btc_dist = btc.get("dist_ma200_pct", 0.0)
    btc_pnl = btc.get("dist_avg_pct", 0.0)
    eth_bull = eth.get("is_bull", True)
    sol_bull = sol.get("is_bull", True)

    sentences = []
    if btc_dist >= 0:
        sentences.append(f"비트코인은 200일선 대비 {btc_dist:+.1f}% 위에서 중기 상승 추세를 지지하며 평단가 대비 단기 할인({btc_pnl:+.1f}%) 구간에 머물고 있습니다.")
    else:
        sentences.append(f"비트코인은 200일선 아래에서 바닥 다지기를 진행 중이며 가중 적립 전략으로 수량을 축적하고 있습니다.")

    if eth_bull and sol_bull:
        sentences.append("이더리움과 솔라나 모두 200시간선 기준 상승 국면(Bull)을 안정적으로 유지하며 지지선 방어 및 추가 반등을 모색 중입니다.")
    elif eth_bull or sol_bull:
        sentences.append("알트코인은 개별 종목별 차별화 장세가 이어지는 가운데 주요 이동평균선 지지 여부를 테스트하고 있습니다.")
    else:
        sentences.append("알트코인은 단기 하락 방어 모드에서 무리한 추격 매수를 지양하고 보수적으로 리스크를 관리하고 있습니다.")

    return " ".join(sentences)


def generate_gemini_market_commentary(tech_data: Dict[str, Dict[str, Any]]) -> str:
    """
    Google Gemini AI를 호출하여 정기 브리핑용 2~3문장 고품질 마켓 코멘터리를 생성합니다.
    API 키가 없거나 네트워크 오류 시 룰 기반 고품질 시황으로 100% 자동 폴백합니다.
    """
    if not _GOOGLE_API_KEY:
        return _build_rule_based_fallback_commentary(tech_data)

    btc = tech_data.get("KRW-BTC", {})
    eth = tech_data.get("KRW-ETH", {})
    sol = tech_data.get("KRW-SOL", {})

    prompt = f"""당신은 전문 암호화폐 퀀트 수석 애널리스트입니다.
아래 실시간 암호화폐 기술 지표를 바탕으로, 텔레그램 정기 브리핑 상단에 들어갈 2~3문장(120자 내외)의 간결하고 품격 있는 핵심 마켓 시황 코멘트를 한국어로 작성해주세요.
인사말이나 서론 없이 바로 시황 본문만 출력하세요.

[실시간 기술 지표]
• 비트코인(BTC): 현재가 {btc.get('current_price', 0):,.0f}원, 200일선 대비 {btc.get('dist_ma200_pct', 0):+.1f}% 지지, 계좌 평단 대비 {btc.get('dist_avg_pct', 0):+.1f}% 구간, 1시간 RSI {btc.get('rsi_1h', 50):.0f}({btc.get('rsi_desc', '중립')})
• 이더리움(ETH): 현재가 {eth.get('current_price', 0):,.0f}원, 200시간선 대비 {eth.get('dist_ma200_pct', 0):+.1f}% ({'상승국면' if eth.get('is_bull') else '하락국면'}), 1시간 RSI {eth.get('rsi_1h', 50):.0f}({eth.get('rsi_desc', '중립')})
• 솔라나(SOL): 현재가 {sol.get('current_price', 0):,.0f}원, 200시간선 대비 {sol.get('dist_ma200_pct', 0):+.1f}% ({'상승국면' if sol.get('is_bull') else '하락국면'}), 1시간 RSI {sol.get('rsi_1h', 50):.0f}({sol.get('rsi_desc', '중립')})
"""

    headers = {"Content-Type": "application/json"}
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.2
        }
    }

    # 후보 모델 순차 시도 (빠르고 안정적인 Flash-Lite 우선)
    for model in _PREFERRED_GEMINI_MODELS:
        try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={_GOOGLE_API_KEY}"
            resp = requests.post(url, headers=headers, json=payload, timeout=6)
            if resp.status_code == 200:
                data = resp.json()
                candidates = data.get("candidates", [])
                if candidates and "content" in candidates[0]:
                    parts = candidates[0]["content"].get("parts", [])
                    if parts and "text" in parts[0]:
                        commentary = parts[0]["text"].strip()
                        if len(commentary) > 15:
                            return commentary
        except Exception:
            continue

    # 모든 모델 호출 실패 시 룰 기반 폴백 반환
    return _build_rule_based_fallback_commentary(tech_data)
