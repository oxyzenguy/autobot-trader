import os

import re
import time
import json
import sqlite3
import pandas as pd
import numpy as np
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st
import pyupbit
from datetime import datetime
from typing import Optional, Dict, Any, List

from config import INVESTMENTS, MIN_KRW_ALERT_THRESHOLD, get_profit_margin, get_market_strategy
from utils.analytics import (
    get_total_account_summary,
    get_all_active_strategies
)
from utils.db_logger import DB_PATH
from utils.sync_manager import fetch_snapshot_from_cloud, load_local_snapshot
from utils.dashboard_data import filter_trades_for_candles, sort_timeline_items

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def get_latest_trade_signature() -> str:
    """
    매매 체결 및 주문 상태 변경을 초경량으로 감지하기 위한 시그니처 생성
    1) trade_history.db의 trades 테이블 레코드 수 및 최신 ID (실제 체결 여부)
    2) real_strategy_state_{market}.json 핵심 상태(수량, 평단, 차수 수, 사이클 수)
       - 파일 수정 시각(mtime) 대신 실제 데이터 변경 여부만 확인하여 허위 알림 방지
    """
    db_sig = "0_0"
    try:
        if os.path.exists(DB_PATH):
            conn = sqlite3.connect(DB_PATH, timeout=2.0)
            cur = conn.cursor()
            cur.execute("SELECT count(*), coalesce(max(id), 0) FROM trades")
            row = cur.fetchone()
            if row:
                db_sig = f"{row[0]}_{row[1]}"
            conn.close()
    except Exception:
        pass

    pocket_sig = "0_0"
    try:
        p_db = os.path.join(BASE_DIR, "pocket_trade_history.db")
        if os.path.exists(p_db):
            conn = sqlite3.connect(p_db, timeout=2.0)
            cur = conn.cursor()
            cur.execute("SELECT count(*), coalesce(max(id), 0) FROM pocket_trades")
            row = cur.fetchone()
            if row:
                pocket_sig = f"{row[0]}_{row[1]}"
            conn.close()
    except Exception:
        pass

    cascade_sig = "0_0"
    try:
        c_db = os.path.join(BASE_DIR, "pocket2_basket_trade_history.db")
        if os.path.exists(c_db):
            conn = sqlite3.connect(c_db, timeout=2.0)
            cur = conn.cursor()
            cur.execute("SELECT count(*), coalesce(max(id), 0) FROM pocket_trades")
            row = cur.fetchone()
            if row:
                cascade_sig = f"{row[0]}_{row[1]}"
            conn.close()
    except Exception:
        pass

    state_sig = []
    for m in INVESTMENTS.keys():
        s_file = os.path.join(BASE_DIR, f"real_strategy_state_{m.replace('-', '_')}.json")
        if os.path.exists(s_file):
            try:
                with open(s_file, "r", encoding="utf-8") as f:
                    s_data = json.load(f)
                    bot_q = s_data.get("bot_quantity", 0.0)
                    bot_avg = s_data.get("bot_avg_price", 0.0)
                    num_tranches = len(s_data.get("tranches", []))
                    cycles = s_data.get("completed_cycles", 0)
                    mode = s_data.get("active_mode", "")
                    state_sig.append(f"{m}:{bot_q:.6f}_{bot_avg:.0f}_{num_tranches}_{cycles}_{mode}")
            except Exception:
                pass

    for m in ["KRW_SOL", "KRW_XRP"]:
        p_file = os.path.join(BASE_DIR, f"pocket_grid_state_{m}.json")
        if os.path.exists(p_file):
            try:
                with open(p_file, "r", encoding="utf-8") as f:
                    p_data = json.load(f)
                    p_grids = len(p_data.get("active_grids", []))
                    p_pnl = p_data.get("realized_pnl", 0.0)
                    p_trades = p_data.get("total_trades", 0)
                    state_sig.append(f"{m}:{p_grids}_{p_pnl:.1f}_{p_trades}")
            except Exception:
                pass

    for m in ["KRW_SOL", "KRW_ETH"]:
        c_file = os.path.join(BASE_DIR, f"pocket2_basket_state_{m}.json")
        if os.path.exists(c_file):
            try:
                with open(c_file, "r", encoding="utf-8") as f:
                    c_data = json.load(f)
                    c_cycle = c_data.get("cycle_count", 0)
                    c_lvls = c_data.get("active_levels", 0)
                    c_pnl = c_data.get("total_realized_pnl", 0.0)
                    state_sig.append(f"C_{m}:{c_cycle}_{c_lvls}_{c_pnl:.1f}")
            except Exception:
                pass

    return f"{db_sig}|{pocket_sig}|{cascade_sig}|{'_'.join(state_sig)}"




# --- Streamlit 페이지 설정 ---
st.set_page_config(
    page_title="AutoBot 전략 대시보드",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="collapsed"
)

# --- 커스텀 스타일 (Dark Financial Theme) ---
st.markdown("""
<style>
    /* 배경 및 카드 디자인 */
    .block-container {
        padding-top: 1.5rem;
        padding-bottom: 2rem;
    }
    .top-header {
        background: linear-gradient(135deg, #151a23 0%, #1f2633 100%);
        border: 1px solid #2d3648;
        border-radius: 12px;
        padding: 16px 22px;
        margin-bottom: 18px;
        box-shadow: 0 4px 12px rgba(0,0,0,0.25);
    }
    .krw-alert-box {
        background: linear-gradient(90deg, #4a151b 0%, #731c26 100%);
        border: 2px solid #ff3b69;
        border-radius: 10px;
        padding: 14px 20px;
        margin-bottom: 20px;
        color: #ffffff;
        box-shadow: 0 4px 14px rgba(255, 59, 105, 0.35);
        display: flex;
        align-items: center;
        gap: 15px;
    }
    .section-card {
        background: #181d27;
        border: 1px solid #2a3242;
        border-radius: 12px;
        padding: 18px 22px;
        margin-bottom: 22px;
        box-shadow: 0 4px 12px rgba(0,0,0,0.2);
    }
    .strategy-card {
        background: linear-gradient(135deg, #1a202c 0%, #202736 100%);
        border: 1px solid #323d52;
        border-radius: 12px;
        padding: 18px 22px;
        margin-bottom: 24px;
        box-shadow: 0 4px 14px rgba(0,0,0,0.25);
    }
    .metric-card {
        background: #141822;
        border-radius: 10px;
        padding: 14px 18px;
        border: 1px solid #2b3345;
        height: 100%;
    }
    .metric-title {
        font-size: 0.8rem;
        color: #8c96a5;
        font-weight: 500;
        margin-bottom: 4px;
    }
    .metric-val {
        font-size: 1.55rem;
        font-weight: 700;
        color: #f0f3f6;
    }
    .metric-sub {
        font-size: 0.76rem;
        color: #7b8595;
        margin-top: 4px;
    }
    .text-green { color: #00c087 !important; }
    .text-red { color: #ff3b69 !important; }
    .text-blue { color: #2962ff !important; }
    .text-yellow { color: #ffb300 !important; }

    .stTabs [data-baseweb="tab-list"] {
        gap: 8px;
    }
    .stTabs [data-baseweb="tab"] {
        padding-top: 8px;
        padding-bottom: 8px;
        font-weight: 600;
    }
</style>
""", unsafe_allow_html=True)


# =============================================================================
# 1시간봉 캔들스틱 차트 & 매매 타점 오버레이 컴포넌트
# =============================================================================
ACTION_KO_MAP = {
    "INITIAL_10K_ENTRY": "1회차 신규 진입",
    "TREND_GOLDEN_CROSS_BUY": "5/20 골든크로스 매수",
    "BULL_REGIME_ALIGNED_ENTRY": "상승장 정배열 1차 진입",
    "MARTINGALE_BUY_INITIAL": "마틴게일 1차 매수",
    "BTC_TIERED_BUY_T1": "BTC 1단계 종가적립",
    "BTC_TIERED_BUY_T2": "BTC 2단계 특가적립",
    "BTC_UPBIT_DCA": "업비트 정기적립",
    "TRAILING_STOP_EXIT": "트레일링스탑 익절",
    "TREND_DEAD_CROSS_SELL": "5/20 데드크로스 청산",
    "TREND_MA20_BREAK_SELL": "20선 지지 이탈 청산",
    "BULL_STOP_LOSS_3PCT": "상승장 -3% 손절",
    "BULL_STOP_LOSS_10PCT": "상승장 -10% 긴급손절",
    "BASKET_TAKE_PROFIT": "바스켓 전량 익절",
    "BASKET_TAKE_PROFIT_LIMIT": "지정가 바스켓 익절",
    "TRANCHE_TAKE_PROFIT": "매직스플릿 차수 익절",
    "STOP_LOSS": "STOP-LOSS 손절",
    "LIMIT_ASK_CANCEL_FILL": "매도 취소 중 체결",
    "GRID_BUY_LVL_1": "그리드 1단계 매수",
    "GRID_BUY_LVL_2": "그리드 2단계 매수",
    "GRID_TP_LVL_1": "그리드 1단계 익절",
    "GRID_TP_LVL_2": "그리드 2단계 익절",
}


def format_action_name(raw_action: str) -> str:
    if not raw_action:
        return "체결"
    if raw_action.startswith("REGIME_SWITCH_PARTIAL_CUT_"):
        pct_s = raw_action.replace("REGIME_SWITCH_PARTIAL_CUT_", "").replace("PCT", "")
        return f"국면전환 {pct_s}% 부분손절"
    if raw_action.startswith("TRANCHE_TAKE_PROFIT"):
        return "매직스플릿 차수 익절"
    if raw_action.startswith("BULL_TIME_DCA_STEP_"):
        step_num = raw_action.replace("BULL_TIME_DCA_STEP_", "")
        return f"12시간 정기적립 ({step_num}회차)"
    if raw_action.startswith("BULL_PYRAMID_STEP_"):
        return f"상승 불타기 ({raw_action.replace('BULL_PYRAMID_STEP_', '')}회차)"
    if raw_action.startswith("BULL_CLOSING_BUY_STEP_"):
        return f"일봉 종가매수 ({raw_action.replace('BULL_CLOSING_BUY_STEP_', '')}회차)"
    return ACTION_KO_MAP.get(raw_action, raw_action)


@st.cache_data(ttl=60)
def fetch_ohlcv_cached(market: str, interval: str = "minute60", count: int = 168) -> pd.DataFrame:
    try:
        df = pyupbit.get_ohlcv(market, interval=interval, count=count)
        if df is not None and not df.empty:
            return df
    except Exception:
        pass
    return pd.DataFrame()


def render_coin_candlestick_chart(
    market: str,
    ticker: str,
    trades_df: pd.DataFrame = None,
    bot_avg_price: float = 0.0,
    target_price: float = 0.0,
    key_prefix: str = "main"
):
    # 1. 상단 컨트롤 바 (봉 주기 & 봉 개수 선택)
    c_itv, c_cnt = st.columns([1.6, 1.4])
    with c_itv:
        interval_choice = st.radio(
            "봉 주기",
            ["1시간", "4시간", "일봉", "주봉", "월봉"],
            index=0,
            horizontal=True,
            key=f"{key_prefix}_{market}_itv_radio"
        )
    
    interval_map = {
        "1시간": ("minute60", {"60봉 (2.5일)": 60, "120봉 (5일)": 120, "168봉 (7일)": 168, "336봉 (14일)": 336}),
        "4시간": ("minute240", {"60봉 (10일)": 60, "120봉 (20일)": 120, "180봉 (30일)": 180}),
        "일봉": ("day", {"60봉 (2달)": 60, "120봉 (4달)": 120, "200봉 (7달)": 200, "365봉 (1년)": 365}),
        "주봉": ("week", {"52봉 (1년)": 52, "104봉 (2년)": 104, "156봉 (3년)": 156}),
        "월봉": ("month", {"24봉 (2년)": 24, "36봉 (3년)": 36, "60봉 (5년)": 60}),
    }
    itv_code, count_options = interval_map.get(interval_choice, ("minute60", {"60봉": 60}))

    with c_cnt:
        count_label = st.radio(
            "조회 범위",
            list(count_options.keys()),
            index=0,
            horizontal=True,
            key=f"{key_prefix}_{market}_cnt_radio"
        )
    candle_count = count_options.get(count_label, 60)

    # 2. 보조 지표 및 표시 옵션 토글 바
    t_col1, t_col2, t_col3, t_col4, t_col5 = st.columns(5)
    with t_col1:
        show_ma = st.checkbox("📈 이평선 (5/20/60선)", value=True, key=f"{key_prefix}_{market}_chk_ma")
    with t_col2:
        show_trades = st.checkbox("🎯 실전 매매 타점", value=True, key=f"{key_prefix}_{market}_chk_trades")
    with t_col3:
        show_callouts = st.checkbox("🏷️ 타점 가격 말풍선", value=True, key=f"{key_prefix}_{market}_chk_callouts")
    with t_col4:
        show_lines = st.checkbox("💳 봇 평단/익절선", value=True, key=f"{key_prefix}_{market}_chk_lines")
    with t_col5:
        show_volume = st.checkbox("📊 거래량 차트", value=True, key=f"{key_prefix}_{market}_chk_vol")

    df = fetch_ohlcv_cached(market, interval=itv_code, count=candle_count)
    if df is None or df.empty:
        st.info(f"{market} 시세 데이터를 불러올 수 없습니다.")
        return

    # 거래 내역 필터링
    f_trades = pd.DataFrame()
    if trades_df is not None and not trades_df.empty and "timestamp" in trades_df.columns:
        f_trades = filter_trades_for_candles(trades_df, df.index, itv_code)

    # 이동평균선 계산
    df["MA5"] = df["close"].rolling(5).mean()
    df["MA20"] = df["close"].rolling(20).mean()
    df["MA60"] = df["close"].rolling(60).mean()

    # 서브플롯 구성 (거래량 표시 여부에 따른 동적 분할)
    if show_volume:
        fig = make_subplots(
            rows=2, cols=1,
            shared_xaxes=True,
            vertical_spacing=0.03,
            row_heights=[0.80, 0.20]
        )
    else:
        fig = make_subplots(
            rows=1, cols=1,
            shared_xaxes=True
        )

    # 1. 캔들스틱 (고선명 트레이딩뷰 대비: 양봉 네온빨강 #ff3b69 / 음봉 일렉트릭블루 #0088ff)
    fig.add_trace(
        go.Candlestick(
            x=df.index,
            open=df["open"],
            high=df["high"],
            low=df["low"],
            close=df["close"],
            name=f"{interval_choice} 캔들",
            increasing_line_color="#ff3b69",
            increasing_fillcolor="#ff3b69",
            decreasing_line_color="#0088ff",
            decreasing_fillcolor="#0088ff",
            line=dict(width=1.2),
            showlegend=False
        ),
        row=1, col=1
    )

    # 2. 이동평균선 (선명한 골드, 핫핑크, 네온스카이)
    if show_ma:
        fig.add_trace(go.Scatter(x=df.index, y=df["MA5"], mode="lines", name="5선 (단기)", line=dict(color="#ffd166", width=1.4)), row=1, col=1)
        fig.add_trace(go.Scatter(x=df.index, y=df["MA20"], mode="lines", name="20선 (생명선)", line=dict(color="#f72585", width=1.8)), row=1, col=1)
        if len(df) >= 60:
            fig.add_trace(go.Scatter(x=df.index, y=df["MA60"], mode="lines", name="60선 (추세선)", line=dict(color="#4cc9f0", width=1.5)), row=1, col=1)

    # 3. 매매 타점 오버레이 (B / S 원형 뱃지 마커 + 말풍선 배지)
    if show_trades and not f_trades.empty:
        buys = f_trades[f_trades["side"] == "bid"]
        sells = f_trades[f_trades["side"] == "ask"]

        if not buys.empty:
            buy_records = []
            for (dt_val, price_val), group_df in buys.groupby(["dt", "price"], sort=False):
                action_ko = format_action_name(str(group_df.iloc[0].get("action", "")))
                p_val = float(price_val or 0.0)
                tot_vol = float(group_df["volume"].sum())
                tot_cost = float(group_df["cost_or_revenue"].sum() if "cost_or_revenue" in group_df.columns else (p_val * tot_vol))
                cnt = len(group_df)
                cnt_str = f" ({cnt}건 일괄)" if cnt > 1 else ""
                buy_records.append({
                    "dt": dt_val,
                    "price": p_val,
                    "hover": (
                        f"<b>🟢 [B 매수 체결{cnt_str}]</b><br>"
                        f"• 시각: {dt_val.strftime('%Y-%m-%d %H:%M')}<br>"
                        f"• 유형: <b>{action_ko}</b><br>"
                        f"• 체결단가: <b>{p_val:,.0f}원</b><br>"
                        f"• 체결수량: {tot_vol:.6f} {ticker}<br>"
                        f"• 체결금액: {tot_cost:,.0f}원"
                    )
                })
            b_df = pd.DataFrame(buy_records)
            fig.add_trace(
                go.Scatter(
                    x=b_df["dt"],
                    y=b_df["price"],
                    mode="markers+text",
                    name=f"매수 B ({len(buys)}건)",
                    marker=dict(
                        symbol="circle",
                        size=24,
                        color="#00c853",
                        line=dict(color="#ffffff", width=2.0)
                    ),
                    text=["B"] * len(b_df),
                    textposition="middle center",
                    textfont=dict(
                        color="#ffffff",
                        size=12,
                        family="Arial Black, Helvetica, sans-serif"
                    ),
                    hovertext=b_df["hover"].tolist(),
                    hoverinfo="text"
                ),
                row=1, col=1
            )

        if not sells.empty:
            sell_records = []
            for (dt_val, price_val), group_df in sells.groupby(["dt", "price"], sort=False):
                action_ko = format_action_name(str(group_df.iloc[0].get("action", "")))
                p_val = float(price_val or 0.0)
                tot_vol = float(group_df["volume"].sum())
                tot_pnl = float(group_df.get("pnl", pd.Series([0.0] * len(group_df))).sum())
                cnt = len(group_df)
                cnt_str = f" ({cnt}차수 일괄청산)" if cnt > 1 else ""
                pnl_str = f"<br>• 실현손익: <b>{tot_pnl:+,.0f}원</b>" if tot_pnl != 0 else ""
                if cnt > 1 and tot_pnl != 0:
                    pnl_str += " (합산)"
                sell_records.append({
                    "dt": dt_val,
                    "price": p_val,
                    "hover": (
                        f"<b>🔴 [S 매도 체결{cnt_str}]</b><br>"
                        f"• 시각: {dt_val.strftime('%Y-%m-%d %H:%M')}<br>"
                        f"• 유형: <b>{action_ko}</b><br>"
                        f"• 체결단가: <b>{p_val:,.0f}원</b><br>"
                        f"• 체결수량: {tot_vol:.6f} {ticker}"
                        f"{pnl_str}"
                    )
                })
            s_df = pd.DataFrame(sell_records)
            fig.add_trace(
                go.Scatter(
                    x=s_df["dt"],
                    y=s_df["price"],
                    mode="markers+text",
                    name=f"매도 S ({len(sells)}건)",
                    marker=dict(
                        symbol="circle",
                        size=24,
                        color="#ff1744",
                        line=dict(color="#ffffff", width=2.0)
                    ),
                    text=["S"] * len(s_df),
                    textposition="middle center",
                    textfont=dict(
                        color="#ffffff",
                        size=12,
                        family="Arial Black, Helvetica, sans-serif"
                    ),
                    hovertext=s_df["hover"].tolist(),
                    hoverinfo="text"
                ),
                row=1, col=1
            )

        # 4) 말풍선 배지 핀 (Callout Annotations)
        if show_callouts and not f_trades.empty:
            grouped_callouts = []
            for (dt_val, side_val, price_val), group_df in f_trades.groupby(["dt", "side", "price"], sort=False):
                tot_pnl = float(group_df.get("pnl", pd.Series([0.0] * len(group_df))).sum())
                tot_vol = float(group_df["volume"].sum())
                cnt = len(group_df)
                grouped_callouts.append({
                    "dt": dt_val,
                    "side": side_val,
                    "price": float(price_val),
                    "total_pnl": tot_pnl,
                    "total_vol": tot_vol,
                    "count": cnt
                })
            grouped_callouts_df = pd.DataFrame(grouped_callouts).head(12)
            for _, r in grouped_callouts_df.iterrows():
                p_val = float(r["price"])
                pnl_val = float(r["total_pnl"])
                cnt_val = int(r["count"])
                if r["side"] == "bid":
                    bid_badge = f"<b>B 매수</b> {p_val:,.0f}원" if cnt_val == 1 else f"<b>B 매수</b> {p_val:,.0f}원 ({cnt_val}건)"
                    fig.add_annotation(
                        x=r["dt"],
                        y=p_val,
                        xref="x",
                        yref="y",
                        text=bid_badge,
                        showarrow=True,
                        arrowhead=2,
                        arrowsize=1.2,
                        arrowwidth=2,
                        arrowcolor="#00e676",
                        ay=46,
                        ax=0,
                        bgcolor="#007a3d",
                        bordercolor="#00e676",
                        borderwidth=1.8,
                        borderpad=3,
                        font=dict(color="#ffffff", size=10, family="sans-serif")
                    )
                else:
                    if cnt_val > 1:
                        pnl_badge_txt = f"<br>합산손익: {pnl_val:+,.0f}원 ({cnt_val}차수)" if pnl_val != 0 else f"<br>({cnt_val}차수 일괄)"
                    else:
                        pnl_badge_txt = f"<br>손익: {pnl_val:+,.0f}원" if pnl_val != 0 else ""
                    fig.add_annotation(
                        x=r["dt"],
                        y=p_val,
                        xref="x",
                        yref="y",
                        text=f"<b>S 매도</b> {p_val:,.0f}원{pnl_badge_txt}",
                        showarrow=True,
                        arrowhead=2,
                        arrowsize=1.2,
                        arrowwidth=2,
                        arrowcolor="#ff3b69",
                        ay=-46,
                        ax=0,
                        bgcolor="#b91c1c",
                        bordercolor="#ff3b69",
                        borderwidth=1.8,
                        borderpad=3,
                        font=dict(color="#ffffff", size=10, family="sans-serif")
                    )

    # 4. 봇 평단가 및 익절 목표가 가이드선
    if show_lines:
        if bot_avg_price > 0:
            fig.add_hline(
                y=bot_avg_price,
                line_dash="dash",
                line_color="#ffd166",
                line_width=1.6,
                annotation_text=f" 봇 평단 {bot_avg_price:,.0f}원",
                annotation_position="top left",
                annotation_font=dict(color="#ffd166", size=11, family="sans-serif"),
                row=1, col=1
            )
        if target_price > 0:
            fig.add_hline(
                y=target_price,
                line_dash="dot",
                line_color="#00f0ff",
                line_width=1.4,
                annotation_text=f" 익절 목표 {target_price:,.0f}원",
                annotation_position="bottom left",
                annotation_font=dict(color="#00f0ff", size=11, family="sans-serif"),
                row=1, col=1
            )

    # 5. 거래량 바 차트
    if show_volume:
        v_colors = ["#ff3b69" if c >= o else "#0088ff" for c, o in zip(df["close"], df["open"])]
        fig.add_trace(
            go.Bar(
                x=df.index,
                y=df["volume"],
                name="거래량",
                marker_color=v_colors,
                showlegend=False,
                opacity=0.6
            ),
            row=2, col=1
        )

    # 6. 트레이딩뷰 스타일 레이아웃 & 십자선(Crosshairs) 활성화
    chart_h = 510 if show_volume else 440
    fig.update_layout(
        template="plotly_dark",
        plot_bgcolor="#131722",
        paper_bgcolor="#131722",
        height=chart_h,
        margin=dict(l=10, r=50, t=25, b=10),
        hovermode="closest",
        legend=dict(
            orientation="h",
            yanchor="bottom",
            y=1.02,
            xanchor="right",
            x=1,
            font=dict(size=11, color="#d1d4dc")
        )
    )
    
    # 십자선(Crosshair spikelines) 및 그리드 설정
    fig.update_xaxes(
        rangeslider=dict(visible=False),
        showgrid=True,
        gridcolor="#222735",
        type="date",
        showspikes=True,
        spikemode="across",
        spikethickness=1,
        spikedash="dot",
        spikecolor="#8c96a5",
        row=1, col=1
    )
    fig.update_yaxes(
        showgrid=True,
        gridcolor="#222735",
        tickformat=",.0f",
        side="right",
        showspikes=True,
        spikemode="across",
        spikethickness=1,
        spikedash="dot",
        spikecolor="#8c96a5",
        row=1, col=1
    )
    if show_volume:
        fig.update_xaxes(
            showgrid=True,
            gridcolor="#222735",
            type="date",
            showspikes=True,
            spikemode="across",
            spikethickness=1,
            spikedash="dot",
            spikecolor="#8c96a5",
            row=2, col=1
        )
        fig.update_yaxes(showgrid=False, side="right", row=2, col=1)

    st.plotly_chart(fig, use_container_width=True)


# =============================================================================
# 상단 헤더 & 컨트롤 바
# =============================================================================
with st.container():
    c_title, c_mode, c_refresh, c_btn = st.columns([3.6, 1.6, 1.8, 1.0])

    with c_title:
        st.html("""
        <div style="display:flex; align-items:center; gap:12px;">
            <span style="font-size:2.2rem;">⚡</span>
            <div>
                <h2 style="margin:0; font-size:1.6rem; font-weight:800; color:#f0f3f6;">
                    AutoBot 전략 대시보드
                </h2>
                <div style="color:#8c96a5; font-size:0.84rem;">
                    업비트 실전 매매 연동 | 전체 계좌 포트폴리오 & 다중 전략 성과 관리
                </div>
            </div>
        </div>
        """)

    with c_mode:
        st.html("""
        <div style="padding-top:8px;">
            <div style="font-size:0.75rem; color:#8c96a5;">현재 매매 모드</div>
            <div style="font-size:1.05rem; font-weight:700; color:#00c087;">🟢 실전매매 (Real)</div>
        </div>
        """)

    with c_refresh:
        refresh_mode = st.selectbox(
            "🔄 갱신 주기",
            ["trade_event", 10, 30, 60, "manual"],
            index=0,
            format_func=lambda x: {
                "trade_event": "⚡ 매매체결 시 (기본)",
                10: "⏱️ 10초 주기",
                30: "⏱️ 30초 주기",
                60: "⏱️ 60초 주기",
                "manual": "🛑 수동 갱신"
            }.get(x, str(x))
        )

    with c_btn:
        st.markdown("<div style='height:28px;'></div>", unsafe_allow_html=True)
        if st.button("🔄 즉시 새로고침", use_container_width=True):
            st.rerun()

st.markdown("<div style='height:10px;'></div>", unsafe_allow_html=True)


# =============================================================================
# 데이터 로드 (실시간 업비트 API 또는 암호화 스냅샷 동기화)
# =============================================================================
account_data = get_total_account_summary()
active_strategies = get_all_active_strategies()

data_source_mode = "DIRECT_API"  # "DIRECT_API", "CLOUD_SYNC", "LOCAL_SNAPSHOT"
snapshot_time_str = None
sync_status_note = ""
loaded_snapshot = None

# 업비트 API 키가 없거나 IP 제한 오류 등으로 계좌 조회가 불가능한 경우 (Streamlit Cloud 환경 등)
if account_data.get("is_api_key_missing"):
    snapshot, status_msg = fetch_snapshot_from_cloud()
    if snapshot and "account_data" in snapshot:
        loaded_snapshot = snapshot
        account_data = snapshot["account_data"]
        # 전략 목록 및 DataFrame 복원
        raw_strategies = snapshot.get("active_strategies", [])
        active_strategies = []
        for s in raw_strategies:
            s_copy = dict(s)
            trades_list = s_copy.get("trades_list", [])
            s_copy["trades_df"] = pd.DataFrame(trades_list) if trades_list else pd.DataFrame()
            active_strategies.append(s_copy)

        data_source_mode = "CLOUD_SYNC"
        snapshot_time_str = snapshot.get("updated_at")
        sync_status_note = status_msg
    else:
        # 로컬 스냅샷 폴백 확인
        local_snap = load_local_snapshot()
        if local_snap and "account_data" in local_snap:
            loaded_snapshot = local_snap
            account_data = local_snap["account_data"]
            raw_strategies = local_snap.get("active_strategies", [])
            active_strategies = []
            for s in raw_strategies:
                s_copy = dict(s)
                trades_list = s_copy.get("trades_list", [])
                s_copy["trades_df"] = pd.DataFrame(trades_list) if trades_list else pd.DataFrame()
                active_strategies.append(s_copy)

            data_source_mode = "LOCAL_SNAPSHOT"
            snapshot_time_str = local_snap.get("updated_at")
            sync_status_note = "로컬 백업 스냅샷"


# =============================================================================
# [동기화 상태 뱃지 또는 Secrets 설정 안내 배너]
# =============================================================================
if data_source_mode in ["CLOUD_SYNC", "LOCAL_SNAPSHOT"]:
    elapsed_text = ""
    is_stale = False
    if snapshot_time_str:
        try:
            snap_dt = datetime.strptime(snapshot_time_str, "%Y-%m-%d %H:%M:%S")
            diff_sec = int((datetime.now() - snap_dt).total_seconds())
            if diff_sec < 60:
                elapsed_text = f"{max(0, diff_sec)}초 전"
            elif diff_sec < 3600:
                elapsed_text = f"{diff_sec // 60}분 전"
                if diff_sec >= 180:
                    is_stale = True
            else:
                elapsed_text = f"{diff_sec // 3600}시간 전"
                is_stale = True
        except Exception:
            elapsed_text = "-"

    if not is_stale:
        st.html(f"""
        <div style="background:linear-gradient(90deg, #00c08718, #00c08708); border:1px solid #00c08755; border-radius:10px; padding:10px 16px; margin-bottom:14px; display:flex; justify-content:space-between; align-items:center;">
            <div style="display:flex; align-items:center; gap:10px;">
                <span style="font-size:1.3rem;">🟢</span>
                <div>
                    <span style="color:#00c087; font-weight:800; font-size:0.95rem;">로컬 봇 스냅샷 실시간 동기화 모드 (정상)</span>
                    <div style="color:#8c96a5; font-size:0.8rem; margin-top:2px;">
                        사용자 Mac 로컬 봇(main.py)의 암호화 스냅샷과 실시간 연동되어 있습니다. (IP 제한 에러 없음)
                    </div>
                </div>
            </div>
            <div style="text-align:right;">
                <span style="background:#00c08722; color:#00c087; padding:3px 10px; border-radius:8px; font-weight:700; font-size:0.82rem; border:1px solid #00c08755;">
                    동기화 시각: {snapshot_time_str} ({elapsed_text})
                </span>
            </div>
        </div>
        """)
    else:
        st.html(f"""
        <div style="background:linear-gradient(90deg, #f59e0b18, #f59e0b08); border:1px solid #f59e0b55; border-radius:10px; padding:10px 16px; margin-bottom:14px; display:flex; justify-content:space-between; align-items:center;">
            <div style="display:flex; align-items:center; gap:10px;">
                <span style="font-size:1.3rem;">🟡</span>
                <div>
                    <span style="color:#f59e0b; font-weight:800; font-size:0.95rem;">스냅샷 동기화 지연 ({elapsed_text})</span>
                    <div style="color:#8c96a5; font-size:0.8rem; margin-top:2px;">
                        마지막 갱신 시각: {snapshot_time_str}. 로컬 Mac에서 봇(main.py) 실행 여부 및 GITHUB_TOKEN 만료 여부를 확인하세요.
                    </div>
                </div>
            </div>
        </div>
        """)

elif account_data.get("is_api_key_missing"):
    st.warning("""
    ### ⚠️ [웹 대시보드] 스냅샷 동기화 키(Secrets) 등록 필요
    
    웹 대시보드(Streamlit Cloud)에서 로컬 봇의 암호화 스냅샷 데이터를 복호화하기 위해 **Streamlit Secrets**에 키 등록이 필요합니다.
    
    **👉 설정 방법 (30초 완료):**
    1. 화면 우측 하단의 **'Manage app'** 클릭 (또는 우측 상단 `⋮` 메뉴)
    2. **Settings** ➔ **Secrets** 탭 선택
    3. 아래 양식을 복사하여 입력 후 **Save** 클릭:
    ```toml
    SYNC_SECRET_KEY = "내_SYNC_SECRET_KEY"
    GITHUB_TOKEN = "내_GITHUB_PERSONAL_ACCESS_TOKEN"
    ```
    4. 저장 즉시 페이지가 자동으로 새로고침되며 실시간 동기화 대시보드가 정상 연동됩니다!
    """)


# =============================================================================
# [🚨 예수금 10만원 미만 긴급 경고 배너]
# =============================================================================
if account_data.get("is_krw_warning", False):
    st.html(f"""
    <div class="krw-alert-box">
        <span style="font-size:2.2rem;">🚨</span>
        <div>
            <div style="font-size:1.15rem; font-weight:800; color:#ffffff;">
                [예수금 10만원 미만 경고] 현재 주문가능 예수금이 {account_data['krw_balance']:,.0f}원 입니다!
            </div>
            <div style="font-size:0.86rem; color:#ffcdd2; margin-top:3px;">
                최소 유지 기준({MIN_KRW_ALERT_THRESHOLD:,}원) 이하로 떨어졌습니다. 
                추가 원화를 입금하시거나 다른 보유 자산을 매도하여 예수금을 확보하세요. (텔레그램 알림 발송됨)
            </div>
        </div>
    </div>
    """)


# =============================================================================
# 1. 전체 계좌 현황 & 전체 계좌 수익률 (제목의 [위] 삭제)
# =============================================================================
st.markdown("### 🏛️ 전체 계좌 현황 & 전체 계좌 수익률")

# 1-1. 핵심 계좌 지표 4분할 카드
c1, c2, c3, c4 = st.columns(4)

tot_equity = account_data["total_equity"]
growth_pct = account_data["growth_pct"]
growth_amt = account_data["growth_amount"]
growth_color = "text-green" if growth_pct >= 0 else "text-red"

with c1:
    st.html(f"""
    <div class="metric-card">
        <div class="metric-title">1. 전체 계좌 총 평가자산</div>
        <div class="metric-val">{tot_equity:,.0f} <span style="font-size:1rem; color:#8c96a5;">KRW</span></div>
        <div class="metric-sub">
            기준 대비 성장률: <b class="{growth_color}">{growth_pct:+.2f}%</b> ({growth_amt:+,.0f}원)
        </div>
    </div>
    """)

with c2:
    krw_bal = account_data["krw_balance"]
    krw_locked = account_data.get("krw_locked", 0.0)
    total_krw = account_data.get("total_krw", krw_bal + krw_locked)
    krw_warn = account_data["is_krw_warning"]
    krw_badge = '<span style="background:#ff3b6922; color:#ff3b69; padding:2px 8px; border-radius:10px; font-size:0.75rem; border:1px solid #ff3b69; font-weight:700;">🚨 10만원 미만 부족</span>' if krw_warn else '<span style="background:#00c08722; color:#00c087; padding:2px 8px; border-radius:10px; font-size:0.75rem; border:1px solid #00c087; font-weight:700;">✅ 정상</span>'
    krw_color = "text-red" if krw_warn else "text-green"
    st.html(f"""
    <div class="metric-card" style="border-color: {'#ff3b69' if krw_warn else '#2b3345'};">
        <div class="metric-title" style="display:flex; justify-content:space-between; align-items:center;">
            <span>2. 💵 예수금 (총 {total_krw:,.0f}원)</span>
            {krw_badge}
        </div>
        <div class="metric-val {krw_color}">{krw_bal:,.0f} <span style="font-size:1rem; color:#8c96a5;">원 (가용)</span></div>
        <div class="metric-sub">
            주문 대기(잠금): <b style="color:#ffb300;">{krw_locked:,.0f}원</b> | 총 보유원화: <b>{total_krw:,.0f}원</b>
        </div>
    </div>
    """)

with c3:
    c_eval = account_data["total_coin_eval"]
    u_pnl = account_data["unrealized_pnl"]
    c_pnl_pct = account_data["coin_pnl_pct"]
    pnl_color = "text-green" if u_pnl >= 0 else "text-red"
    st.html(f"""
    <div class="metric-card">
        <div class="metric-title">3. 코인 총 평가금액</div>
        <div class="metric-val">{c_eval:,.0f} <span style="font-size:1rem; color:#8c96a5;">원</span></div>
        <div class="metric-sub">
            총 평가손익: <b class="{pnl_color}">{u_pnl:+,.0f}원 ({c_pnl_pct:+.2f}%)</b>
        </div>
    </div>
    """)

with c4:
    t_cost = account_data["total_invested"]
    st.html(f"""
    <div class="metric-card">
        <div class="metric-title">4. 전체 투자 원가 (원금)</div>
        <div class="metric-val">{t_cost:,.0f} <span style="font-size:1rem; color:#8c96a5;">원</span></div>
        <div class="metric-sub">
            코인 매수원가: {account_data['total_coin_cost']:,.0f}원
        </div>
    </div>
    """)

st.markdown("<div style='height:14px;'></div>", unsafe_allow_html=True)

# 1-2. 계좌 자산 구성 파이 차트 & 보유 자산 상세 테이블
with st.expander("📊 계좌 자산 포트폴리오 구성 & 보유 종목 상세", expanded=True):
    col_chart, col_table = st.columns([1.1, 1.9])

    coins = account_data["coins"]

    with col_chart:
        labels = ["KRW (예수금)"] + [c["currency"] for c in coins if c["eval_amount"] > 0]
        values = [account_data["total_krw"]] + [c["eval_amount"] for c in coins if c["eval_amount"] > 0]
        colors = ["#2962ff", "#f7931a", "#627eea", "#14f195", "#8c96a5"]

        fig_pie = go.Figure(data=[go.Pie(
            labels=labels,
            values=values,
            hole=0.55,
            marker=dict(colors=colors),
            textinfo="label+percent",
            hovertemplate="<b>%{label}</b><br>평가액: %{value:,.0f}원<br>비중: %{percent}<extra></extra>"
        )])
        fig_pie.update_layout(
            height=250,
            margin=dict(l=10, r=10, t=15, b=15),
            template="plotly_dark",
            showlegend=False
        )
        st.plotly_chart(fig_pie, use_container_width=True)

    with col_table:
        table_rows = []
        # KRW 행 추가
        table_rows.append({
            "자산": "KRW (원화)",
            "총보유수량": f"{account_data['total_krw']:,.0f}원",
            "매수평단가": "-",
            "현재가": "-",
            "평가금액": f"{account_data['total_krw']:,.0f}원",
            "평가손익(수익률)": "-",
            "비중": f"{(account_data['total_krw']/tot_equity*100.0):.1f}%" if tot_equity > 0 else "0%"
        })

        for c in coins:
            pnl_s = f"{c['pnl']:+,.0f}원 ({c['pnl_pct']:+.2f}%)" if c["cost_amount"] > 0 else "-"
            table_rows.append({
                "자산": c["currency"],
                "총보유수량": f"{c['total_balance']:.6f}",
                "매수평단가": f"{c['avg_buy_price']:,.0f}원" if c["avg_buy_price"] > 0 else "-",
                "현재가": f"{c['current_price']:,.0f}원",
                "평가금액": f"{c['eval_amount']:,.0f}원",
                "평가손익(수익률)": pnl_s,
                "비중": f"{c['weight_pct']:.1f}%"
            })

        st.dataframe(pd.DataFrame(table_rows), hide_index=True, use_container_width=True)

st.divider()


def render_main_sol_exit_only_card(
    strat: Dict[str, Any],
    cur_p: float,
    open_orders: List[Dict[str, Any]],
    trades_df: pd.DataFrame,
    idx: int = 3
) -> None:
    """
    메인봇 솔라나(KRW-SOL) 익절 전용 모드 간소화 카드
    - 사용자 요청: "대시보드를 정리해줘. 메인봇 솔은 매도되는것만 확인하면 될것 같아."
    - 복잡한 1시간봉 차트/스쿼드 테이블 대신, 현재가, 등록된 익절 매도가(142,400원), 잔여 거리, 청산 대기 상태를 직관적으로 요약 표시합니다.
    """
    market = strat["market"]
    ticker = strat["ticker"]
    bot_q = float(strat.get("bot_quantity", 0.0) or 0.0)
    bot_avg = float(strat.get("bot_avg_price", 0.0) or 0.0)
    bot_eval = bot_q * cur_p
    unrealized_pnl = (cur_p - bot_avg) * bot_q if bot_avg > 0 else 0.0
    unrealized_pnl_pct = ((cur_p - bot_avg) / bot_avg * 100.0) if bot_avg > 0 else 0.0

    # 등록된 지정가 매도 주문 탐색
    ask_orders = [o for o in open_orders if o.get("side") == "ask"]
    target_sell_p = float(ask_orders[0].get("price", 142400.0)) if ask_orders else 142400.0
    target_sell_vol = float(ask_orders[0].get("volume", bot_q)) if ask_orders else bot_q
    sell_order_uuid = ask_orders[0].get("uuid", "-") if ask_orders else "-"

    dist_to_tp = ((target_sell_p - cur_p) / cur_p * 100.0) if cur_p > 0 else 0.0
    expected_profit = (target_sell_p * target_sell_vol * 0.9995) - (bot_avg * target_sell_vol)
    expected_pnl_pct = ((target_sell_p - bot_avg) / bot_avg * 100.0) if bot_avg > 0 else 0.0

    pnl_color = "#00e676" if unrealized_pnl >= 0 else "#ff5252"
    dist_badge_color = "#00e676" if dist_to_tp <= 0 else "#ffb300"

    html_card = f"""
<div style="background:linear-gradient(135deg, #181d27 0%, #1f2737 100%); border:1px solid #ffb30055; border-radius:12px; padding:18px 22px; margin-bottom:20px; box-shadow:0 4px 14px rgba(255, 179, 0, 0.08);">
    <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:14px; border-bottom:1px solid #2d3648; padding-bottom:10px; flex-wrap:wrap; gap:8px;">
        <div style="display:flex; align-items:center; gap:10px;">
            <span style="font-size:1.3rem;">🟣</span>
            <span style="font-size:1.15rem; font-weight:800; color:#f0f3f6;">
                전략 {idx}. 메인봇 솔라나 ({market})
            </span>
            <span style="background:#ffb30022; color:#ffb300; border:1px solid #ffb300; padding:2px 10px; border-radius:12px; font-size:0.8rem; font-weight:700;">
                🎯 익절 청산 대기중 (EXIT ONLY)
            </span>
        </div>
        <div style="font-size:0.85rem; color:#8c96a5;">
            전략 배정 원금: <b style="color:#f0f3f6;">114,844원</b> | 신규 매수 <b>차단</b>
        </div>
    </div>

    <div style="display:grid; grid-template-columns: repeat(4, 1fr); gap:12px; margin-bottom:12px;">
        <div style="background:#12161f; padding:12px 16px; border-radius:8px; border:1px solid #232b3a;">
            <div style="font-size:0.75rem; color:#8c96a5; margin-bottom:2px;">현재 시세</div>
            <div style="font-size:1.25rem; font-weight:700; color:#f0f3f6;">{cur_p:,.0f}원</div>
            <div style="font-size:0.75rem; color:{pnl_color}; margin-top:2px;">평가손익: {unrealized_pnl:+,.0f}원 ({unrealized_pnl_pct:+.2f}%)</div>
        </div>
        <div style="background:#12161f; padding:12px 16px; border-radius:8px; border:1px solid #232b3a;">
            <div style="font-size:0.75rem; color:#8c96a5; margin-bottom:2px;">🤖 봇 매수평단 & 수량</div>
            <div style="font-size:1.25rem; font-weight:700; color:#81d4fa;">{bot_avg:,.0f}원</div>
            <div style="font-size:0.75rem; color:#8c96a5; margin-top:2px;">보유: {bot_q:.6f} SOL ({bot_eval:,.0f}원)</div>
        </div>
        <div style="background:#12161f; padding:12px 16px; border-radius:8px; border:1px solid #00e67644;">
            <div style="font-size:0.75rem; color:#00e676; margin-bottom:2px;">🎯 등록된 지정가 매도가</div>
            <div style="font-size:1.25rem; font-weight:800; color:#00e676;">{target_sell_p:,.0f}원</div>
            <div style="font-size:0.75rem; color:#00e676; margin-top:2px;">목표 마진: +{expected_pnl_pct:.2f}% (순익: +{expected_profit:,.0f}원)</div>
        </div>
        <div style="background:#12161f; padding:12px 16px; border-radius:8px; border:1px solid #232b3a;">
            <div style="font-size:0.75rem; color:#8c96a5; margin-bottom:2px;">익절 도달까지 잔여 거리</div>
            <div style="font-size:1.25rem; font-weight:700; color:{dist_badge_color};">+{dist_to_tp:.2f}%</div>
            <div style="font-size:0.75rem; color:#8c96a5; margin-top:2px;">미체결 매도: {len(ask_orders)}건 | 매수: 0건</div>
        </div>
    </div>

    <div style="background:#141b24; padding:10px 14px; border-radius:8px; border:1px dashed #2d3848; font-size:0.83rem; color:#cbd5e1; display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap; gap:8px;">
        <div>
            💡 <b>운용 알림</b>: 추가 매수가 전면 차단되었으며, 업비트에 <b>{target_sell_p:,.0f}원</b> 전량 매도 지정가 주문이 등록되어 있습니다.
            체결 즉시 메인봇 솔라나 운용이 완료되며, 회수 자금은 신규 <b>Cascade 포켓</b>으로 이전됩니다.
        </div>
        <div style="font-size:0.75rem; color:#8c96a5; white-space:nowrap;">
            주문 UUID: <code>{sell_order_uuid[:8]}...</code>
        </div>
    </div>
</div>
"""
    st.html(html_card)

    with st.expander(f"📜 {ticker} 최근 체결 내역 및 주문 상세 ({len(trades_df)}건)", expanded=False):
        if not trades_df.empty:
            show_df = trades_df.head(10).copy()
            show_cols = [c for c in ["timestamp", "side", "action", "price", "volume", "cost_or_revenue", "pnl"] if c in show_df.columns]
            st.dataframe(show_df[show_cols], hide_index=True, use_container_width=True)
        else:
            st.info("체결 내역이 없습니다.")


# =============================================================================
# 2. 현재 적용한 전략의 수익률 및 현황 (제목의 [아래] 삭제, 복수 전략 지원)
# =============================================================================
st.markdown("### 🎯 현재 적용한 전략의 수익률 및 현황")
st.caption("각 코인/전략별로 배정된 투자금과 개별 전략의 실시간 수익률 및 미체결 물타기 주문을 아래에 별도로 표시합니다.")
st.caption("ETH·SOL 손익은 DB 장부 기준입니다. 매수·매도 수수료 차감 후 순손익을 보장하지 않으며, 과거 기록을 추정 보정하지 않습니다.")

if not active_strategies:
    st.info("현재 활성화된 전략이 없습니다. config.py의 INVESTMENTS 설정을 확인해주세요.")
else:
    for idx, strat in enumerate(active_strategies, start=1):
        market = strat["market"]
        ticker = strat["ticker"]
        strat_name = strat["strategy_name"]
        unit_krw = strat["unit_krw"]
        initial_cap = strat["initial_capital"]
        strat_ret = strat.get("unrealized_pnl_pct", strat["strategy_return_pct"]) if market == "KRW-BTC" else strat["strategy_return_pct"]
        strat_pnl = strat["total_strat_pnl"]
        realized_pnl = strat["realized_pnl"]
        unrealized_pnl = strat["unrealized_pnl"]
        cur_p = strat["current_price"]
        avg_p = strat["avg_buy_price"]
        coin_bal = strat["coin_balance"]
        eval_amt = strat["eval_amount"]
        open_orders = strat["open_orders"]
        trades_df = strat["trades_df"]

        if market == "KRW-SOL":
            render_main_sol_exit_only_card(strat, cur_p, open_orders, trades_df, idx=idx)
            st.markdown("<div style='height:14px;'></div>", unsafe_allow_html=True)
            continue


        ret_color = "text-green" if strat_ret >= 0 else "text-red"
        pnl_color = "text-green" if strat_pnl >= 0 else "text-red"
        badge_style = "background:#2962ff22; color:#2962ff; border:1px solid #2962ff;"

        regime_info = strat.get("regime_info", {})
        r_state = strat.get("runtime_state", {})
        persisted_regime = r_state.get("current_regime", "BEAR")

        raw_is_bull = regime_info.get("is_bull")
        if raw_is_bull is None:
            # 완충/확인 대기(HOLD) 구간에서는 봇이 유지 중인 직전 확정 국면(runtime_state)을 따름
            is_bull = (persisted_regime == "BULL")
        else:
            is_bull = bool(raw_is_bull)

        regime_color = "#00c087" if is_bull else "#ff3b69"
        if raw_is_bull is None:
            hold_sub = "상승 유지" if is_bull else "하락 유지"
            regime_badge = f'<span style="background:#ffb30022; color:#ffb300; border:1px solid #ffb300; padding:2px 10px; border-radius:12px; font-size:0.8rem; font-weight:700;">국면 확인 대기 ({hold_sub})</span>'
        else:
            regime_badge = f'<span style="background:{regime_color}22; color:{regime_color}; border:1px solid {regime_color}; padding:2px 10px; border-radius:12px; font-size:0.8rem; font-weight:700;">{regime_info.get("regime_korean", "국면 분석 중")}</span>'
        
        target_base_p = strat.get("bot_avg_price", 0.0) or avg_p or cur_p
        from config import is_adaptive_grid_enabled
        if is_adaptive_grid_enabled(market):
            from strategy.adaptive_grid import calculate_basket_sell_price, get_dynamic_tp_margins
            basket_target_p = calculate_basket_sell_price(target_base_p, is_bull=is_bull)
            b_m, t_m = get_dynamic_tp_margins(is_bull=is_bull, market=market)
            strat_profit_margin = b_m
            active_tranche_margin_pct = (t_m - 1.0) * 100
        else:
            basket_target_p = target_base_p * strat.get("profit_margin", 1.005) if target_base_p > 0 else 0.0
            strat_profit_margin = strat.get("profit_margin", 1.005)
            from config import get_magic_split_tranche_profit
            active_tranche_margin_pct = get_magic_split_tranche_profit(market) * 100

        dist_pct = float(regime_info.get("distance_ma200_pct", 0.0))
        dist_color = "#00c087" if dist_pct >= 0 else "#ff3b69"
        sig_text = regime_info.get("signal", "대기")
        raw_reason = regime_info.get("reason", "-")
        short_reason = raw_reason[:28] + "..." if len(raw_reason) > 28 else raw_reason
        unrealized_pnl_pct = strat.get("unrealized_pnl_pct", 0.0)

        m_strat = get_market_strategy(market)

        if market == "KRW-BTC":
            mode_desc = "🟡 무손절 계층형 적립 (08:55 봇 스마트 가중 + 15:05 업비트 1만)"
            regime_badge = f'<span style="background:#ffb30022; color:#ffb300; border:1px solid #ffb300; padding:2px 10px; border-radius:12px; font-size:0.8rem; font-weight:700;">{regime_info.get("regime_korean", "200일선 분석 중")}</span>'
            sub_info_html = f"""
                    <div>
                        <span style="color:#8c96a5;">📱 업비트 정기 모으기:</span>
                        <b style="color:#00c087; margin-left:4px;">매일 15:05 (10,000원 무조건 적립)</b>
                    </div>
                    <div>
                        <span style="color:#8c96a5;">🤖 봇 스마트 종가 적립:</span>
                        <b style="color:#ffb300; margin-left:4px;">매일 08:55 ({regime_info.get('signal', '0~2만 원')} 가중 추가)</b>
                    </div>
                    <div>
                        <span style="color:#8c96a5;">🛡️ 리스크 관리:</span>
                        <b style="color:#81d4fa; margin-left:4px;">무손절 영구 수량 축적 (매도/청산 원천 배제)</b>
                    </div>
            """
        elif m_strat == "PURE_MARTINGALE":
            if is_adaptive_grid_enabled(market):
                mode_desc = "⚡ 적응형 그리드 C-E (Adaptive 1-1-2-4 + Dynamic Exit)"
                martingale_desc = "NATR × 1.25^Step 적응형 간격 + 1-1-2-4 순환"
                regime_handover_desc = "국면별 동적 익절 (BULL +1.2%/+4% | BEAR +0.4%/+2.5%)"
                regime_badge = '<span style="background:#00e67622; color:#00e676; border:1px solid #00e676; padding:2px 10px; border-radius:12px; font-size:0.8rem; font-weight:700;">Adaptive C-E (실전)</span>'
                regime_color = "#00e676"
            else:
                mode_desc = "🟣 순수 마틴게일 (24시간 무손절 1-1-2-4 순환 스쿼드)"
                martingale_desc = "1-1-2-4 배수 무제한 순환 (손절 없음 / 상시 순환)"
                regime_handover_desc = "국면 전환 및 손절 없음 (무손절 매직스플릿 상시 방어)"
                regime_badge = '<span style="background:#ab47bc22; color:#ab47bc; border:1px solid #ab47bc; padding:2px 10px; border-radius:12px; font-size:0.8rem; font-weight:700;">순수 마틴게일</span>'
                regime_color = "#ab47bc"
            sub_info_html = f"""
                    <div>
                        <span style="color:#8c96a5;">🎯 바스켓 익절 목표가:</span>
                        <b style="color:#00c087; margin-left:4px;">{basket_target_p:,.0f}원</b>
                        <span style="color:#8c96a5; font-size:0.74rem;">(평단가 대비 +{(strat_profit_margin-1)*100:.2f}%)</span>
                    </div>
                    <div>
                        <span style="color:#8c96a5;">💧 마틴게일 & 매직스플릿:</span>
                        <b style="color:#ffb300; margin-left:4px;">{martingale_desc}</b>
                        <span style="color:#8c96a5; font-size:0.74rem;">(개별 +{active_tranche_margin_pct:.1f}% OR 바스켓 익절)</span>
                    </div>
                    <div>
                        <span style="color:#8c96a5;">🛡️ 국면 전환 규칙:</span>
                        <b style="color:{regime_color}; margin-left:4px;">{regime_handover_desc}</b>
                    </div>
            """
        elif is_bull:
            mode_desc = "🚀 5/20 MA 추세추종 (12h 정기적립 + 동적 트레일링 익절)"
            r_state = strat.get("runtime_state", {})
            tranches = r_state.get("tranches", [])
            from config import BULL_TIME_DCA_INTERVAL_HOURS, MAX_BULL_DCA_STEPS, get_bull_trailing_stop_trigger, BULL_STOP_LOSS_PCT
            curr_steps = len(tranches) if tranches else (1 if strat.get("bot_quantity", 0) > 0 else 0)
            sl_price = target_base_p * (1.0 + BULL_STOP_LOSS_PCT) if target_base_p > 0 else 0.0
            ts_active = r_state.get("trailing_stop_active", False)
            active_ts_trigger = get_bull_trailing_stop_trigger(curr_steps)
            ts_status = "🔥 고점 추적 가동 중" if ts_active else f"대기 (+{active_ts_trigger*100:.0f}% 도달 시)"

            sub_info_html = f"""
                    <div>
                        <span style="color:#8c96a5;">📈 20U 동적 스쿼드:</span>
                        <b style="color:#ffb300; margin-left:4px;">{curr_steps} / {MAX_BULL_DCA_STEPS}회차 ({curr_steps * 10000:,.0f}원)</b>
                        <span style="color:#8c96a5; font-size:0.74rem;">(동적익절 +{active_ts_trigger*100:.0f}% / 트레일링: {ts_status})</span>
                    </div>
                    <div>
                        <span style="color:#8c96a5;">🛡️ 긴급 재난 손절 (최후 방어선):</span>
                        <b style="color:#ff5252; margin-left:4px;">손절 기준가: {sl_price:,.0f}원 ({BULL_STOP_LOSS_PCT*100:.1f}%)</b>
                    </div>
            """
        else:
            if market == "KRW-SOL":
                mode_desc = "🛡️ 하락장 마틴-매직스플릿 (70% 손절 인계 / 4개 스쿼드(16차 32U) 캡 홀딩)"
                martingale_desc = "1-1-2-4 배수 x 4 (최대 16차수 / 32U 홀딩 캡)"
                regime_handover_desc = "200 MA 하향 돌파 시 70% 손절 후 30% 방어 인계"
            else:
                mode_desc = "🛡️ 하락장 마틴-매직스플릿 (50% 손절 인계 / 1-1-2-4 무제한 순환 스쿼드)"
                martingale_desc = "1-1-2-4 배수 무제한 순환 (예수금 한도 내 무한 확장)"
                regime_handover_desc = "200 MA 하향 돌파 시 50% 손절 후 50% 방어 인계"

            rule_color = "#ff5252"
            sub_info_html = f"""
                    <div>
                        <span style="color:#8c96a5;">🎯 바스켓 익절 목표가:</span>
                        <b style="color:#00c087; margin-left:4px;">{basket_target_p:,.0f}원</b>
                        <span style="color:#8c96a5; font-size:0.74rem;">(평단가 대비 +{(strat['profit_margin']-1)*100:.2f}%)</span>
                    </div>
                    <div>
                        <span style="color:#8c96a5;">💧 마틴게일 & 매직스플릿:</span>
                        <b style="color:#ffb300; margin-left:4px;">{martingale_desc}</b>
                        <span style="color:#8c96a5; font-size:0.74rem;">(개별 +3% OR 바스켓 익절)</span>
                    </div>
                    <div>
                        <span style="color:#8c96a5;">🛡️ 국면 전환 규칙:</span>
                        <b style="color:{rule_color}; margin-left:4px;">{regime_handover_desc}</b>
                    </div>
            """

        # 4-Grid 상태 표시 (BTC 전용 및 알트코인 전용)
        if market == "KRW-BTC":
            grid_html = f"""
                <div style="display:grid; grid-template-columns: repeat(4, 1fr); gap:12px; background:#12161f; padding:10px 14px; border-radius:8px; border:1px solid #232b3a;">
                    <div>
                        <div style="font-size:0.75rem; color:#8c96a5; margin-bottom:2px;">현재 시세</div>
                        <div style="font-size:1.05rem; font-weight:700; color:#f0f3f6;">{cur_p:,.0f}원</div>
                    </div>
                    <div>
                        <div style="font-size:0.75rem; color:#8c96a5; margin-bottom:2px;">200일 이동평균선 (시장 기준)</div>
                        <div style="font-size:1.05rem; font-weight:700; color:#81d4fa;">
                            {regime_info.get('ma200', 0):,.0f}원 <span style="font-size:0.78rem; color:{dist_color}; font-weight:600;">({dist_pct:+.2f}%)</span>
                        </div>
                    </div>
                    <div>
                        <div style="font-size:0.75rem; color:#8c96a5; margin-bottom:2px;">내 계좌 매수 평단가</div>
                        <div style="font-size:1.05rem; font-weight:700; color:#f48fb1;">
                            {avg_p:,.0f}원 <span style="font-size:0.78rem; color:#ff3b69; font-weight:600;">({regime_info.get('dist_avg_pct', 0):+.2f}%)</span>
                        </div>
                    </div>
                    <div>
                        <div style="font-size:0.75rem; color:#8c96a5; margin-bottom:2px;">오늘 종가 적립 판정</div>
                        <div style="font-size:0.92rem; font-weight:700; color:#ffd54f;">
                            {sig_text} <span style="font-size:0.75rem; color:#8c96a5; font-weight:normal;">({short_reason})</span>
                        </div>
                    </div>
                </div>
            """
        else:
            grid_html = f"""
                <div style="display:grid; grid-template-columns: repeat(4, 1fr); gap:12px; background:#12161f; padding:10px 14px; border-radius:8px; border:1px solid #232b3a;">
                    <div>
                        <div style="font-size:0.75rem; color:#8c96a5; margin-bottom:2px;">현재가</div>
                        <div style="font-size:1.05rem; font-weight:700; color:#f0f3f6;">{cur_p:,.0f}원</div>
                    </div>
                    <div>
                        <div style="font-size:0.75rem; color:#8c96a5; margin-bottom:2px;">200시간선 (국면 기준)</div>
                        <div style="font-size:1.05rem; font-weight:700; color:#81d4fa;">
                            {regime_info.get('ma200', 0):,.0f}원 <span style="font-size:0.78rem; color:{dist_color}; font-weight:600;">({dist_pct:+.2f}%)</span>
                        </div>
                    </div>
                    <div>
                        <div style="font-size:0.75rem; color:#8c96a5; margin-bottom:2px;">5시간선 / 20시간선</div>
                        <div style="font-size:1.05rem; font-weight:700; color:#f48fb1;">
                            {regime_info.get('ma5', 0):,.0f}원 / {regime_info.get('ma20', 0):,.0f}원
                        </div>
                    </div>
                    <div>
                        <div style="font-size:0.75rem; color:#8c96a5; margin-bottom:2px;">실시간 신호 판정</div>
                        <div style="font-size:0.92rem; font-weight:700; color:#ffd54f;">
                            {sig_text} <span style="font-size:0.75rem; color:#8c96a5; font-weight:normal;">({short_reason})</span>
                        </div>
                    </div>
                </div>
            """

        if market == "KRW-BTC":
            cap_info_html = f"계좌 매수원가: <b style=\"color:#f0f3f6;\">{strat['cost_amount']:,.0f}원</b> | 봇 추가적립 한도: <b style=\"color:#f0f3f6;\">{initial_cap:,.0f}원</b> (1 Unit: {unit_krw:,.0f}원)"
        else:
            cap_info_html = f"전략 배정 원금: <b style=\"color:#f0f3f6;\">{initial_cap:,.0f}원</b> | 1 Unit: <b style=\"color:#f0f3f6;\">{unit_krw:,.0f}원</b>"

        # 전략 개별 카드 (st.html을 사용하여 코드 노출 원천 차단)
        st.html(f"""
        <div class="strategy-card">
            <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:14px; border-bottom:1px solid #2d3648; padding-bottom:10px;">
                <div style="display:flex; align-items:center; gap:10px; flex-wrap:wrap;">
                    <span style="font-size:1.4rem;">⚡</span>
                    <span style="font-size:1.2rem; font-weight:800; color:#f0f3f6;">
                        전략 {idx}. {strat_name}
                    </span>
                    <span style="{badge_style} padding:2px 10px; border-radius:12px; font-size:0.82rem; font-weight:700;">
                        {market}
                    </span>
                </div>
                <div style="font-size:0.85rem; color:#8c96a5;">
                    {cap_info_html}
                </div>
            </div>

            <div style="background:linear-gradient(135deg, #181d27 0%, #1f2634 100%); border:1px solid #333d4e; border-radius:10px; padding:12px 16px; margin-bottom:14px;">
                <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:10px; flex-wrap:wrap; gap:8px;">
                    <div style="display:flex; align-items:center; gap:10px;">
                        <span style="font-size:1.25rem;">🧭</span>
                        <span style="font-size:0.95rem; font-weight:700; color:#f0f3f6;">실시간 시장 국면 & 매매 엔진 상태</span>
                        {regime_badge}
                    </div>
                    <div>
                        <span style="background:{regime_color}18; color:{regime_color}; border:1px solid {regime_color}44; padding:3px 10px; border-radius:8px; font-size:0.84rem; font-weight:700;">
                            {mode_desc}
                        </span>
                    </div>
                </div>
                {grid_html}

                <div style="display:flex; justify-content:space-between; align-items:center; margin-top:8px; padding-top:8px; border-top:1px dashed #2a3344; font-size:0.8rem;">
                    {sub_info_html}
                </div>
            </div>
        </div>
        """)

        # 전략 핵심 성과 지표 4분할
        sc1, sc2, sc3, sc4 = st.columns(4)
        if market == "KRW-BTC":
            with sc1:
                st.html(f"""
                <div class="metric-card">
                    <div class="metric-title">계좌 총 평가손익 (수익률)</div>
                    <div class="metric-val {ret_color}">{strat_ret:+.2f}%</div>
                    <div class="metric-sub">평가손익: <b class="{pnl_color}">{strat_pnl:+,.0f}원</b></div>
                </div>
                """)
            with sc2:
                st.html(f"""
                <div class="metric-card">
                    <div class="metric-title">총 보유 비트코인</div>
                    <div class="metric-val text-yellow">{coin_bal:.6f} <span style="font-size:1rem; color:#8c96a5;">BTC</span></div>
                    <div class="metric-sub">평가금액: <b>{eval_amt:,.0f}원</b></div>
                </div>
                """)
            with sc3:
                r_state = strat.get("runtime_state", {})
                dca_avg = r_state.get("dca_avg_price", 0.0)
                dca_sub_text = f" | 적립평단: <b style='color:#00c087;'>{dca_avg:,.0f}원</b>" if dca_avg > 0 else ""
                st.html(f"""
                <div class="metric-card">
                    <div class="metric-title">내 계좌 매수 평단가</div>
                    <div class="metric-val text-blue">{avg_p:,.0f} <span style="font-size:1rem; color:#8c96a5;">원</span></div>
                    <div class="metric-sub">매수원가: <b>{strat['cost_amount']:,.0f}원</b>{dca_sub_text}</div>
                </div>
                """)
            with sc4:
                st.html(f"""
                <div class="metric-card">
                    <div class="metric-title">200일선 (시장 기준선)</div>
                    <div class="metric-val text-green">{regime_info.get('ma200', 0):,.0f} <span style="font-size:1rem; color:#8c96a5;">원</span></div>
                    <div class="metric-sub">200일선 이격도: <b style="color:{dist_color};">{dist_pct:+.2f}%</b></div>
                </div>
                """)
        else:
            with sc1:
                st.html(f"""
                <div class="metric-card">
                    <div class="metric-title">전략 총 수익률</div>
                    <div class="metric-val {ret_color}">{strat_ret:+.2f}%</div>
                    <div class="metric-sub">장부손익: <b class="{pnl_color}">{strat_pnl:+,.0f}원</b></div>
                </div>
                """)

            with sc2:
                real_color = "text-green" if realized_pnl >= 0 else "text-red"
                st.html(f"""
                <div class="metric-card">
                    <div class="metric-title">실현 누적 손익 (장부)</div>
                    <div class="metric-val {real_color}">{realized_pnl:+,.0f} <span style="font-size:1rem; color:#8c96a5;">원</span></div>
                    <div class="metric-sub">완료 사이클: {strat['completed_cycles']}회</div>
                </div>
                """)

            with sc3:
                st.html(f"""
                <div class="metric-card">
                    <div class="metric-title">승률 (Win Rate)</div>
                    <div class="metric-val text-blue">{strat['win_rate']:.1f}%</div>
                    <div class="metric-sub">{strat['wins']}승 {strat['losses']}패 (총 청산 {strat['wins'] + strat['losses']}건)</div>
                </div>
                """)

            with sc4:
                pf = strat["profit_factor"]
                pf_color = "text-green" if pf >= 1.0 else "text-red"
                st.html(f"""
                <div class="metric-card">
                    <div class="metric-title">손익비 (Profit Factor)</div>
                    <div class="metric-val {pf_color}">{pf:.2f}</div>
                    <div class="metric-sub">평균익 {strat['avg_win']:,.0f}원 / 평균손 {strat['avg_loss']:,.0f}원</div>
                </div>
                """)

        st.markdown("<div style='height:10px;'></div>", unsafe_allow_html=True)

        if market == "KRW-BTC":
            # 비트코인 스마트 적립: 포지션현황 및 차트 제거, 체결내역만 표시
            # 사용자 요청: "비트의 포지션현황, 차트는 제거. 체결내역만 보여주면 될꺼 같음"
            st.markdown(f"##### 📜 {ticker} 스마트 적립 체결 내역 (총 {len(trades_df)}건)")
            if not trades_df.empty:
                trades_df_sorted = trades_df.sort_values("timestamp", ascending=False)
                latest_fill = trades_df_sorted.iloc[0]
                latest_side = "매도" if latest_fill.get("side") == "ask" else "매수"
                st.caption(
                    f"최근 체결: {latest_fill['timestamp']} | {latest_side} "
                    f"{float(latest_fill.get('price') or 0):,.0f}원 | "
                    f"{float(latest_fill.get('volume') or 0):.8f} {ticker} | "
                    f"{float(latest_fill.get('cost_or_revenue') or 0):,.0f}원"
                )
                action_map = {
                    "BTC_TIERED_BUY_T1": "봇 1단계 스마트 종가적립 (08:50)",
                    "BTC_TIERED_BUY_T2": "봇 2단계 특가 추가적립 (08:50)",
                    "BTC_UPBIT_DCA": "업비트 정기 모으기 (15:05)",
                }
                btc_table_items = []
                for _, t in trades_df_sorted.head(25).iterrows():
                    raw_act = str(t.get("action", ""))
                    act_kr = action_map.get(raw_act, raw_act or "적립 매수")
                    t_time = str(t.get("timestamp", "-"))[:19]
                    p_val = float(t.get("price") or 0.0)
                    v_val = float(t.get("volume") or 0.0)
                    c_val = float(t.get("cost_or_revenue") or 0.0)

                    btc_table_items.append({
                        "상태": "✅ 적립완료",
                        "체결시각": t_time,
                        "구분": f"🔵 매수 ({act_kr})",
                        "체결단가": f"{p_val:,.0f}원",
                        "적립수량": f"{v_val:.8f} BTC",
                        "체결금액": f"{c_val:,.0f}원"
                    })
                if btc_table_items:
                    st.dataframe(
                        pd.DataFrame(btc_table_items),
                        hide_index=True,
                        use_container_width=True,
                        height=280,
                        key="btc_dca_trades_table"
                    )
            else:
                st.info("비트코인 체결 내역이 없습니다.")

            st.markdown("<div style='height:14px;'></div>", unsafe_allow_html=True)
            continue

        # 포지션 및 미체결 주문 2분할 (스쿼드 정보 가독성을 위해 1.35:1.65 비율 적용)
        p_col1, p_col2 = st.columns([1.35, 1.65])

        with p_col1:
            st.markdown(f"##### 💼 {ticker} 포지션 현황 (계좌 잔고 & 모으기 현황)")
            r_state = strat.get("runtime_state", {})
            bot_q = strat.get("bot_quantity", 0.0)
            bot_avg = strat.get("bot_avg_price", 0.0)
            bot_eval = strat.get("bot_eval", 0.0)
            bot_pnl = strat.get("bot_unrealized_pnl", 0.0)
            bot_pnl_pct = strat.get("bot_pnl_pct", 0.0)
            prot_q = strat.get("protected_quantity", 0.0)
            tot_coin = strat.get("account_total_coin_balance", coin_bal)
            tranches = r_state.get("tranches", [])


            from config import is_adaptive_grid_enabled, get_magic_split_tranche_profit
            if is_adaptive_grid_enabled(market):
                from strategy.adaptive_grid import get_dynamic_tp_margins
                _, dyn_t_margin = get_dynamic_tp_margins(is_bull=is_bull, market=market)
                squad_tp_margin_pct = (dyn_t_margin - 1.0) * 100.0
            else:
                squad_tp_margin_pct = get_magic_split_tranche_profit(market) * 100.0

            # 스쿼드별 보유 차수 집계
            squad_map = {}
            for t in tranches:
                s_id = t.get("squad_id", 0)
                squad_map.setdefault(s_id, []).append(t)

            squad_rows = []
            for s_id, s_tranches in sorted(squad_map.items(), reverse=True):
                s_vol = sum(float(t.get("volume", 0.0)) for t in s_tranches)
                s_cost = sum(float(t.get("volume", 0.0)) * float(t.get("buy_price", 0.0)) for t in s_tranches)
                s_vwap = s_cost / s_vol if s_vol > 0 else 0.0
                s_units = sum(t.get("units", 1) for t in s_tranches)
                s_name = f"스쿼드 {s_id}" if s_id > 0 else "국면 인계"
                s_pnl_pct = ((cur_p - s_vwap) / s_vwap * 100.0) if s_vwap > 0 else 0.0
                s_target_p = s_vwap * (1.0 + squad_tp_margin_pct / 100.0)
                dist_to_tp = ((s_target_p - cur_p) / cur_p * 100.0) if cur_p > 0 else 0.0

                if cur_p >= s_target_p:
                    status_badge = "🎯 익절 도달"
                elif dist_to_tp <= 0.6:
                    status_badge = f"🔥 익절 근접 (+{dist_to_tp:.2f}%)"
                elif s_pnl_pct >= 0:
                    status_badge = f"🟢 수익 중 ({s_pnl_pct:+.2f}%)"
                else:
                    status_badge = f"💧 대기 ({s_pnl_pct:+.2f}%)"

                squad_rows.append({
                    "스쿼드": s_name,
                    "체결": f"{len(s_tranches)}차 ({s_units}U)",
                    "스쿼드평단": f"{s_vwap:,.0f}원",
                    "현재손익": f"{s_pnl_pct:+.2f}%",
                    "단독익절가": f"{s_target_p:,.0f}원 (+{squad_tp_margin_pct:.1f}%)",
                    "수량": f"{s_vol:.6f} {ticker}",
                    "상태": status_badge
                })

            # 호가창 미체결 중 신규 대기 스쿼드 확인 (예: 스쿼드 7)
            active_buys = {}
            for k in ["active_limit_orders", "bear_limit_buys"]:
                b_dict = r_state.get(k, {})
                if isinstance(b_dict, dict):
                    active_buys.update(b_dict)
            pending_sq_ids = sorted(list(set(
                b_meta.get("squad_id") for b_meta in active_buys.values()
                if b_meta.get("state") == "wait" and b_meta.get("squad_id", 0) > 0 and b_meta.get("squad_id") not in squad_map
            )))

            pnl_color_hex = "#00e676" if bot_pnl >= 0 else "#ff5252"
            active_squad_cnt = len([s for s in squad_map.keys() if s > 0])
            squad_title_cnt = f"{active_squad_cnt}개 운용 중" if active_squad_cnt > 0 else "미보유 (대기)"

            st.html(f"""
            <div style="background:#12161f; padding:8px 14px; border-radius:8px; border:1px solid #232b3a; margin-bottom:8px; display:flex; justify-content:space-between; align-items:center;">
                <div>
                    <span style="font-size:0.75rem; color:#8c96a5;">🤖 봇 운용 포지션</span><br>
                    <b style="color:#f0f3f6; font-size:0.95rem;">{bot_q:.6f} {ticker}</b>
                    <span style="font-size:0.78rem; color:#8c96a5;">(@ {bot_avg:,.0f}원)</span>
                </div>
                <div style="text-align:right;">
                    <span style="font-size:0.75rem; color:#8c96a5;">🎯 바스켓 목표 / 손익</span><br>
                    <b style="color:{pnl_color_hex}; font-size:0.95rem;">{bot_pnl:+,.0f}원 ({bot_pnl_pct:+.2f}%)</b>
                    <span style="font-size:0.8rem; color:#00e676; font-weight:700;">(🎯 {basket_target_p:,.0f}원)</span>
                </div>
            </div>
            """)

            tab_squad, tab_account = st.tabs([
                f"🪖 스쿼드별 상세 ({squad_title_cnt})",
                f"🏛️ 계좌 총잔고 & 보호자산"
            ])

            with tab_squad:
                if squad_rows:
                    st.dataframe(pd.DataFrame(squad_rows), hide_index=True, use_container_width=True, height=210,
                                 key=f"{market}_squad_status_table")
                else:
                    st.info(f"현재 투입된 스쿼드가 없습니다. (다음 신호 대기 중)")

                pending_note = f" | ⏳ **신규 대기**: 스쿼드 {', '.join(str(s) for s in pending_sq_ids)} (호가창 대기)" if pending_sq_ids else ""
                st.caption(f"💡 **이중 익절 규칙**: 스쿼드별 평단 대비 +{squad_tp_margin_pct:.1f}% 도달 시 해당 스쿼드만 단독 익절{pending_note}")

            with tab_account:
                pos_data = [
                    {"항목": "현재 시세", "값": f"{cur_p:,.0f} 원"},
                    {"항목": "🤖 봇 운용 수량", "값": f"{bot_q:.6f} {ticker}"},
                    {"항목": "🤖 봇 매수평단", "값": f"{bot_avg:,.0f} 원" if bot_avg > 0 else "미보유 (대기)"},
                    {"항목": "🤖 봇 평가금액", "값": f"{bot_eval:,.0f} 원"},
                    {"항목": "🤖 봇 미실현손익", "값": f"{bot_pnl:+,.0f} 원 ({bot_pnl_pct:+.2f}%)" if bot_avg > 0 else "-"},
                    {"항목": "🔒 기존 자산 (보호중)", "값": f"{prot_q:.6f} {ticker}"},
                    {"항목": "🏛️ 계좌 전체 총수량", "값": f"{tot_coin:.6f} {ticker}"}
                ]
                st.dataframe(pd.DataFrame(pos_data), hide_index=True, use_container_width=True, height=210,
                             key=f"{market}_account_balance_table")

        with p_col2:
            st.markdown(f"##### 📋 {ticker} 실시간 주문 및 체결 현황")
            pending_items = []

            # 1. 미체결 대기 주문 (호가창 등록 중인 주문)
            r_state = strat.get("runtime_state", {})
            basket_info = r_state.get("basket_limit_ask", {})
            basket_uuid = basket_info.get("uuid") if isinstance(basket_info, dict) else None

            active_buys = {}
            for k in ["active_limit_orders", "bear_limit_buys"]:
                b_dict = r_state.get(k, {})
                if isinstance(b_dict, dict):
                    active_buys.update(b_dict)

            if open_orders:
                for o in open_orders:
                    o_uuid = o.get("uuid")
                    side = o.get("side")
                    diff = ((o["price"] - cur_p) / cur_p * 100.0) if cur_p > 0 else 0.0
                    reg_time = str(o.get("created_at", "-"))[:19].replace("T", " ")

                    if side == "ask":
                        if o_uuid and o_uuid == basket_uuid:
                            side_kr = "🎯 [바스켓 익절매도]"
                            detail_str = f"바스켓 목표가 ({o['price']:,.0f}원) 전량 익절 대기"
                        else:
                            side_kr = "🎯 [익절매도]"
                            detail_str = "호가창 등록 대기 중 (미체결)"
                    else:
                        if o_uuid and o_uuid in active_buys:
                            b_meta = active_buys[o_uuid]
                            sq_id = b_meta.get("squad_id", "?")
                            st_id = b_meta.get("squad_stage", b_meta.get("stage", "?"))
                            u_cnt = b_meta.get("units", "?")
                            side_kr = f"💧 [마틴게일 SQ{sq_id} {st_id}차]"
                            detail_str = f"스쿼드 {sq_id} {st_id}단계 물타기 ({u_cnt} Unit)"
                        else:
                            side_kr = "💧 [물타기매수]"
                            detail_str = "호가창 등록 대기 중 (미체결)"

                    pending_items.append({
                        "상태": "⏳ 대기중",
                        "구분": side_kr,
                        "주문가격": f"{o['price']:,.0f}원 ({diff:+.2f}%)",
                        "수량": f"{o['volume']:.6f} {ticker}",
                        "주문금액": f"{o['price'] * o['volume']:,.0f}원",
                        "상세 / 목표": detail_str,
                        "등록시각": reg_time,
                        "_price_raw": o["price"],
                        "_side_raw": side
                    })

                # 정렬: 매도 주문 상단, 매수 주문 가격 내림차순(호가창 스타일)
                pending_items.sort(key=lambda x: (0 if x["_side_raw"] == "ask" else 1, -x["_price_raw"]))
                for item in pending_items:
                    item.pop("_price_raw", None)
                    item.pop("_side_raw", None)

            # 사후 복기 교훈 조회
            ref_items = []
            try:
                from strategy.settlement_reflection import get_market_reflections
                ref_items = get_market_reflections(market, limit=10)
            except Exception:
                pass

            trade_count = len(trades_df) if not trades_df.empty else 0
            tab_pending, tab_trades, tab_reflection = st.tabs([
                f"⏳ 진행 중인 주문 ({len(pending_items)}건)",
                f"📜 최근 체결 내역 ({min(trade_count, 15)}건)",
                f"🎓 사후 복기·교훈 ({len(ref_items)}건)"
            ])

            with tab_pending:
                if pending_items:
                    st.dataframe(pd.DataFrame(pending_items),
                                 hide_index=True, use_container_width=True, height=270,
                                 key=f"{market}_pending_orders_table")
                else:
                    st.info(f"현재 {market}에 호가창 등록 대기 중인 주문이 없습니다.")

            with tab_trades:
                timeline_items = []
                # 2. 최근 체결 완료 내역 (실거래 trades 테이블)
                if not trades_df.empty:
                    trades_df = trades_df.sort_values("timestamp", ascending=False)
                    latest_fill = trades_df.iloc[0]
                    latest_side = "매도" if latest_fill.get("side") == "ask" else "매수"
                    st.caption(
                        f"최근 체결: {latest_fill['timestamp']} | {latest_side} "
                        f"{float(latest_fill.get('price') or 0):,.0f}원 | "
                        f"{float(latest_fill.get('volume') or 0):.8f} {ticker} | "
                        f"{float(latest_fill.get('cost_or_revenue') or 0):,.0f}원"
                    )
                    action_map = {
                        "BTC_TIERED_BUY_T1": "비트코인 1단계 일반세일 매수 (+1만)",
                        "BTC_TIERED_BUY_T2": "비트코인 2단계 역대급세일 매수 (+2만)",
                        "INITIAL_10K_ENTRY": "1회차 신규 진입",
                        "TREND_GOLDEN_CROSS_BUY": "5/20 골든크로스 매수",
                        "BULL_REGIME_ALIGNED_ENTRY": "상승장 정배열 1차 진입",
                        "MARTINGALE_BUY_INITIAL": "마틴게일 1차 매수",
                        "TRAILING_STOP_EXIT": "트레일링스탑 익절",
                        "TREND_DEAD_CROSS_SELL": "5/20 데드크로스 청산",
                        "TREND_MA20_BREAK_SELL": "20선 지지 이탈 청산",
                        "BULL_STOP_LOSS_3PCT": "상승장 -3% 손절",
                        "BULL_STOP_LOSS_10PCT": "상승장 -10% 긴급손절",
                        "REGIME_SWITCH_PARTIAL_CUT_50PCT": "국면전환 50% 부분손절",
                        "REGIME_SWITCH_PARTIAL_CUT_70PCT": "국면전환 70% 부분손절",
                        "BASKET_TAKE_PROFIT": "바스켓 전량 익절",
                        "STOP_LOSS": "STOP-LOSS 손절"
                    }
                    for _, t in trades_df.head(15).iterrows():
                        raw_action = str(t.get("action", ""))
                        if raw_action.startswith("REGIME_SWITCH_PARTIAL_CUT_"):
                            pct_s = raw_action.replace("REGIME_SWITCH_PARTIAL_CUT_", "").replace("PCT", "")
                            action_kr = f"국면전환 {pct_s}% 부분손절"
                        elif raw_action.startswith("TRANCHE_TAKE_PROFIT"):
                            action_kr = "매직스플릿 차수 익절"
                        elif raw_action.startswith("MARTINGALE_LIMIT_BUY_SQUAD_"):
                            parts = raw_action.replace("MARTINGALE_LIMIT_BUY_SQUAD_", "").split("_STAGE_")
                            if len(parts) == 2:
                                action_kr = f"마틴게일 SQ{parts[0]} ST{parts[1]} 체결"
                            else:
                                action_kr = "마틴게일 지정가 체결"
                        elif raw_action.startswith("MARTINGALE_LIMIT_BUY_") or raw_action.startswith("MARTINGALE_BUY_"):
                            action_kr = "마틴게일 물타기 체결"
                        elif raw_action.startswith("BULL_TIME_DCA_STEP_"):
                            step_num = raw_action.replace("BULL_TIME_DCA_STEP_", "")
                            action_kr = f"12시간 정기적립 ({step_num}회차)"
                        elif raw_action.startswith("BULL_PYRAMID_STEP_"):
                            action_kr = f"상승 불타기 ({raw_action.replace('BULL_PYRAMID_STEP_', '')}회차)"
                        elif raw_action.startswith("BULL_CLOSING_BUY_STEP_"):
                            action_kr = f"일봉 종가매수 ({raw_action.replace('BULL_CLOSING_BUY_STEP_', '')}회차)"
                        else:
                            action_kr = action_map.get(raw_action, raw_action or "실전 체결")

                        side_icon = "🔴 매도" if t.get("side") == "ask" else "🔵 매수"
                        pnl_val = float(t.get("pnl") or 0.0)
                        detail_str = f"손익: {pnl_val:+,.0f}원" if pnl_val != 0 else f"{action_kr} 완료"

                        t_time = str(t.get("timestamp", "-"))[:19]
                        p_val = float(t.get("price") or 0.0)
                        v_val = float(t.get("volume") or 0.0)
                        c_val = float(t.get("cost_or_revenue") or 0.0)

                        timeline_items.append({
                            "상태": "✅ 체결완료",
                            "시각": t_time,
                            "구분": f"{side_icon} ({action_kr})",
                            "주문가격": f"{p_val:,.0f}원",
                            "수량": f"{v_val:.6f} {ticker}",
                            "주문금액": f"{c_val:,.0f}원",
                            "상세 / 손익": detail_str
                        })

                if timeline_items:
                    st.dataframe(pd.DataFrame(sort_timeline_items(timeline_items)),
                                 hide_index=True, use_container_width=True, height=270,
                                 key=f"{market}_order_trade_timeline")
                else:
                    st.info(f"현재 {market}에 등록된 체결 내역이 없습니다.")

            with tab_reflection:
                if ref_items:
                    for ref in ref_items:
                        g = ref.get("grade", "NEUTRAL")
                        badge = "🏆 EXCELLENT" if g == "EXCELLENT" else "🟢 GOOD" if g == "GOOD" else "🟡 NEUTRAL" if g == "NEUTRAL" else "⚠️ 거래정체 진단 (STAGNATION)" if g == "STAGNATION_ALERT" else "🔴 UNDERPERFORM"
                        st.markdown(f"**[{badge}]** `{ref.get('created_at')}`")
                        st.info(ref.get("rule_or_lesson", "-"))
                else:
                    st.info(f"아직 {market}에 축적된 사후 복기 교훈이 없습니다.")

        profit_margin = get_profit_margin(market)
        target_p = 0.0
        b_avg = strat.get("bot_avg_price", 0.0)
        if b_avg > 0 and profit_margin > 1.0:
            target_p = b_avg * profit_margin
        elif market == "KRW-BTC" and avg_p > 0:
            target_p = avg_p * 1.10

        st.markdown("<div style='height:8px;'></div>", unsafe_allow_html=True)
        with st.expander(f"📈 {ticker} 인터랙티브 캔들스틱 차트 & 실전 매매 타점 오버레이 (1시간/4시간/일봉/주봉/월봉)", expanded=True):
            render_coin_candlestick_chart(
                market=market,
                ticker=ticker,
                trades_df=trades_df,
                bot_avg_price=b_avg if b_avg > 0 else (avg_p if market == "KRW-BTC" else 0.0),
                target_price=target_p,
                key_prefix=f"strat_{idx}"
            )

        # 해당 전략의 전체 체결 기록 더보기
        with st.expander(f"📜 전략 {idx}. {market} 전체 체결 원본 기록 (총 {len(trades_df)}건)"):
            if not trades_df.empty:
                show_trades = trades_df.copy()
                show_trades["체결단가"] = show_trades["price"].apply(lambda x: f"{x:,.0f}원")
                show_trades["체결수량"] = show_trades["volume"].apply(lambda x: f"{x:.6f}")
                show_trades["체결금액"] = show_trades["cost_or_revenue"].apply(lambda x: f"{x:,.0f}원")
                show_trades["실현손익"] = show_trades["pnl"].apply(lambda x: f"{x:+,.0f}원" if x != 0 else "-")
                show_trades["구분"] = show_trades["side"].apply(lambda x: "매도 (ASK)" if x == "ask" else "매수 (BID)")
                cols = ["timestamp", "action", "구분", "체결단가", "체결수량", "체결금액", "실현손익", "cycle"]
                st.dataframe(show_trades[cols], hide_index=True, use_container_width=True)
            else:
                st.info(f"아직 {market} 실거래 체결 내역이 없습니다.")

        st.markdown("<div style='height:18px;'></div>", unsafe_allow_html=True)

# =============================================================================
# 3. G60 서브포켓 스쿼드 그리드 실거래 현황 (솔 · 리플 2단 배치, 이더리움 제외)
# =============================================================================
def get_pocket_g60_summary(loaded_snapshot: Optional[Dict[str, Any]] = None):
    """G60 서브포켓 실거래 2종(SOL, XRP)의 상태 및 매매 내역을 조회합니다 (클라우드 스냅샷 및 로컬 파일 겸용)."""
    markets = [
        {"market": "KRW-SOL", "ticker": "SOL", "name": "솔라나", "icon": "🟣"},
        {"market": "KRW-XRP", "ticker": "XRP", "name": "리플", "icon": "🌊"}
    ]

    trades_by_market = {m["market"]: [] for m in markets}
    states_by_market = {m["market"]: {} for m in markets}

    # 1. 클라우드 스냅샷의 pocket_grid 데이터 로드 (Streamlit Cloud 환경 우선 대응)
    pocket_cloud = (loaded_snapshot or {}).get("pocket_grid", {})
    cloud_states = pocket_cloud.get("states", {})
    cloud_trades = pocket_cloud.get("trades_list", [])
    if cloud_states:
        for m_key, st_dict in cloud_states.items():
            if m_key in states_by_market:
                states_by_market[m_key] = st_dict
    if cloud_trades:
        for t in cloud_trades:
            m_key = t.get("market")
            if m_key in trades_by_market:
                trades_by_market[m_key].append(t)

    # 2. 로컬 파일 및 DB가 존재하면(로컬 구동 시) 최신 데이터로 덮어쓰기
    pocket_db = os.path.join(BASE_DIR, "pocket_trade_history.db")
    if os.path.exists(pocket_db):
        try:
            conn = sqlite3.connect(pocket_db, timeout=2.0)
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            c.execute("""
                SELECT id, timestamp, market, side, action, price, volume, cost_or_revenue, fee, pnl, level, strategy
                FROM pocket_trades
                ORDER BY id DESC
            """)
            local_trades = {m["market"]: [] for m in markets}
            for r in c.fetchall():
                m_key = r["market"]
                if m_key in local_trades:
                    local_trades[m_key].append(dict(r))
            conn.close()
            for m_key, tr_list in local_trades.items():
                if tr_list:
                    trades_by_market[m_key] = tr_list
        except Exception:
            pass

    for m in markets:
        m_key = m["market"]
        s_file = os.path.join(BASE_DIR, f"pocket_grid_state_{m_key.replace('-', '_')}.json")
        if os.path.exists(s_file):
            try:
                with open(s_file, "r", encoding="utf-8") as f:
                    states_by_market[m_key] = json.load(f)
            except Exception:
                pass

    results = []
    for m in markets:
        m_key = m["market"]
        df_trades = pd.DataFrame(trades_by_market[m_key])
        results.append({
            **m,
            "state": states_by_market[m_key],
            "trades_df": df_trades
        })

    return results


def format_g60_action(row) -> str:
    """G60 서브포켓 체결 액션을 사용자 친화적 명칭으로 변환합니다."""
    act = str(row.get("action", ""))
    side = str(row.get("side", ""))
    if "TP" in act:
        if "SQUAD" in act:
            return "스쿼드 익절"
        return "개별 익절"
    elif side == "bid" or "BUY" in act:
        return f"매수 (Lvl {row.get('level', 1)})"
    return act


def format_cascade_action(row) -> str:
    """Cascade 서브포켓 체결 액션을 한글 명칭으로 변환합니다."""
    act = str(row.get("action", ""))
    side = str(row.get("side", ""))
    cyc = row.get("cycle", 1)
    if "INITIAL" in act:
        return f"1단 신규 매수 (Cyc {cyc})"
    elif "LADDER" in act or "BUY" in act or side == "bid":
        if "LVL_" in act:
            lvl = act.split("LVL_")[-1]
            return f"래더 매수 (Lvl {lvl})"
        return f"래더 매수 (Cyc {cyc})"
    elif "RESET" in act or "SELL" in act or side == "ask":
        return f"🎯 바스켓 전량 익절 (Cyc {cyc})"
    return act


def build_subpockets_integrated_normalized_figure(
    g60_data: Optional[List[Dict[str, Any]]] = None,
    cascade_data: Optional[Dict[str, Any]] = None,
    interval: str = "minute60",
    count: int = 72,
    show_g60_trades: bool = True,
    show_cascade_trades: bool = True,
    show_zero_line: bool = True,
    line_width: float = 2.2,
    ohlcv_fetcher=None
) -> go.Figure:
    """
    서브포켓 전체(G60 2종 + Cascade 2종, 총 3개 코인: ETH, SOL, XRP)의
    정규화(%) 수익률 및 각 전략별 실전 매매 타점을 단일 평면에 오버레이하는 Plotly Figure를 생성합니다.
    """
    if ohlcv_fetcher is None:
        ohlcv_fetcher = fetch_ohlcv_cached

    markets_meta_map = {
        "KRW-ETH": {"market": "KRW-ETH", "ticker": "ETH", "name": "이더리움", "icon": "💎", "color": "#00e5ff"},
        "KRW-SOL": {"market": "KRW-SOL", "ticker": "SOL", "name": "솔라나", "icon": "🟣", "color": "#c77dff"},
        "KRW-XRP": {"market": "KRW-XRP", "ticker": "XRP", "name": "리플", "icon": "🌊", "color": "#ffb703"},
    }

    if cascade_data is not None:
        # 통합 모드: ETH, SOL, XRP 3종 대상
        target_markets = ["KRW-ETH", "KRW-SOL", "KRW-XRP"]
    else:
        # G60 전용 모드 (하위 호환 및 단위 테스트)
        target_markets = [item["market"] for item in (g60_data or [])]

    fig = go.Figure()
    candle_width = pd.DateOffset(hours=1 if "minute60" in interval else (4 if "minute240" in interval else 24))
    has_valid_data = False

    for m_key in target_markets:
        meta = markets_meta_map.get(m_key, {
            "market": m_key,
            "ticker": m_key.replace("KRW-", ""),
            "name": m_key,
            "icon": "🪙",
            "color": "#ffffff"
        })
        market = meta["market"]
        ticker = meta["ticker"]
        name = meta["name"]
        icon = meta["icon"]
        coin_color = meta["color"]

        df = ohlcv_fetcher(market, interval=interval, count=count)
        if df is None or df.empty or "close" not in df.columns:
            continue

        base_price = float(df["close"].iloc[0])
        if base_price <= 0:
            continue

        has_valid_data = True
        df = df.copy()
        df["return_pct"] = ((df["close"] / base_price) - 1.0) * 100.0
        curr_ret = float(df["return_pct"].iloc[-1])

        # (1) 정규화 가격 변동률 라인
        fig.add_trace(
            go.Scatter(
                x=df.index,
                y=df["return_pct"],
                mode="lines",
                name=f"{icon} {ticker} ({curr_ret:+.2f}%)",
                line=dict(color=coin_color, width=line_width),
                customdata=np.stack((df["close"], np.full(len(df), base_price)), axis=-1),
                hovertemplate=(
                    f"<b>{icon} {name} ({ticker})</b><br>"
                    f"• 시각: %{{x|%m-%d %H:%M}}<br>"
                    f"• 현재가: <b>%{{customdata[0]:,.0f}}원</b><br>"
                    f"• 시작 기준가: %{{customdata[1]:,.0f}}원<br>"
                    f"• 기간 수익률: <b>%{{y:+.2f}}%</b>"
                    f"<extra></extra>"
                )
            )
        )

        window_start = df.index.min()
        window_end = df.index.max() + candle_width

        # (2) G60 실전 매매 타점 오버레이 (▲ 매수 / ▼ 익절)
        if show_g60_trades and g60_data:
            g_item = next((item for item in g60_data if item.get("market") == market), None)
            if g_item:
                trades_df = g_item.get("trades_df")
                if trades_df is not None and not trades_df.empty and "timestamp" in trades_df.columns:
                    t_df = trades_df.copy()
                    t_df["dt"] = pd.to_datetime(t_df["timestamp"], errors="coerce")
                    t_df = t_df.dropna(subset=["dt"])
                    t_df = t_df[(t_df["dt"] >= window_start) & (t_df["dt"] <= window_end)]

                    if not t_df.empty:
                        buys = t_df[t_df["side"] == "bid"]
                        sells = t_df[t_df["side"] == "ask"]

                        # G60 매수 (▲ 에메랄드 그린 + 코인 컬러 테두리)
                        if not buys.empty:
                            buy_records = []
                            for (dt_val, price_val), grp in buys.groupby(["dt", "price"], sort=False):
                                tot_vol = float(grp["volume"].astype(float).sum())
                                tot_cost = float(grp["cost_or_revenue"].astype(float).sum()) if "cost_or_revenue" in grp.columns else (float(price_val) * tot_vol)
                                cnt = len(grp)
                                cnt_str = f" ({cnt}건 일괄)" if cnt > 1 else ""
                                act_str = format_g60_action(grp.iloc[0]) + cnt_str
                                ret_pos = ((float(price_val) / base_price) - 1.0) * 100.0
                                buy_records.append({
                                    "dt": dt_val,
                                    "ret_pos": ret_pos,
                                    "price": float(price_val),
                                    "volume": tot_vol,
                                    "cost": tot_cost,
                                    "action": act_str
                                })
                            b_plot_df = pd.DataFrame(buy_records)
                            fig.add_trace(
                                go.Scatter(
                                    x=b_plot_df["dt"],
                                    y=b_plot_df["ret_pos"],
                                    mode="markers",
                                    name=f"[G60] {ticker} 매수 ({len(buys)}건)",
                                    marker=dict(
                                        symbol="triangle-up",
                                        size=12,
                                        color="#00e676",
                                        line=dict(color=coin_color, width=2.0)
                                    ),
                                    customdata=np.stack((
                                        b_plot_df["price"],
                                        b_plot_df["volume"],
                                        b_plot_df["cost"],
                                        b_plot_df["action"]
                                    ), axis=-1),
                                    hovertemplate=(
                                        f"<b>🟢 [G60 {ticker} 매수 체결]</b><br>"
                                        f"• 시각: %{{x|%m-%d %H:%M:%S}}<br>"
                                        f"• 체결단가: <b>%{{customdata[0]:,.0f}}원</b><br>"
                                        f"• 변동률 위치: <b>%{{y:+.2f}}%</b><br>"
                                        f"• 체결수량: %{{customdata[1]:.6f}} {ticker}<br>"
                                        f"• 체결금액: %{{customdata[2]:,.0f}}원<br>"
                                        f"• 구분: <b>%{{customdata[3]}}</b>"
                                        f"<extra></extra>"
                                    )
                                )
                            )

                        # G60 익절 매도 (▼ 네온 코랄레드 + 코인 컬러 테두리)
                        if not sells.empty:
                            sell_records = []
                            for (dt_val, price_val), grp in sells.groupby(["dt", "price"], sort=False):
                                tot_vol = float(grp["volume"].astype(float).sum())
                                tot_pnl = float(grp["pnl"].astype(float).sum()) if "pnl" in grp.columns else 0.0
                                cnt = len(grp)
                                cnt_str = f" ({cnt}건 일괄)" if cnt > 1 else ""
                                act_str = format_g60_action(grp.iloc[0]) + cnt_str
                                ret_pos = ((float(price_val) / base_price) - 1.0) * 100.0
                                sell_records.append({
                                    "dt": dt_val,
                                    "ret_pos": ret_pos,
                                    "price": float(price_val),
                                    "volume": tot_vol,
                                    "pnl": tot_pnl,
                                    "action": act_str
                                })
                            s_plot_df = pd.DataFrame(sell_records)
                            fig.add_trace(
                                go.Scatter(
                                    x=s_plot_df["dt"],
                                    y=s_plot_df["ret_pos"],
                                    mode="markers",
                                    name=f"[G60] {ticker} 익절 ({len(sells)}건)",
                                    marker=dict(
                                        symbol="triangle-down",
                                        size=12,
                                        color="#ff1744",
                                        line=dict(color=coin_color, width=2.0)
                                    ),
                                    customdata=np.stack((
                                        s_plot_df["price"],
                                        s_plot_df["volume"],
                                        s_plot_df["pnl"],
                                        s_plot_df["action"]
                                    ), axis=-1),
                                    hovertemplate=(
                                        f"<b>🔴 [G60 {ticker} 익절 체결]</b><br>"
                                        f"• 시각: %{{x|%m-%d %H:%M:%S}}<br>"
                                        f"• 체결단가: <b>%{{customdata[0]:,.0f}}원</b><br>"
                                        f"• 변동률 위치: <b>%{{y:+.2f}}%</b><br>"
                                        f"• 체결수량: %{{customdata[1]:.6f}} {ticker}<br>"
                                        f"• 실현손익: <b>%{{customdata[2]:+,.1f}}원</b><br>"
                                        f"• 구분: <b>%{{customdata[3]}}</b>"
                                        f"<extra></extra>"
                                    )
                                )
                            )

        # (3) Cascade 실전 매매 타점 오버레이 (◆ 래더 매수 / ⭐ 바스켓 전량 익절)
        if show_cascade_trades and cascade_data:
            raw_c_trades = [t for t in cascade_data.get("trades_list", []) if t.get("market") == market]
            if raw_c_trades:
                c_df = pd.DataFrame(raw_c_trades)
                if not c_df.empty and "timestamp" in c_df.columns:
                    c_df["dt"] = pd.to_datetime(c_df["timestamp"], errors="coerce")
                    c_df = c_df.dropna(subset=["dt"])
                    c_df = c_df[(c_df["dt"] >= window_start) & (c_df["dt"] <= window_end)]

                    if not c_df.empty:
                        c_buys = c_df[c_df["side"] == "bid"]
                        c_sells = c_df[c_df["side"] == "ask"]

                        # Cascade 래더 매수 (◆ 청록 다이아몬드 + 코인 컬러 테두리)
                        if not c_buys.empty:
                            c_buy_records = []
                            for (dt_val, price_val), grp in c_buys.groupby(["dt", "price"], sort=False):
                                tot_vol = float(grp["volume"].astype(float).sum())
                                tot_cost = float(grp["cost_or_revenue"].astype(float).sum()) if "cost_or_revenue" in grp.columns else (float(price_val) * tot_vol)
                                cnt = len(grp)
                                cnt_str = f" ({cnt}건 일괄)" if cnt > 1 else ""
                                act_str = format_cascade_action(grp.iloc[0]) + cnt_str
                                ret_pos = ((float(price_val) / base_price) - 1.0) * 100.0
                                c_buy_records.append({
                                    "dt": dt_val,
                                    "ret_pos": ret_pos,
                                    "price": float(price_val),
                                    "volume": tot_vol,
                                    "cost": tot_cost,
                                    "action": act_str
                                })
                            cb_plot_df = pd.DataFrame(c_buy_records)
                            fig.add_trace(
                                go.Scatter(
                                    x=cb_plot_df["dt"],
                                    y=cb_plot_df["ret_pos"],
                                    mode="markers",
                                    name=f"[Cascade] {ticker} 매수 ({len(c_buys)}건)",
                                    marker=dict(
                                        symbol="diamond",
                                        size=12,
                                        color="#00b4d8",
                                        line=dict(color=coin_color, width=2.0)
                                    ),
                                    customdata=np.stack((
                                        cb_plot_df["price"],
                                        cb_plot_df["volume"],
                                        cb_plot_df["cost"],
                                        cb_plot_df["action"]
                                    ), axis=-1),
                                    hovertemplate=(
                                        f"<b>🔷 [Cascade {ticker} 래더 매수]</b><br>"
                                        f"• 시각: %{{x|%m-%d %H:%M:%S}}<br>"
                                        f"• 체결단가: <b>%{{customdata[0]:,.0f}}원</b><br>"
                                        f"• 변동률 위치: <b>%{{y:+.2f}}%</b><br>"
                                        f"• 체결수량: %{{customdata[1]:.6f}} {ticker}<br>"
                                        f"• 투입금액: %{{customdata[2]:,.0f}}원<br>"
                                        f"• 구분: <b>%{{customdata[3]}}</b>"
                                        f"<extra></extra>"
                                    )
                                )
                            )

                        # Cascade 바스켓 전량 익절 (⭐ 골드 스타 + 화이트 테두리)
                        if not c_sells.empty:
                            c_sell_records = []
                            for (dt_val, price_val), grp in c_sells.groupby(["dt", "price"], sort=False):
                                tot_vol = float(grp["volume"].astype(float).sum())
                                tot_pnl = float(grp["pnl"].astype(float).sum()) if "pnl" in grp.columns else 0.0
                                cnt = len(grp)
                                cnt_str = f" ({cnt}건 일괄)" if cnt > 1 else ""
                                act_str = format_cascade_action(grp.iloc[0]) + cnt_str
                                ret_pos = ((float(price_val) / base_price) - 1.0) * 100.0
                                c_sell_records.append({
                                    "dt": dt_val,
                                    "ret_pos": ret_pos,
                                    "price": float(price_val),
                                    "volume": tot_vol,
                                    "pnl": tot_pnl,
                                    "action": act_str
                                })
                            cs_plot_df = pd.DataFrame(c_sell_records)
                            fig.add_trace(
                                go.Scatter(
                                    x=cs_plot_df["dt"],
                                    y=cs_plot_df["ret_pos"],
                                    mode="markers",
                                    name=f"[Cascade] {ticker} 바스켓 익절 ({len(c_sells)}건)",
                                    marker=dict(
                                        symbol="star",
                                        size=16,
                                        color="#ffd166",
                                        line=dict(color="#ffffff", width=1.5)
                                    ),
                                    customdata=np.stack((
                                        cs_plot_df["price"],
                                        cs_plot_df["volume"],
                                        cs_plot_df["pnl"],
                                        cs_plot_df["action"]
                                    ), axis=-1),
                                    hovertemplate=(
                                        f"<b>⭐ [Cascade {ticker} 바스켓 전량 익절]</b><br>"
                                        f"• 시각: %{{x|%m-%d %H:%M:%S}}<br>"
                                        f"• 청산단가: <b>%{{customdata[0]:,.0f}}원</b><br>"
                                        f"• 변동률 위치: <b>%{{y:+.2f}}%</b><br>"
                                        f"• 청산수량: %{{customdata[1]:.6f}} {ticker}<br>"
                                        f"• 실현손익: <b>%{{customdata[2]:+,.1f}}원</b><br>"
                                        f"• 구분: <b>%{{customdata[3]}}</b>"
                                        f"<extra></extra>"
                                    )
                                )
                            )

    if not has_valid_data:
        return fig

    # (4) 0% 기준선
    if show_zero_line:
        fig.add_hline(
            y=0,
            line_dash="dash",
            line_color="#64748b",
            line_width=1.2,
            annotation_text="0.0% (시작가 기준)",
            annotation_position="bottom right",
            annotation_font=dict(color="#94a3b8", size=10)
        )

    # (5) 레이아웃 설정
    fig.update_layout(
        template="plotly_dark",
        plot_bgcolor="#131722",
        paper_bgcolor="#181d27",
        height=520,
        margin=dict(l=15, r=40, t=30, b=20),
        hovermode="closest",
        legend=dict(
            orientation="h",
            yanchor="bottom",
            y=1.02,
            xanchor="right",
            x=1,
            bgcolor="rgba(24, 29, 39, 0.8)",
            bordercolor="#2d3648",
            borderwidth=1,
            font=dict(size=11, color="#e2e8f0")
        ),
        xaxis=dict(
            showgrid=True,
            gridcolor="#232936",
            showspikes=True,
            spikemode="across",
            spikesnap="cursor",
            spikecolor="#64748b",
            spikethickness=1,
            spikedash="dot"
        ),
        yaxis=dict(
            title="수익률 / 변동률 (%)",
            ticksuffix="%",
            showgrid=True,
            gridcolor="#232936",
            zeroline=False,
            showspikes=True,
            spikemode="across",
            spikesnap="cursor",
            spikecolor="#64748b",
            spikethickness=1,
            spikedash="dot"
        )
    )

    return fig


def build_g60_normalized_figure(
    g60_data: List[Dict[str, Any]],
    interval: str = "minute60",
    count: int = 72,
    show_trades: bool = True,
    show_zero_line: bool = True,
    line_width: float = 2.2,
    ohlcv_fetcher=None
) -> go.Figure:
    """
    (하위 호환 유지) 서브포켓 G60 정규화(%) 수익률 및 매매 타점을 단일 평면에 오버레이하는 Plotly Figure를 생성합니다.
    """
    return build_subpockets_integrated_normalized_figure(
        g60_data=g60_data,
        cascade_data=None,
        interval=interval,
        count=count,
        show_g60_trades=show_trades,
        show_cascade_trades=False,
        show_zero_line=show_zero_line,
        line_width=line_width,
        ohlcv_fetcher=ohlcv_fetcher
    )


def render_pocket_g60_section(loaded_snapshot: Optional[Dict[str, Any]] = None):
    """G60 서브포켓 스쿼드 그리드 실거래 2단(솔라나, 리플) 매매 현황 컴포넌트"""
    st.markdown("### ⚡ G60 서브포켓 스쿼드 그리드 실거래 현황")
    st.caption("업비트 서브포켓(G60) 독립 데몬의 실시간 포지션 및 매매 체결 내역을 2단(솔라나 · 리플)으로 모니터링합니다. (※ 이더리움은 +1.0% 바스켓 탈출 모드로 대기 중이며 대시보드 표시에서 제외되었습니다.)")

    g60_data = get_pocket_g60_summary(loaded_snapshot=loaded_snapshot)

    # 상단 종합 지표 바
    tot_budget = sum(item["state"].get("allocated_budget", 300000.0) for item in g60_data)
    tot_pnl = sum(float(item["state"].get("realized_pnl", 0.0) or 0.0) for item in g60_data)
    tot_fees = sum(float(item["state"].get("total_fees", 0.0) or 0.0) for item in g60_data)
    tot_wins = sum(int(item["state"].get("win_count", 0) or 0) for item in g60_data)
    tot_trades = sum(len(item["trades_df"]) for item in g60_data)

    sb1, sb2, sb3, sb4 = st.columns(4)
    with sb1:
        st.metric(label="G60 총 운용예산", value=f"{tot_budget:,.0f}원", delta=f"{len(g60_data)}개 마켓 (SOL · XRP)")
    with sb2:
        pnl_delta = f"{tot_pnl:+,.1f}원"
        st.metric(label="G60 누적 실현손익", value=f"{tot_pnl:+,.1f}원", delta=pnl_delta)
    with sb3:
        st.metric(label="G60 누적 거래수수료", value=f"{tot_fees:,.1f}원")
    with sb4:
        st.metric(label="누적 익절 / 총 체결", value=f"{tot_wins}회 / {tot_trades}건")

    st.markdown("<div style='height:12px;'></div>", unsafe_allow_html=True)

    # 2단 배치: 솔(col_sol), 리플(col_xrp)
    col_sol, col_xrp = st.columns(2)
    cols = [col_sol, col_xrp]

    for col, item in zip(cols, g60_data):
        with col:
            st.markdown(f"""
            <div style="background:#181d27; border:1px solid #2d3648; border-radius:12px; padding:14px 16px; margin-bottom:12px;">
                <div style="display:flex; justify-content:space-between; align-items:center;">
                    <span style="font-size:1.15rem; font-weight:800; color:#f0f3f6;">
                        {item['icon']} {item['name']} <span style="font-size:0.85rem; color:#8c96a5;">({item['ticker']})</span>
                    </span>
                    <span style="background:#00c08722; color:#00c087; border:1px solid #00c087; padding:2px 8px; border-radius:10px; font-size:0.75rem; font-weight:700;">G60 가동중</span>
                </div>
            </div>
            """, unsafe_allow_html=True)

            state = item["state"]
            active_grids = state.get("active_grids", [])
            holding_vol = sum(float(g.get("vol", 0.0) or 0.0) for g in active_grids)
            realized_pnl = float(state.get("realized_pnl", 0.0) or 0.0)
            base_p = float(state.get("base_price", 0.0) or 0.0)
            win_cnt = int(state.get("win_count", 0) or 0)
            trades_cnt = len(item["trades_df"])
            coin_fees = float(state.get("total_fees", 0.0) or 0.0)
            allocated_budget = float(state.get("allocated_budget", 300000.0) or 300000.0)
            eff_levels = int(allocated_budget // 10000.0)

            # 핵심 지표 2열
            m1, m2 = st.columns(2)
            with m1:
                st.metric(label="실현 손익", value=f"{realized_pnl:+,.1f}원")
            with m2:
                st.metric(label="보유 그리드", value=f"{len(active_grids)}/{eff_levels}단", delta=f"{holding_vol:.4f} {item['ticker']}")

            st.caption(f"🎯 기준가: **{base_p:,.0f}원** | 익절: **{win_cnt}회** (총 {trades_cnt}건) | 수수료: **{coin_fees:,.1f}원**")

            # 보유 포지션 상세 (있을 경우에만 접이식)
            if active_grids:
                with st.expander(f"📦 현재 보유 포지션 ({len(active_grids)}단)", expanded=False):
                    g_rows = []
                    for g in active_grids:
                        g_rows.append({
                            "차수": f"Lvl {g.get('lvl')}",
                            "매수가": f"{float(g.get('buy_p', 0)):,.0f}원",
                            "수량": f"{float(g.get('vol', 0)):.6f}",
                            "투입원금": f"{float(g.get('cost', 0)):,.0f}원",
                            "목표익절가": f"{float(g.get('tp_p', 0)):,.0f}원"
                        })
                    st.dataframe(pd.DataFrame(g_rows), hide_index=True, use_container_width=True)

            # 매매 내역 테이블
            st.markdown(f"**📜 {item['name']} 매매내역** (총 {trades_cnt}건)")
            df = item["trades_df"]
            if not df.empty:
                show_df = df.copy()
                show_df["구분"] = show_df.apply(format_g60_action, axis=1)
                show_df["체결시각"] = show_df["timestamp"].apply(lambda x: str(x)[5:19] if len(str(x)) >= 19 else str(x))
                show_df["단가"] = show_df["price"].apply(lambda x: f"{float(x):,.0f}원" if pd.notnull(x) else "-")
                show_df["수량"] = show_df["volume"].apply(lambda x: f"{float(x):.6f}" if pd.notnull(x) else "-")
                show_df["체결금액"] = show_df["cost_or_revenue"].apply(lambda x: f"{float(x):,.0f}원" if pd.notnull(x) else "-")
                show_df["손익"] = show_df["pnl"].apply(lambda x: f"{float(x):+,.1f}원" if float(x) != 0 else "-")

                view_cols = ["체결시각", "구분", "단가", "수량", "체결금액", "손익"]
                st.dataframe(show_df[view_cols], hide_index=True, use_container_width=True, height=280)
            else:
                st.info(f"아직 {item['name']} 체결 내역이 없습니다.")


# =============================================================================
# 3. Cascade 서브포켓 (200단 바스켓 리셋 전략) 실거래 현황
# =============================================================================
def get_cascade_summary(loaded_snapshot: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Cascade 서브포켓 2종(SOL, ETH)의 실거래 상태 및 체결 내역을 조회합니다."""
    markets = [
        {"market": "KRW-SOL", "ticker": "SOL", "name": "솔라나", "icon": "🟣", "status": "RUNNING"},
        {"market": "KRW-ETH", "ticker": "ETH", "name": "이더리움", "icon": "💎", "status": "PENDING_TRANSFER"}
    ]

    states = {}
    trades_list = []

    # 1. 클라우드 스냅샷 (Streamlit Cloud 환경 대응)
    cascade_cloud = (loaded_snapshot or {}).get("cascade_pocket", {})
    cloud_states = cascade_cloud.get("states", {})
    cloud_trades = cascade_cloud.get("trades_list", [])
    if cloud_states:
        states.update(cloud_states)
    if cloud_trades:
        trades_list.extend(cloud_trades)

    # 2. 로컬 파일 및 DB 로드
    db_file = os.path.join(BASE_DIR, "pocket2_basket_trade_history.db")
    if os.path.exists(db_file):
        try:
            conn = sqlite3.connect(db_file, timeout=2.0)
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            c.execute("""
                SELECT id, timestamp, market, side, action, price, volume, cost_or_revenue, fee, pnl, cycle, order_uuid, strategy
                FROM pocket_trades
                ORDER BY id DESC
            """)
            local_trades = [dict(r) for r in c.fetchall()]
            conn.close()
            if local_trades:
                trades_list = local_trades
        except Exception:
            pass

    for m in markets:
        m_key = m["market"]
        s_file = os.path.join(BASE_DIR, f"pocket2_basket_state_{m_key.replace('-', '_')}.json")
        if os.path.exists(s_file):
            try:
                with open(s_file, "r", encoding="utf-8") as f:
                    states[m_key] = json.load(f)
            except Exception:
                pass

    # 3. 거래소 실시간 잔고 및 미체결 주문 (UPBIT_POCKET2)
    krw_balance = float(cascade_cloud.get("krw_balance", 240222.0) or 240222.0)
    open_orders_map = cascade_cloud.get("open_orders") or {m["market"]: [] for m in markets}
    try:
        from utils.pocket_api import UpbitPocketClient
        client = UpbitPocketClient(env_prefix="UPBIT_POCKET2")
        krw_balance = client.get_balance("KRW")
        for m in markets:
            open_orders_map[m["market"]] = client.get_open_orders(m["market"])
    except Exception:
        pass

    return {
        "markets": markets,
        "states": states,
        "trades_list": trades_list,
        "krw_balance": krw_balance,
        "open_orders": open_orders_map
    }


def render_cascade_pocket_section(loaded_snapshot: Optional[Dict[str, Any]] = None):
    """Cascade 서브포켓 (1.8% 하락 매수 / 0.8% 전량 익절 리셋 200단) 매매 현황 컴포넌트"""
    st.markdown("### 🌊 Cascade 서브포켓 실거래 현황")
    st.caption("업비트 서브포켓(UPBIT_POCKET2 / Cascade) 독립 데몬의 1.8% 하락 매수 / 0.8% 전량 익절 리셋(최대 200단) 실시간 현황을 모니터링합니다.")

    data = get_cascade_summary(loaded_snapshot=loaded_snapshot)
    states = data["states"]
    trades_list = data["trades_list"]
    krw_bal = data["krw_balance"]
    open_orders = data["open_orders"]

    # 상단 요약 바
    sol_state = states.get("KRW-SOL", {})
    eth_state = states.get("KRW-ETH", {})

    sol_pnl = float(sol_state.get("total_realized_pnl", 0.0) or 0.0)
    eth_pnl = float(eth_state.get("total_realized_pnl", 0.0) or 0.0)
    tot_pnl = sol_pnl + eth_pnl

    sol_fees = float(sol_state.get("total_fees", 0.0) or 0.0)
    eth_fees = float(eth_state.get("total_fees", 0.0) or 0.0)
    tot_fees = sol_fees + eth_fees

    sol_wins = int(sol_state.get("win_count", 0) or 0)
    eth_wins = int(eth_state.get("win_count", 0) or 0)
    tot_wins = sol_wins + eth_wins

    sb1, sb2, sb3, sb4 = st.columns(4)
    with sb1:
        st.metric(label="Cascade 총 운용예산", value="300,000원", delta="SOL 15만 + ETH 15만 가동")
    with sb2:
        st.metric(label="가용 현금 잔고", value=f"{krw_bal:,.0f}원")
    with sb3:
        pnl_delta = f"{tot_pnl:+,.1f}원"
        st.metric(label="Cascade 누적 실현손익", value=f"{tot_pnl:+,.1f}원", delta=pnl_delta)
    with sb4:
        st.metric(label="완료 사이클 / 총 수수료", value=f"{tot_wins}회 리셋 완료", delta=f"수수료 {tot_fees:,.1f}원")

    st.markdown("<div style='height:12px;'></div>", unsafe_allow_html=True)

    # 2열 배치: 솔라나(col_sol), 이더리움(col_eth)
    col_sol, col_eth = st.columns(2)

    with col_sol:
        st.html("""
        <div style="background:#181d27; border:1px solid #c77dff44; border-radius:12px; padding:14px 16px; margin-bottom:12px;">
            <div style="display:flex; justify-content:space-between; align-items:center;">
                <span style="font-size:1.15rem; font-weight:800; color:#f0f3f6;">
                    🟣 솔라나 <span style="font-size:0.85rem; color:#8c96a5;">(KRW-SOL)</span>
                </span>
                <span style="background:#00c08722; color:#00c087; border:1px solid #00c087; padding:2px 8px; border-radius:10px; font-size:0.75rem; font-weight:700;">🟢 실거래 가동중</span>
            </div>
        </div>
        """)

        cycle_cnt = sol_state.get("cycle_count", 1)
        active_lvls = sol_state.get("active_levels", 1)
        vwap = float(sol_state.get("vwap", 0.0) or 0.0)
        held_vol = float(sol_state.get("held_volume", 0.0) or 0.0)
        total_cost = float(sol_state.get("total_cost", 0.0) or 0.0)
        pending_sell = sol_state.get("pending_sell") or {}
        tp_price = float(pending_sell.get("price", 0.0) or (vwap * 1.008 if vwap > 0 else 0.0))

        sol_orders = open_orders.get("KRW-SOL", [])
        sol_asks = [o for o in sol_orders if o.get("side") == "ask"]
        sol_bids = [o for o in sol_orders if o.get("side") == "bid"]

        cur_p = 137800.0
        try:
            cur_p = float(pyupbit.get_current_price("KRW-SOL") or 137800.0)
        except Exception:
            pass

        dist_to_tp = ((tp_price - cur_p) / cur_p * 100.0) if cur_p > 0 and tp_price > 0 else 0.0
        expected_pnl = (tp_price * held_vol * 0.9995) - total_cost if tp_price > 0 and held_vol > 0 else 0.0
        pnl_pct_expected = ((tp_price - vwap) / vwap * 100.0) if vwap > 0 and tp_price > 0 else 0.0

        m1, m2 = st.columns(2)
        with m1:
            st.metric(label="현재 운용 상태", value=f"사이클 {cycle_cnt}회차", delta=f"현재 {active_lvls}/200단")
        with m2:
            st.metric(label="목표 익절가", value=f"{tp_price:,.0f}원", delta=f"+{pnl_pct_expected:.2f}% (익절까지 +{dist_to_tp:.2f}%)")

        st.caption(f"🎯 1단 평단: **{vwap:,.0f}원** | 보유: **{held_vol:.6f} SOL** ({total_cost:,.0f}원) | 목표 순익: **+{expected_pnl:,.0f}원**")

        # 거래소 호가창 대기 주문 테이블
        st.markdown("**📥 거래소 호가창 실시간 대기 주문**")
        order_rows = []
        if sol_asks:
            for o in sol_asks:
                order_rows.append({
                    "구분": "🎯 바스켓 전량 익절",
                    "가격": f"{float(o.get('price', 0)):,.0f}원",
                    "수량": f"{float(o.get('volume', 0)):.6f} SOL",
                    "주문금액": f"{float(o.get('price', 0)) * float(o.get('volume', 0)):,.0f}원",
                    "상태": "호가창 등록 대기"
                })
        if sol_bids:
            sorted_bids = sorted(sol_bids, key=lambda x: -float(x.get("price", 0)))
            for i, o in enumerate(sorted_bids, start=2):
                order_rows.append({
                    "구분": f"📥 매수 Lvl {i} (-{1.8*(i-1):.1f}%)",
                    "가격": f"{float(o.get('price', 0)):,.0f}원",
                    "수량": f"{float(o.get('volume', 0)):.6f} SOL",
                    "주문금액": f"{float(o.get('price', 0)) * float(o.get('volume', 0)):,.0f}원",
                    "상태": "호가창 등록 대기"
                })
        if order_rows:
            st.dataframe(pd.DataFrame(order_rows), hide_index=True, use_container_width=True, height=180)
        else:
            st.info("호가창 대기 주문 조회 중...")

        # 최근 체결 내역
        st.markdown("**📜 최근 체결 내역**")
        sol_trades = [t for t in trades_list if t.get("market") == "KRW-SOL"]
        if sol_trades:
            df_s = pd.DataFrame(sol_trades)
            df_s["구분"] = df_s.apply(format_cascade_action, axis=1)
            df_s["체결시각"] = df_s["timestamp"].apply(lambda x: str(x)[5:19] if len(str(x)) >= 19 else str(x))
            df_s["단가"] = df_s["price"].apply(lambda x: f"{float(x):,.0f}원" if pd.notnull(x) else "-")
            df_s["수량"] = df_s["volume"].apply(lambda x: f"{float(x):.6f}" if pd.notnull(x) else "-")
            df_s["체결금액"] = df_s["cost_or_revenue"].apply(lambda x: f"{float(x):,.0f}원" if pd.notnull(x) else "-")
            view_cols = ["체결시각", "구분", "단가", "수량", "체결금액"]
            st.dataframe(df_s[view_cols].head(8), hide_index=True, use_container_width=True, height=160)
        else:
            st.info("아직 체결 내역이 없습니다.")

    with col_eth:
        if eth_state and (eth_state.get("active_levels", 0) > 0 or eth_state.get("held_volume", 0) > 0 or eth_state.get("cycle_count", 0) > 0):
            st.html("""
            <div style="background:#181d27; border:1px solid #00e5ff44; border-radius:12px; padding:14px 16px; margin-bottom:12px;">
                <div style="display:flex; justify-content:space-between; align-items:center;">
                    <span style="font-size:1.15rem; font-weight:800; color:#f0f3f6;">
                        💎 이더리움 <span style="font-size:0.85rem; color:#8c96a5;">(KRW-ETH)</span>
                    </span>
                    <span style="background:#00c08722; color:#00c087; border:1px solid #00c087; padding:2px 8px; border-radius:10px; font-size:0.75rem; font-weight:700;">🟢 실거래 가동중</span>
                </div>
            </div>
            """)

            e_cycle_cnt = eth_state.get("cycle_count", 1)
            e_active_lvls = eth_state.get("active_levels", 1)
            e_vwap = float(eth_state.get("vwap", 0.0) or 0.0)
            e_held_vol = float(eth_state.get("held_volume", 0.0) or 0.0)
            e_total_cost = float(eth_state.get("total_cost", 0.0) or 0.0)
            e_pending_sell = eth_state.get("pending_sell") or {}
            e_tp_price = float(e_pending_sell.get("price", 0.0) or (e_vwap * 1.008 if e_vwap > 0 else 0.0))

            eth_orders = open_orders.get("KRW-ETH", [])
            eth_asks = [o for o in eth_orders if o.get("side") == "ask"]
            eth_bids = [o for o in eth_orders if o.get("side") == "bid"]

            e_cur_p = 3018000.0
            try:
                e_cur_p = float(pyupbit.get_current_price("KRW-ETH") or 3018000.0)
            except Exception:
                pass

            e_dist_to_tp = ((e_tp_price - e_cur_p) / e_cur_p * 100.0) if e_cur_p > 0 and e_tp_price > 0 else 0.0
            e_expected_pnl = (e_tp_price * e_held_vol * 0.9995) - e_total_cost if e_tp_price > 0 and e_held_vol > 0 else 0.0
            e_pnl_pct_expected = ((e_tp_price - e_vwap) / e_vwap * 100.0) if e_vwap > 0 and e_tp_price > 0 else 0.0

            em1, em2 = st.columns(2)
            with em1:
                st.metric(label="현재 운용 상태", value=f"사이클 {e_cycle_cnt}회차", delta=f"현재 {e_active_lvls}/200단")
            with em2:
                st.metric(label="목표 익절가", value=f"{e_tp_price:,.0f}원", delta=f"+{e_pnl_pct_expected:.2f}% (익절까지 +{e_dist_to_tp:.2f}%)")

            st.caption(f"🎯 1단 평단: **{e_vwap:,.0f}원** | 보유: **{e_held_vol:.6f} ETH** ({e_total_cost:,.0f}원) | 목표 순익: **+{e_expected_pnl:,.0f}원**")

            # 거래소 호가창 대기 주문 테이블
            st.markdown("**📥 거래소 호가창 실시간 대기 주문**")
            e_order_rows = []
            if eth_asks:
                for o in eth_asks:
                    e_order_rows.append({
                        "구분": "🎯 바스켓 전량 익절",
                        "가격": f"{float(o.get('price', 0)):,.0f}원",
                        "수량": f"{float(o.get('volume', 0)):.6f} ETH",
                        "주문금액": f"{float(o.get('price', 0)) * float(o.get('volume', 0)):,.0f}원",
                        "상태": "호가창 등록 대기"
                    })
            if eth_bids:
                sorted_e_bids = sorted(eth_bids, key=lambda x: -float(x.get("price", 0)))
                for i, o in enumerate(sorted_e_bids, start=2):
                    e_order_rows.append({
                        "구분": f"📥 매수 Lvl {i} (-{1.8*(i-1):.1f}%)",
                        "가격": f"{float(o.get('price', 0)):,.0f}원",
                        "수량": f"{float(o.get('volume', 0)):.6f} ETH",
                        "주문금액": f"{float(o.get('price', 0)) * float(o.get('volume', 0)):,.0f}원",
                        "상태": "호가창 등록 대기"
                    })
            if e_order_rows:
                st.dataframe(pd.DataFrame(e_order_rows), hide_index=True, use_container_width=True, height=180)
            else:
                st.info("호가창 대기 주문 조회 중...")

            # 최근 체결 내역
            st.markdown("**📜 최근 체결 내역**")
            eth_trades = [t for t in trades_list if t.get("market") == "KRW-ETH"]
            if eth_trades:
                df_e = pd.DataFrame(eth_trades)
                df_e["구분"] = df_e.apply(format_cascade_action, axis=1)
                df_e["체결시각"] = df_e["timestamp"].apply(lambda x: str(x)[5:19] if len(str(x)) >= 19 else str(x))
                df_e["단가"] = df_e["price"].apply(lambda x: f"{float(x):,.0f}원" if pd.notnull(x) else "-")
                df_e["수량"] = df_e["volume"].apply(lambda x: f"{float(x):.6f}" if pd.notnull(x) else "-")
                df_e["체결금액"] = df_e["cost_or_revenue"].apply(lambda x: f"{float(x):,.0f}원" if pd.notnull(x) else "-")
                view_cols = ["체결시각", "구분", "단가", "수량", "체결금액"]
                st.dataframe(df_e[view_cols].head(8), hide_index=True, use_container_width=True, height=160)
            else:
                st.info("아직 체결 내역이 없습니다.")
        else:
            st.html("""
            <div style="background:#181d27; border:1px solid #00e5ff44; border-radius:12px; padding:14px 16px; margin-bottom:12px;">
                <div style="display:flex; justify-content:space-between; align-items:center;">
                    <span style="font-size:1.15rem; font-weight:800; color:#f0f3f6;">
                        💎 이더리움 <span style="font-size:0.85rem; color:#8c96a5;">(KRW-ETH)</span>
                    </span>
                    <span style="background:#ffb30022; color:#ffb300; border:1px solid #ffb300; padding:2px 8px; border-radius:10px; font-size:0.75rem; font-weight:700;">⏳ 메인봇 익절 후 이전 대기</span>
                </div>
            </div>
            """)

            st.metric(label="배정 예정 예산", value="150,000원", delta="1Unit 10,000원 (최대 200단)")

            st.html("""
            <div style="background:#12161f; border:1px dashed #2d3848; border-radius:10px; padding:18px 20px; margin-top:14px; color:#cbd5e1; font-size:0.88rem; line-height:1.65;">
                <div style="font-size:1rem; font-weight:700; color:#00e5ff; margin-bottom:8px;">
                    💎 이더리움 Cascade 포켓 진입 계획
                </div>
                1. <b>현재 상태</b>: 메인계좌에서 이더리움 하이브리드 전략이 정상 운용 중입니다.<br>
                2. <b>이전 계획</b>: 메인계좌의 이더리움 포지션이 목표가에 도달하여 익절 청산되면, 회수된 원화를 Cascade 포켓으로 이전합니다.<br>
                3. <b>가동 엔진</b>: 자산 이전 완료 후 <code>pocket2_basket_eth.py</code>를 실행하면 1.8% 하락 매수 / 0.8% 전량 익절 리셋 200단 모드로 즉시 자동 매매가 시작됩니다.<br>
                <div style="margin-top:10px; padding:8px 12px; background:#18202c; border-radius:6px; font-size:0.8rem; color:#8c96a5;">
                    • 준비된 실행 스크립트: <code>pocket2_basket_eth.py</code><br>
                    • 텔레그램 간편 기동 명령어: <code>/재기동 포켓2이더</code>
                </div>
            </div>
            """)


def render_subpockets_integrated_normalized_chart(loaded_snapshot: Optional[Dict[str, Any]] = None):
    """
    서브포켓 전체(G60 2종 + Cascade 2종, 총 3개 코인: ETH · SOL · XRP)의 수익률(%)을
    0% 기준 정규화하여 단일 선 차트에 표시하고, 각 전략의 실전 매매 타점을 오버레이합니다.
    """
    st.markdown("### 📈 서브포켓 통합 수익률 및 매매 타점 (Cascade + G60 정규화 %)")
    st.caption(
        "서브포켓 독립 데몬으로 운용 중인 3개 코인(ETH · SOL · XRP)의 시작가를 0.0%로 정규화하여 가격 궤적을 비교하고, "
        "G60 스쿼드 그리드(▲/▼)와 Cascade 바스켓 리셋(◆/⭐)의 실시간 매매 타점을 단일 차트에서 직관적으로 모니터링합니다."
    )

    with st.container(border=True):
        # 컨트롤 바
        c_itv, c_opt = st.columns([1.5, 2.5])
        with c_itv:
            period_choice = st.radio(
                "조회 기간",
                ["최근 24시간", "최근 3일", "최근 7일", "최근 14일"],
                index=1,
                horizontal=True,
                key="subpockets_norm_period_radio"
            )

        period_map = {
            "최근 24시간": ("minute60", 24),
            "최근 3일": ("minute60", 72),
            "최근 7일": ("minute60", 168),
            "최근 14일": ("minute60", 336),
        }
        itv_code, candle_count = period_map.get(period_choice, ("minute60", 72))

        with c_opt:
            o1, o2, o3, o4 = st.columns(4)
            with o1:
                show_g60 = st.checkbox("⚡ G60 (▲/▼)", value=True, key="subpockets_norm_chk_g60")
            with o2:
                show_cascade = st.checkbox("🌊 Cascade (◆/⭐)", value=True, key="subpockets_norm_chk_casc")
            with o3:
                show_zero = st.checkbox("➖ 0% 기준선", value=True, key="subpockets_norm_chk_zero")
            with o4:
                line_w = st.selectbox("선 굵기", [1.5, 2.0, 2.5], index=1, key="subpockets_norm_line_w")

        g60_data = get_pocket_g60_summary(loaded_snapshot=loaded_snapshot)
        cascade_data = get_cascade_summary(loaded_snapshot=loaded_snapshot)

        fig = build_subpockets_integrated_normalized_figure(
            g60_data=g60_data,
            cascade_data=cascade_data,
            interval=itv_code,
            count=candle_count,
            show_g60_trades=show_g60,
            show_cascade_trades=show_cascade,
            show_zero_line=show_zero,
            line_width=line_w
        )

        if not fig.data:
            st.info("서브포켓 시세 데이터를 불러오는 중입니다...")
        else:
            st.plotly_chart(fig, use_container_width=True, key="subpockets_integrated_normalized_chart")


st.divider()
render_cascade_pocket_section(loaded_snapshot=loaded_snapshot)
st.divider()
render_pocket_g60_section(loaded_snapshot=loaded_snapshot)
st.divider()
render_subpockets_integrated_normalized_chart(loaded_snapshot=loaded_snapshot)
st.divider()



# =============================================================================
# 자동 새로고침 타이머 & 체결 이벤트 감지기
# =============================================================================
@st.fragment(run_every=2)
def trade_event_listener():
    """2초 주기로 신규 매매 체결 및 주문/차수 변경을 감지하는 초경량 리스너"""
    curr_sig = get_latest_trade_signature()
    prev_sig = st.session_state.get("last_trade_sig")

    if prev_sig is not None and curr_sig != prev_sig:
        st.session_state["last_trade_sig"] = curr_sig
        st.toast("⚡ 신규 매매 체결(또는 주문 상태 변경)이 감지되어 대시보드를 갱신합니다!", icon="🔔")
        st.rerun()
    else:
        st.caption("🟢 체결 감지 리스너 가동 중 (신규 체결 시 실시간 자동 갱신)")


# 현재 상태를 세션 스테이트에 최신화
st.session_state["last_trade_sig"] = get_latest_trade_signature()

if refresh_mode == "trade_event":
    trade_event_listener()
elif isinstance(refresh_mode, int) and refresh_mode > 0:
    time.sleep(refresh_mode)
    st.rerun()
