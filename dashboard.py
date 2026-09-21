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

from config import INVESTMENTS, MIN_KRW_ALERT_THRESHOLD, get_profit_margin
from utils.analytics import (
    get_total_account_summary,
    get_all_active_strategies,
    get_strategy_performance
)
from utils.db_logger import (
    DB_PATH,
    init_db,
    get_strategy_history,
    archive_strategy,
    save_or_update_strategy
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def get_latest_trade_signature() -> str:
    """
    매매 체결 및 주문 상태 변경을 초경량으로 감지하기 위한 시그니처 생성
    1) trade_history.db의 trades 테이블 레코드 수 및 최신 ID
    2) real_strategy_state_{market}.json 파일들의 최종 수정 시각
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

    state_sig = []
    for m in INVESTMENTS.keys():
        s_file = os.path.join(BASE_DIR, f"real_strategy_state_{m.replace('-', '_')}.json")
        if os.path.exists(s_file):
            try:
                mtime = os.path.getmtime(s_file)
                state_sig.append(f"{m}:{mtime:.1f}")
            except Exception:
                pass

    return f"{db_sig}|{'_'.join(state_sig)}"



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
# 데이터 로드 (실시간 업비트 전체 계좌 및 활성 전략)
# =============================================================================
account_data = get_total_account_summary()
active_strategies = get_all_active_strategies()


# =============================================================================
# [🔑 Streamlit Cloud Secrets 설정 안내 배너 (API 키 미설정 시)]
# =============================================================================
if account_data.get("is_api_key_missing"):
    api_err = account_data.get("api_error_message", "")
    is_ip_error = ("no_authorization_i_p" in api_err.lower()) or ("허용되지 않은 ip" in api_err.lower())
    
    if is_ip_error:
        st.error(f"""
        ### 🚨 [업비트 API] IP 주소 제한 오류 감지
        **업비트 응답**: `{api_err}`
        
        현재 등록된 업비트 API 키는 특정 IP 주소(예: 자택 PC)만 허용되어 있어, 고정 IP가 없는 **Streamlit Cloud(클라우드 서버)**의 요청이 업비트에서 차단되었습니다.
        
        **👉 해결 방법 (자산조회 전용 키 신규 발급 권장):**
        1. 업비트 로그인 ➔ **[마이] ➔ [Open API 관리]** 이동
        2. **'자산조회'** 권한만 체크 (출금/주문 권한 제외로 안전)
        3. ⚠️ **'IP 주소 등록'을 비워둔 상태(미등록)**로 신규 API 키 발급
        4. 발급받은 새 키를 Streamlit Cloud의 **Secrets**에 업데이트 후 저장
        """)
    else:
        err_hint = f"\n\n**세부 응답/오류**: `{api_err}`" if api_err else ""
        st.warning(f"""
        ### ⚠️ 업비트 API 키 연동 필요 (Streamlit Cloud 환경){err_hint}
        
        웹 대시보드(Streamlit Cloud)는 보안상 로컬 `.env` 파일을 읽지 못하므로, **Streamlit Secrets**에 업비트 API 키를 등록해주셔야 실시간 계좌 및 매매 현황 조회가 가능합니다.
        
        **👉 설정 방법 (30초 완료):**
        1. 화면 우측 하단의 **'Manage app'** 클릭 (또는 우측 상단 `⋮` 메뉴)
        2. **Settings** ➔ **Secrets** 탭 선택
        3. 아래 내용을 복사하여 본인의 API 키를 입력 후 **Save** 클릭:
        ```toml
        UPBIT_ACCESS_KEY = "발급받은_UPBIT_ACCESS_KEY"
        UPBIT_SECRET_KEY = "발급받은_UPBIT_SECRET_KEY"
        ```
        4. 저장 즉시 페이지가 자동으로 새로고침되며 실시간 계좌 정보가 정상 연동됩니다!
        """)


# =============================================================================
# [🚨 예수금 10만원 미만 긴급 경고 배너]
# =============================================================================
if account_data["is_krw_warning"]:
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
    krw_warn = account_data["is_krw_warning"]
    krw_badge = '<span style="background:#ff3b6922; color:#ff3b69; padding:2px 8px; border-radius:10px; font-size:0.75rem; border:1px solid #ff3b69; font-weight:700;">🚨 10만원 미만 부족</span>' if krw_warn else '<span style="background:#00c08722; color:#00c087; padding:2px 8px; border-radius:10px; font-size:0.75rem; border:1px solid #00c087; font-weight:700;">✅ 정상</span>'
    krw_color = "text-red" if krw_warn else "text-green"
    st.html(f"""
    <div class="metric-card" style="border-color: {'#ff3b69' if krw_warn else '#2b3345'};">
        <div class="metric-title" style="display:flex; justify-content:space-between; align-items:center;">
            <span>2. 💵 주문가능 예수금 (KRW)</span>
            {krw_badge}
        </div>
        <div class="metric-val {krw_color}">{krw_bal:,.0f} <span style="font-size:1rem; color:#8c96a5;">원</span></div>
        <div class="metric-sub">
            묶인 금액(주문중): {account_data['krw_locked']:,.0f}원 | 알림 기준: {MIN_KRW_ALERT_THRESHOLD:,}원
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


# =============================================================================
# 2. 현재 적용한 전략의 수익률 및 현황 (제목의 [아래] 삭제, 복수 전략 지원)
# =============================================================================
st.markdown("### 🎯 현재 적용한 전략의 수익률 및 현황")
st.caption("각 코인/전략별로 배정된 투자금과 개별 전략의 실시간 수익률 및 미체결 물타기 주문을 아래에 별도로 표시합니다.")

if not active_strategies:
    st.info("현재 활성화된 전략이 없습니다. config.py의 INVESTMENTS 설정을 확인해주세요.")
else:
    for idx, strat in enumerate(active_strategies, start=1):
        market = strat["market"]
        ticker = strat["ticker"]
        strat_name = strat["strategy_name"]
        unit_krw = strat["unit_krw"]
        initial_cap = strat["initial_capital"]
        strat_ret = strat["strategy_return_pct"]
        strat_pnl = strat["total_strat_pnl"]
        realized_pnl = strat["realized_pnl"]
        unrealized_pnl = strat["unrealized_pnl"]
        cur_p = strat["current_price"]
        avg_p = strat["avg_buy_price"]
        coin_bal = strat["coin_balance"]
        eval_amt = strat["eval_amount"]
        open_orders = strat["open_orders"]
        trades_df = strat["trades_df"]

        ret_color = "text-green" if strat_ret >= 0 else "text-red"
        pnl_color = "text-green" if strat_pnl >= 0 else "text-red"
        badge_style = "background:#2962ff22; color:#2962ff; border:1px solid #2962ff;"

        regime_info = strat.get("regime_info", {})
        is_bull = regime_info.get("is_bull", False)
        regime_color = "#00c087" if is_bull else "#ff3b69"
        regime_badge = f'<span style="background:{regime_color}22; color:{regime_color}; border:1px solid {regime_color}; padding:2px 10px; border-radius:12px; font-size:0.8rem; font-weight:700;">{regime_info.get("regime_korean", "국면 분석 중")}</span>'
        mode_desc = "🚀 5/20 MA 추세추종 & 트레일링 스탑" if is_bull else "🛡️ 마틴-매직스플릿 방어 (개별+3% OR 바스켓 이중익절)"

        target_base_p = strat.get("bot_avg_price", 0.0) if strat.get("bot_avg_price", 0.0) > 0 else avg_p
        basket_target_p = target_base_p * strat["profit_margin"] if target_base_p > 0 else 0.0

        dist_pct = regime_info.get("distance_ma200_pct", 0.0)
        dist_color = "#00c087" if dist_pct >= 0 else "#ff3b69"
        sig_text = regime_info.get("signal", "HOLD")
        reason_text = regime_info.get("reason", "분석 중")
        short_reason = (reason_text[:35] + "...") if len(reason_text) > 35 else reason_text

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
                    전략 배정 원금: <b style="color:#f0f3f6;">{initial_cap:,.0f}원</b> | 1 Unit: <b style="color:#f0f3f6;">{unit_krw:,.0f}원</b>
                </div>
            </div>

            <div style="background:linear-gradient(135deg, #181d27 0%, #1f2634 100%); border:1px solid #333d4e; border-radius:10px; padding:12px 16px; margin-bottom:14px;">
                <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:10px; flex-wrap:wrap; gap:8px;">
                    <div style="display:flex; align-items:center; gap:10px;">
                        <span style="font-size:1.25rem;">🧭</span>
                        <span style="font-size:0.95rem; font-weight:700; color:#f0f3f6;">실시간 시장 국면 & 하이브리드 엔진 상태</span>
                        {regime_badge}
                    </div>
                    <div>
                        <span style="background:{regime_color}18; color:{regime_color}; border:1px solid {regime_color}44; padding:3px 10px; border-radius:8px; font-size:0.84rem; font-weight:700;">
                            {mode_desc}
                        </span>
                    </div>
                </div>
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

                <div style="display:flex; justify-content:space-between; align-items:center; margin-top:8px; padding-top:8px; border-top:1px dashed #2a3344; font-size:0.8rem;">
                    <div>
                        <span style="color:#8c96a5;">🎯 바스켓 익절 목표가:</span>
                        <b style="color:#00c087; margin-left:4px;">{basket_target_p:,.0f}원</b>
                        <span style="color:#8c96a5; font-size:0.74rem;">(평단가 대비 +{(strat['profit_margin']-1)*100:.2f}%)</span>
                    </div>
                    <div>
                        <span style="color:#8c96a5;">💧 개별 차수 매직스플릿 익절선:</span>
                        <b style="color:#ffb300; margin-left:4px;">각 차수 매수가 대비 +3.0% 반등</b>
                        <span style="color:#8c96a5; font-size:0.74rem;">(차수 단독 청산)</span>
                    </div>
                </div>
            </div>
        </div>
        """)

        # 전략 핵심 성과 지표 4분할
        sc1, sc2, sc3, sc4 = st.columns(4)
        with sc1:
            st.html(f"""
            <div class="metric-card">
                <div class="metric-title">전략 총 수익률</div>
                <div class="metric-val {ret_color}">{strat_ret:+.2f}%</div>
                <div class="metric-sub">순손익: <b class="{pnl_color}">{strat_pnl:+,.0f}원</b></div>
            </div>
            """)

        with sc2:
            real_color = "text-green" if realized_pnl >= 0 else "text-red"
            st.html(f"""
            <div class="metric-card">
                <div class="metric-title">실현 누적 손익</div>
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

        # 포지션 및 미체결 주문 2분할
        p_col1, p_col2 = st.columns([1.1, 1.9])

        with p_col1:
            st.markdown(f"##### 💼 {ticker} 포지션 현황 (봇 운용 & 기존 자산 보호)")
            bot_q = strat.get("bot_quantity", 0.0)
            bot_avg = strat.get("bot_avg_price", 0.0)
            bot_eval = strat.get("bot_eval", 0.0)
            bot_pnl = strat.get("bot_unrealized_pnl", 0.0)
            bot_pnl_pct = strat.get("bot_pnl_pct", 0.0)
            prot_q = strat.get("protected_quantity", 0.0)
            tot_coin = strat.get("account_total_coin_balance", coin_bal)

            pos_data = [
                {"항목": "현재 시세", "값": f"{cur_p:,.0f} 원"},
                {"항목": "🤖 봇 운용 수량", "값": f"{bot_q:.6f} {ticker}"},
                {"항목": "🤖 봇 매수평단", "값": f"{bot_avg:,.0f} 원" if bot_avg > 0 else "미보유 (대기)"},
                {"항목": "🤖 봇 평가금액", "값": f"{bot_eval:,.0f} 원"},
                {"항목": "🤖 봇 미실현손익", "값": f"{bot_pnl:+,.0f} 원 ({bot_pnl_pct:+.2f}%)" if bot_avg > 0 else "-"},
                {"항목": "🔒 기존 자산 (보호중)", "값": f"{prot_q:.6f} {ticker}"},
                {"항목": "🏛️ 계좌 전체 총수량", "값": f"{tot_coin:.6f} {ticker}"}
            ]
            st.dataframe(pd.DataFrame(pos_data), hide_index=True, use_container_width=True)

        with p_col2:
            st.markdown(f"##### 📋 {ticker} 실시간 주문 & 체결 통합 타임라인")
            timeline_items = []

            # 1. 미체결 대기 주문 (호가창 등록 중인 주문)
            if open_orders:
                for o in open_orders:
                    side_kr = "🎯 [익절매도]" if o["side"] == "ask" else "💧 [물타기매수]"
                    diff = ((o["price"] - cur_p) / cur_p * 100.0) if cur_p > 0 else 0.0
                    reg_time = str(o.get("created_at", "-"))[:19].replace("T", " ")
                    timeline_items.append({
                        "상태": "⏳ 대기중",
                        "시각": reg_time,
                        "구분": side_kr,
                        "주문가격": f"{o['price']:,.0f}원 ({diff:+.2f}%)",
                        "수량": f"{o['volume']:.6f} {ticker}",
                        "주문금액": f"{o['price'] * o['volume']:,.0f}원",
                        "상세 / 손익": "호가창 등록 대기 중 (미체결)"
                    })

            # 2. 최근 체결 완료 내역 (실거래 trades 테이블)
            if not trades_df.empty:
                action_map = {
                    "INITIAL_10K_ENTRY": "1회차 신규 진입",
                    "TREND_GOLDEN_CROSS_BUY": "5/20 골든크로스 매수",
                    "MARTINGALE_BUY_INITIAL": "마틴게일 1차 매수",
                    "TRAILING_STOP_EXIT": "트레일링스탑 익절",
                    "TREND_DEAD_CROSS_SELL": "5/20 데드크로스 청산",
                    "BASKET_TAKE_PROFIT": "바스켓 전량 익절",
                    "STOP_LOSS": "STOP-LOSS 손절"
                }
                for _, t in trades_df.head(15).iterrows():
                    raw_action = str(t.get("action", ""))
                    if raw_action.startswith("TRANCHE_TAKE_PROFIT"):
                        action_kr = "매직스플릿 차수 익절"
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
                st.dataframe(pd.DataFrame(timeline_items), hide_index=True, use_container_width=True, height=270)
            else:
                st.info(f"현재 {market}에 등록된 대기 주문 및 체결 내역이 없습니다.")

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

st.divider()


# =============================================================================
# 3. 전략 히스토리 & 1~3달 주기 평가 관리소
# =============================================================================
st.markdown("### 📚 1~3달 주기 전략 평가 & 히스토리 보관소 (Strategy Archive)")
st.caption("1달 ~ 3달 운영 후 전략을 교체하더라도, 과거 전략의 수익률, 승률, MDD 및 설정 파라미터를 보관하고 언제든 다시 불러올 수 있습니다.")

tab_history, tab_archive_tool = st.tabs(["📜 과거 전략 히스토리 목록 & 파라미터 불러오기", "⚙️ 현재 전략 평가 완료 & 아카이브(종료) 등록"])

with tab_history:
    histories = get_strategy_history(include_active=True)

    if histories:
        hist_df = pd.DataFrame([
            {
                "전략ID": h["strategy_id"],
                "전략명": h["strategy_name"],
                "종목": h["market"],
                "시작일": h["start_time"],
                "종료일": h["end_time"] or "운영 중 (ACTIVE)",
                "투자원금": f"{h['initial_capital']:,.0f}원",
                "최종수익률": f"{h['return_pct']:+.2f}%",
                "실현손익": f"{h['realized_pnl']:+,.0f}원",
                "승률": f"{h['win_rate']:.1f}%",
                "거래수": f"{h['trade_count']}회",
                "손익비": f"{h['profit_factor']:.2f}",
                "상태": "🟢 운영중" if h["status"] == "ACTIVE" else "📦 보관됨",
                "평가메모": h.get("notes", "-")
            }
            for h in histories
        ])
        st.dataframe(hist_df, hide_index=True, use_container_width=True)

        st.markdown("##### 🔍 과거 전략 파라미터 상세 조회 & 복원 가이드")
        selected_strat_id = st.selectbox("파라미터를 확인할 과거 전략 선택", [h["strategy_id"] for h in histories])
        selected_hist = next((h for h in histories if h["strategy_id"] == selected_strat_id), None)

        if selected_hist:
            col_p1, col_p2 = st.columns([1, 1])
            with col_p1:
                st.markdown(f"**전략명**: {selected_hist['strategy_name']} ({selected_hist['market']})")
                st.markdown(f"**운영 기간**: {selected_hist['start_time']} ~ {selected_hist['end_time'] or '현재'}")
                st.markdown(f"**최종 성과**: 수익률 {selected_hist['return_pct']:+.2f}% | 승률 {selected_hist['win_rate']:.1f}%")
                st.markdown(f"**평가 메모**: {selected_hist.get('notes', '없음')}")

            with col_p2:
                st.markdown("**저장된 설정 파라미터 (JSON):**")
                try:
                    params = json.loads(selected_hist.get("parameters_json", "{}"))
                    st.json(params)
                except Exception:
                    st.code(selected_hist.get("parameters_json", "{}"))
    else:
        st.info("아직 보관된 전략 히스토리가 없습니다. 현재 전략을 운영 후 아래 탭에서 아카이브(보관)할 수 있습니다.")

with tab_archive_tool:
    st.markdown("##### 📝 현재 전략 성과를 히스토리에 아카이브(보관)하기")
    st.write("1~3달간 운영한 전략을 종료하고 성과를 영구 기록할 때 사용합니다.")

    target_strat_market = st.selectbox("종료 및 보관할 전략 종목 선택", list(INVESTMENTS.keys()))
    strat_perf = get_strategy_performance(target_strat_market)

    col_ar1, col_ar2 = st.columns(2)
    with col_ar1:
        archive_name = st.text_input("보관할 전략 이름", value=strat_perf["strategy_name"])
        archive_id = st.text_input("고유 전략 ID", value=f"STRAT_{target_strat_market.replace('-', '_')}_{datetime.now().strftime('%Y%m%d')}")
        archive_notes = st.text_area("전략 평가 메모 (1~3달 운영 소회 및 평가 요인)", placeholder="예: 2026년 9월~11월 횡보장에서 안정적 익절. 손절 없이 100% 승률 달성. 하락장 방어력 우수.")

    with col_ar2:
        st.markdown("**보관될 성과 지표 요약:**")
        st.write(f"- 시작원금: {strat_perf['initial_capital']:,.0f}원")
        st.write(f"- 누적수익률: **{strat_perf['strategy_return_pct']:+.2f}%**")
        st.write(f"- 누적 실현손익: {strat_perf['realized_pnl']:+,.0f}원")
        st.write(f"- 승률: {strat_perf['win_rate']:.1f}% ({strat_perf['wins']}승 {strat_perf['losses']}패)")
        st.write(f"- 거래횟수: {strat_perf['total_trades']}건 | 완료 사이클: {strat_perf['completed_cycles']}회")

        if st.button("📦 이 전략을 히스토리에 보관(Archive) 등록", use_container_width=True):
            params_dict = {
                "market": target_strat_market,
                "unit_krw": strat_perf["unit_krw"],
                "profit_margin": strat_perf["profit_margin"],
                "initial_capital": strat_perf["initial_capital"],
                "stop_loss_pct": -0.10
            }
            save_or_update_strategy(
                strategy_id=archive_id,
                strategy_name=archive_name,
                market=target_strat_market,
                start_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                initial_capital=strat_perf["initial_capital"],
                parameters_dict=params_dict,
                status="ARCHIVED",
                end_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                final_capital=strat_perf["initial_capital"] + strat_perf["total_strat_pnl"],
                realized_pnl=strat_perf["realized_pnl"],
                return_pct=strat_perf["strategy_return_pct"],
                win_rate=strat_perf["win_rate"],
                trade_count=strat_perf["total_trades"],
                profit_factor=strat_perf["profit_factor"],
                notes=archive_notes
            )
            st.success(f"✅ [{archive_id}] 전략이 히스토리에 성공적으로 보관되었습니다!")
            time.sleep(1)
            st.rerun()


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

