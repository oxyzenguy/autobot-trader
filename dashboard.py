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
from config import INVESTMENTS
from strategy.hybrid_regime import get_hybrid_regime_and_signals


# --- Streamlit 페이지 설정 (사이드바 기본 접힘 상태) ---
st.set_page_config(
    page_title="AutoBot 퀀트 하이브리드 대시보드",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="collapsed"
)

# --- 커스텀 스타일 (Dark Financial Theme + 상단 컨트롤 바) ---
st.markdown("""
<style>
    /* 상단 컨트롤 패널 카드 */
    .top-control-card {
        background: #1e222d;
        border: 1px solid #2a2e39;
        border-radius: 12px;
        padding: 14px 20px;
        margin-bottom: 16px;
        box-shadow: 0 4px 12px rgba(0,0,0,0.25);
    }
    .summary-banner {
        background: linear-gradient(90deg, #181d26 0%, #202632 100%);
        border: 1px solid #2f3746;
        border-radius: 10px;
        padding: 12px 18px;
        margin-bottom: 20px;
        display: flex;
        flex-wrap: wrap;
        gap: 20px;
        align-items: center;
        justify-content: space-between;
    }
    .summary-item {
        display: flex;
        flex-direction: column;
    }
    .summary-label {
        font-size: 0.76rem;
        color: #848e9c;
        font-weight: 500;
        margin-bottom: 2px;
    }
    .summary-value {
        font-size: 1.1rem;
        font-weight: 700;
        color: #f0f3f6;
    }
    .summary-green {
        color: #00c087;
    }
    .summary-red {
        color: #ff3b69;
    }

    /* 실제 코인모으기 vs 마틴게일 비교 박스 */
    .dca-compare-box {
        background: linear-gradient(135deg, #191f2c 0%, #202737 100%);
        border: 1px solid #3d4659;
        border-radius: 12px;
        padding: 16px 20px;
        margin-bottom: 20px;
        box-shadow: 0 4px 14px rgba(0,0,0,0.3);
    }
    .metric-card {
        background: linear-gradient(135deg, #1e222d 0%, #262b37 100%);
        border-radius: 10px;
        padding: 14px 18px;
        border: 1px solid #363c4e;
        box-shadow: 0 4px 8px -2px rgba(0, 0, 0, 0.3);
        height: 100%;
    }
    .metric-label {
        font-size: 0.82rem;
        color: #9aa0a6;
        font-weight: 500;
        margin-bottom: 4px;
    }
    .metric-val-green {
        font-size: 1.55rem;
        font-weight: 700;
        color: #00c087;
    }
    .metric-val-red {
        font-size: 1.55rem;
        font-weight: 700;
        color: #ff3b69;
    }
    .metric-val-blue {
        font-size: 1.55rem;
        font-weight: 700;
        color: #2962ff;
    }
    .metric-val-white {
        font-size: 1.55rem;
        font-weight: 700;
        color: #f0f3f6;
    }
    .metric-sub {
        font-size: 0.76rem;
        color: #787b86;
        margin-top: 4px;
    }

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


def get_available_markets():
    """현재 전략이 적용된 유효 마켓(INVESTMENTS에 설정된 종목)만 반환 (BTC, XRP 등 미적용 종목 제외)"""
    # .env 및 config.py에 설정된 대상 종목 (현재 KRW-SOL, KRW-ETH)
    configured_markets = list(INVESTMENTS.keys()) if INVESTMENTS else ["KRW-SOL", "KRW-ETH"]

    active_markets = []

    # 실제 상태 파일이 존재하는 설정 종목 우선 배치
    for m in configured_markets:
        state_file = os.path.join(BASE_DIR, f"paper_state_{m.replace('-', '_')}.json")
        if os.path.exists(state_file) and m not in active_markets:
            active_markets.append(m)

    # 아직 상태 파일이 생성되지 않은 설정 종목 추가
    for m in configured_markets:
        if m not in active_markets:
            active_markets.append(m)

    return active_markets


BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def load_live_state(market: str):
    """현재 가상 계좌의 실시간 JSON 상태 로드"""
    state_file = os.path.join(BASE_DIR, f"paper_state_{market.replace('-', '_')}.json")
    if os.path.exists(state_file):
        try:
            with open(state_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return None


# =============================================================================
# 1. 상단 컨트롤 패널 (기존 좌측 사이드바를 위쪽으로 전면 재배치)
# =============================================================================
available_markets = get_available_markets()

with st.container():
    c_title, c_market, c_refresh, c_btn = st.columns([3.2, 1.8, 2.2, 1.0])

    with c_title:
        st.markdown("""
        <div style="display:flex; align-items:center; gap:10px; padding-top:2px;">
            <span style="font-size:2rem;">⚡</span>
            <div>
                <h2 style="margin:0; font-size:1.55rem; font-weight:800; color:#f0f3f6;">AutoBot Trader 성과 대시보드</h2>
                <div style="color:#848e9c; font-size:0.82rem;">실시간 하이브리드(국면전환) 자동매매 & 8대 퀀트 성과 분석</div>
            </div>
        </div>
        """, unsafe_allow_html=True)

    with c_market:
        selected_market = st.selectbox(
            "🎯 분석 마켓 선택",
            available_markets,
            index=0,
            help="실제 거래가 감지된 마켓이 맨 위에 표시됩니다."
        )

    with c_refresh:
        refresh_mode = st.selectbox(
            "🔄 갱신 주기",
            ["trade_event", 10, 30, 60, "manual"],
            index=0,
            format_func=lambda x: {
                "trade_event": "⚡ 거래 체결 시 (추천)",
                10: "⏱️ 10초 주기 (시세)",
                30: "⏱️ 30초 주기",
                60: "⏱️ 60초 주기",
                "manual": "🛑 수동 갱신"
            }.get(x, str(x)),
            help="⚡ '거래 체결 시': 새로운 매수/매도/물타기 체결 발생 시 자동으로 대시보드를 즉시 갱신합니다."
        )

    with c_btn:
        st.markdown("<div style='height:28px;'></div>", unsafe_allow_html=True)
        if st.button("🔄 새로고침", use_container_width=True):
            st.rerun()


# =============================================================================
# 2. 거래 체결 감지 리스너 & 대시보드 렌더링
# =============================================================================
def get_trade_signature(market: str) -> str:
    """체결 거래 수, 마지막 거래 ID, 최근 체결 시각, 실시간 상태 시그니처 생성"""
    sig_parts = []
    try:
        db_path = os.path.join(BASE_DIR, "trade_history.db")
        if os.path.exists(db_path):
            with sqlite3.connect(db_path) as conn:
                cur = conn.cursor()
                cur.execute("SELECT count(*), max(id), max(timestamp) FROM paper_trades WHERE market = ?", (market,))
                row = cur.fetchone()
                if row and row[0] is not None:
                    sig_parts.append(f"db:{row[0]}-{row[1]}-{row[2]}")
    except Exception:
        pass

    st_data = load_live_state(market)
    if st_data:
        sig_parts.append(f"st:{st_data.get('completed_cycles', 0)}-{len(st_data.get('open_orders', []))}-{st_data.get('realized_pnl', 0)}-{st_data.get('coin_balance', 0)}")
    return "|".join(sig_parts)


@st.fragment(run_every=2)
def trade_event_listener(market: str):
    """체결 이벤트 감지 백그라운드 리스너 (체결 발생 시 대시보드 자동 갱신)"""
    current_sig = get_trade_signature(market)
    sig_key = f"last_known_trade_sig_{market}"

    if sig_key not in st.session_state:
        st.session_state[sig_key] = current_sig
        return

    if current_sig != st.session_state[sig_key]:
        st.session_state[sig_key] = current_sig
        st.toast(f"🎯 [{market}] 새로운 주문이 체결되었습니다! 대시보드를 즉시 갱신합니다.", icon="⚡")
        time.sleep(0.3)
        st.rerun()


periodic_seconds = refresh_mode if isinstance(refresh_mode, int) else None

@st.fragment(run_every=periodic_seconds)
def render_dashboard(market: str):
    # 1. 퀀트 분석 엔진 구동 및 실시간 데이터 로드
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
    current_price = pyupbit.get_current_price(market)
    if not current_price and not raw_equity.empty:
        current_price = float(raw_equity["coin_price"].iloc[-1])
    current_price = current_price or 0.0

    live_state = load_live_state(market) or {}
    initial_cap = live_state.get("initial_capital", data.get("initial_capital", 1000000.0))
    krw_bal = live_state.get("krw_balance", initial_cap)
    coin_bal = live_state.get("coin_balance", 0.0)
    eval_coin = coin_bal * current_price
    total_cur_equity = krw_bal + eval_coin
    total_ret_pct = ((total_cur_equity - initial_cap) / initial_cap) * 100.0 if initial_cap > 0 else 0.0
    realized_pnl = live_state.get("realized_pnl", trade_perf.get("net_profit", 0.0))
    cycles = live_state.get("completed_cycles", trade_perf.get("total_closed_trades", 0))

    # 하이브리드 실시간 국면 및 신호 조회
    try:
        hybrid_info = get_hybrid_regime_and_signals(market)
    except Exception:
        hybrid_info = {
            "regime": live_state.get("current_regime", "BEAR") if live_state else "BEAR",
            "regime_korean": "하락 국면 (기본)",
            "curr_price": current_price,
            "ma5": 0.0, "ma20": 0.0, "ma200": 0.0,
            "distance_ma200_pct": 0.0,
            "signal": "MARTINGALE_DEFENSE",
            "reason": "시세 정보 동기화 중"
        }

    # -------------------------------------------------------------
    # 상단 요약 배너 (실시간 계좌 상태 카드 바)
    # -------------------------------------------------------------
    ret_class = "summary-green" if total_ret_pct >= 0 else "summary-red"
    pnl_class = "summary-green" if realized_pnl >= 0 else "summary-red"
    regime_color = "#00c087" if hybrid_info["regime"] == "BULL" else "#ff3b69"

    st.markdown(f"""
    <div class="summary-banner">
        <div class="summary-item">
            <span class="summary-label">🎯 종목 / 하이브리드 국면</span>
            <span class="summary-value" style="color:#2962ff;">{market} <span style="font-size:0.85rem; color:{regime_color}; background:{regime_color}22; padding:2px 8px; border-radius:12px; border:1px solid {regime_color};">{hybrid_info['regime_korean']}</span></span>
        </div>
        <div class="summary-item">
            <span class="summary-label">⚡ 현재 시세</span>
            <span class="summary-value">{current_price:,.0f} KRW</span>
        </div>
        <div class="summary-item">
            <span class="summary-label">💰 총 평가 자산 (수익률)</span>
            <span class="summary-value {ret_class}">{total_cur_equity:,.0f}원 ({total_ret_pct:+.2f}%)</span>
        </div>
        <div class="summary-item">
            <span class="summary-label">💵 보유 현금 (KRW)</span>
            <span class="summary-value">{krw_bal:,.0f} 원</span>
        </div>
        <div class="summary-item">
            <span class="summary-label">🪙 보유 코인 ({ticker})</span>
            <span class="summary-value">{coin_bal:.6f} ({eval_coin:,.0f}원)</span>
        </div>
        <div class="summary-item">
            <span class="summary-label">🏆 실현 누적 손익</span>
            <span class="summary-value {pnl_class}">{realized_pnl:+,.0f} 원</span>
        </div>
        <div class="summary-item">
            <span class="summary-label">🔁 완료 사이클</span>
            <span class="summary-value">{cycles} 회 완료</span>
        </div>
        <div class="summary-item">
            <span class="summary-label">⏱️ 실시간 기준 시각</span>
            <span class="summary-value" style="font-size:0.88rem; color:#848e9c; font-weight:normal;">{datetime.now().strftime('%H:%M:%S')}</span>
        </div>
    </div>
    """, unsafe_allow_html=True)

    # -------------------------------------------------------------
    # 🌟 실제 코인모으기(실계좌) vs 100만원 마틴게일(가상매매) 맞대결 카드
    # -------------------------------------------------------------
    real_dca = data.get("real_dca", {})
    real_bal = real_dca.get("current_balance", 0.0)
    real_avg = real_dca.get("current_avg_price", 0.0)
    real_cost = real_dca.get("total_cost", 0.0)
    real_eval = real_dca.get("current_eval", 0.0)
    real_pnl_pct = real_dca.get("total_pnl_pct", 0.0)
    base_eval = real_dca.get("base_eval_amount", real_eval)
    growth_pct = real_dca.get("growth_pct_since_base", 0.0)

    alpha_vs_dca = total_ret_pct - growth_pct
    alpha_dca_color = "#00c087" if alpha_vs_dca >= 0 else "#ff3b69"
    alpha_desc = "하이브리드 전략 우세" if alpha_vs_dca >= 0 else "코인모으기 전략 우세"
    real_pnl_color = "#00c087" if real_pnl_pct >= 0 else "#ff3b69"
    tot_ret_color = "#00c087" if total_ret_pct >= 0 else "#ff3b69"

    from config import SHOWDOWN_START_TIME
    try:
        start_dt = datetime.strptime(SHOWDOWN_START_TIME, "%Y-%m-%d %H:%M:%S")
        days_elapsed = (datetime.now() - start_dt).days
    except Exception:
        days_elapsed = 0

    st.html(f"""
    <div class="dca-compare-box">
        <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:12px; border-bottom:1px solid #333d4e; padding-bottom:8px;">
            <div style="font-size:1.05rem; font-weight:700; color:#ffb300;">
                ⚔️ 2~4주 실전 검증 맞대결: 실제 코인모으기(실계좌) vs 100만원 하이브리드(가상매매)
            </div>
            <div style="font-size:0.82rem; color:#4fc3f7; font-weight:600;">
                🚀 검증 시작점: {SHOWDOWN_START_TIME} (경과: D+{days_elapsed}일차)
            </div>
        </div>
        <div style="display:grid; grid-template-columns: 1.2fr 0.8fr 1.2fr; gap:14px; align-items:center;">
            <div style="background:#161b24; padding:12px 14px; border-radius:8px; border:1px solid #2d3648;">
                <div style="color:#ffa726; font-size:0.83rem; font-weight:600; margin-bottom:4px;">
                    🟢 [실제 계좌] 매일 1만원 코인모으기
                </div>
                <div style="font-size:1.15rem; font-weight:700; color:#f0f3f6;">
                    {real_eval:,.0f}원 <span style="font-size:0.85rem; color:{real_pnl_color};">({real_pnl_pct:+.2f}%)</span>
                </div>
                <div style="font-size:0.76rem; color:#848e9c; margin-top:4px;">
                    보유: {real_bal:.4f} {ticker} | 평단: {real_avg:,.0f}원 | 원금: {real_cost:,.0f}원
                </div>
                <div style="font-size:0.75rem; color:#29b6f6; margin-top:2px;">
                    기점 이후 가치변화: {growth_pct:+.2f}%
                </div>
            </div>
            <div style="text-align:center; padding:8px;">
                <div style="font-size:0.78rem; color:#848e9c; margin-bottom:2px;">하이브리드 초과성과 (Alpha)</div>
                <div style="font-size:1.45rem; font-weight:800; color:{alpha_dca_color};">
                    {alpha_vs_dca:+.2f}%p
                </div>
                <div style="font-size:0.74rem; color:#9aa0a6; margin-top:2px;">
                    {alpha_desc}
                </div>
            </div>
            <div style="background:#161b24; padding:12px 14px; border-radius:8px; border:1px solid #2d3648;">
                <div style="color:#00c087; font-size:0.83rem; font-weight:600; margin-bottom:4px;">
                    🔵 [가상 계좌] 100만원 하이브리드(국면전환) 봇
                </div>
                <div style="font-size:1.15rem; font-weight:700; color:#f0f3f6;">
                    {total_cur_equity:,.0f}원 <span style="font-size:0.85rem; color:{tot_ret_color};">({total_ret_pct:+.2f}%)</span>
                </div>
                <div style="font-size:0.76rem; color:#848e9c; margin-top:4px;">
                    현금: {krw_bal:,.0f}원 | 코인: {coin_bal:.4f} {ticker} ({eval_coin:,.0f}원)
                </div>
                <div style="font-size:0.75rem; color:#00e676; margin-top:2px;">
                    실현손익: {realized_pnl:+,.0f}원 | 완료: {cycles}회 사이클
                </div>
            </div>
        </div>
    </div>
    """)

    # -------------------------------------------------------------
    # 🧭 실시간 하이브리드 국면 & 전략 가동 상태 배너
    # -------------------------------------------------------------
    is_bull = hybrid_info.get("is_bull", False)
    active_mode = live_state.get("active_sub_strategy", "WAITING") if live_state else "WAITING"
    
    if is_bull:
        regime_badge = '<span style="background:#00c087; color:#12161f; padding:4px 12px; border-radius:14px; font-weight:800; font-size:0.88rem;">🟢 상승 국면 (BULL)</span>'
        mode_desc = '<span style="color:#00e676; font-weight:700; font-size:1.05rem;">🚀 5/20 MA 추세추종 모드 가동 중</span>'
    else:
        regime_badge = '<span style="background:#ff3b69; color:#fff; padding:4px 12px; border-radius:14px; font-weight:800; font-size:0.88rem;">🔴 하락 국면 (BEAR)</span>'
        mode_desc = '<span style="color:#ffb300; font-weight:700; font-size:1.05rem;">🛡️ 마틴게일 1-2-3-6 방어 모드 가동 중</span>'

    dist_color = "#00c087" if hybrid_info["distance_ma200_pct"] >= 0 else "#ff3b69"

    st.html(f"""
    <div style="background:linear-gradient(135deg, #1c222e 0%, #222938 100%); border:1px solid #364154; border-radius:10px; padding:14px 18px; margin-bottom:20px;">
        <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:10px;">
            <div style="display:flex; align-items:center; gap:10px;">
                <span style="font-size:1.2rem;">🧭</span>
                <span style="font-size:0.95rem; font-weight:700; color:#f0f3f6;">실시간 시장 국면 & 하이브리드 엔진 상태</span>
                {regime_badge}
            </div>
            <div>{mode_desc}</div>
        </div>
        <div style="display:grid; grid-template-columns: repeat(4, 1fr); gap:12px; background:#141822; padding:10px 14px; border-radius:8px; border:1px solid #283142;">
            <div>
                <div style="font-size:0.73rem; color:#848e9c;">현재가</div>
                <div style="font-size:0.95rem; font-weight:700; color:#f0f3f6;">{hybrid_info['curr_price']:,.0f}원</div>
            </div>
            <div>
                <div style="font-size:0.73rem; color:#848e9c;">200시간선 (국면 기준)</div>
                <div style="font-size:0.95rem; font-weight:700; color:#81d4fa;">{hybrid_info['ma200']:,.0f}원 <span style="font-size:0.75rem; color:{dist_color};">({hybrid_info['distance_ma200_pct']:+.2f}%)</span></div>
            </div>
            <div>
                <div style="font-size:0.73rem; color:#848e9c;">5시간선 / 20시간선</div>
                <div style="font-size:0.95rem; font-weight:700; color:#f48fb1;">{hybrid_info['ma5']:,.0f}원 / {hybrid_info['ma20']:,.0f}원</div>
            </div>
            <div>
                <div style="font-size:0.73rem; color:#848e9c;">실시간 신호 판정</div>
                <div style="font-size:0.85rem; font-weight:600; color:#ffd54f;">{hybrid_info['signal']} ({hybrid_info['reason'][:28]}...)</div>
            </div>
        </div>
    </div>
    """)

    # -------------------------------------------------------------
    # 섹션 1: 8대 핵심 퀀트 KPI 카드
    # -------------------------------------------------------------
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        ret_pct = total_ret_pct
        ret_color = "metric-val-green" if ret_pct >= 0 else "metric-val-red"
        st.markdown(f"""
        <div class="metric-card">
            <div class="metric-label">1. 총 자산 & 누적 수익률</div>
            <div class="{ret_color}">{ret_pct:+.2f}%</div>
            <div class="metric-sub">자산: {total_cur_equity:,.0f}원 (초기: {initial_cap:,.0f}원)</div>
        </div>
        """, unsafe_allow_html=True)

    with c2:
        mdd_pct = returns_mdd["mdd_pct"]
        st.markdown(f"""
        <div class="metric-card">
            <div class="metric-label">2. MDD (최대 낙폭)</div>
            <div class="metric-val-red">{mdd_pct:.2f}%</div>
            <div class="metric-sub">현재 낙폭: {returns_mdd['current_drawdown_pct']:.2f}% | 최고: {returns_mdd['peak_equity']:,.0f}원</div>
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
            <div class="metric-sub">익/손비: {trade_perf['win_loss_ratio']:.2f} (평균익: {trade_perf['avg_win']:,.0f}원 / 손: {trade_perf['avg_loss']:,.0f}원)</div>
        </div>
        """, unsafe_allow_html=True)

    st.markdown("<div style='height: 10px;'></div>", unsafe_allow_html=True)

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
            <div class="metric-sub">전략: {bnh['strategy_return_pct']:+.2f}% vs B&H: {bnh['bnh_return_pct']:+.2f}%</div>
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

    # -------------------------------------------------------------
    # 섹션 2: 🔥 최근 체결 거래 내역 (사용자가 바로 볼 수 있도록 즉시 노출)
    # -------------------------------------------------------------
    st.markdown("<div style='height: 18px;'></div>", unsafe_allow_html=True)
    st.markdown("### ⚡ 최근 체결 거래 피드 (Live Trade Feed)")

    if not raw_trades.empty:
        # 최근 10개 거래 포맷팅
        feed_df = raw_trades.sort_values("timestamp", ascending=False).head(10).copy()

        def format_action_kr(act):
            act_s = str(act)
            if "HYBRID_BULL_BUY" in act_s:
                return "🚀 [하이브리드] 5/20 MA 추세 매수"
            elif "HYBRID_BULL_SELL" in act_s or "TREND_DEAD_CROSS" in act_s:
                return "🛑 [하이브리드] 5/20 MA 추세 매도"
            elif "BEAR_REGIME_CUT" in act_s:
                return "🛡️ [하이브리드] 200선 이탈 청산/방어 전환"
            elif "TAKE_PROFIT" in act_s:
                return "🎯 익절 매도"
            elif "STOP_LOSS" in act_s:
                return "🚨 긴급 손절"
            elif "MARKET_BUY" in act_s:
                return "🚀 1차 진입 매수"
            elif "BUY" in act_s:
                return f"💧 {act_s.replace('LIMIT_BUY_', '')} 물타기 매수"
            return act_s

        feed_df["체결일시"] = feed_df["timestamp"].dt.strftime("%Y-%m-%d %H:%M:%S")
        feed_df["구분"] = feed_df["action"].apply(format_action_kr)
        feed_df["포지션"] = feed_df["side"].apply(lambda x: "매도 (ASK)" if x == "ask" else "매수 (BID)")
        feed_df["체결단가"] = feed_df["price"].apply(lambda x: f"{x:,.0f}원")
        feed_df["체결수량"] = feed_df["volume"].apply(lambda x: f"{x:.6f} {ticker}")
        feed_df["체결금액"] = feed_df["cost_or_revenue"].apply(lambda x: f"{x:,.0f}원")
        feed_df["실현손익"] = feed_df["pnl"].apply(lambda x: f"+{x:,.1f}원" if x > 0 else (f"{x:,.1f}원" if x < 0 else "-"))
        feed_df["사이클"] = feed_df["cycle"].apply(lambda x: f"{x}차")

        display_cols = ["체결일시", "구분", "포지션", "체결단가", "체결수량", "체결금액", "실현손익", "사이클"]
        st.dataframe(feed_df[display_cols], hide_index=True, use_container_width=True)
    else:
        st.info(f"아직 {market}에 기록된 체결 거래 내역이 없습니다. (자동매매 봇이 신규 거래를 체결하면 즉시 표시됩니다)")

    st.divider()

    # -------------------------------------------------------------
    # 섹션 3: 모바일 최적화 세로 원페이지 스크롤 레이아웃
    # -------------------------------------------------------------
    st.markdown("<div style='height: 10px;'></div>", unsafe_allow_html=True)

    # =============================================================
    # 1. 📈 자산 성장 곡선 & MDD
    # =============================================================
    st.divider()
    st.markdown("### 📈 1. 자산 성장 곡선 & MDD")
    df_comp = bnh["df_comparison"]
    df_mdd = returns_mdd["df_mdd"]

    if not df_comp.empty and "timestamp" in df_comp.columns:
        fig = make_subplots(
            rows=2, cols=1,
            shared_xaxes=True,
            vertical_spacing=0.08,
            subplot_titles=("자산 가치 추이 (KRW)", "고점 대비 낙폭 Drawdown Underwater (%)"),
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

        # 2) 실제 코인모으기 (실계좌 환산 비교) 곡선
        if "real_dca_equity" in df_comp.columns and df_comp["real_dca_equity"].notna().any():
            fig.add_trace(
                go.Scatter(
                    x=df_comp["timestamp"],
                    y=df_comp["real_dca_equity"],
                    name=f"실제 코인모으기 (환산 비교)",
                    line=dict(color="#ffa726", width=2.2, dash="dash"),
                    hovertemplate="코인모으기(환산): %{y:,.0f}원<br>일시: %{x}<extra></extra>"
                ),
                row=1, col=1
            )

        # 3) Buy & Hold 곡선
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

        # 4) 초기 자본 기준선
        fig.add_hline(
            y=initial_cap,
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
            height=480,
            margin=dict(l=15, r=15, t=35, b=20),
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
            template="plotly_dark",
            hovermode="x unified"
        )
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("자산 시계열 스냅샷이 누적되는 중입니다. 잠시 후 차트가 표시됩니다.")

    # =============================================================
    # 2. 🌐 시장 국면별 성과 (Bull / Sideways / Bear)
    # =============================================================
    st.divider()
    st.markdown("### 🌐 2. 시장 국면별 성과 (Bull/Bear)")
    st.caption("이동평균선(SMA 20/60) 기울기를 바탕으로 시장 국면을 판별하고 각 구간에서의 매매 성과를 평가합니다.")

    r_col1, r_col2 = st.columns([1, 1])
    reg_data = regimes["summary"]
    reg_df = pd.DataFrame([
        {
            "국면 (Regime)": "상승장 (Bull)",
            "거래 횟수": reg_data["Bull"]["trades"],
            "승률 (%)": f"{reg_data['Bull']['win_rate']:.1f}%",
            "총 실현손익": f"{reg_data['Bull']['total_pnl']:+,.0f}원",
            "평균 손익": f"{reg_data['Bull']['avg_pnl']:+,.0f}원"
        },
        {
            "국면 (Regime)": "횡보장 (Sideways)",
            "거래 횟수": reg_data["Sideways"]["trades"],
            "승률 (%)": f"{reg_data['Sideways']['win_rate']:.1f}%",
            "총 실현손익": f"{reg_data['Sideways']['total_pnl']:+,.0f}원",
            "평균 손익": f"{reg_data['Sideways']['avg_pnl']:+,.0f}원"
        },
        {
            "국면 (Regime)": "하락장 (Bear)",
            "거래 횟수": reg_data["Bear"]["trades"],
            "승률 (%)": f"{reg_data['Bear']['win_rate']:.1f}%",
            "총 실현손익": f"{reg_data['Bear']['total_pnl']:+,.0f}원",
            "평균 손익": f"{reg_data['Bear']['avg_pnl']:+,.0f}원"
        }
    ])

    with r_col1:
        st.markdown("##### 📊 국면별 성과 요약")
        st.dataframe(reg_df, hide_index=True, use_container_width=True)

    with r_col2:
        st.markdown("##### 🎯 국면별 승률 비교")
        fig_bar = go.Figure()
        fig_bar.add_trace(go.Bar(
            x=["상승장 (Bull)", "횡보장 (Sideways)", "하락장 (Bear)"],
            y=[reg_data["Bull"]["win_rate"], reg_data["Sideways"]["win_rate"], reg_data["Bear"]["win_rate"]],
            name="승률 (%)",
            marker_color=["#00c087", "#ffa726", "#ff3b69"]
        ))
        fig_bar.update_layout(
            height=250,
            margin=dict(l=15, r=15, t=25, b=20),
            template="plotly_dark",
            yaxis=dict(title="승률 (%)", range=[0, 100])
        )
        st.plotly_chart(fig_bar, use_container_width=True)

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
            height=320,
            margin=dict(l=15, r=15, t=15, b=20),
            template="plotly_dark",
            xaxis_rangeslider_visible=False
        )
        st.plotly_chart(fig_candle, use_container_width=True)

    # =============================================================
    # 3. ⚖️ 물타기 배수 & 리스크 분석
    # =============================================================
    st.divider()
    st.markdown("### ⚖️ 3. 물타기 배수 & 리스크 분석")

    rk1, rk2 = st.columns(2)
    with rk1:
        st.markdown("##### 🌊 마틴게일 물타기 차수별 도달 횟수")
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
            height=260,
            margin=dict(l=15, r=15, t=25, b=20),
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
            height=260,
            margin=dict(l=15, r=15, t=25, b=20),
            template="plotly_dark",
            yaxis_title="금액 (KRW)"
        )
        st.plotly_chart(fig_pl, use_container_width=True)

    # =============================================================
    # 4. 📋 실시간 미체결 주문 & 전체 거래 로그
    # =============================================================
    st.divider()
    st.markdown("### 📋 4. 실시간 미체결 주문 & 전체 거래 로그")

    p_col1, p_col2 = st.columns([1, 1])

    with p_col1:
        st.markdown("##### 💼 현재 보유 포지션")
        if live_state:
            active_mode_str = {
                "TREND": "🚀 상승장 5/20 추세모드",
                "MARTINGALE": "🛡️ 하락장 마틴게일 방어모드",
                "WAITING": "⏳ 대기 / 관망 중"
            }.get(live_state.get("active_sub_strategy", "WAITING"), "기타")

            regime_str = "🟢 상승 국면 (200 MA 상회)" if live_state.get("current_regime") == "BULL" else "🔴 하락 국면 (200 MA 하회)"

            pos_df = pd.DataFrame([
                {"항목": "가동 전략 모드", "값": active_mode_str},
                {"항목": "시장 국면", "값": regime_str},
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
        st.markdown("##### ⏳ 현재 등록된 미체결 주문")
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

    st.markdown("##### 📜 전체 체결 거래 이력")
    if not raw_trades.empty:
        full_trades = raw_trades.sort_values("timestamp", ascending=False).copy()
        full_trades["체결일시"] = full_trades["timestamp"].dt.strftime("%Y-%m-%d %H:%M:%S")
        full_trades["체결단가"] = full_trades["price"].apply(lambda x: f"{x:,.0f}원")
        full_trades["체결금액"] = full_trades["cost_or_revenue"].apply(lambda x: f"{x:,.0f}원")
        full_trades["실현손익"] = full_trades["pnl"].apply(lambda x: f"+{x:,.1f}원" if x > 0 else (f"{x:,.1f}원" if x < 0 else "-"))
        full_trades["체결수량"] = full_trades["volume"].apply(lambda x: f"{x:.6f}")
        full_trades["구분"] = full_trades["action"].apply(lambda x: "익절 매도" if "SELL" in str(x) else "매수")
        full_trades["방향"] = full_trades["side"].apply(lambda x: "매도 (ASK)" if x == "ask" else "매수 (BID)")
        
        show_cols = ["체결일시", "구분", "방향", "체결단가", "체결수량", "체결금액", "실현손익", "cycle"]
        st.dataframe(full_trades[show_cols], hide_index=True, use_container_width=True)
    else:
        st.info("아직 기록된 체결 거래 내역이 없습니다.")


# 메인 대시보드 렌더링 호출
render_dashboard(selected_market)

# 거래 체결 감지 모드인 경우 2초 주기 초경량 이벤트 감지 리스너 가동
if refresh_mode == "trade_event":
    trade_event_listener(selected_market)

