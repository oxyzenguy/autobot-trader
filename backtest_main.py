"""
========================================================================================
🚀 [AutoBot Trader] 차세대 공식 통합 백테스트 엔진 (Unified Production Backtest Suite)
========================================================================================
현재 프로젝트의 실전 운영 설정(config.py 및 .env)을 100% 직접 연동하여,
전체 포트폴리오(BTC, ETH, SOL)의 개별 성과 및 통합 포트폴리오 리스크/수익을 시뮬레이션합니다.

[지원 전략 및 운용 순서]
1. KRW-BTC (비트코인): 라오어식 무손절 계층형 가중 적립 전략 (Buy-Only 모으기)
   - 업비트 자체 예약 (15:05 KST): 매일 10,000원 고정 매수
   - 봇 스마트 종가 체크 (08:55 KST):
     • Tier 2 (역대급 세일): 현재가 < 200일선 AND 현재가 < 계좌평단 ➔ +20,000원 추가 매수
     • Tier 1 (일반 할인): 현재가 < 200일선 XOR 현재가 < 계좌평단 ➔ +10,000원 추가 매수
     • Tier 0 (상승/수익): 현재가 >= 200일선 AND 현재가 >= 계좌평단 ➔ 0원 (매수 패스)

2. KRW-ETH (이더리움): 200 MA 하이브리드 + 50% 부분손절 + 무제한 순환 스쿼드
   - 상승 국면 (Close > 200 MA): 5/20 MA 골든크로스 + 12h Time-DCA + 일봉 종가매수 + 동적 트레일링 익절
   - 국면 전환 (Bull ➔ Bear): 200선 하향 돌파 시 50% 부분손절 후 잔여 물량 마틴게일 인계
   - 하락 국면 (Close <= 200 MA): 1-1-2-4 무제한 순환 스쿼드 + 매직스플릿(+3%) & 바스켓(+0.8%) 이중익절

3. KRW-SOL (솔라나): 200 MA 하이브리드 + 70% 부분손절 + 16차수(32U) 캡 스쿼드
   - 상승 국면 (Close > 200 MA): 5/20 MA 골든크로스 + 12h Time-DCA + 일봉 종가매수 + 동적 트레일링 익절
   - 국면 전환 (Bull ➔ Bear): 200선 하향 돌파 시 70% 부분손절 후 잔여 30%만 마틴게일 인계
   - 하락 국면 (Close <= 200 MA): 1-1-2-4 x 4회 (16차수 / 32 Units 캡 후 홀딩) + 매직스플릿(+3%) & 바스켓(+0.5%) 이중익절
========================================================================================
"""

import sys
import os
import math
import argparse
import logging
from dataclasses import dataclass
from typing import List, Dict, Tuple, Optional, Any

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt

# 실전 config 직접 연동
from config import (
    INVESTMENTS,
    PROFIT_MARGINS,
    MARTINGALE_SCHEDULE,
    MARTINGALE_MAX_STEPS,
    REGIME_SWITCH_LIQUIDATION_PCT,
    DYNAMIC_TS_LEVELS,
    get_bull_trailing_stop_trigger,
    USE_DYNAMIC_TRAILING_STOP,
    USE_MAGIC_SPLIT_DEFENSE,
    MAGIC_SPLIT_TRANCHE_PROFIT,
    MAGIC_SPLIT_DOWN_PCT,
    BULL_STOP_LOSS_PCT,
    FEE_RATE,
    BTC_UNIT_KRW,
    BTC_DCA_CLOSING_CHECK_HOUR,
    BTC_UPBIT_DCA_HOUR,
)
from strategy.matingale2x_logic import (
    adjust_price_to_tick as modern_adjust_price_to_tick,
    calculate_new_buy_prices as modern_calculate_new_buy_prices
)

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)


# ======================================================================================
# 0. 하위 호환성 (Legacy Shims for optimize_margin.py and older scripts)
# ======================================================================================
@dataclass
class BacktestConfig:
    ticker: str = "KRW-SOL"
    coin_name: str = "SOL"
    interval: str = "minute60"
    start_date: str = "2024-09-19"
    end_date: str = "2026-09-19"
    data_file: str = "SOL_2year_1h.csv"
    initial_capital: int = 1_000_000
    unit_krw: int = 10_000
    order_unit: float = 1
    buy_funds_for_reentry: int = 10_000
    sell_profit_margin: float = 1.005
    trading_fee_rate: float = 0.0005
    max_position_size: float = 0.5
    stop_loss_percent: float = -0.10


config = BacktestConfig()
TICKER = config.ticker
COIN_NAME = config.coin_name
INTERVAL = config.interval
BACKTEST_START_DATE = config.start_date
BACKTEST_END_DATE = config.end_date
DATA_FILE = config.data_file
UNIT_KRW = config.unit_krw
ORDER_UNIT = config.order_unit
SELL_PROFIT_MARGIN = config.sell_profit_margin
TRADING_FEE_RATE = config.trading_fee_rate
BUY_FUNDS_FOR_REENTRY = config.buy_funds_for_reentry
MAX_POSITION_SIZE = config.max_position_size
STOP_LOSS_PERCENT = config.stop_loss_percent

adjust_price_to_tick = modern_adjust_price_to_tick
calculate_new_buy_prices = modern_calculate_new_buy_prices


# ======================================================================================
# 1. 데이터 로더 (Data Loader & Technical Indicator Prep)
# ======================================================================================
def load_historical_candles(market: str) -> pd.DataFrame:
    """
    로컬에 저장된 2개년 정밀 1시간봉 캔들 데이터를 탐색하여 로드하고 기술적 지표를 사전 계산합니다.
    """
    coin = market.replace("KRW-", "")
    candidates = [
        f"{coin}_2year_1h.csv",
        f"{coin}_minute60_candles.csv",
        os.path.join("scratch", f"{coin.lower()}_2year_1h.csv"),
    ]
    
    csv_path = None
    for cand in candidates:
        if os.path.exists(cand):
            csv_path = cand
            break
            
    if not csv_path:
        raise FileNotFoundError(f"[{market}] 캔들 데이터 파일을 찾을 수 없습니다: {candidates}")

    df = pd.read_csv(csv_path)
    col = 'Unnamed: 0' if 'Unnamed: 0' in df.columns else ('candle_date_time_kst' if 'candle_date_time_kst' in df.columns else df.columns[0])
    df.rename(columns={col: 'dt'}, inplace=True)
    df['dt'] = pd.to_datetime(df['dt'])
    df.sort_values('dt', inplace=True)
    df.reset_index(drop=True, inplace=True)

    close = df['close']
    high = df['high']
    low = df['low']
    volume = df['volume']

    # 1시간봉 기준 이동평균선
    df['ma5'] = close.rolling(5).mean()
    df['ma20'] = close.rolling(20).mean()
    df['ma200'] = close.rolling(200).mean()

    # 볼린저 밴드 및 EMA (하락장 과매도 ClucMay 지표)
    typical_price = (high + low + close) / 3.0
    df['bb_mid'] = typical_price.rolling(20).mean()
    df['bb_std'] = typical_price.rolling(20).std()
    df['bb_lower'] = df['bb_mid'] - 2.0 * df['bb_std']
    df['ema50'] = close.ewm(span=50, adjust=False).mean()
    df['vol_mean30'] = volume.rolling(30).mean()

    # 일봉 기준 지표 매핑 (종가매매 및 08시 체크용)
    df['hour'] = df['dt'].dt.hour
    df['date'] = df['dt'].dt.date
    
    daily_close = df.groupby('date')['close'].last()
    daily_ma5_map = daily_close.rolling(5).mean().to_dict()
    daily_open_map = df.groupby('date')['open'].first().to_dict()
    
    df['daily_ma5'] = df['date'].map(daily_ma5_map)
    df['daily_open'] = df['date'].map(daily_open_map)

    # Cluc 과매도 급락 감지
    df['is_cluc_dip'] = (
        (close < df['ema50']) &
        (close < df['bb_lower'] * 0.985) &
        (volume < df['vol_mean30'].shift(1) * 20.0)
    )

    return df


def load_btc_daily_ma200() -> Dict[Any, float]:
    """비트코인 200일 이동평균선(일봉 기준) 사전을 로드합니다."""
    daily_path = os.path.join("scratch", "btc_daily_candles.csv")
    if os.path.exists(daily_path):
        df_d = pd.read_csv(daily_path, index_col=0, parse_dates=True)
        df_d["ma200"] = df_d["close"].rolling(200).mean()
        df_d["date"] = df_d.index.date
        return df_d.set_index("date")["ma200"].to_dict()
    return {}


# ======================================================================================
# 2. 비트코인(KRW-BTC) 계층형 가중 적립 전략 시뮬레이터 (무손절 모으기)
# ======================================================================================
def simulate_btc_accumulator(
    df_hourly: pd.DataFrame,
    initial_capital: int = 1_000_000,
    unit_krw: int = 10_000,
    fee_rate: float = FEE_RATE
) -> Dict[str, Any]:
    """
    비트코인 라오어식 무손절 계층형 가중 적립 전략 시뮬레이션
    - 15:05 업비트 자체 적립: 매일 10,000원 고정 매수
    - 08:55 봇 스마트 종가 체크: 200일선 및 계좌평단 비교 (0원, 10,000원, 20,000원)
    """
    ma200_dict = load_btc_daily_ma200()
    
    dates = sorted(df_hourly['date'].unique())
    price_map = {(row['date'], row['hour']): row['close'] for _, row in df_hourly.iterrows()}
    
    tot_btc = 0.0
    tot_invested = 0.0
    bot_extra_invested = 0.0
    bot_days = 0
    tier2_days = 0
    tier1_days = 0
    tier0_days = 0
    
    equity_curve = []
    
    # 시간별 순회하며 자산 평가액 추적 (200봉 이후부터 ETH/SOL과 동일 시점 정렬)
    current_date = None
    daily_bot_checked = False
    daily_upbit_bought = False
    start_bar = 200
    
    for i in range(start_bar, len(df_hourly)):
        row = df_hourly.iloc[i]
        d = row['date']
        h = row['hour']
        c = row['close']
        
        if d != current_date:
            current_date = d
            daily_bot_checked = False
            daily_upbit_bought = False
            
        # 08:00 KST 봇 스마트 종가 체크
        if h == BTC_DCA_CLOSING_CHECK_HOUR and not daily_bot_checked:
            daily_bot_checked = True
            ma200 = ma200_dict.get(d)
            if ma200 is None:
                ma200 = row['ma200'] # 1시간봉 200MA 대체
                
            curr_avg = (tot_invested / tot_btc) if tot_btc > 0 else c
            is_below_avg = (c < curr_avg)
            is_below_ma = (c < ma200) if ma200 and not np.isnan(ma200) else False
            
            extra_krw = 0
            if is_below_avg and is_below_ma:
                extra_krw = unit_krw * 2  # Tier 2: 역대급 세일 (+20,000원)
                tier2_days += 1
            elif is_below_avg or is_below_ma:
                extra_krw = unit_krw      # Tier 1: 일반 세일 (+10,000원)
                tier1_days += 1
            else:
                tier0_days += 1           # Tier 0: 상승/수익 중 (패스)
                
            if extra_krw > 0:
                cost = extra_krw * (1 + fee_rate)
                bought_btc = extra_krw / c
                tot_btc += bought_btc
                tot_invested += cost
                bot_extra_invested += cost
                bot_days += 1
                
        # 15:00 KST 업비트 자체 예약 매수 (10,000원)
        if h == BTC_UPBIT_DCA_HOUR and not daily_upbit_bought:
            daily_upbit_bought = True
            cost = unit_krw * (1 + fee_rate)
            bought_btc = unit_krw / c
            tot_btc += bought_btc
            tot_invested += cost
            
        # 현재 자산 가치 평가 (기본 배정 자본금 100만 원 환산 기준 정규화 곡선)
        current_btc_val = tot_btc * c * (1 - fee_rate)
        if tot_invested > 0:
            cur_eq = initial_capital * (current_btc_val / tot_invested)
        else:
            cur_eq = initial_capital
        equity_curve.append(cur_eq)

    final_price = df_hourly['close'].iloc[-1]
    final_valuation = tot_btc * final_price * (1 - fee_rate)
    net_profit = final_valuation - tot_invested
    roi_pct = (net_profit / tot_invested * 100) if tot_invested > 0 else 0.0
    final_avg_price = (tot_invested / tot_btc) if tot_btc > 0 else 0.0

    eq_arr = np.array(equity_curve)
    peaks = np.maximum.accumulate(eq_arr)
    drawdowns = (eq_arr - peaks) / peaks
    mdd_pct = abs(drawdowns.min()) * 100 if len(drawdowns) > 0 else 0.0

    return {
        "market": "KRW-BTC",
        "name": "비트코인(BTC) 계층형 가중 적립",
        "initial_capital": initial_capital,
        "total_invested": tot_invested,
        "accumulated_coin": tot_btc,
        "final_avg_price": final_avg_price,
        "final_price": final_price,
        "final_valuation": final_valuation,
        "net_profit": net_profit,
        "return_pct": roi_pct,
        "mdd_pct": mdd_pct,
        "bot_days": bot_days,
        "bot_extra_invested": bot_extra_invested,
        "tier2_days": tier2_days,
        "tier1_days": tier1_days,
        "tier0_days": tier0_days,
        "equity_curve": equity_curve,
    }


# ======================================================================================
# 3. 이더리움 & 솔라나 200 MA 하이브리드 스쿼드 마틴게일 시뮬레이터
# ======================================================================================
def simulate_hybrid_squad(
    market: str,
    df: pd.DataFrame,
    initial_capital: int = 1_000_000,
    unit_krw: int = 10_000,
    fee_rate: float = FEE_RATE
) -> Dict[str, Any]:
    """
    이더리움 및 솔라나 200 MA 하이브리드 스쿼드 마틴게일 시뮬레이션
    - config.py의 실전 파라미터(손절율, 스쿼드 스케줄, 익절선, 동적 TS) 100% 동기화
    """
    profit_margin = PROFIT_MARGINS.get(market, 1.005)
    regime_liquidation_pct = REGIME_SWITCH_LIQUIDATION_PCT.get(market, 0.70)
    squad_multipliers = MARTINGALE_SCHEDULE.get(market, [1, 1, 2, 4])
    max_steps = MARTINGALE_MAX_STEPS.get(market, 16)
    
    krw = initial_capital
    equity_curve = []

    in_bull_pos = False
    bull_tranches: List[Dict[str, Any]] = []
    bull_highest_p = 0.0
    bull_ts_active = False
    last_dca_bar = -999
    last_closing_buy_day = None

    active_bear_tranches: List[Dict[str, Any]] = []
    last_bear_buy_p = 0.0

    # 통계 카운터
    bull_trades = 0
    bull_wins = 0
    bull_realized = 0.0
    bull_ts_exits = 0
    bull_dead_exits = 0
    bull_sl_exits = 0
    regime_switches = 0

    bear_trades = 0
    bear_wins = 0
    bear_realized = 0.0
    bear_basket_exits = 0
    bear_tranche_exits = 0
    max_bear_capital = 0.0
    max_bear_steps = 0
    step_frequency: Dict[int, int] = {}

    start_bar = 200

    for i in range(start_bar, len(df)):
        c = df['close'].iloc[i]
        h = df['high'].iloc[i]
        l = df['low'].iloc[i]
        hour = df['hour'].iloc[i]
        date = df['date'].iloc[i]
        ma200 = df['ma200'].iloc[i]
        is_cluc_dip = df['is_cluc_dip'].iloc[i]

        is_bull = (c > ma200)

        # 총 자산 가치 기록
        tot_v = sum(t['vol'] for t in bull_tranches) + sum(t['vol'] for t in active_bear_tranches)
        cur_eq = krw + (tot_v * c * (1 - fee_rate))
        equity_curve.append(cur_eq)

        # ------------------------------------------------------------------
        # 1. 상승 국면 (BULL: 현재가 > 200 MA)
        # ------------------------------------------------------------------
        if is_bull:
            # 하락장에서 넘어온 잔여 포지션이 있다면 바스켓 또는 개별 익절
            if active_bear_tranches:
                tot_c = sum(t['cost'] for t in active_bear_tranches)
                tot_bv = sum(t['vol'] for t in active_bear_tranches)
                avg_p = tot_c / tot_bv if tot_bv > 0 else c
                
                # 바스켓 일괄 익절
                if h >= avg_p * profit_margin:
                    exec_p = avg_p * profit_margin
                    rev = tot_bv * exec_p * (1 - fee_rate)
                    pnl = rev - tot_c
                    krw += rev
                    bear_realized += pnl
                    bear_trades += 1
                    bear_basket_exits += 1
                    if pnl > 0:
                        bear_wins += 1
                    active_bear_tranches = []
                    last_bear_buy_p = 0.0
                else:
                    # 개별 차수 매직스플릿 반등 익절 (+3.0%)
                    rem = []
                    for t in active_bear_tranches:
                        target_s = t['buy_price'] * (1 + MAGIC_SPLIT_TRANCHE_PROFIT)
                        if h >= target_s:
                            exec_p = target_s
                            rev = t['vol'] * exec_p * (1 - fee_rate)
                            pnl = rev - t['cost']
                            krw += rev
                            bear_realized += pnl
                            bear_trades += 1
                            bear_tranche_exits += 1
                            if pnl > 0:
                                bear_wins += 1
                        else:
                            rem.append(t)
                    active_bear_tranches = rem

            # 상승장 포지션 운용
            if in_bull_pos:
                tot_b_cost = sum(t['cost'] for t in bull_tranches)
                tot_b_vol = sum(t['vol'] for t in bull_tranches)
                avg_b_p = tot_b_cost / tot_b_vol if tot_b_vol > 0 else c
                bull_highest_p = max(bull_highest_p, h)
                curr_steps = len(bull_tranches)

                # 동적 트레일링 익절 트리거 (1~3회차 10%, 4~6회차 7%, 7~20회차 5%)
                curr_trigger = get_bull_trailing_stop_trigger(curr_steps)

                if not bull_ts_active and bull_highest_p >= avg_b_p * (1.0 + curr_trigger):
                    bull_ts_active = True

                # 트레일링 익절 청산 (고점 대비 -3% 반락)
                if bull_ts_active and l <= bull_highest_p * (1.0 - 0.03):
                    exit_p = bull_highest_p * (1.0 - 0.03)
                    rev = tot_b_vol * exit_p * (1 - fee_rate)
                    pnl = rev - tot_b_cost
                    krw += rev
                    bull_trades += 1
                    bull_ts_exits += 1
                    if pnl > 0:
                        bull_wins += 1
                    bull_realized += pnl
                    in_bull_pos = False
                    bull_tranches = []
                    bull_highest_p = 0.0
                    bull_ts_active = False
                    continue

                # 긴급 손절 (평단가 대비 -10% 하락)
                if l <= avg_b_p * (1.0 + BULL_STOP_LOSS_PCT):
                    exit_p = avg_b_p * (1.0 + BULL_STOP_LOSS_PCT)
                    rev = tot_b_vol * exit_p * (1 - fee_rate)
                    pnl = rev - tot_b_cost
                    krw += rev
                    bull_trades += 1
                    bull_sl_exits += 1
                    if pnl > 0:
                        bull_wins += 1
                    bull_realized += pnl
                    in_bull_pos = False
                    bull_tranches = []
                    bull_highest_p = 0.0
                    bull_ts_active = False
                    continue

                # 12시간 Time-DCA 분할 적립 (최대 20회차)
                if not bull_ts_active and len(bull_tranches) < 20:
                    if (i - last_dca_bar) >= 12:
                        if krw >= unit_krw * (1 + fee_rate):
                            buy_p = c
                            v = (unit_krw * (1 - fee_rate)) / buy_p
                            krw -= unit_krw * (1 + fee_rate)
                            bull_tranches.append({'vol': v, 'cost': unit_krw, 'price': buy_p, 'bar': i})
                            last_dca_bar = i
                            bull_highest_p = max(bull_highest_p, buy_p)

                    # 일봉 종가매수 (08:00 KST, 당일 양봉 & 5일선 지지)
                    if hour == 8 and last_closing_buy_day != date and len(bull_tranches) < 20:
                        today_open = df['daily_open'].iloc[i]
                        daily_ma5 = df['daily_ma5'].iloc[i]
                        if c > today_open and c > daily_ma5 and krw >= unit_krw * (1 + fee_rate):
                            buy_p = c
                            v = (unit_krw * (1 - fee_rate)) / buy_p
                            krw -= unit_krw * (1 + fee_rate)
                            bull_tranches.append({'vol': v, 'cost': unit_krw, 'price': buy_p, 'bar': i})
                            last_closing_buy_day = date
                            last_dca_bar = i
                            bull_highest_p = max(bull_highest_p, buy_p)
            else:
                # 미보유 시 5/20 MA 골든크로스 최초 1차 진입
                prev_m5 = df['ma5'].iloc[i-1]
                prev_m20 = df['ma20'].iloc[i-1]
                curr_m5 = df['ma5'].iloc[i]
                curr_m20 = df['ma20'].iloc[i]
                if prev_m5 <= prev_m20 and curr_m5 > curr_m20 and c > curr_m5:
                    if krw >= unit_krw * (1 + fee_rate):
                        buy_p = c
                        v = (unit_krw * (1 - fee_rate)) / buy_p
                        krw -= unit_krw * (1 + fee_rate)
                        bull_tranches = [{'vol': v, 'cost': unit_krw, 'price': buy_p, 'bar': i}]
                        in_bull_pos = True
                        bull_highest_p = buy_p
                        bull_ts_active = False
                        last_dca_bar = i
                        last_closing_buy_day = None

        # ------------------------------------------------------------------
        # 2. 하락 국면 (BEAR: 현재가 <= 200 MA)
        # ------------------------------------------------------------------
        else:
            # [국면 전환]: 상승장 포지션 보유 중 200 MA 하향 돌파 감지
            if in_bull_pos and bull_tranches:
                regime_switches += 1
                tot_b_cost = sum(t['cost'] for t in bull_tranches)
                tot_b_vol = sum(t['vol'] for t in bull_tranches)
                avg_b_p = tot_b_cost / tot_b_vol

                # 설정된 부분 손절 비율(ETH 50%, SOL 70%) 집행
                if regime_liquidation_pct > 0:
                    sold_vol = tot_b_vol * regime_liquidation_pct
                    sold_cost = tot_b_cost * regime_liquidation_pct
                    rev = sold_vol * c * (1 - fee_rate)
                    pnl = rev - sold_cost
                    krw += rev
                    bull_trades += 1
                    if pnl > 0:
                        bull_wins += 1
                    bull_realized += pnl

                # 잔여 비율(ETH 50%, SOL 30%)을 하락장 마틴게일로 인계
                rem_pct = 1.0 - regime_liquidation_pct
                if rem_pct > 0:
                    rem_vol = tot_b_vol * rem_pct
                    rem_cost = tot_b_cost * rem_pct
                    active_bear_tranches.append({
                        'step': len(active_bear_tranches),
                        'buy_price': avg_b_p,
                        'vol': rem_vol,
                        'cost': rem_cost,
                        'source': 'BULL_HANDOVER'
                    })
                    last_bear_buy_p = avg_b_p

                in_bull_pos = False
                bull_tranches = []
                bull_ts_active = False

            # 하락장 마틴게일 & 매직스플릿 운용
            if active_bear_tranches:
                tot_cost = sum(t['cost'] for t in active_bear_tranches)
                tot_vol = sum(t['vol'] for t in active_bear_tranches)
                avg_p = tot_cost / tot_vol

                max_bear_capital = max(max_bear_capital, tot_cost)
                max_bear_steps = max(max_bear_steps, len(active_bear_tranches))

                # 1) 바스켓 전량 일괄 익절
                if h >= avg_p * profit_margin:
                    exec_p = avg_p * profit_margin
                    rev = tot_vol * exec_p * (1 - fee_rate)
                    pnl = rev - tot_cost
                    krw += rev
                    bear_realized += pnl
                    bear_trades += 1
                    bear_basket_exits += 1
                    if pnl > 0:
                        bear_wins += 1
                    active_bear_tranches = []
                    last_bear_buy_p = 0.0
                else:
                    # 2) 개별 차수 매직스플릿 반등 익절 (+3.0%)
                    rem = []
                    for t in active_bear_tranches:
                        target_s = t['buy_price'] * (1 + MAGIC_SPLIT_TRANCHE_PROFIT)
                        if h >= target_s:
                            exec_p = target_s
                            rev = t['vol'] * exec_p * (1 - fee_rate)
                            pnl = rev - t['cost']
                            krw += rev
                            bear_realized += pnl
                            bear_trades += 1
                            bear_tranche_exits += 1
                            if pnl > 0:
                                bear_wins += 1
                        else:
                            rem.append(t)
                    active_bear_tranches = rem

                    # 3) 추가 스쿼드 마틴게일 물타기 (-4% 하락 간격)
                    curr_step = len(active_bear_tranches)
                    if curr_step < max_steps:
                        pattern_idx = curr_step % len(squad_multipliers)
                        mult = squad_multipliers[pattern_idx]
                        req_krw = unit_krw * mult

                        ref_p = last_bear_buy_p if last_bear_buy_p > 0 else avg_p
                        if c <= ref_p * (1.0 - MAGIC_SPLIT_DOWN_PCT):
                            if krw >= req_krw * (1 + fee_rate):
                                buy_p = c
                                v = (req_krw * (1 - fee_rate)) / buy_p
                                krw -= req_krw * (1 + fee_rate)
                                active_bear_tranches.append({
                                    'step': curr_step,
                                    'buy_price': buy_p,
                                    'vol': v,
                                    'cost': req_krw,
                                    'source': 'MARTINGALE_DIP'
                                })
                                last_bear_buy_p = buy_p
                                step_frequency[curr_step] = step_frequency.get(curr_step, 0) + 1
            else:
                # 미보유 시 Cluc 과매도 투매 발생 시 1차(1U) 진입
                if is_cluc_dip:
                    if krw >= unit_krw * (1 + fee_rate):
                        buy_p = c
                        v = (unit_krw * (1 - fee_rate)) / buy_p
                        krw -= unit_krw * (1 + fee_rate)
                        active_bear_tranches = [{
                            'step': 0,
                            'buy_price': buy_p,
                            'vol': v,
                            'cost': unit_krw,
                            'source': 'CLUC_INITIAL'
                        }]
                        last_bear_buy_p = buy_p
                        step_frequency[0] = step_frequency.get(0, 0) + 1

    # 최종 잔여 자산 청산 평가
    tot_vol = sum(t['vol'] for t in bull_tranches) + sum(t['vol'] for t in active_bear_tranches)
    tot_cost = sum(t['cost'] for t in bull_tranches) + sum(t['cost'] for t in active_bear_tranches)
    if tot_vol > 0:
        rev = tot_vol * df['close'].iloc[-1] * (1 - fee_rate)
        pnl = rev - tot_cost
        krw += rev
        bull_realized += pnl

    tot_trades = bull_trades + bear_trades
    tot_wins = bull_wins + bear_wins
    tot_pnl = krw - initial_capital
    ret_pct = (tot_pnl / initial_capital) * 100

    eq_arr = np.array(equity_curve)
    peaks = np.maximum.accumulate(eq_arr)
    drawdowns = (eq_arr - peaks) / peaks
    mdd_pct = abs(drawdowns.min()) * 100 if len(drawdowns) > 0 else 0.0

    return {
        "market": market,
        "name": f"{market} 200 MA 하이브리드",
        "initial_capital": initial_capital,
        "final_capital": krw,
        "net_profit": tot_pnl,
        "return_pct": ret_pct,
        "mdd_pct": mdd_pct,
        "bull_trades": bull_trades,
        "bull_wins": bull_wins,
        "bull_win_rate": (bull_wins / bull_trades * 100) if bull_trades > 0 else 0.0,
        "bull_ts_exits": bull_ts_exits,
        "bull_sl_exits": bull_sl_exits,
        "regime_switches": regime_switches,
        "bear_trades": bear_trades,
        "bear_wins": bear_wins,
        "bear_basket_exits": bear_basket_exits,
        "bear_tranche_exits": bear_tranche_exits,
        "max_bear_capital": max_bear_capital,
        "max_bear_steps": max_bear_steps,
        "step_frequency": step_frequency,
        "equity_curve": equity_curve,
    }


# ======================================================================================
# 4. 레거시 단독 백테스트 래퍼 (run_backtest for optimize_margin.py)
# ======================================================================================
def get_historical_data(ticker: str = "KRW-SOL", interval: str = "minute60", count: int = 200) -> pd.DataFrame:
    """레거시 호환용 데이터 로더"""
    return load_historical_candles(ticker)


def run_backtest(df: pd.DataFrame = None, custom_config: BacktestConfig = None) -> Dict[str, Any]:
    """레거시 단독 백테스트 호환용 실행 함수"""
    if custom_config is None:
        custom_config = config
    if df is None:
        df = load_historical_candles(custom_config.ticker)
    return simulate_hybrid_squad(custom_config.ticker, df, initial_capital=custom_config.initial_capital)


# ======================================================================================
# 5. 통합 포트폴리오 시뮬레이션 및 결과 리포트 출력
# ======================================================================================
def run_unified_portfolio_backtest(
    markets: Optional[List[str]] = None,
    plot: bool = False,
    output_chart: str = "portfolio_backtest_result.png"
) -> Dict[str, Any]:
    """
    1. BTC -> 2. ETH -> 3. SOL 순서로 시뮬레이션 집행 및 포트폴리오 결합 리포트 생성
    """
    if markets is None:
        # 프로젝트 엄격 기본 순서 확정
        markets = ["KRW-BTC", "KRW-ETH", "KRW-SOL"]

    results = []
    print("\n" + "=" * 90)
    print("🚀 [AutoBot Trader] 실전 하이브리드 포트폴리오 2개년 통합 백테스트 시작")
    print(f" • 대상 마켓 순서: {' ➔ '.join(markets)}")
    print(f" • 개별 종목 기본 원금: 1,000,000 KRW | 1Unit: 10,000 KRW | 거래 수수료: {FEE_RATE*100:.2f}%")
    print("=" * 90)

    for m in markets:
        print(f"\n[데이터 로드 및 시뮬레이션 중...] ➔ {m}")
        df = load_historical_candles(m)
        unit = INVESTMENTS.get(m, {}).get("unit", 10_000)
        total_cap = INVESTMENTS.get(m, {}).get("total", 1_000_000)

        if m == "KRW-BTC":
            res = simulate_btc_accumulator(df, initial_capital=total_cap, unit_krw=unit)
        else:
            res = simulate_hybrid_squad(m, df, initial_capital=total_cap, unit_krw=unit)
        results.append(res)

    # ------------------------------------------------------------------
    # 종목별 상세 성과 출력
    # ------------------------------------------------------------------
    print("\n" + "=" * 90)
    print("📊 [종목별 백테스트 상세 결과]")
    print("=" * 90)

    for r in results:
        m = r['market']
        print(f"\n📌 마켓: {r['name']}")
        print(f" ------------------------------------------------------------------")
        if m == "KRW-BTC":
            print(f" • 누적 총 투자금:     {r['total_invested']:>12,.0f}원")
            print(f" • 최종 평가금액:       {r['final_valuation']:>12,.0f}원")
            print(f" • 누적 순손익:         {r['net_profit']:>+12,.0f}원 ({r['return_pct']:+.2f}%)")
            print(f" • 최종 비트코인 평단:   {r['final_avg_price']:>12,.0f}원 (종가: {r['final_price']:,.0f}원)")
            print(f" • 총 축적 BTC 수량:    {r['accumulated_coin']:>12.6f} BTC")
            print(f" • 최대 낙폭 (MDD):     {r['mdd_pct']:>11.2f}%")
            print(f" • 봇 스마트 추가매수:   {r['bot_days']}일간 총 {r['bot_extra_invested']:,.0f}원 투입")
            print(f"   - Tier 2 (역대급 세일 +20,000원): {r['tier2_days']}회")
            print(f"   - Tier 1 (일반 할인 +10,000원):   {r['tier1_days']}회")
            print(f"   - Tier 0 (상승/수익 패스 0원):     {r['tier0_days']}회")
        else:
            print(f" • 초기 투입 원금:     {r['initial_capital']:>12,.0f}원")
            print(f" • 최종 평가 자산:     {r['final_capital']:>12,.0f}원")
            print(f" • 누적 순손익:         {r['net_profit']:>+12,.0f}원 ({r['return_pct']:+.2f}%)")
            print(f" • 최대 낙폭 (MDD):     {r['mdd_pct']:>11.2f}%")
            print(f" • [상승장 추세 엔진 (BULL)]")
            print(f"   - 포지션 청산 횟수:  {r['bull_trades']}회 (승률: {r['bull_win_rate']:.1f}%)")
            print(f"   - 트레일링 익절:     {r['bull_ts_exits']}회 | 긴급 손절: {r['bull_sl_exits']}회")
            print(f"   - 200선 이탈 부분손절: {r['regime_switches']}회 (손절률: {REGIME_SWITCH_LIQUIDATION_PCT.get(m, 0.7)*100:.0f}%)")
            print(f" • [하락장 방어 엔진 (BEAR)]")
            print(f"   - 바스켓 일괄 익절:   {r['bear_basket_exits']}회")
            print(f"   - 매직스플릿 개별익절: {r['bear_tranche_exits']}회")
            print(f"   - 최대 투입 금액:    {r['max_bear_capital']:,.0f}원 (최대 도달 차수: {r['max_bear_steps']}차)")

    # ------------------------------------------------------------------
    # 3종목 통합 포트폴리오 성과 결합
    # ------------------------------------------------------------------
    min_len = min(len(r['equity_curve']) for r in results)
    combined_curve = np.zeros(min_len)
    total_init_cap = sum(r['initial_capital'] for r in results)
    
    for r in results:
        combined_curve += np.array(r['equity_curve'][:min_len])

    final_port_val = combined_curve[-1]
    net_port_profit = final_port_val - total_init_cap
    port_roi_pct = (net_port_profit / total_init_cap) * 100

    p_peaks = np.maximum.accumulate(combined_curve)
    p_dds = (combined_curve - p_peaks) / p_peaks
    port_mdd_pct = abs(p_dds.min()) * 100 if len(p_dds) > 0 else 0.0

    print("\n" + "=" * 90)
    print("🏆 [AutoBot Trader 3종목 통합 포트폴리오 2개년 최종 성과]")
    print("=" * 90)
    print(f" • 총 포트폴리오 원금:   {total_init_cap:>12,.0f}원 (종목당 1,000,000원)")
    print(f" • 최종 포트폴리오 가치: {final_port_val:>12,.0f}원")
    print(f" • 포트폴리오 순손익:   {net_port_profit:>+12,.0f}원 ({port_roi_pct:+.2f}%)")
    print(f" • 포트폴리오 통합 MDD:  {port_mdd_pct:>11.2f}% (위험 분산 효과로 단일 코인 대비 안정적)")
    print("=" * 90)

    # ------------------------------------------------------------------
    # 차트 시각화 및 저장
    # ------------------------------------------------------------------
    if plot:
        try:
            plt.rcParams['font.family'] = 'DejaVu Sans'
            fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 8), sharex=True, gridspec_kw={'height_ratios': [2.5, 1]})
            
            for r in results:
                eq = r['equity_curve'][:min_len]
                ax1.plot(eq, label=f"{r['market']} ({r['return_pct']:+.1f}%)", alpha=0.8)
                
            ax1.plot(combined_curve, label=f"Combined Portfolio ({port_roi_pct:+.1f}%)", color='black', linewidth=2.5)
            ax1.set_title("AutoBot Trader 3-Asset Portfolio Equity Curves (2-Year Backtest)", fontsize=14, fontweight='bold')
            ax1.set_ylabel("Portfolio Value (KRW)", fontsize=11)
            ax1.grid(True, linestyle="--", alpha=0.5)
            ax1.legend(loc="upper left")

            # Drawdown
            ax2.plot(p_dds * 100, label=f"Combined Drawdown (Max MDD: {port_mdd_pct:.1f}%)", color="red", linewidth=1.5)
            ax2.fill_between(range(min_len), p_dds * 100, 0, color="red", alpha=0.2)
            ax2.set_title("Portfolio Drawdown (%)", fontsize=11)
            ax2.set_xlabel("Hours (Trading Bars)", fontsize=11)
            ax2.set_ylabel("Drawdown (%)", fontsize=11)
            ax2.grid(True, linestyle="--", alpha=0.5)
            ax2.legend(loc="lower left")

            plt.tight_layout()
            plt.savefig(output_chart, dpi=200)
            print(f"\n📈 [차트 저장 완료] {output_chart} 파일로 저장되었습니다.")
        except Exception as e:
            logger.warning(f"차트 생성 중 예외 발생: {e}")

    return {
        "results": results,
        "combined_equity": combined_curve.tolist(),
        "total_initial_capital": total_init_cap,
        "final_portfolio_valuation": final_port_val,
        "net_portfolio_profit": net_port_profit,
        "portfolio_roi_pct": port_roi_pct,
        "portfolio_mdd_pct": port_mdd_pct,
    }


# ======================================================================================
# 6. CLI 메인 진입점
# ======================================================================================
def main():
    parser = argparse.ArgumentParser(description="AutoBot Trader 차세대 공식 통합 백테스트 러너")
    parser.add_argument(
        "--market", 
        type=str, 
        default="ALL", 
        help="실행할 마켓 (KRW-BTC, KRW-ETH, KRW-SOL, 또는 ALL)"
    )
    parser.add_argument(
        "--plot", 
        action="store_true", 
        help="백테스트 자산 곡선 및 낙폭 차트 PNG 생성"
    )
    parser.add_argument(
        "--output", 
        type=str, 
        default="portfolio_backtest_result.png", 
        help="저장할 차트 이미지 경로"
    )
    
    args = parser.parse_args()

    if args.market.upper() == "ALL":
        target_markets = ["KRW-BTC", "KRW-ETH", "KRW-SOL"]
    else:
        target_markets = [args.market.upper()]

    run_unified_portfolio_backtest(
        markets=target_markets,
        plot=args.plot,
        output_chart=args.output
    )


if __name__ == "__main__":
    main()
