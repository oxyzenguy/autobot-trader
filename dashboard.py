import os
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

from utils.analytics import PerformanceAnalyzer
from utils.db_logger import DB_PATH, init_db


# --- Streamlit 페이지 설정 ---
st.set_page_config(
    page_title="AutoBot 퀀트 트레이딩 성과 대시보드",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="expanded"
)

# --- 커스텀 스타일 (Dark Financial Theme) ---
st.markdown("""
<style>
    .metric-card {
        background: linear-gradient(135deg, #1e222d 0%, #2a2e39 100%);
        border-radius: 10px;
        padding: 16px 20px;
        border: 1px solid #363c4e;
        box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.2);
    }
    .metric-label {
        font-size: 0.85rem;
        color: #9aa0a6;
        font-weight: 500;
        margin-bottom: 4px;
    }
    .metric-val-green {
        font-size: 1.6rem;
        font-weight: 700;
        color: #00c087;
    }
    .metric-val-red {
        font-size: 1.6rem;
        font-weight: 700;
        color: #ff3b69;
    }
    .metric-val-blue {
        font-size: 1.6rem;
        font-weight: 700;
        color: #2962ff;
    }
    .metric-val-white {
        font-size: 1.6rem;
        font-weight: 700;
        color: #f0f3f6;
    }
    .metric-sub {
        font-size: 0.78rem;
        color: #787b86;
        margin-top: 4px;
    }
    .stTabs [data-baseweb="tab-list"] {
        gap: 8px;
    }
    .stTabs [data-baseweb="tab"] {
        padding-top: 10px;
        padding-bottom: 10px;
        font-weight: 600;
    }
</style>
""", unsafe_allow_html=True)


def get_available_markets():
    """모의 매매 상태 파일 또는 DB에서 감지된 마켓 목록 반환"""
    markets = set(["KRW-SOL", "KRW-ETH"])
    for f in os.listdir("."):
        if f.startswith("paper_state_") and f.endswith(".json"):
            m = f.replace("paper_state_", "").replace(".json", "").replace("_", "-")
            markets.add(m)
    return sorted(list(markets))


def load_live_state(market: str):
    """현재 가상 계좌의 실시간 JSON 상태 로드"""
    state_file = f"paper_state_{market.replace('-', '_')}.json"
    if os.path.exists(state_file):
        try:
            with open(state_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return None


# --- 사이드바 구성 ---
st.sidebar.title("🤖 AutoBot Trader")
st.sidebar.caption("실시간 모의 매매 8대 퀀트 성과 분석 대시보드")

available_markets = get_available_markets()
selected_market = st.sidebar.selectbox("🎯 분석 마켓 선택", available_markets, index=0)

refresh_interval = st.sidebar.slider("⏱️ 자동 갱신 주기 (초)", min_value=5, max_value=60, value=10, step=5)
auto_refresh = st.sidebar.toggle("실시간 자동 갱신 활성화", value=True)

if st.sidebar.button("🔄 즉시 데이터 새로고침"):
    st.rerun()

st.sidebar.divider()

# 계좌 메타데이터 요약
live_state = load_live_state(selected_market)
if live_state:
    st.sidebar.markdown(f"**초기 자본:** {live_state.get('initial_capital', 500000):,.0f} 원")
    st.sidebar.markdown(f"**보유 현금:** {live_state.get('krw_balance', 0):,.0f} 원")
    st.sidebar.markdown(f"**보유 코인:** {live_state.get('coin_balance', 0):.6f} {selected_market.split('-')[1]}")
    st.sidebar.markdown(f"**진행 사이클:** {live_state.get('completed_cycles', 0)}회")
st.sidebar.divider()
st.sidebar.caption(f"기준 시각: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")


# --- 메인 대시보드 프래그먼트 (지정 주기로 자동 갱신) ---
@st.fragment(run_every=refresh_interval if auto_refresh else None)
def render_dashboard(market: str):
    # 1. 퀀트 성과 분석 데이터 산출
    analyzer = PerformanceAnalyzer(market)
    data = analyzer.get_full_analysis()

    ticker = data["ticker"]
    returns_mdd = data["returns_mdd"]
    trade_perf = data["trade_perf"]
    cons_losses = data["cons_losses"]
    trade_freq = data["trade_freq"]
    bnh = data["buy_and_hold"]
    regimes = data["regimes"]
    raw_equity = data["raw_equity"]
    raw_trades = data["raw_trades"]

    # 실시간 시세
    current_price = pyupbit.get_current_price(market) or (raw_equity["coin_price"].iloc[-1] if not raw_equity.empty else 0.0)

    # 헤더 타이틀 및 배지
    regime_color = "#00c087" if regimes["current_regime"] == "Bull" else ("#ff3b69" if regimes["current_regime"] == "Bear" else "#ffa726")
    st.markdown(f"""
    <div style="display: flex; justify-content: space-between; align-items: flex-end; margin-bottom: 20px;">
        <div>
            <h1 style="margin: 0; font-size: 2rem;">⚡ {market} 전략 성과 모니터</h1>
            <p style="margin: 4px 0 0 0; color: #848e9c;">마틴게일 배수 물타기 + 리스크 관리 봇 실시간 통계</p>
        </div>
        <div style="text-align: right;">
            <span style="background-color: {regime_color}22; color: {regime_color}; border: 1px solid {regime_color}; padding: 4px 12px; border-radius: 20px; font-weight: 600; font-size: 0.9rem;">
                시장 국면: {regimes['current_regime']}
            </span>
            <div style="font-size: 1.4rem; font-weight: 700; margin-top: 6px;">
                현재가: {current_price:,.0f} KRW
            </div>
        </div>
    </div>
    """, unsafe_allow_html=True)

    # -------------------------------------------------------------
    # 섹션 1: 8대 핵심 KPI 카드
    # -------------------------------------------------------------
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        ret_pct = returns_mdd["total_return_pct"]
        ret_color = "metric-val-green" if ret_pct >= 0 else "metric-val-red"
        st.markdown(f"""
        <div class="metric-card">
            <div class="metric-label">1. 총 자산 & 누적 수익률</div>
            <div class="{ret_color}">{ret_pct:+.2f}%</div>
            <div class="metric-sub">자산: {returns_mdd['current_equity']:,.0f}원 (손익: {returns_mdd['total_pnl']:+,.0f}원)</div>
        </div>
        """, unsafe_allow_html=True)

    with c2:
        mdd_pct = returns_mdd["mdd_pct"]
        st.markdown(f"""
        <div class="metric-card">
            <div class="metric-label">2. MDD (최대 낙폭)</div>
            <div class="metric-val-red">{mdd_pct:.2f}%</div>
            <div class="metric-sub">현재 낙폭: {returns_mdd['current_drawdown_pct']:.2f}% | 최고점: {returns_mdd['peak_equity']:,.0f}원</div>
        </div>
        """, unsafe_allow_html=True)

    with c3:
        win_rate = trade_perf["win_rate"]
        st.markdown(f"""
        <div class="metric-card">
            <div class="metric-label">3. 승률 (Win Rate)</div>
            <div class="metric-val-blue">{win_rate:.1f}%</div>
            <div class="metric-sub">청산 {trade_perf['total_closed_trades']}건 ({trade_perf['wins']}승 {trade_perf['losses']}패)</div>
        </div>
        """, unsafe_allow_html=True)

    with c4:
        pf = trade_perf["profit_factor"]
        pf_color = "metric-val-green" if pf >= 1.0 else "metric-val-red"
        st.markdown(f"""
        <div class="metric-card">
            <div class="metric-label">4. 손익비 (Profit Factor)</div>
            <div class="{pf_color}">{pf:.2f}</div>
            <div class="metric-sub">익/손비: {trade_perf['win_loss_ratio']:.2f} (평균익: {trade_perf['avg_win']:,.0f} / 손: {trade_perf['avg_loss']:,.0f})</div>
        </div>
        """, unsafe_allow_html=True)

    st.markdown("<div style='height: 12px;'></div>", unsafe_allow_html=True)

    c5, c6, c7, c8 = st.columns(4)
    with c5:
        max_cl = cons_losses["max_consecutive_losses"]
        st.markdown(f"""
        <div class="metric-card">
            <div class="metric-label">5. 연속 손실 (Consecutive Losses)</div>
            <div class="metric-val-white">{max_cl} <span style="font-size: 1rem; color:#848e9c;">회</span></div>
            <div class="metric-sub">현재 연속 손실: {cons_losses['current_consecutive_losses']}회 | 최장 연승: {cons_losses['max_consecutive_wins']}회</div>
        </div>
        """, unsafe_allow_html=True)

    with c6:
        st.markdown(f"""
        <div class="metric-card">
            <div class="metric-label">6. 거래 빈도 (Trade Frequency)</div>
            <div class="metric-val-white">{trade_freq['daily_trade_frequency']} <span style="font-size: 1rem; color:#848e9c;">건/일</span></div>
            <div class="metric-sub">총 체결: {trade_freq['total_orders']}건 | 평균 보유: {trade_freq['avg_cycle_duration_hours']:.1f}시간</div>
        </div>
        """, unsafe_allow_html=True)

    with c7:
        alpha = bnh["alpha_pct"]
        alpha_color = "metric-val-green" if alpha >= 0 else "metric-val-red"
        st.markdown(f"""
        <div class="metric-card">
            <div class="metric-label">7. Buy & Hold 대비 성과 (Alpha)</div>
            <div class="{alpha_color}">{alpha:+.2f}%p</div>
            <div class="metric-sub">봇: {bnh['strategy_return_pct']:+.2f}% vs B&H: {bnh['bnh_return_pct']:+.2f}%</div>
        </div>
        """, unsafe_allow_html=True)

    with c8:
        st.markdown(f"""
        <div class="metric-card">
            <div class="metric-label">8. 시장 국면별 승률 (Regime)</div>
            <div class="metric-val-white">{regimes['summary'].get('Sideways', {}).get('win_rate', 0)}% <span style="font-size: 0.9rem; color:#848e9c;">(횡보)</span></div>
            <div class="metric-sub">상승: {regimes['summary'].get('Bull', {}).get('win_rate', 0)}% | 하락: {regimes['summary'].get('Bear', {}).get('win_rate', 0)}%</div>
        </div>
        """, unsafe_allow_html=True)

    st.divider()

    # -------------------------------------------------------------
    # 섹션 2: 4대 상세 탭
    # -------------------------------------------------------------
    tab1, tab2, tab3, tab4 = st.tabs([
        "📈 자산 곡선 vs B&H & MDD",
        "🌐 시장 국면별 성과 분석",
        "⚖️ 리스크 & 거래 통계",
        "📋 실시간 주문 & 체결 내역"
    ])

    # -------------------------------------------------------------
    # TAB 1: 자산 곡선 vs B&H 및 Underwater Drawdown
    # -------------------------------------------------------------
    with tab1:
        st.subheader("📊 자산 성장 곡선 vs Buy & Hold 벤치마크 & MDD")
        
        df_comp = bnh["df_comparison"]
        df_mdd = returns_mdd["df_mdd"]

        if not df_comp.empty and "timestamp" in df_comp.columns:
            fig = make_subplots(
                rows=2, cols=1,
                shared_xaxes=True,
                vertical_spacing=0.08,
                subplot_titles=("자산 가치 비교 (KRW)", "고점 대비 낙폭 Drawdown Underwater (%)"),
                row_heights=[0.7, 0.3]
            )

            # 1) 전략 자산 곡선
            fig.add_trace(
                go.Scatter(
                    x=df_comp["timestamp"],
                    y=df_comp["total_equity"],
                    name="AutoBot 전략 자산",
                    line=dict(color="#00c087", width=2.5),
                    hovertemplate="전략: %{y:,.0f}원<br>일시: %{x}<extra></extra>"
                ),
                row=1, col=1
            )

            # 2) Buy & Hold 곡선
            fig.add_trace(
                go.Scatter(
                    x=df_comp["timestamp"],
                    y=df_comp["bnh_equity"],
                    name=f"Buy & Hold ({ticker})",
                    line=dict(color="#2962ff", width=1.8, dash="dot"),
                    hovertemplate="B&H: %{y:,.0f}원<br>일시: %{x}<extra></extra>"
                ),
                row=1, col=1
            )

            # 3) 초기 자본 기준선
            fig.add_hline(
                y=data["initial_capital"],
                line=dict(color="#848e9c", width=1, dash="dash"),
                row=1, col=1
            )

            # 4) Drawdown Underwater Chart
            if not df_mdd.empty and "drawdown_pct" in df_mdd.columns:
                fig.add_trace(
                    go.Scatter(
                        x=df_mdd["timestamp"],
                        y=df_mdd["drawdown_pct"],
                        name="Drawdown (%)",
                        line=dict(color="#ff3b69", width=1.5),
                        fill="tozeroy",
                        fillcolor="rgba(255, 59, 105, 0.2)",
                        hovertemplate="낙폭: %{y:.2f}%<br>일시: %{x}<extra></extra>"
                    ),
                    row=2, col=1
                )

            fig.update_layout(
                height=520,
                margin=dict(l=20, r=20, t=40, b=20),
                legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
                template="plotly_dark",
                hovermode="x unified"
            )
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.info("자산 시계열 스냅샷이 누적되는 중입니다. 잠시 후 차트가 표시됩니다.")

    # -------------------------------------------------------------
    # TAB 2: 시장 국면별 성과 분석 (Regime Analysis)
    # -------------------------------------------------------------
    with tab2:
        st.subheader("🌐 시장 상승 / 하락 / 횡보 국면별 성과 비교")
        st.caption("업비트 캔들의 이동평균선(SMA 20/60) 및 기울기를 기준으로 시장 국면을 분류하고 각 구간에서의 매매 성과를 평가합니다.")

        r_col1, r_col2 = st.columns([1, 1])
        
        reg_data = regimes["summary"]
        reg_df = pd.DataFrame([
            {
                "국면 (Regime)": "상승장 (Bull)",
                "거래 횟수": reg_data["Bull"]["trades"],
                "승률 (%)": reg_data["Bull"]["win_rate"],
                "총 실현손익": f"{reg_data['Bull']['total_pnl']:+,.0f}원",
                "평균 손익": f"{reg_data['Bull']['avg_pnl']:+,.0f}원"
            },
            {
                "국면 (Regime)": "횡보장 (Sideways)",
                "거래 횟수": reg_data["Sideways"]["trades"],
                "승률 (%)": reg_data["Sideways"]["win_rate"],
                "총 실현손익": f"{reg_data['Sideways']['total_pnl']:+,.0f}원",
                "평균 손익": f"{reg_data['Sideways']['avg_pnl']:+,.0f}원"
            },
            {
                "국면 (Regime)": "하락장 (Bear)",
                "거래 횟수": reg_data["Bear"]["trades"],
                "승률 (%)": reg_data["Bear"]["win_rate"],
                "총 실현손익": f"{reg_data['Bear']['total_pnl']:+,.0f}원",
                "평균 손익": f"{reg_data['Bear']['avg_pnl']:+,.0f}원"
            }
        ])

        with r_col1:
            st.markdown("##### 📊 국면별 성과 요약 테이블")
            st.dataframe(reg_df, hide_index=True, use_container_width=True)

        with r_col2:
            st.markdown("##### 🎯 국면별 승률 및 거래량 비교")
            fig_bar = go.Figure()
            fig_bar.add_trace(go.Bar(
                x=["상승장 (Bull)", "횡보장 (Sideways)", "하락장 (Bear)"],
                y=[reg_data["Bull"]["win_rate"], reg_data["Sideways"]["win_rate"], reg_data["Bear"]["win_rate"]],
                name="승률 (%)",
                marker_color=["#00c087", "#ffa726", "#ff3b69"]
            ))
            fig_bar.update_layout(
                height=260,
                margin=dict(l=20, r=20, t=30, b=20),
                template="plotly_dark",
                yaxis=dict(title="승률 (%)", range=[0, 100])
            )
            st.plotly_chart(fig_bar, use_container_width=True)

        # 캔들 및 국면 이평선 차트
        df_candles = regimes.get("df_candles")
        if df_candles is not None and not df_candles.empty:
            st.markdown("##### 🕯️ 최근 시장 시세 및 이동평균 국면")
            fig_candle = go.Figure()
            fig_candle.add_trace(go.Candlestick(
                x=df_candles.index,
                open=df_candles['open'],
                high=df_candles['high'],
                low=df_candles['low'],
                close=df_candles['close'],
                name="캔들"
            ))
            if "sma20" in df_candles.columns:
                fig_candle.add_trace(go.Scatter(x=df_candles.index, y=df_candles['sma20'], line=dict(color="#f48fb1", width=1.5), name="SMA 20"))
            if "sma60" in df_candles.columns:
                fig_candle.add_trace(go.Scatter(x=df_candles.index, y=df_candles['sma60'], line=dict(color="#81d4fa", width=1.5), name="SMA 60"))
            fig_candle.update_layout(
                height=350,
                margin=dict(l=20, r=20, t=20, b=20),
                template="plotly_dark",
                xaxis_rangeslider_visible=False
            )
            st.plotly_chart(fig_candle, use_container_width=True)

    # -------------------------------------------------------------
    # TAB 3: 리스크 & 거래 통계
    # -------------------------------------------------------------
    with tab3:
        st.subheader("⚖️ 리스크 지표 및 물타기 단계 도달 분석")

        rk1, rk2 = st.columns(2)
        with rk1:
            st.markdown("##### 🌊 마틴게일 물타기 차수별(1X, 2X, 3X, 6X) 도달 횟수")
            st.caption("진입 차수가 깊어질수록 리스크가 증가합니다. 각 차수별 도달 빈도를 확인하세요.")
            steps = cons_losses["martingale_steps"]
            fig_steps = go.Figure(data=[
                go.Bar(
                    x=list(steps.keys()),
                    y=list(steps.values()),
                    marker_color=["#2962ff", "#00bcd4", "#ffb300", "#ff3b69", "#78909c"]
                )
            ])
            fig_steps.update_layout(
                height=280,
                margin=dict(l=20, r=20, t=30, b=20),
                template="plotly_dark",
                xaxis_title="물타기 배수",
                yaxis_title="체결 횟수"
            )
            st.plotly_chart(fig_steps, use_container_width=True)

        with rk2:
            st.markdown("##### 💰 평균 수익 vs 평균 손실 비교")
            st.caption("이긴 거래와 진 거래의 1회당 평균 금액 비교")
            fig_pl = go.Figure(data=[
                go.Bar(
                    x=["평균 익절", "평균 손절"],
                    y=[trade_perf["avg_win"], trade_perf["avg_loss"]],
                    marker_color=["#00c087", "#ff3b69"]
                )
            ])
            fig_pl.update_layout(
                height=280,
                margin=dict(l=20, r=20, t=30, b=20),
                template="plotly_dark",
                yaxis_title="금액 (KRW)"
            )
            st.plotly_chart(fig_pl, use_container_width=True)

    # -------------------------------------------------------------
    # TAB 4: 실시간 포지션 및 거래 로그
    # -------------------------------------------------------------
    with tab4:
        st.subheader("📋 실시간 포지션 상태 및 체결 로그")

        p_col1, p_col2 = st.columns([1, 1])
        with p_col1:
            st.markdown("##### 💼 현재 보유 포지션")
            if live_state:
                pos_df = pd.DataFrame([
                    {"항목": "보유 코인", "값": f"{live_state.get('coin_balance', 0):.6f} {ticker}"},
                    {"항목": "매수 평균단가", "값": f"{live_state.get('avg_buy_price', 0):,.0f} 원"},
                    {"항목": "총 매수 원가", "값": f"{live_state.get('total_cost', 0):,.0f} 원"},
                    {"항목": "현재 평가금액", "값": f"{live_state.get('coin_balance', 0) * current_price:,.0f} 원"},
                    {"항목": "미실현 손익", "값": f"{(live_state.get('coin_balance', 0) * current_price) - live_state.get('total_cost', 0):+,.0f} 원"}
                ])
                st.dataframe(pos_df, hide_index=True, use_container_width=True)
            else:
                st.info("실시간 상태 정보를 불러올 수 없습니다.")

        with p_col2:
            st.markdown("##### ⏳ 현재 걸려있는 미체결 주문")
            if live_state and live_state.get("open_orders"):
                orders = []
                for o in live_state["open_orders"]:
                    orders.append({
                        "구분": "매도(익절)" if o["side"] == "ask" else f"매수({o.get('units', 1)}X 물타기)",
                        "주문가격": f"{o['price']:,.0f}원",
                        "수량": f"{o['volume']:.6f}",
                        "주문일시": o.get("created_at", "-")
                    })
                st.dataframe(pd.DataFrame(orders), hide_index=True, use_container_width=True)
            else:
                st.write("현재 등록된 미체결 주문이 없습니다.")

        st.markdown("##### 📜 최근 체결 거래 내역")
        if not raw_trades.empty:
            show_trades = raw_trades.sort_values("timestamp", ascending=False).head(30).copy()
            show_trades["price"] = show_trades["price"].apply(lambda x: f"{x:,.0f}원")
            show_trades["cost_or_revenue"] = show_trades["cost_or_revenue"].apply(lambda x: f"{x:,.0f}원")
            show_trades["pnl"] = show_trades["pnl"].apply(lambda x: f"{x:+,.0f}원" if x != 0 else "-")
            show_trades["volume"] = show_trades["volume"].apply(lambda x: f"{x:.6f}")
            st.dataframe(show_trades, hide_index=True, use_container_width=True)
        else:
            st.info("아직 기록된 체결 거래 내역이 없습니다.")


# 대시보드 렌더링 호출
render_dashboard(selected_market)
