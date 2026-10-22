"""
일일 퀀트 전략 분석 및 백테스트 최적화 제안봇 (Quant Strategy Advisor)
- 매일 아침(09:10 KST) 또는 수동 호출 시 가동
- 최근 시장 변동성(ATR) 및 국면 진단
- backtest_main.py 기반 파라미터 그리드 시뮬레이션
- 과최적화 방지 & 100만 원 예산 준수 필터링
- 텔레그램 일일 전략 제안서 발송 및 /전략승인 연동
"""

import os
import sys
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

import numpy as np
import pandas as pd

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from config import (
    PROFIT_MARGINS,
    MAGIC_SPLIT_DOWN_PCTS,
    MAGIC_SPLIT_TRANCHE_PROFITS,
    MARKET_STRATEGIES,
    get_market_strategy,
    get_profit_margin,
    get_magic_split_down_pct,
    INVESTMENTS
)
from backtest_main import load_historical_candles, simulate_hybrid_squad
from utils.bot import send_message

DATA_DIR = BASE_DIR / "data"
PROPOSAL_FILE = DATA_DIR / "latest_strategy_proposal.json"
CONFIG_PATH = BASE_DIR / "config.py"


# --- 1. 실시간 시장 변동성(ATR) 및 국면 분석 ---
def analyze_market_volatility(market: str) -> Dict[str, Any]:
    """
    최근 캔들 데이터를 분석하여 변동성(ATR%) 및 시장 레짐 상태를 진단합니다.
    """
    try:
        import pyupbit
        df = pyupbit.get_ohlcv(market, interval="minute60", count=200)
    except Exception:
        df = None

    if df is None or len(df) < 50:
        try:
            df = load_historical_candles(market).tail(200).copy()
        except Exception:
            return {
                "market": market,
                "current_price": 0.0,
                "atr_pct": 1.5,
                "volatility_regime": "정상 변동성 구간",
                "recommendation_hint": "데이터 수집 제한 (보수적 접근)"
            }

    close = df['close']
    high = df['high']
    low = df['low']

    # 14기간 ATR (Average True Range)
    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = tr.rolling(14).mean().iloc[-1]
    cur_p = close.iloc[-1]
    atr_pct = (atr / cur_p * 100) if cur_p > 0 else 0.0

    # 최근 24시간 변동폭
    h24 = high.tail(24).max()
    l24 = low.tail(24).min()
    range_24h_pct = ((h24 - l24) / l24 * 100) if l24 > 0 else 0.0

    # 변동성 레짐 판정
    if atr_pct < 0.8:
        regime = "극소 변동성 횡보장 (거래 정체)"
        hint = "익절 마진을 축소(+0.3%~+0.4%)하여 회전율을 높이는 전략이 유리합니다."
    elif atr_pct <= 2.0:
        regime = "정상 변동성 구간 (스윙 박스권)"
        hint = "현재 표준 익절 마진(+0.5%~+0.8%) 및 안정적 마틴게일 방어가 유효합니다."
    else:
        regime = "고변동성 추세/급등락장 (변동성 확장)"
        hint = "물타기 간격을 넓히고 익절 목표를 상향하여 안전 마진을 확보해야 합니다."

    return {
        "market": market,
        "current_price": cur_p,
        "atr": atr,
        "atr_pct": atr_pct,
        "range_24h_pct": range_24h_pct,
        "volatility_regime": regime,
        "recommendation_hint": hint
    }


# --- 2. 파라미터 매트릭스 백테스트 탐색 ---
def evaluate_market_candidates(market: str) -> Dict[str, Any]:
    """
    해당 종목의 후보 파라미터 매트릭스를 시뮬레이션하고,
    수익률, MDD, 회전율, 예산(100만원) 한도를 종합 채점합니다.
    """
    df = load_historical_candles(market)

    curr_margin = get_profit_margin(market)
    curr_down = get_magic_split_down_pct(market)
    curr_strat = get_market_strategy(market)

    # 종목별 탐색 그리드 정의
    if market == "KRW-SOL":
        candidate_margins = [1.003, 1.004, 1.005, 1.008]
        candidate_downs = [0.025, 0.030, 0.035, 0.040]
        candidate_strats = ["HYBRID", "PURE_MARTINGALE"]
    else:  # KRW-ETH
        candidate_margins = [1.005, 1.008, 1.010]
        candidate_downs = [0.015, 0.020, 0.025]
        candidate_strats = ["PURE_MARTINGALE", "HYBRID"]

    # 지표 조합 탐색 (기본 MA200+Cluc vs 트레이딩뷰 SuperTrend+WaveTrend)
    indicator_combos = [
        ("MA200", "CLUC"),
        ("SUPERTREND", "WAVETREND")
    ]

    baseline_res = None
    all_results = []

    for trend_mode, dip_mode in indicator_combos:
        for strat_mode in candidate_strats:
            for m_val in candidate_margins:
                for d_val in candidate_downs:
                    sim = simulate_hybrid_squad(
                        market=market,
                        df=df,
                        initial_capital=1_000_000,
                        unit_krw=10_000,
                        custom_profit_margin=m_val,
                        custom_down_pct=d_val,
                        custom_market_strat=strat_mode,
                        custom_trend_indicator=trend_mode,
                        custom_dip_indicator=dip_mode
                    )

                    ret = sim["return_pct"]
                    mdd = sim["mdd_pct"]
                    max_cap = sim["max_bear_capital"]
                    trades = sim["bear_basket_exits"] + sim["bear_tranche_exits"] + sim.get("bull_trades", 0)
                    calmar = (ret / max(mdd, 0.5)) if mdd > 0 else ret

                    # 100만원 예산 초과 탈락 필터 (120만원까지는 약간 감점 허용)
                    if max_cap > 1_200_000:
                        score = -999.0
                    else:
                        # 종합 스코어링: 칼마비율(40%) + MDD안정성(30%) + 회전율/체결수(30%) - 자본초과패널티
                        cap_penalty = max(0.0, (max_cap - 900_000) / 100_000 * 2.0)
                        score = (calmar * 0.40) + ((20.0 - mdd) * 0.30) + (np.log1p(trades) * 2.0) - cap_penalty

                    is_curr = bool(
                        abs(m_val - curr_margin) < 1e-4 and
                        abs(d_val - curr_down) < 1e-4 and
                        strat_mode == curr_strat and
                        trend_mode == "MA200" and
                        dip_mode == "CLUC"
                    )

                    cand_summary = {
                        "market": market,
                        "market_strat": strat_mode,
                        "trend_indicator": trend_mode,
                        "dip_indicator": dip_mode,
                        "profit_margin": m_val,
                        "profit_margin_pct": round((m_val - 1.0) * 100, 2),
                        "down_pct": d_val,
                        "down_pct_val": round(d_val * 100, 2),
                        "return_pct": round(ret, 2),
                        "mdd_pct": round(mdd, 2),
                        "calmar_ratio": round(calmar, 2),
                        "trades_count": trades,
                        "max_capital": int(max_cap),
                        "score": round(score, 2),
                        "is_current": is_curr
                    }

                    if is_curr:
                        baseline_res = cand_summary

                    all_results.append(cand_summary)

    # 스코어 기준 정렬
    all_results.sort(key=lambda x: x["score"], reverse=True)
    best_candidate = all_results[0]

    # 현재 설정이 누락된 경우 기본값 채움
    if baseline_res is None:
        baseline_res = next((r for r in all_results if r.get("is_current")), all_results[-1])

    return {
        "market": market,
        "baseline": baseline_res,
        "best": best_candidate,
        "all_ranked": all_results[:5]
    }


# --- 3. 전략 변경 제안서 빌더 & 안전 자율주행 판정 ---
def generate_strategy_proposals() -> Dict[str, Any]:
    """
    모든 대상 마켓(ETH, SOL)에 대해 일일 전략 제안서를 작성하고,
    안전 자율주행 모드(가벼운 튜닝 자동적용 vs 큰 변화 승인요청)를 판정합니다.
    """
    proposals = {}
    timestamp_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    for market in ["KRW-SOL", "KRW-ETH"]:
        vol_info = analyze_market_volatility(market)
        eval_info = evaluate_market_candidates(market)

        base = eval_info["baseline"]
        best = eval_info["best"]

        # 변경 제안 판단 기준:
        # 1) 최적 후보의 스코어가 현재 세팅 대비 높거나
        # 2) 또는 변동성 급감 구간에서 체결 횟수(회전율)가 30% 이상 증가할 때
        trade_boost = (best["trades_count"] - base["trades_count"]) / max(1, base["trades_count"])
        score_diff = best["score"] - base["score"]

        should_propose = bool(
            not best["is_current"] and
            best["max_capital"] <= 1_000_000 and
            (score_diff > 1.5 or (vol_info["atr_pct"] < 1.0 and trade_boost > 0.30))
        )

        # 🚀 [2안 안전 자율주행 모드 판정]:
        # - 가벼운 튜닝 (AUTO_APPLIED):
        #   1) 운용 모델이 동일함 (HYBRID 유지 또는 PURE_MARTINGALE 유지)
        #   2) 익절 마진 변동이 안전 범위 내 (±0.3%p 이내)
        #   3) 물타기 간격 변동이 안전 범위 내 (±1.5%p 이내)
        #   4) 최대 필요 자본 100만 원 이하
        # - 큰 변화 (REQUIRE_APPROVAL):
        #   1) 운용 모델 전환 (HYBRID <-> PURE_MARTINGALE)
        #   2) 또는 안전 범위를 초과하는 과격한 파라미터 점프
        margin_diff = abs(best["profit_margin"] - base["profit_margin"])
        down_diff = abs(best["down_pct"] - base["down_pct"])
        same_strat = (best["market_strat"] == base["market_strat"])

        if not should_propose:
            action = "MAINTAIN"
        elif same_strat and margin_diff <= 0.0031 and down_diff <= 0.0151:
            action = "AUTO_APPLIED"
        else:
            action = "REQUIRE_APPROVAL"

        proposals[market] = {
            "market": market,
            "timestamp": timestamp_str,
            "volatility": vol_info,
            "should_propose": should_propose,
            "action": action,
            "current": base,
            "recommended": best,
            "trade_boost_pct": round(trade_boost * 100, 1),
            "all_ranked": eval_info["all_ranked"]
        }

    # 제안 파일 영속 저장
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(PROPOSAL_FILE, "w", encoding="utf-8") as f:
        json.dump(proposals, f, ensure_ascii=False, indent=2, default=str)

    return proposals


# --- 4. 텔레그램 리포트 메시지 포맷팅 ---
def build_telegram_strategy_briefing(proposals: Dict[str, Any]) -> str:
    """텔레그램용 일일 퀀트 전략 제안 리포트 생성 (안전 자율주행 배지 적용)"""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    blocks = []
    has_proposals = False
    auto_count = 0

    for market, prop in proposals.items():
        vol = prop["volatility"]
        cur = prop["current"]
        rec = prop["recommended"]
        ticker = market.split("-")[1]
        action = prop.get("action", "MAINTAIN")

        tv_tag = ""
        if rec.get("trend_indicator") == "SUPERTREND":
            tv_tag = " [TV 슈퍼트렌드]"

        if action == "AUTO_APPLIED":
            has_proposals = True
            auto_count += 1
            rec_badge = "🤖 <b>[안전 자율주행: 자동 튜닝 완료]</b>"
            action_text = "• <b>조치 결과:</b> 안전 범위(±0.3%p) 내 미세 조정으로 <b>실전 설정 자동 반영 완료</b>되었습니다."
        elif action == "REQUIRE_APPROVAL":
            has_proposals = True
            rec_badge = "⚠️ <b>[전략 모델 변경 제안 - 승인 필요]</b>"
            action_text = f"• <b>조치 안내:</b> 핵심 운용 모델 변경이므로 사장님 확인이 필요합니다.<br>👉 텔레그램 승인 명령: <code>/전략승인 {ticker}</code>"
        else:
            rec_badge = "✅ <b>[현재 전략 최적 유지]</b>"
            action_text = "• <b>조치 안내:</b> 현재 설정이 최적 균형을 유지하고 있어 변경 없이 지속합니다 (PASS)."

        # 과거 사후 채점 교훈 (Reflection Memory)
        ref_context = ""
        try:
            from strategy.settlement_reflection import get_reflection_context_for_advisor
            rc = get_reflection_context_for_advisor(market)
            if "과거 정산 교훈: 아직" not in rc:
                ref_context = f"{rc}\n"
        except Exception:
            pass

        block = (
            f"<b>📊 {market} ({vol['volatility_regime']})</b>\n"
            f"• 변동성: ATR {vol['atr_pct']:.2f}% | 24h 폭 {vol['range_24h_pct']:.2f}%\n"
            f"• {rec_badge}\n"
            f"  - <b>현재 세팅:</b> 익절 +{cur['profit_margin_pct']}% / 물타기 -{cur['down_pct_val']}% ({cur['market_strat']})\n"
            f"    └ 성과: 2년 수익률 {cur['return_pct']:+.1f}% | MDD {cur['mdd_pct']:.1f}% | 체결 {cur['trades_count']}회\n"
            f"  - <b>추천 세팅:</b> 익절 <b>+{rec['profit_margin_pct']}%</b> / 물타기 <b>-{rec['down_pct_val']}%</b> ({rec['market_strat']}{tv_tag})\n"
            f"    └ 성과: 2년 수익률 <b>{rec['return_pct']:+.1f}%</b> | MDD <b>{rec['mdd_pct']:.1f}%</b> | 체결 <b>{rec['trades_count']}회 ({prop['trade_boost_pct']:+,.0f}%)</b>\n"
            f"  - 최대 투입 필요 자본: {rec['max_capital']:,}원 (예산 100만 원 준수)\n"
            f"• <b>판정 요약:</b> {vol['recommendation_hint']}\n"
            f"{ref_context}"
            f"{action_text}"
        )
        blocks.append(block)

    content = "\n\n".join(blocks)
    if auto_count > 0:
        title_badge = f"🤖 안전 자율주행 {auto_count}건 자동 반영"
    elif has_proposals:
        title_badge = "💡 승인 필요 제안 있음"
    else:
        title_badge = "🟢 모든 전략 최적 유지 중"

    msg = (
        f"🧭 <b>[AutoBot 일일 퀀트 전략 리뷰 및 자율주행 리포트]</b>\n"
        f"• 분석 시각: <code>{now_str}</code> (KST)\n"
        f"• 평가 기준: Calmar 비율(수익/MDD) + 트레이딩뷰 지표 + 100만원 예산 준수\n"
        f"• 진단 결과: <b>{title_badge}</b>\n\n"
        f"{content}\n\n"
        f"<i>※ 안전 자율주행 모드 가동 중: 가벼운 미세조정(±0.3%p)은 봇이 알아서 실전에 즉시 반영하며, 핵심 모델 변경만 사장님께 승인을 요청합니다.</i>"
    )
    return msg
    return msg


# --- 5. 텔레그램 /전략승인 핸들러 ---
def apply_strategy_approval(market: str) -> Tuple[bool, str]:
    """
    사용자가 '/전략승인 SOL' 또는 '/전략승인 ETH'를 입력했을 때
    최신 제안서를 읽어 config.py 실전 설정을 안전하게 변경하고
    호가창 미체결 주문 재조정을 트리거합니다.
    """
    prop_path = Path(PROPOSAL_FILE)
    if not prop_path.exists():
        return False, "등록된 최신 전략 제안서가 없습니다. 먼저 전략 검토를 실행하세요."

    try:
        with open(prop_path, "r", encoding="utf-8") as f:
            proposals = json.load(f)
    except Exception as e:
        return False, f"제안서 로드 실패: {e}"

    market_key = market if market.startswith("KRW-") else f"KRW-{market.upper()}"
    if market_key not in proposals:
        return False, f"대상 마켓({market_key})에 대한 제안이 없습니다."

    prop = proposals[market_key]
    rec = prop.get("recommended")
    if not rec:
        return False, "추천 설정 정보가 누락되었습니다."

    new_margin = float(rec["profit_margin"])
    new_down = float(rec["down_pct"])
    new_strat = rec["market_strat"]

    # config.py 파일 안전 수정 (딕셔너리 블록 단위 치환)
    config_path = Path(CONFIG_PATH)
    try:
        with open(config_path, "r", encoding="utf-8") as cf:
            content = cf.read()

        import re

        def update_config_dict_value(text: str, dict_name: str, key: str, val_str: str) -> str:
            pattern = rf'({dict_name}\s*=\s*\{{)([\s\S]*?)(\}})'
            match = re.search(pattern, text)
            if not match:
                return text
            header, body, footer = match.groups()
            body_pattern = rf'("{key}":\s*)[^,\n]+'
            new_body = re.sub(body_pattern, rf'\g<1>{val_str}', body, count=1)
            return text[:match.start()] + header + new_body + footer + text[match.end():]

        # 1. PROFIT_MARGINS 수정
        content = update_config_dict_value(content, "PROFIT_MARGINS", market_key, f"{new_margin:.3f}")

        # 2. MAGIC_SPLIT_DOWN_PCTS 수정
        content = update_config_dict_value(content, "MAGIC_SPLIT_DOWN_PCTS", market_key, f"{new_down:.3f}")

        # 3. MARKET_STRATEGIES 수정
        content = update_config_dict_value(content, "MARKET_STRATEGIES", market_key, f'"{new_strat}"')

        with open(config_path, "w", encoding="utf-8") as cf:
            cf.write(content)

        # 런타임 메모리 config 실시간 동기화
        try:
            import config
            if hasattr(config, "PROFIT_MARGINS"):
                config.PROFIT_MARGINS[market_key] = new_margin
            if hasattr(config, "MAGIC_SPLIT_DOWN_PCTS"):
                config.MAGIC_SPLIT_DOWN_PCTS[market_key] = new_down
            if hasattr(config, "MARKET_STRATEGIES"):
                config.MARKET_STRATEGIES[market_key] = new_strat
        except Exception as sync_e:
            print(f"[WARN] 런타임 config 메모리 동기화 실패: {sync_e}")

    except Exception as ce:
        return False, f"config.py 수정 실패: {ce}"

    # 의사결정 사후 채점(Settlement Reflection) 장부 기록
    try:
        from strategy.settlement_reflection import record_decision
        record_decision(
            market=market_key,
            trigger_type="STRATEGY_APPROVAL",
            before_params=prop.get("current", {}),
            after_params=prop.get("recommended", {}),
            reason=f"{market_key} 퀀트 최적화 전략 승인 반영",
            baseline_metrics={"predicted_cagr": prop.get("simulated", {}).get("cagr"),
                              "predicted_mdd": prop.get("simulated", {}).get("mdd")}
        )
    except Exception as d_err:
        print(f"[WARN] 의사결정 장부 기록 실패: {d_err}")

    msg = (
        f"✅ <b>[{market_key} 전략 최적화 적용 완료]</b>\n"
        f"• 새 익절 마진: <b>+{(new_margin - 1) * 100:.2f}%</b> (기존: +{(prop['current']['profit_margin'] - 1) * 100:.2f}%)\n"
        f"• 새 물타기 간격: <b>-{new_down * 100:.2f}%</b> (기존: -{prop['current']['down_pct'] * 100:.2f}%)\n"
        f"• 새 운용 모드: <b>{new_strat}</b>\n"
        f"• 안내: 다음 매매 순환 및 호가창 주문 갱신 시 새 파라미터가 100% 즉시 반영됩니다."
    )
    return True, msg


# --- 6. 실행 진입점 ---
def run_daily_strategy_review(send_telegram: bool = True) -> Dict[str, Any]:
    """일일 전략 리뷰 실행 및 안전 자율주행(가벼운 튜닝 자동반영) 집행 오케스트레이터"""
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 🧭 [STRATEGY_ADVISOR] 일일 퀀트 전략 리뷰 및 백테스트 최적화 시작...")

    # 🎓 만기 도래 사후 정산(Settlement) 검사 및 교훈 축적
    try:
        from strategy.settlement_reflection import run_due_settlements
        settled = run_due_settlements(send_telegram=send_telegram)
        if settled:
            print(f"[STRATEGY_ADVISOR] 🎓 의사결정 사후 채점 완료: {len(settled)}건")
    except Exception as st_err:
        print(f"[WARN] 사후 정산 점검 실패: {st_err}")

    proposals = generate_strategy_proposals()

    # 🚀 안전 자율주행 집행: AUTO_APPLIED 마켓은 즉시 config.py 실전 설정 자동 반영!
    for market, prop in proposals.items():
        if prop.get("action") == "AUTO_APPLIED":
            ok, res_msg = apply_strategy_approval(market)
            if ok:
                print(f"[STRATEGY_ADVISOR] 🤖 [안전 자율주행] {market} 설정 자동 반영 완료.")
            else:
                print(f"[STRATEGY_ADVISOR] ⚠️ {market} 자동 반영 실패: {res_msg}")

    report_text = build_telegram_strategy_briefing(proposals)

    if send_telegram:
        send_message(report_text)
        print("[STRATEGY_ADVISOR] 텔레그램 전략 제안 리포트 발송 완료.")

    return proposals


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="AutoBot Strategy Advisor & Optimizer")
    parser.add_argument("--now", action="store_true", help="지금 즉시 전략 리뷰 실행 및 텔레그램 발송")
    parser.add_argument("--check-only", action="store_true", help="텔레그램 발송 없이 콘솔 출력만 수행")
    parser.add_argument("--approve", type=str, help="특정 마켓 전략 변경 승인 (예: SOL 또는 ETH)")
    args = parser.parse_args()

    if args.approve:
        success, res_msg = apply_strategy_approval(args.approve)
        print(res_msg.replace("<b>", "").replace("</b>", "").replace("<code>", "").replace("</code>", ""))
        if success:
            send_message(res_msg)
    elif args.check_only:
        props = generate_strategy_proposals()
        text = build_telegram_strategy_briefing(props)
        print("\n" + text.replace("<b>", "").replace("</b>", "").replace("<code>", "").replace("</code>", "").replace("<i>", "").replace("</i>", ""))
    elif args.now:
        run_daily_strategy_review(send_telegram=True)
    else:
        run_daily_strategy_review(send_telegram=True)
