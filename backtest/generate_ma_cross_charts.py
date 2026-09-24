import os
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import pandas as pd
import numpy as np

# Set Korean Font
plt.rcParams['font.family'] = 'AppleGothic'
plt.rcParams['axes.unicode_minus'] = False

ARTIFACT_DIR = "/Users/xyz/.gemini/antigravity/brain/21db25cc-51d5-4ea7-abf4-037895d4e75d"
os.makedirs(ARTIFACT_DIR, exist_ok=True)

# 1. Load Data
df = pd.read_csv('SOL_2year_1h.csv', index_col=0, parse_dates=True)
df['ma20'] = df['close'].rolling(20).mean()
df['ma100'] = df['close'].rolling(100).mean()

# Simulate 20/100 Cross
INITIAL_KRW = 1_000_000.0
MAX_EXPOSURE = 0.50
FEE = 0.0005
SLIP = 0.0005

cash = INITIAL_KRW
qty = 0.0
entry_price = 0.0
entry_time = None
trades = []
equity_series = []

for i in range(101, len(df)):
    prev = df.iloc[i-2]
    curr = df.iloc[i-1]
    bar = df.iloc[i]
    close = float(bar['close'])
    fill_p = float(bar['open'])
    ts = bar.name
    
    buy_sig = prev['ma20'] <= prev['ma100'] and curr['ma20'] > curr['ma100']
    sell_sig = prev['ma20'] >= prev['ma100'] and curr['ma20'] < curr['ma100']
    
    if qty == 0.0 and buy_sig:
        notional = INITIAL_KRW * MAX_EXPOSURE / (1 + FEE)
        fill = fill_p * (1 + SLIP)
        qty = notional / fill
        cash -= notional * (1 + FEE)
        entry_price = fill
        entry_time = ts
        trades.append({
            'type': 'BUY',
            'time': ts,
            'price': fill,
            'bar_idx': i
        })
    elif qty > 0.0 and sell_sig:
        revenue = qty * fill_p * (1 - FEE - SLIP)
        pnl = revenue - (qty * entry_price)
        pnl_pct = (fill_p - entry_price) / entry_price * 100
        cash += revenue
        trades.append({
            'type': 'SELL',
            'time': ts,
            'price': fill_p,
            'pnl': pnl,
            'pnl_pct': pnl_pct,
            'entry_time': entry_time,
            'entry_price': entry_price,
            'bar_idx': i
        })
        qty = 0.0
        entry_price = 0.0
        entry_time = None
        
    cur_eq = cash + qty * close
    equity_series.append({'time': ts, 'equity': cur_eq, 'in_pos': 1 if qty > 0 else 0})

eq_df = pd.DataFrame(equity_series).set_index('time')
eq_df['peak'] = eq_df['equity'].cummax()
eq_df['drawdown_pct'] = (eq_df['equity'] - eq_df['peak']) / eq_df['peak'] * 100

buy_trades = [t for t in trades if t['type'] == 'BUY']
sell_trades = [t for t in trades if t['type'] == 'SELL']

# ==============================================================================
# CHART 1: 전체 2개년 타임라인 (가격 & MA20/100, 진입/청산, 자산 곡선, 낙폭)
# ==============================================================================
fig, (ax1, ax2, ax3) = plt.subplots(3, 1, figsize=(16, 12), sharex=True, 
                                     gridspec_kw={'height_ratios': [2.5, 1.2, 1.0]})
fig.patch.set_facecolor('#ffffff')

# Subplot 1: Price and Moving Averages
ax1.plot(df.index, df['close'], label='SOL 가격 (KRW)', color='#64748b', linewidth=1.0, alpha=0.7)
ax1.plot(df.index, df['ma20'], label='MA 20 (단기 20시간 이평)', color='#f59e0b', linewidth=1.4)
ax1.plot(df.index, df['ma100'], label='MA 100 (중기 100시간 이평)', color='#3b82f6', linewidth=1.6)

# Highlight holding periods
for bt, st in zip(buy_trades, sell_trades):
    color = '#10b981' if st['pnl'] > 0 else '#ef4444'
    alpha = 0.12 if st['pnl'] > 0 else 0.18
    ax1.axvspan(bt['time'], st['time'], color=color, alpha=alpha)

# Mark entries and exits
b_times = [t['time'] for t in buy_trades]
b_prices = [t['price'] for t in buy_trades]
s_times = [t['time'] for t in sell_trades]
s_prices = [t['price'] for t in sell_trades]

ax1.scatter(b_times, b_prices, marker='^', color='#10b981', s=55, label='골든크로스 매수 진입 (MA20 > MA100)', zorder=5)
ax1.scatter(s_times, s_prices, marker='v', color='#ef4444', s=55, label='데드크로스 매도 청산 (MA20 < MA100)', zorder=5)

ax1.set_title("솔라나(KRW-SOL) 20/100 이동평균 교차 전략 2개년 매매 궤적 (진입·청산 및 포지션 묶임)", fontsize=15, fontweight='bold', pad=12)
ax1.set_ylabel("가격 (KRW)", fontsize=11, fontweight='bold')
ax1.grid(True, linestyle='--', alpha=0.4)
ax1.legend(loc='upper left', frameon=True, facecolor='white', framealpha=0.9, fontsize=10)
ax1.yaxis.set_major_formatter(matplotlib.ticker.StrMethodFormatter('{x:,.0f}'))

# Subplot 2: Equity Curve
ax2.plot(eq_df.index, eq_df['equity'], color='#2563eb', linewidth=1.5, label='20/100 전략 평가금액 (초기 100만 원)')
ax2.axhline(INITIAL_KRW, color='#94a3b8', linestyle=':', label='원금 기준선 (100만 원)')
ax2.set_ylabel("평가금액 (KRW)", fontsize=11, fontweight='bold')
ax2.grid(True, linestyle='--', alpha=0.4)
ax2.legend(loc='upper left', frameon=True, facecolor='white', framealpha=0.9, fontsize=10)
ax2.yaxis.set_major_formatter(matplotlib.ticker.StrMethodFormatter('{x:,.0f}'))

# Subplot 3: Drawdown Curve
ax3.plot(eq_df.index, eq_df['drawdown_pct'], color='#dc2626', linewidth=1.2, label='고점 대비 낙폭 (Drawdown %)')
ax3.fill_between(eq_df.index, eq_df['drawdown_pct'], 0, color='#ef4444', alpha=0.25)
ax3.axhline(-35.28, color='#991b1b', linestyle='--', linewidth=1.2, label='최대 낙폭 (-35.28% MDD)')
ax3.set_ylabel("낙폭 (%)", fontsize=11, fontweight='bold')
ax3.set_xlabel("시간 (KST)", fontsize=11, fontweight='bold')
ax3.grid(True, linestyle='--', alpha=0.4)
ax3.legend(loc='lower left', frameon=True, facecolor='white', framealpha=0.9, fontsize=10)

# Format Date
ax3.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m'))
ax3.xaxis.set_major_locator(mdates.MonthLocator(interval=2))
fig.autofmt_xdate()

plt.tight_layout()
chart1_path = os.path.join(ARTIFACT_DIR, "ma20_100_sol_overview.png")
plt.savefig(chart1_path, dpi=200)
plt.close()
print(f"Saved: {chart1_path}")

# ==============================================================================
# CHART 2: 핵심 메커니즘 줌인 (진입/매도 및 고점 물림/횡보 휩소 3대 케이스)
# ==============================================================================
fig, (c_ax1, c_ax2, c_ax3) = plt.subplots(3, 1, figsize=(16, 14))
fig.patch.set_facecolor('#ffffff')

# ------------------------------------------------------------------------------
# Case 1: 성공적인 대세 상승 추세 추종 (2024년 11월 랠리 구간)
# ------------------------------------------------------------------------------
t1_start = pd.Timestamp("2024-11-04")
t1_end = pd.Timestamp("2024-11-18")
df1 = df.loc[t1_start:t1_end]

c_ax1.plot(df1.index, df1['close'], label='SOL 가격', color='#334155', linewidth=1.4)
c_ax1.plot(df1.index, df1['ma20'], label='MA 20 (단기 20시간)', color='#f59e0b', linewidth=1.6)
c_ax1.plot(df1.index, df1['ma100'], label='MA 100 (중기 100시간)', color='#3b82f6', linewidth=1.8)

b1 = [t for t in buy_trades if t1_start <= t['time'] <= t1_end]
s1 = [t for t in sell_trades if t1_start <= t['time'] <= t1_end]
if b1 and s1:
    c_ax1.axvspan(b1[0]['time'], s1[0]['time'], color='#10b981', alpha=0.15, label='포지션 보유 기간 (추세 완주)')
    c_ax1.scatter([b1[0]['time']], [b1[0]['price']], marker='^', color='#10b981', s=130, zorder=6, label=f"골든크로스 매수 ({b1[0]['price']:,.0f}원)")
    c_ax1.scatter([s1[0]['time']], [s1[0]['price']], marker='v', color='#ef4444', s=130, zorder=6, label=f"데드크로스 매도 ({s1[0]['price']:,.0f}원, +{s1[0]['pnl_pct']:.1f}%)")

c_ax1.set_title("【Case 1. 진입과 매도: 왜 2년 총수익이 높았는가?】 대세 상승장에서 추세를 길게 추종하며 큰 수익 포착 (+27.6%)", fontsize=13, fontweight='bold', color='#0f766e')
c_ax1.set_ylabel("가격 (KRW)", fontsize=10, fontweight='bold')
c_ax1.grid(True, linestyle='--', alpha=0.4)
c_ax1.legend(loc='upper left', frameon=True, facecolor='white', framealpha=0.9, fontsize=9)
c_ax1.yaxis.set_major_formatter(matplotlib.ticker.StrMethodFormatter('{x:,.0f}'))
c_ax1.xaxis.set_major_formatter(mdates.DateFormatter('%m-%d %H시'))

# ------------------------------------------------------------------------------
# Case 2: 고점 폭락 및 지연 매도로 인한 물림/묶임 (2025년 1월 최고점 급락 구간)
# ------------------------------------------------------------------------------
t2_start = pd.Timestamp("2025-01-14")
t2_end = pd.Timestamp("2025-01-25")
df2 = df.loc[t2_start:t2_end]

c_ax2.plot(df2.index, df2['close'], label='SOL 가격', color='#334155', linewidth=1.4)
c_ax2.plot(df2.index, df2['ma20'], label='MA 20 (단기 20시간)', color='#f59e0b', linewidth=1.6)
c_ax2.plot(df2.index, df2['ma100'], label='MA 100 (중기 100시간)', color='#3b82f6', linewidth=1.8)

b2 = [t for t in buy_trades if t2_start <= t['time'] <= t2_end]
s2 = [t for t in sell_trades if t2_start <= t['time'] <= t2_end]
if b2 and s2:
    c_ax2.axvspan(b2[0]['time'], s2[0]['time'], color='#ef4444', alpha=0.15, label='포지션 묶임 기간 (최고점 45.5만 -> 36만 폭락 방치)')
    c_ax2.scatter([b2[0]['time']], [b2[0]['price']], marker='^', color='#10b981', s=130, zorder=6, label=f"골든크로스 매수 ({b2[0]['price']:,.0f}원)")
    c_ax2.scatter([s2[0]['time']], [s2[0]['price']], marker='v', color='#ef4444', s=130, zorder=6, label=f"데드크로스 지연 매도 ({s2[0]['price']:,.0f}원)")
    
    # Annotate peak and lag
    peak_t = pd.Timestamp("2025-01-19 19:00:00")
    peak_p = 455000.0
    c_ax2.annotate(f"최고점 455,000원 (+62% 수익)\n이평선 데드크로스 안 떠서 매도 불가!", 
                   xy=(peak_t, peak_p), xytext=(peak_t - pd.Timedelta(hours=45), 445000),
                   arrowprops=dict(facecolor='black', arrowstyle='->', lw=1.5),
                   fontsize=9.5, fontweight='bold', color='#991b1b',
                   bbox=dict(boxstyle="round,pad=0.3", fc="#fef2f2", ec="#ef4444", lw=1))
    
    c_ax2.annotate(f"이평선 후행성(Lag)으로 고점 대비 -39% 폭락하는 내내 묶여있다가\n바닥 찍고 나서야 뒤늦게 청산 (수익 절반 강제 반납)", 
                   xy=(s2[0]['time'], s2[0]['price']), xytext=(s2[0]['time'] - pd.Timedelta(hours=65), 310000),
                   arrowprops=dict(facecolor='#ef4444', arrowstyle='->', lw=1.5),
                   fontsize=9.5, fontweight='bold', color='#b91c1c',
                   bbox=dict(boxstyle="round,pad=0.3", fc="#fef2f2", ec="#b91c1c", lw=1))

c_ax2.set_ylim(270000, 475000)
c_ax2.set_title("【Case 2. 포지션 묶임: 왜 -35% MDD가 발생하는가?】 고점 폭락 시 이평선 지연(Lag)으로 손절 없이 방치되는 치명적 함정", fontsize=13, fontweight='bold', color='#b91c1c')
c_ax2.set_ylabel("가격 (KRW)", fontsize=10, fontweight='bold')
c_ax2.grid(True, linestyle='--', alpha=0.4)
c_ax2.legend(loc='upper left', frameon=True, facecolor='white', framealpha=0.9, fontsize=9)
c_ax2.yaxis.set_major_formatter(matplotlib.ticker.StrMethodFormatter('{x:,.0f}'))
c_ax2.xaxis.set_major_formatter(mdates.DateFormatter('%m-%d %H시'))

# ------------------------------------------------------------------------------
# Case 3: 횡보장 잦은 휩소 (Whipsaw)로 연속 손실 (2025년 4월 구간)
# ------------------------------------------------------------------------------
t3_start = pd.Timestamp("2025-04-01")
t3_end = pd.Timestamp("2025-04-20")
df3 = df.loc[t3_start:t3_end]

c_ax3.plot(df3.index, df3['close'], label='SOL 가격', color='#334155', linewidth=1.4)
c_ax3.plot(df3.index, df3['ma20'], label='MA 20 (단기 20시간)', color='#f59e0b', linewidth=1.6)
c_ax3.plot(df3.index, df3['ma100'], label='MA 100 (중기 100시간)', color='#3b82f6', linewidth=1.8)

b3 = [t for t in buy_trades if t3_start <= t['time'] <= t3_end]
s3 = [t for t in sell_trades if t3_start <= t['time'] <= t3_end]

for b, s in zip(b3, s3):
    c_ax3.axvspan(b['time'], s['time'], color='#ef4444', alpha=0.18)
    c_ax3.scatter([b['time']], [b['price']], marker='^', color='#10b981', s=110, zorder=6)
    c_ax3.scatter([s['time']], [s['price']], marker='v', color='#ef4444', s=110, zorder=6)
    mid_time = b['time'] + (s['time'] - b['time'])/2
    y_pos = max(b['price'], s['price']) + 3500
    c_ax3.text(mid_time, y_pos, f"{s['pnl_pct']:+.1f}%", 
               ha='center', fontsize=9.5, fontweight='bold', color='#b91c1c')

c_ax3.set_title("【Case 3. 횡보장 휩소: 왜 승률이 30%에 불과한가?】 반등 끝자락 상투 매수 -> 바닥 손절이 반복되는 휩소(Whipsaw) 늪", fontsize=13, fontweight='bold', color='#c2410c')
c_ax3.set_ylabel("가격 (KRW)", fontsize=10, fontweight='bold')
c_ax3.set_xlabel("시간 (KST)", fontsize=10, fontweight='bold')
c_ax3.grid(True, linestyle='--', alpha=0.4)
c_ax3.legend(loc='upper left', frameon=True, facecolor='white', framealpha=0.9, fontsize=9)
c_ax3.yaxis.set_major_formatter(matplotlib.ticker.StrMethodFormatter('{x:,.0f}'))
c_ax3.xaxis.set_major_formatter(mdates.DateFormatter('%m-%d %H시'))

plt.tight_layout()
chart2_path = os.path.join(ARTIFACT_DIR, "ma20_100_case_studies.png")
plt.savefig(chart2_path, dpi=200)
plt.close()
print(f"Saved updated: {chart2_path}")
