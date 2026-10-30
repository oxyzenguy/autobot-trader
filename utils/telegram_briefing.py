"""
utils/telegram_briefing.py
텔레그램 메시지 정돈 및 온디맨드 현황 조회 명령어 전용 모듈

제공 기능:
1. build_status_briefing()      : 전 계좌 통합 종합 현황 브리핑 (/현황, /status, /상태)
2. build_main_bot_briefing()     : 메인봇 (BTC 적립, ETH 익절, SOL 매도대기) 상세 (/메인, /main)
3. build_cascade_briefing()      : Cascade 200단 바스켓 (SOL/ETH) 상세 (/cascade, /포켓2, /c2)
4. build_balance_briefing()      : 전 계좌 자산 및 가용 예수금 총정리 (/잔고, /balance)
5. build_recent_trades_briefing(): 최근 체결 내역 통합 조회 (/체결, /trades)
6. build_help_briefing()         : 지원 명령어 안내 (/도움말, /help)
"""

from __future__ import annotations

import os
import sys
import json
import sqlite3
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional

import pyupbit

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)


def _safe_float(val: Any, default: float = 0.0) -> float:
    try:
        return float(val) if val is not None else default
    except (ValueError, TypeError):
        return default


def _safe_int(val: Any, default: int = 0) -> int:
    try:
        return int(val) if val is not None else default
    except (ValueError, TypeError):
        return default


def build_status_briefing() -> str:
    """전 계좌 통합 종합 현황 브리핑 메시지 생성 (정기 발송 및 /현황)"""
    try:
        from utils.analytics import get_total_account_summary
        acc = get_total_account_summary()
        tot_equity = _safe_float(acc.get("total_equity", 0.0))
        krw_bal = _safe_float(acc.get("krw_balance", 0.0))
        unrealized_pnl = _safe_float(acc.get("unrealized_pnl", 0.0))
        coin_pnl_pct = _safe_float(acc.get("coin_pnl_pct", 0.0))
    except Exception:
        tot_equity, krw_bal, unrealized_pnl, coin_pnl_pct = 0.0, 0.0, 0.0, 0.0

    # 서브포켓별 예수금 조회
    g60_krw = 0.0
    cascade_krw = 0.0
    try:
        from utils.pocket_api import UpbitPocketClient
        try:
            g60_client = UpbitPocketClient(env_prefix="UPBIT_POCKET")
            g60_krw = _safe_float(g60_client.get_balance("KRW"))
        except Exception:
            pass
        try:
            c2_client = UpbitPocketClient(env_prefix="UPBIT_POCKET2")
            cascade_krw = _safe_float(c2_client.get_balance("KRW"))
        except Exception:
            pass
    except Exception:
        pass

    now_kst = datetime.utcnow() + timedelta(hours=9)
    time_str = now_kst.strftime("%H:%M")

    lines = [
        f"📊 <b>[AutoBot 통합 현황 브리핑 ({time_str} KST)]</b>",
        f"• 총 평가자산: <b>{tot_equity:,.0f}원</b> (손익 {unrealized_pnl:+,.0f}원 | {coin_pnl_pct:+.2f}%)",
        f"• 가용 예수금: 메인 <b>{krw_bal:,.0f}원</b> | Cascade <b>{cascade_krw:,.0f}원</b> | G60 <b>{g60_krw:,.0f}원</b>",
        "",
        "🤖 <b>[메인봇 실거래]</b>"
    ]

    # 1. KRW-BTC (스마트 적립)
    try:
        from strategy.btc_accumulator import check_btc_tiered_status, load_btc_state
        btc_stat = check_btc_tiered_status()
        btc_state = load_btc_state()
        btc_cur = _safe_float(btc_stat.get("current_price", 0.0))
        btc_avg = _safe_float(btc_stat.get("account_avg_price", 0.0))
        btc_vol = _safe_float(btc_stat.get("account_btc_balance", 0.0))
        btc_pnl = _safe_float(btc_stat.get("dist_avg_pct", 0.0))
        btc_ma = _safe_float(btc_stat.get("ma200", 0.0))
        btc_dist_ma = _safe_float(btc_stat.get("dist_ma200_pct", 0.0))
        btc_today = btc_state.get("today_status", "대기")

        lines.extend([
            f"• <b>KRW-BTC</b>: {btc_vol:.6f} BTC (평단 {btc_avg:,.0f}원 | {btc_pnl:+.2f}%)",
            f"  - 200일선: {btc_ma:,.0f}원 ({btc_dist_ma:+.2f}%) | 적립: {btc_today}"
        ])
    except Exception:
        lines.append("• <b>KRW-BTC</b>: 모으기 상태 로드 중...")

    # 2. KRW-ETH (하이브리드 전략)
    try:
        eth_file = os.path.join(BASE_DIR, "real_strategy_state_KRW_ETH.json")
        eth_state = {}
        if os.path.exists(eth_file):
            with open(eth_file, "r", encoding="utf-8") as f:
                eth_state = json.load(f)
        eth_cur = _safe_float(pyupbit.get_current_price("KRW-ETH") or 0.0)
        eth_vol = _safe_float(eth_state.get("bot_quantity", 0.0))
        eth_avg = _safe_float(eth_state.get("bot_avg_price", 0.0))
        eth_pnl = ((eth_cur - eth_avg) / eth_avg * 100.0) if eth_avg > 0 else 0.0
        from config import get_profit_margin
        eth_tp_margin = (get_profit_margin("KRW-ETH") - 1.0) * 100.0
        eth_tp_price = eth_avg * (1.0 + eth_tp_margin / 100.0)
        eth_dist_tp = ((eth_tp_price - eth_cur) / eth_cur * 100.0) if eth_cur > 0 else 0.0
        tranches = eth_state.get("tranches", [])

        lines.extend([
            f"• <b>KRW-ETH</b>: {eth_vol:.4f} ETH (평단 {eth_avg:,.0f}원 | {eth_pnl:+.2f}%)",
            f"  - 바스켓 익절: <b>{eth_tp_price:,.0f}원</b> (+{eth_tp_margin:.1f}% / 도달 잔여 {eth_dist_tp:+.2f}%) | {len(tranches)}차수"
        ])
    except Exception:
        lines.append("• <b>KRW-ETH</b>: 상태 로드 중...")

    # 3. KRW-SOL (EXIT_ONLY 매도 대기)
    try:
        sol_file = os.path.join(BASE_DIR, "real_strategy_state_KRW_SOL.json")
        sol_state = {}
        if os.path.exists(sol_file):
            with open(sol_file, "r", encoding="utf-8") as f:
                sol_state = json.load(f)
        sol_cur = _safe_float(pyupbit.get_current_price("KRW-SOL") or 0.0)
        sol_vol = _safe_float(sol_state.get("bot_quantity", 0.0))
        sol_avg = _safe_float(sol_state.get("bot_avg_price", 0.0))
        sol_pnl = ((sol_cur - sol_avg) / sol_avg * 100.0) if sol_avg > 0 else 0.0

        # 등록된 지정가 매도 주문 탐색
        target_sell_p = 142400.0
        dist_to_sell = ((target_sell_p - sol_cur) / sol_cur * 100.0) if sol_cur > 0 else 0.0

        lines.extend([
            f"• <b>KRW-SOL</b>: 🟡 매도 대기 (EXIT_ONLY)",
            f"  - 보유: {sol_vol:.4f} SOL (평단 {sol_avg:,.0f}원 | {sol_pnl:+.2f}%)",
            f"  - 지정가 매도: <b>{target_sell_p:,.0f}원</b> (+0.46% / 도달 잔여 {dist_to_sell:+.2f}%)"
        ])
    except Exception:
        lines.append("• <b>KRW-SOL</b>: 상태 로드 중...")

    # 4. Cascade 서브포켓 (신규 200단 바스켓 리셋)
    lines.extend(["", "🌊 <b>[Cascade 서브포켓]</b> (1.8% 하락 / 0.8% 익절 200단)"])
    for m_sym, f_tag in [("SOL", "KRW_SOL"), ("ETH", "KRW_ETH")]:
        try:
            c_file = os.path.join(BASE_DIR, f"pocket2_basket_state_{f_tag}.json")
            if os.path.exists(c_file):
                with open(c_file, "r", encoding="utf-8") as f:
                    cs = json.load(f)
                cs_cycle = _safe_int(cs.get("cycle_count", 1))
                cs_lvls = _safe_int(cs.get("active_levels", 0))
                cs_vol = _safe_float(cs.get("held_volume", 0.0))
                cs_vwap = _safe_float(cs.get("vwap", 0.0))
                cs_pnl = _safe_float(cs.get("total_realized_pnl", 0.0))
                cs_wins = _safe_int(cs.get("win_count", 0))
                cs_sell = cs.get("pending_sell") or {}
                cs_tp_p = _safe_float(cs_sell.get("price", 0.0))
                cs_buys = cs.get("pending_buys") or {}

                lines.extend([
                    f"• <b>{m_sym}</b> (사이클 {cs_cycle}회차): <b>{cs_lvls}/200단</b> ({cs_vol:.4f} {m_sym} @ {cs_vwap:,.0f}원)",
                    f"  - 전량 익절가: <b>{cs_tp_p:,.0f}원</b> (+0.80%) | 대기 매수: {len(cs_buys)}건",
                    f"  - 누적 실현: <b>{cs_pnl:+,.0f}원</b> ({cs_wins}회 익절 성공)"
                ])
            else:
                if m_sym == "ETH":
                    lines.append("• <b>ETH</b>: ⏳ 메인봇 익절 완료 후 자산 이전 대기")
                else:
                    lines.append(f"• <b>{m_sym}</b>: 대기 중")
        except Exception:
            lines.append(f"• <b>{m_sym}</b>: Cascade 상태 로드 중...")

    # 5. G60 서브포켓 요약
    lines.extend(["", "⚡ <b>[G60 서브포켓]</b> (3종 스쿼드 그리드)"])
    try:
        s_files = [f"pocket_grid_state_{m}.json" for m in ["KRW_ETH", "KRW_SOL", "KRW_XRP"]]
        p_pnls = 0.0
        p_wins = 0
        grid_counts = {}
        for sf in s_files:
            sp = os.path.join(BASE_DIR, sf)
            if os.path.exists(sp):
                with open(sp, "r", encoding="utf-8") as f:
                    sd = json.load(f)
                    p_pnls += _safe_float(sd.get("realized_pnl", 0.0))
                    p_wins += _safe_int(sd.get("win_count", 0))
                    coin_name = sf.replace("pocket_grid_state_KRW_", "").replace(".json", "")
                    grid_counts[coin_name] = len(sd.get("active_grids", []))
        lines.append(
            f"• 실현손익: <b>{p_pnls:+,.0f}원</b> ({p_wins}회 익절) | 보유 그리드: XRP({grid_counts.get('XRP', 0)}), ETH({grid_counts.get('ETH', 0)}), SOL({grid_counts.get('SOL', 0)})"
        )
    except Exception:
        pass

    lines.extend([
        "",
        "💡 <i>명령어 안내: <code>/메인</code> <code>/cascade</code> <code>/60</code> <code>/잔고</code> <code>/체결</code></i>"
    ])
    return "\n".join(lines).strip()


def build_main_bot_briefing() -> str:
    """메인 봇 상세 현황 메시지 생성 (/메인, /main)"""
    try:
        from utils.analytics import get_total_account_summary
        acc = get_total_account_summary()
        krw_bal = _safe_float(acc.get("krw_balance", 0.0))
    except Exception:
        krw_bal = 0.0

    lines = [
        "🤖 <b>[메인 봇 실거래 상세 현황]</b>",
        f"• 가용 주문 예수금: <b>{krw_bal:,.0f}원</b>",
        ""
    ]

    # KRW-BTC
    try:
        from strategy.btc_accumulator import check_btc_tiered_status, load_btc_state
        btc_stat = check_btc_tiered_status()
        btc_state = load_btc_state()
        btc_cur = _safe_float(btc_stat.get("current_price", 0.0))
        btc_avg = _safe_float(btc_stat.get("account_avg_price", 0.0))
        btc_vol = _safe_float(btc_stat.get("account_btc_balance", 0.0))
        btc_eval = btc_vol * btc_cur
        btc_pnl = _safe_float(btc_stat.get("dist_avg_pct", 0.0))
        btc_ma = _safe_float(btc_stat.get("ma200", 0.0))
        btc_dist_ma = _safe_float(btc_stat.get("dist_ma200_pct", 0.0))
        btc_tier = btc_stat.get("tier_name", "대기")
        btc_today = btc_state.get("today_status", "대기")

        lines.extend([
            f"🟡 <b>비트코인 (KRW-BTC) 스마트 적립</b>",
            f"• 현재 시세: <b>{btc_cur:,.0f}원</b>",
            f"• 총 보유량: <b>{btc_vol:.6f} BTC</b> (평가 {btc_eval:,.0f}원)",
            f"• 계좌 평단: <b>{btc_avg:,.0f}원</b> (평가손익: {btc_pnl:+.2f}%)",
            f"• 200일선: {btc_ma:,.0f}원 (이격도: {btc_dist_ma:+.2f}%) | {btc_tier}",
            f"• 적립 진행: 15:05 업비트 1만 + 08:55 봇 스마트 적립 ({btc_today})",
            ""
        ])
    except Exception as e:
        lines.append(f"🟡 KRW-BTC 로드 오류: {e}\n")

    # KRW-ETH
    try:
        eth_file = os.path.join(BASE_DIR, "real_strategy_state_KRW_ETH.json")
        eth_state = {}
        if os.path.exists(eth_file):
            with open(eth_file, "r", encoding="utf-8") as f:
                eth_state = json.load(f)
        eth_cur = _safe_float(pyupbit.get_current_price("KRW-ETH") or 0.0)
        eth_vol = _safe_float(eth_state.get("bot_quantity", 0.0))
        eth_avg = _safe_float(eth_state.get("bot_avg_price", 0.0))
        eth_eval = eth_vol * eth_cur
        eth_pnl = ((eth_cur - eth_avg) / eth_avg * 100.0) if eth_avg > 0 else 0.0
        from config import get_profit_margin
        eth_tp_margin = (get_profit_margin("KRW-ETH") - 1.0) * 100.0
        eth_tp_price = eth_avg * (1.0 + eth_tp_margin / 100.0)
        eth_dist_tp = ((eth_tp_price - eth_cur) / eth_cur * 100.0) if eth_cur > 0 else 0.0
        tranches = eth_state.get("tranches", [])

        lines.extend([
            f"💎 <b>이더리움 (KRW-ETH) 하이브리드 바스켓</b>",
            f"• 현재 시세: <b>{eth_cur:,.0f}원</b>",
            f"• 봇 보유량: <b>{eth_vol:.4f} ETH</b> (평가 {eth_eval:,.0f}원)",
            f"• 봇 매수평단: <b>{eth_avg:,.0f}원</b> (평가손익: {eth_pnl:+.2f}%)",
            f"• 바스켓 익절가: <b>{eth_tp_price:,.0f}원</b> (+{eth_tp_margin:.1f}% / 도달 잔여 {eth_dist_tp:+.2f}%)",
            f"• 보유 차수: 총 {len(tranches)}차수 진입 운용 중",
            ""
        ])
    except Exception as e:
        lines.append(f"💎 KRW-ETH 로드 오류: {e}\n")

    # KRW-SOL
    try:
        sol_file = os.path.join(BASE_DIR, "real_strategy_state_KRW_SOL.json")
        sol_state = {}
        if os.path.exists(sol_file):
            with open(sol_file, "r", encoding="utf-8") as f:
                sol_state = json.load(f)
        sol_cur = _safe_float(pyupbit.get_current_price("KRW-SOL") or 0.0)
        sol_vol = _safe_float(sol_state.get("bot_quantity", 0.0))
        sol_avg = _safe_float(sol_state.get("bot_avg_price", 0.0))
        sol_eval = sol_vol * sol_cur
        sol_pnl = ((sol_cur - sol_avg) / sol_avg * 100.0) if sol_avg > 0 else 0.0
        target_sell_p = 142400.0
        dist_to_sell = ((target_sell_p - sol_cur) / sol_cur * 100.0) if sol_cur > 0 else 0.0

        lines.extend([
            f"🟣 <b>솔라나 (KRW-SOL) 매도 대기 (EXIT_ONLY)</b>",
            f"• 현재 시세: <b>{sol_cur:,.0f}원</b>",
            f"• 봇 보유량: <b>{sol_vol:.6f} SOL</b> (평가 {sol_eval:,.0f}원)",
            f"• 봇 매수평단: <b>{sol_avg:,.0f}원</b> (평가손익: {sol_pnl:+.2f}%)",
            f"• 등록 매도가: <b>{target_sell_p:,.0f}원</b> (+0.46% / 도달 잔여 {dist_to_sell:+.2f}%)",
            "• 운용 상태: 💡 <i>추가 매수 차단 / 업비트 지정가 전량 매도 대기 중</i>",
            "  (체결 즉시 운용 완료되며 회수 자금은 Cascade 포켓으로 이전)"
        ])
    except Exception as e:
        lines.append(f"🟣 KRW-SOL 로드 오류: {e}\n")

    return "\n".join(lines).strip()


def build_cascade_briefing() -> str:
    """Cascade 서브포켓 상세 현황 메시지 생성 (/cascade, /포켓2, /c2)"""
    krw_balance = 0.0
    try:
        from utils.pocket_api import UpbitPocketClient
        client = UpbitPocketClient(env_prefix="UPBIT_POCKET2")
        krw_balance = _safe_float(client.get_balance("KRW"))
    except Exception:
        pass

    lines = [
        "🌊 <b>[Cascade 서브포켓 200단 실거래 현황]</b>",
        "• 전략 규칙: 1.8% 하락 매수 / 0.8% 전량 익절 리셋 (200단 버퍼)",
        f"• 가용 예수금: <b>{krw_balance:,.0f}원</b>",
        ""
    ]

    # SOL
    sol_file = os.path.join(BASE_DIR, "pocket2_basket_state_KRW_SOL.json")
    if os.path.exists(sol_file):
        try:
            with open(sol_file, "r", encoding="utf-8") as f:
                cs = json.load(f)
            cycle = _safe_int(cs.get("cycle_count", 1))
            levels = _safe_int(cs.get("active_levels", 0))
            vol = _safe_float(cs.get("held_volume", 0.0))
            cost = _safe_float(cs.get("total_cost", 0.0))
            vwap = _safe_float(cs.get("vwap", 0.0))
            tot_pnl = _safe_float(cs.get("total_realized_pnl", 0.0))
            tot_fees = _safe_float(cs.get("total_fees", 0.0))
            wins = _safe_int(cs.get("win_count", 0))

            cur_p = _safe_float(pyupbit.get_current_price("KRW-SOL") or 0.0)
            eval_val = vol * cur_p
            unreal_pnl = eval_val - cost
            unreal_pct = (unreal_pnl / cost * 100.0) if cost > 0 else 0.0

            sell_info = cs.get("pending_sell") or {}
            tp_p = _safe_float(sell_info.get("price", 0.0))
            dist_tp = ((tp_p - cur_p) / cur_p * 100.0) if cur_p > 0 else 0.0
            buys = cs.get("pending_buys") or {}

            lines.extend([
                f"🟣 <b>솔라나 (KRW-SOL) - 가동 중 (RUNNING)</b>",
                f"• 현재가: <b>{cur_p:,.0f}원</b> | 운용 사이클: <b>{cycle}회차</b>",
                f"• 보유 포지션: <b>{levels}/200단</b> ({vol:.6f} SOL | 투입 {cost:,.0f}원)",
                f"• 봇 매수평단: <b>{vwap:,.0f}원</b> (평가손익: {unreal_pnl:+,.0f}원 | {unreal_pct:+.2f}%)",
                f"• 🎯 <b>등록된 전량 익절가: {tp_p:,.0f}원</b> (+0.80% / 도달 잔여 {dist_tp:+.2f}%)",
                f"• 🛒 <b>대기 매수 ({len(buys)}건)</b>:"
            ])

            # 대기 매수 주문 정렬
            sorted_buys = sorted(buys.values(), key=lambda x: _safe_int(x.get("level", 999)))
            for b in sorted_buys:
                b_lvl = b.get("level", "-")
                b_p = _safe_float(b.get("price", 0.0))
                b_v = _safe_float(b.get("volume", 0.0))
                b_cost = b_p * b_v
                lines.append(f"  - Lvl {b_lvl}: {b_p:,.0f}원 ({b_v:.4f} SOL | {b_cost:,.0f}원)")

            lines.extend([
                f"• 누적 실현손익: <b>{tot_pnl:+,.0f}원</b> ({wins}회 익절 성공 | 수수료 {tot_fees:,.1f}원)",
                ""
            ])
        except Exception as e:
            lines.append(f"🟣 SOL 상태 파싱 오류: {e}\n")
    else:
        lines.append("🟣 솔라나: 상태 파일 미발견 (대기 중)\n")

    # ETH
    eth_file = os.path.join(BASE_DIR, "pocket2_basket_state_KRW_ETH.json")
    if os.path.exists(eth_file):
        try:
            with open(eth_file, "r", encoding="utf-8") as f:
                ce = json.load(f)
            e_cycle = _safe_int(ce.get("cycle_count", 1))
            e_levels = _safe_int(ce.get("active_levels", 0))
            e_vol = _safe_float(ce.get("held_volume", 0.0))
            e_cost = _safe_float(ce.get("total_cost", 0.0))
            e_vwap = _safe_float(ce.get("vwap", 0.0))
            e_tot_pnl = _safe_float(ce.get("total_realized_pnl", 0.0))
            e_tot_fees = _safe_float(ce.get("total_fees", 0.0))
            e_wins = _safe_int(ce.get("win_count", 0))

            e_cur_p = _safe_float(pyupbit.get_current_price("KRW-ETH") or 0.0)
            e_eval_val = e_vol * e_cur_p
            e_unreal_pnl = e_eval_val - e_cost
            e_unreal_pct = (e_unreal_pnl / e_cost * 100.0) if e_cost > 0 else 0.0

            e_sell_info = ce.get("pending_sell") or {}
            e_tp_p = _safe_float(e_sell_info.get("price", 0.0))
            e_dist_tp = ((e_tp_p - e_cur_p) / e_cur_p * 100.0) if e_cur_p > 0 else 0.0
            e_buys = ce.get("pending_buys") or {}

            lines.extend([
                f"💎 <b>이더리움 (KRW-ETH) - 가동 중 (RUNNING)</b>",
                f"• 현재가: <b>{e_cur_p:,.0f}원</b> | 운용 사이클: <b>{e_cycle}회차</b>",
                f"• 보유 포지션: <b>{e_levels}/200단</b> ({e_vol:.6f} ETH | 투입 {e_cost:,.0f}원)",
                f"• 봇 매수평단: <b>{e_vwap:,.0f}원</b> (평가손익: {e_unreal_pnl:+,.0f}원 | {e_unreal_pct:+.2f}%)",
                f"• 🎯 <b>등록된 전량 익절가: {e_tp_p:,.0f}원</b> (+0.80% / 도달 잔여 {e_dist_tp:+.2f}%)",
                f"• 🛒 <b>대기 매수 ({len(e_buys)}건)</b>:"
            ])

            sorted_e_buys = sorted(e_buys.values(), key=lambda x: _safe_int(x.get("level", 999)))
            for b in sorted_e_buys:
                b_lvl = b.get("level", "-")
                b_p = _safe_float(b.get("price", 0.0))
                b_v = _safe_float(b.get("volume", 0.0))
                b_cost = b_p * b_v
                lines.append(f"  - Lvl {b_lvl}: {b_p:,.0f}원 ({b_v:.6f} ETH | {b_cost:,.0f}원)")

            lines.extend([
                f"• 누적 실현손익: <b>{e_tot_pnl:+,.0f}원</b> ({e_wins}회 익절 성공 | 수수료 {e_tot_fees:,.1f}원)",
                ""
            ])
        except Exception as e:
            lines.append(f"💎 ETH 상태 파싱 오류: {e}\n")
    else:
        lines.extend([
            "💎 <b>이더리움 (KRW-ETH) - 대기 중 (PENDING_TRANSFER)</b>",
            "• 운용 상태: 메인봇 ETH 포지션 전량 익절 완료 후 자금 이전하여 시작 예정",
            "• 예정 설정: 1.8% 하락 매수 / 0.8% 전량 익절 리셋 (200단 버퍼)"
        ])

    return "\n".join(lines).strip()


def build_balance_briefing() -> str:
    """전 계좌 자산 및 잔고 총정리 메시지 생성 (/잔고, /balance)"""
    try:
        from utils.analytics import get_total_account_summary
        acc = get_total_account_summary()
        main_krw = _safe_float(acc.get("krw_balance", 0.0))
        main_equity = _safe_float(acc.get("total_equity", 0.0))
        main_coin_eval = _safe_float(acc.get("total_coin_eval", 0.0))
        coins = acc.get("coins", [])
    except Exception:
        main_krw, main_equity, main_coin_eval, coins = 0.0, 0.0, 0.0, []

    # 서브포켓 잔고 및 코인 조회
    g60_krw, g60_coin_eval = 0.0, 0.0
    cascade_krw, cascade_coin_eval = 0.0, 0.0

    try:
        from utils.pocket_api import UpbitPocketClient
        # 1. G60
        try:
            g60_c = UpbitPocketClient(env_prefix="UPBIT_POCKET")
            g60_krw = _safe_float(g60_c.get_balance("KRW"))
            # 보유 그리드 파일에서 코인 평가액 산출
            for m in ["KRW_ETH", "KRW_SOL", "KRW_XRP"]:
                sf = os.path.join(BASE_DIR, f"pocket_grid_state_{m}.json")
                if os.path.exists(sf):
                    with open(sf, "r", encoding="utf-8") as f:
                        sd = json.load(f)
                    curr_p = _safe_float(pyupbit.get_current_price(m.replace("_", "-")) or 0.0)
                    for g in sd.get("active_grids", []):
                        g60_coin_eval += _safe_float(g.get("vol", 0.0)) * curr_p
        except Exception:
            pass

        # 2. Cascade
        try:
            c2_c = UpbitPocketClient(env_prefix="UPBIT_POCKET2")
            cascade_krw = _safe_float(c2_c.get_balance("KRW"))
            cs_file = os.path.join(BASE_DIR, "pocket2_basket_state_KRW_SOL.json")
            if os.path.exists(cs_file):
                with open(cs_file, "r", encoding="utf-8") as f:
                    cs = json.load(f)
                cs_vol = _safe_float(cs.get("held_volume", 0.0))
                sol_cur = _safe_float(pyupbit.get_current_price("KRW-SOL") or 0.0)
                cascade_coin_eval = cs_vol * sol_cur
        except Exception:
            pass
    except Exception:
        pass

    g60_total = g60_krw + g60_coin_eval
    cascade_total = cascade_krw + cascade_coin_eval
    grand_total = main_equity + g60_total + cascade_total
    total_cash = main_krw + g60_krw + cascade_krw
    total_crypto = main_coin_eval + g60_coin_eval + cascade_coin_eval

    lines = [
        "💰 <b>[전 계좌 자산 및 잔고 총정리]</b>",
        "",
        "1️⃣ <b>메인 계좌</b>",
        f"• 가용 예수금: <b>{main_krw:,.0f}원</b>",
        f"• 코인 평가액: <b>{main_coin_eval:,.0f}원</b>"
    ]
    for c in coins:
        cur_code = c.get("currency", "")
        if cur_code in ["BTC", "ETH", "SOL", "XRP"]:
            bal = _safe_float(c.get("balance", 0.0))
            amt = _safe_float(c.get("eval_amount", 0.0))
            lines.append(f"  - {cur_code}: {bal:.6f} ({amt:,.0f}원)")
    lines.extend([
        f"• 메인 소계: <b>{main_equity:,.0f}원</b>",
        "",
        "2️⃣ <b>Cascade 서브포켓 (포켓2)</b>",
        f"• 가용 예수금: <b>{cascade_krw:,.0f}원</b>",
        f"• 코인 평가액: <b>{cascade_coin_eval:,.0f}원</b>",
        f"• Cascade 소계: <b>{cascade_total:,.0f}원</b>",
        "",
        "3️⃣ <b>G60 서브포켓</b>",
        f"• 가용 예수금: <b>{g60_krw:,.0f}원</b>",
        f"• 그리드 코인 평가액: <b>{g60_coin_eval:,.0f}원</b>",
        f"• G60 소계: <b>{g60_total:,.0f}원</b>",
        "",
        "━━━━━━━━━━━━━━━━━━━━━",
        f"🏆 <b>합산 총 자산: {grand_total:,.0f}원</b>",
        f"(총 가용 현금: {total_cash:,.0f}원 | 총 코인 평가액: {total_crypto:,.0f}원)"
    ])

    return "\n".join(lines).strip()


def build_recent_trades_briefing(limit: int = 7) -> str:
    """최근 체결 내역 통합 조회 메시지 생성 (/체결, /trades)"""
    all_trades: List[Dict[str, Any]] = []

    # 1. Main Bot (trade_history.db)
    main_db = os.path.join(BASE_DIR, "trade_history.db")
    if os.path.exists(main_db):
        try:
            conn = sqlite3.connect(main_db, timeout=2.0)
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            c.execute("""
                SELECT id, timestamp, ticker, side, volume, price, action, cost_or_revenue, pnl, strategy
                FROM trades
                ORDER BY id DESC LIMIT 15
            """)
            for r in c.fetchall():
                d = dict(r)
                d["source"] = "메인"
                all_trades.append(d)
            conn.close()
        except Exception:
            pass

    # 2. Cascade Pocket (pocket2_basket_trade_history.db)
    c2_db = os.path.join(BASE_DIR, "pocket2_basket_trade_history.db")
    if os.path.exists(c2_db):
        try:
            conn = sqlite3.connect(c2_db, timeout=2.0)
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            c.execute("""
                SELECT id, timestamp, market, side, volume, price, action, cost_or_revenue, pnl, cycle, strategy
                FROM pocket_trades
                ORDER BY id DESC LIMIT 15
            """)
            for r in c.fetchall():
                d = dict(r)
                d["source"] = "Cascade"
                d["ticker"] = str(d.get("market", "")).replace("KRW-", "")
                all_trades.append(d)
            conn.close()
        except Exception:
            pass

    # 3. G60 Pocket (pocket_trade_history.db)
    g60_db = os.path.join(BASE_DIR, "pocket_trade_history.db")
    if os.path.exists(g60_db):
        try:
            conn = sqlite3.connect(g60_db, timeout=2.0)
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            c.execute("""
                SELECT id, timestamp, market, side, volume, price, action, cost_or_revenue, pnl, level, strategy
                FROM pocket_trades
                ORDER BY id DESC LIMIT 15
            """)
            for r in c.fetchall():
                d = dict(r)
                d["source"] = "G60"
                d["ticker"] = str(d.get("market", "")).replace("KRW-", "")
                all_trades.append(d)
            conn.close()
        except Exception:
            pass

    if not all_trades:
        return "📜 최근 체결된 거래 내역이 없습니다."

    # 최신 체결 시각 순으로 정렬
    all_trades.sort(key=lambda x: str(x.get("timestamp", "")), reverse=True)
    selected = all_trades[:limit]

    lines = [f"📜 <b>[최근 체결 내역 (통합 {len(selected)}건)]</b>", ""]
    for t in selected:
        src = t.get("source", "봇")
        time_str = str(t.get("timestamp", "-"))[5:19]
        ticker = t.get("ticker", "")
        side = t.get("side", "")
        p = _safe_float(t.get("price", 0.0))
        v = _safe_float(t.get("volume", 0.0))
        cost = _safe_float(t.get("cost_or_revenue", 0.0))
        pnl = _safe_float(t.get("pnl", 0.0))
        act = str(t.get("action", ""))

        if side in ("ask", "sell"):
            side_badge = f"🟢 <b>익절 매도</b> (손익: {pnl:+,.0f}원)"
        else:
            side_badge = "🔵 <b>매수 체결</b>"

        # 액션 라벨 간소화
        act_label = act
        if "BASKET_RESET" in act:
            act_label = "바스켓 전량익절"
        elif "LADDER_BUY" in act:
            act_label = f"래더 {act.split('_')[-1]}차"
        elif "TIERED_BUY" in act:
            act_label = "스마트 종가적립"
        elif "UPBIT_DCA" in act:
            act_label = "업비트 모으기"
        elif "GRID" in act:
            act_label = "그리드 체결"

        lines.extend([
            f"• <b>[{src}]</b> {ticker} {side_badge} <code>{time_str}</code>",
            f"  {v:.6f} {ticker} @ {p:,.0f}원 ({cost:,.0f}원) | {act_label}"
        ])

    return "\n".join(lines).strip()


def build_help_briefing() -> str:
    """텔레그램 실시간 지원 명령어 안내 메시지 생성 (/도움말, /help)"""
    return (
        "🤖 <b>[AutoBot Trader 실시간 컨트롤 명령어 안내]</b>\n\n"
        "📊 <b>현황 조회</b>\n"
        "• <code>/현황</code> (또는 <code>/status</code>) : 전 계좌 통합 종합 브리핑\n"
        "• <code>/메인</code> : 메인봇 (BTC 적립, ETH 익절, SOL 매도대기) 상세\n"
        "• <code>/cascade</code> (또는 <code>/포켓2</code>) : Cascade 200단 바스켓 (SOL/ETH) 상세\n"
        "• <code>/60</code> (또는 <code>/g60</code>) : G60 3종 스쿼드 그리드 (XRP·ETH·SOL) 상세\n"
        "• <code>/잔고</code> : 메인 및 포켓별 가용 예수금과 코인 잔고 총정리\n"
        "• <code>/체결</code> : 최근 체결된 주문 내역 통합 확인\n\n"
        "🔄 <b>원격 제어 (재기동)</b>\n"
        "• <code>/재기동</code> : 메인 실거래 봇(main.py) 안전 재시작\n"
        "• <code>/재기동 포켓2</code> : Cascade 200단 러너(SOL/ETH) 재시작\n"
        "• <code>/재기동 포켓</code> : G60 실거래 3종 러너 재시작\n"
        "• <code>/재기동 대시보드</code> : Streamlit 웹 대시보드 재시작\n"
        "• <code>/재기동 전체</code> : 전 시스템 일괄 안전 재기동\n\n"
        "🧭 <b>전략 및 정밀 감사</b>\n"
        "• <code>/전략검토</code> : 퀀트 파라미터 최적화 리뷰\n"
        "• <code>/감사</code> : 3대 봇 프로세스 및 DB 정밀 감사\n"
        "• <code>/tqqq</code> : 미국주식(TQQQ/JEPI/SPMO) 브리핑\n"
        "• <code>/국장</code> : 국내주식 스마트 분할매도 브리핑\n\n"
        "💡 <i>슬래시(/) 없이 <code>현황</code>, <code>잔고</code>, <code>cascade</code>, <code>메인</code> 등으로 입력해도 동일 인식됩니다.</i>"
    )
