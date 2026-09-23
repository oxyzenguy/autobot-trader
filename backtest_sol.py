"""
⚠️ [안내] 본 스크립트는 과거 솔라나 단일 코인 단순 마틴게일 테스트용 레거시 스크립트입니다.
현재 프로젝트의 실전 전략(BTC 계층형 적립, ETH 50%손절 무제한스쿼드, SOL 70%손절 16차캡스쿼드)을
100% 반영한 공식 백테스트는 `python backtest_main.py`를 실행하십시오.
"""
import pandas as pd
import numpy as np
import pyupbit
import requests
import math
import time
import datetime
import logging
from typing import List, Dict
from dataclasses import dataclass
import matplotlib.pyplot as plt
from strategy.matingale2x_logic import calculate_new_buy_prices


# 로깅 설정
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)
logger = logging.getLogger(__name__)


# 설정 클래스
@dataclass
class BacktestConfig:
    ticker: str = "KRW-SOL"
    coin_name: str = "SOL"
    interval: str = "minute60"     # ← 이것만 변경
    start_date: str = "2023-10-01"
    end_date: str = "2025-10-10"
    data_file: str = "SOL_minute60_candles.csv"  # ← 파일명 변경
    
    initial_capital: int = 1_000_000
    unit_krw: int = 10000          # ← 1시간봉에 맞게
    order_unit: float = 1
    buy_funds_for_reentry: int = 10000
    
    sell_profit_margin: float = 1.005
    trading_fee_rate: float = 0.0005
    
    max_position_size: float = 0.5
    stop_loss_percent: float = -0.15



# 전역 설정
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


def adjust_price_to_tick(price: float) -> float:
    """가격을 거래소 호가 단위에 맞춰 조정합니다."""
    if price >= 2_000_000:
        tick = 1000
    elif price >= 1_000_000:
        tick = 500
    elif price >= 500_000:
        tick = 100
    elif price >= 100_000:
        tick = 50
    elif price >= 10_000:
        tick = 10
    elif price >= 1_000:
        tick = 5
    elif price >= 100:
        tick = 1
    else:
        tick = 0.1
    
    return math.floor(price / tick) * tick


def get_ohlcv_direct(ticker: str, interval: str, to: str, count: int = 200) -> pd.DataFrame:
    """Upbit API를 직접 호출하여 OHLCV 데이터를 가져옵니다."""
    try:
        if interval in ['minute1', 'minute3', 'minute5', 'minute10', 'minute15', 
                       'minute30', 'minute60', 'minute240']:
            unit = interval.replace('minute', '')
            url = f"https://api.upbit.com/v1/candles/minutes/{unit}"
        elif interval == 'day':
            url = "https://api.upbit.com/v1/candles/days"
        elif interval == 'week':
            url = "https://api.upbit.com/v1/candles/weeks"
        elif interval == 'month':
            url = "https://api.upbit.com/v1/candles/months"
        else:
            return pd.DataFrame()
        
        params = {'market': ticker, 'to': to, 'count': count}
        response = requests.get(url, params=params)
        
        if response.status_code != 200:
            return pd.DataFrame()
        
        data = response.json()
        if not data:
            return pd.DataFrame()
        
        df = pd.DataFrame(data)
        df['candle_date_time_kst'] = pd.to_datetime(df['candle_date_time_kst'])
        df = df.set_index('candle_date_time_kst')
        df = df.sort_index()
        
        df = df.rename(columns={
            'opening_price': 'open',
            'high_price': 'high',
            'low_price': 'low',
            'trade_price': 'close',
            'candle_acc_trade_volume': 'volume'
        })
        
        return df[['open', 'high', 'low', 'close', 'volume']]
        
    except Exception as e:
        logger.error(f"API 호출 오류: {e}")
        return pd.DataFrame()


def get_historical_data(
    ticker: str, 
    interval: str, 
    start_date: str, 
    end_date: str, 
    data_file: str
) -> pd.DataFrame:
    """CSV 파일에서 데이터를 로드하고, 부족한 경우 Upbit에서 데이터를 가져와 업데이트합니다."""
    df = pd.DataFrame()
    
    try:
        df = pd.read_csv(data_file, index_col=0, parse_dates=True)
        logger.info(f"📁 기존 데이터 로드: {data_file}")
        logger.info(f"   {len(df):,}개 캔들 ({df.index.min()} ~ {df.index.max()})")
    except FileNotFoundError:
        logger.warning(f"📁 {data_file} 파일이 없습니다. 새로 생성합니다.")
    except Exception as e:
        logger.error(f"데이터 로드 중 오류: {e}")

    req_start_dt = pd.to_datetime(start_date)
    req_end_dt = pd.to_datetime(end_date)

    if not df.empty:
        if df.index.min() <= req_start_dt and df.index.max() >= req_end_dt:
            logger.info("✅ 기존 데이터 충분! API 호출 생략")
            result = df.loc[req_start_dt:req_end_dt]
            logger.info(f"   백테스트 데이터: {len(result):,}개 캔들")
            return result

    fetch_ranges = []
    
    if df.empty:
        fetch_ranges.append({'start': req_start_dt, 'end': req_end_dt})
        logger.info(f"📥 전체 데이터 수집: {req_start_dt} ~ {req_end_dt}")
    else:
        if req_start_dt < df.index.min():
            fetch_ranges.append({
                'start': req_start_dt, 
                'end': df.index.min() - pd.Timedelta(minutes=1)
            })
            logger.info(f"📥 과거 데이터 수집: {req_start_dt} ~ {df.index.min()}")
        
        if req_end_dt > df.index.max():
            actual_end = min(req_end_dt, pd.Timestamp.now())
            if actual_end > df.index.max():
                fetch_ranges.append({
                    'start': df.index.max() + pd.Timedelta(minutes=1),
                    'end': actual_end
                })
                logger.info(f"📥 최신 데이터 수집: {df.index.max()} ~ {actual_end}")

    if not fetch_ranges:
        result = df.loc[req_start_dt:req_end_dt]
        return result

    interval_minutes = {
        'minute1': 1, 'minute3': 3, 'minute5': 5, 'minute10': 10,
        'minute15': 15, 'minute30': 30, 'minute60': 60, 'minute240': 240,
        'day': 1440
    }
    interval_min = interval_minutes.get(interval, 60)
    
    all_new_data = []
    
    for idx, frange in enumerate(fetch_ranges):
        range_start = frange['start']
        range_end = frange['end']
        
        logger.info(f"🔄 범위 {idx+1}/{len(fetch_ranges)} 수집 중...")
        logger.info(f"   {range_start} ~ {range_end}")
        
        time_diff = range_end - range_start
        total_minutes = time_diff.total_seconds() / 60
        expected_candles = int(total_minutes / interval_min)
        max_iterations = int(expected_candles / 200) + 50
        
        logger.info(f"   예상 캔들: {expected_candles:,}개 (최대 {max_iterations:,}회 호출)")
        
        current_to = range_end
        collected = []
        prev_min = None
        same_count = 0
        
        for i in range(max_iterations):
            try:
                to_str = current_to.strftime('%Y-%m-%dT%H:%M:%S')
                chunk = get_ohlcv_direct(ticker, interval, to_str, count=200)
                
                if chunk is None or chunk.empty:
                    time.sleep(0.5)
                    continue
                
                if i == 0:
                    logger.info(f"   수집 시작: {chunk.index.min()} ~ {chunk.index.max()}")
                
                current_min = chunk.index.min()
                if prev_min == current_min:
                    same_count += 1
                    if same_count >= 3:
                        logger.info(f"   ⚠️  더 이상 과거 데이터 없음 ({current_min})")
                        break
                else:
                    same_count = 0
                    prev_min = current_min
                
                collected.append(chunk)
                
                if (i + 1) % 100 == 0:
                    total_collected = sum(len(c) for c in collected)
                    logger.info(f"   진행: {i+1}회, {total_collected:,}개, 위치: {chunk.index.min()}")
                
                if chunk.index.min() <= range_start:
                    total_collected = sum(len(c) for c in collected)
                    logger.info(f"   ✅ 목표 도달! {total_collected:,}개 수집")
                    break
                
                current_to = chunk.index.min() - pd.Timedelta(minutes=interval_min)
                time.sleep(0.12)
                
            except KeyboardInterrupt:
                logger.info("   사용자 중단")
                break
            except Exception as e:
                logger.error(f"   오류: {e}")
                time.sleep(1)
                continue
        
        # ← 핵심 수정: 필터링 없이 전체 데이터 사용
        if collected:
            range_df = pd.concat(collected)
            range_df = range_df[~range_df.index.duplicated(keep='first')]
            range_df = range_df.sort_index()
            
            logger.info(f"   ✅ 범위 {idx+1}: {len(range_df):,}개 캔들 (필터링 전)")
            logger.info(f"      {range_df.index.min()} ~ {range_df.index.max()}")
            
            # ← 무조건 추가 (필터링 안 함)
            all_new_data.append(range_df)
        else:
            logger.warning(f"   ❌ 범위 {idx+1}: 수집 실패")

    # ← 핵심: 수집된 데이터가 있으면 무조건 병합하고 저장
    if all_new_data:
        new_df = pd.concat(all_new_data)
        new_df = new_df[~new_df.index.duplicated(keep='first')]
        new_df = new_df.sort_index()
        
        logger.info(f"\n💾 데이터 병합 및 저장...")
        logger.info(f"   신규 수집: {len(new_df):,}개 ({new_df.index.min()} ~ {new_df.index.max()})")
        
        if not df.empty:
            logger.info(f"   기존 데이터: {len(df):,}개")
            original_count = len(df)
            
            # 병합
            df = pd.concat([df, new_df])
            df = df[~df.index.duplicated(keep='first')]
            df = df.sort_index()
            
            added_count = len(df) - original_count
            logger.info(f"   병합 후: {len(df):,}개 (+{added_count:,}개 추가)")
        else:
            df = new_df
            logger.info(f"   신규 생성: {len(df):,}개")
        
        logger.info(f"   최종 범위: {df.index.min()} ~ {df.index.max()}")
        
        # ← 핵심: 무조건 CSV 저장
        try:
            df.to_csv(data_file)
            logger.info(f"   ✅ CSV 저장 완료: {data_file}")
            logger.info(f"      📊 총 {len(df):,}개 캔들 누적 저장됨")
        except Exception as e:
            logger.error(f"   ❌ CSV 저장 실패: {e}")
    else:
        logger.warning("💾 신규 데이터 없음")

    # 백테스트용 데이터 반환 (요청 범위)
    if not df.empty:
        result = df[(df.index >= req_start_dt) & (df.index <= req_end_dt)]
        
        # 요청 범위에 데이터 없으면 전체 반환
        if len(result) == 0:
            logger.warning(f"⚠️  요청 범위에 데이터 없음. 보유한 전체 데이터 사용")
            result = df
        
        logger.info(f"\n🎯 백테스트 데이터: {len(result):,}개 캔들")
        logger.info(f"   {result.index.min()} ~ {result.index.max()}")
        return result
    else:
        logger.error("❌ 데이터 없음")
        return pd.DataFrame()


def calculate_performance_metrics(
    equity_curve: pd.Series, 
    trades_df: pd.DataFrame, 
    cash_usage: pd.Series, 
    initial_krw: float
) -> Dict:
    """성능 지표를 계산합니다."""
    daily_returns = equity_curve.resample('D').last().pct_change().dropna()
    
    sharpe_ratio = 0
    if len(daily_returns) > 0 and daily_returns.std() > 0:
        sharpe_ratio = np.sqrt(365) * daily_returns.mean() / daily_returns.std()
    
    cumulative_returns = (1 + daily_returns).cumprod()
    running_max = cumulative_returns.expanding().max()
    drawdown = (cumulative_returns - running_max) / running_max
    mdd = drawdown.min() * 100
    
    max_consecutive_wins = 0
    max_consecutive_losses = 0
    
    if '실현손익' in trades_df.columns:
        current_wins = 0
        current_losses = 0
        
        for val in trades_df['실현손익'].dropna():
            if val > 0:
                current_wins += 1
                current_losses = 0
                max_consecutive_wins = max(max_consecutive_wins, current_wins)
            elif val < 0:
                current_losses += 1
                current_wins = 0
                max_consecutive_losses = max(max_consecutive_losses, current_losses)
    
    max_cash_used = initial_krw - cash_usage.min()
    max_cash_used_percent = (max_cash_used / initial_krw) * 100
    avg_cash_used = initial_krw - cash_usage.mean()
    avg_cash_used_percent = (avg_cash_used / initial_krw) * 100
    
    total_return_percent = ((equity_curve.iloc[-1] - initial_krw) / initial_krw) * 100
    capital_efficiency = total_return_percent / max_cash_used_percent if max_cash_used_percent > 0 else 0
    
    return {
        'sharpe_ratio': sharpe_ratio,
        'max_drawdown': mdd,
        'max_consecutive_wins': max_consecutive_wins,
        'max_consecutive_losses': max_consecutive_losses,
        'volatility': daily_returns.std() * np.sqrt(365) * 100,
        'max_cash_used': max_cash_used,
        'max_cash_used_percent': max_cash_used_percent,
        'avg_cash_used': avg_cash_used,
        'avg_cash_used_percent': avg_cash_used_percent,
        'capital_efficiency': capital_efficiency
    }


def create_visualizations(
    df: pd.DataFrame, 
    equity_curve: pd.Series, 
    completed_cycles: List[Dict], 
    cash_usage: pd.Series,
    initial_krw: float,
    filename_prefix: str
):
    """백테스트 결과 시각화를 생성합니다."""
    try:
        plt.style.use('seaborn-v0_8-darkgrid')
    except:
        plt.style.use('default')
    
    fig = plt.figure(figsize=(20, 16))
    
    # 1. 가격 차트
    ax1 = plt.subplot(4, 2, 1)
    ax1.plot(df.index, df['close'], label='Close Price', linewidth=1, alpha=0.7)
    
    for cycle in completed_cycles:
        for entry in cycle['entries']:
            ax1.scatter(entry['time'], entry['price'], c='green', marker='^', s=50, alpha=0.6)
        if 'exit' in cycle:
            ax1.scatter(cycle['exit']['time'], cycle['exit']['price'], c='red', marker='v', s=50, alpha=0.6)
    
    ax1.set_title(f'{COIN_NAME} Price Chart', fontsize=14, fontweight='bold')
    ax1.set_xlabel('Date')
    ax1.set_ylabel('Price (KRW)')
    ax1.legend()
    ax1.grid(True, alpha=0.3)
    
    # 2. 자산 곡선
    ax2 = plt.subplot(4, 2, 2)
    ax2.plot(equity_curve.index, equity_curve.values, linewidth=2, color='blue')
    ax2.fill_between(equity_curve.index, equity_curve.values, 
                     equity_curve.values[0], alpha=0.3, color='blue')
    ax2.set_title('Equity Curve', fontsize=14, fontweight='bold')
    ax2.axhline(y=equity_curve.values[0], color='r', linestyle='--', alpha=0.5)
    ax2.grid(True, alpha=0.3)
    
    # 3. 일별 수익률
    ax3 = plt.subplot(4, 2, 3)
    daily_returns = equity_curve.resample('D').last().pct_change().dropna() * 100
    colors = ['green' if x > 0 else 'red' for x in daily_returns]
    ax3.bar(daily_returns.index, daily_returns.values, color=colors, alpha=0.6)
    ax3.set_title('Daily Returns (%)', fontsize=14, fontweight='bold')
    ax3.axhline(y=0, color='black', linestyle='-', linewidth=0.5)
    ax3.grid(True, alpha=0.3)
    
    # 4. Drawdown
    ax4 = plt.subplot(4, 2, 4)
    cumulative_returns = (1 + equity_curve.pct_change()).cumprod()
    running_max = cumulative_returns.expanding().max()
    drawdown = ((cumulative_returns - running_max) / running_max) * 100
    ax4.fill_between(drawdown.index, 0, drawdown.values, color='red', alpha=0.3)
    ax4.plot(drawdown.index, drawdown.values, color='darkred', linewidth=1)
    ax4.set_title('Drawdown (%)', fontsize=14, fontweight='bold')
    ax4.grid(True, alpha=0.3)
    
    # 5. 수익률 분포
    ax5 = plt.subplot(4, 2, 5)
    pnl_percents = [c['pnl_percent'] for c in completed_cycles if 'pnl_percent' in c]
    if pnl_percents:
        ax5.hist(pnl_percents, bins=30, color='skyblue', edgecolor='black', alpha=0.7)
        ax5.axvline(x=0, color='red', linestyle='--', linewidth=2)
        ax5.set_title('Trade P&L Distribution (%)', fontsize=14, fontweight='bold')
        ax5.grid(True, alpha=0.3)
    
    # 6. 누적 거래 횟수
    ax6 = plt.subplot(4, 2, 6)
    cycle_times = [c['exit']['time'] for c in completed_cycles if 'exit' in c]
    cumulative_trades = list(range(1, len(cycle_times) + 1))
    if cycle_times:
        ax6.plot(cycle_times, cumulative_trades, linewidth=2, color='purple')
        ax6.set_title('Cumulative Trades', fontsize=14, fontweight='bold')
        ax6.grid(True, alpha=0.3)
    
    # 7. 현금 잔고
    ax7 = plt.subplot(4, 2, 7)
    ax7.plot(cash_usage.index, cash_usage.values, linewidth=2, color='green')
    ax7.fill_between(cash_usage.index, cash_usage.values, cash_usage.values.min(), alpha=0.3, color='green')
    ax7.axhline(y=initial_krw, color='blue', linestyle='--', alpha=0.7)
    ax7.set_title('Cash Balance', fontsize=14, fontweight='bold')
    ax7.grid(True, alpha=0.3)
    
    # 8. 현금 사용률
    ax8 = plt.subplot(4, 2, 8)
    cash_usage_percent = ((initial_krw - cash_usage) / initial_krw) * 100
    ax8.plot(cash_usage_percent.index, cash_usage_percent.values, linewidth=2, color='orange')
    ax8.fill_between(cash_usage_percent.index, 0, cash_usage_percent.values, alpha=0.3, color='orange')
    ax8.set_title('Capital Usage (%)', fontsize=14, fontweight='bold')
    ax8.grid(True, alpha=0.3)
    
    plt.tight_layout()
    
    chart_filename = f"{filename_prefix}_charts.png"
    plt.savefig(chart_filename, dpi=300, bbox_inches='tight')
    logger.info(f"차트 저장: '{chart_filename}'")
    plt.close()


def _generate_and_save_report(
    df: pd.DataFrame,
    initial_krw: float,
    final_krw_balance: float,
    coin_balance: float,
    current_cycle: Dict,
    completed_cycles: List[Dict],
    total_actions: int,
    equity_curve: pd.Series,
    cash_usage: pd.Series
):
    """백테스트 결과를 계산, 요약하고 Excel 파일로 저장합니다."""
    print("\n" + "="*50)
    print("백테스트 종료. 결과 요약:")
    print("="*50)

    final_equity = final_krw_balance
    unrealized_pnl = 0
    
    if coin_balance > 0 and current_cycle:
        last_price = df.iloc[-1]['close']
        final_coin_value = coin_balance * last_price
        final_equity += final_coin_value
        unrealized_pnl = (final_coin_value * (1 - TRADING_FEE_RATE)) - current_cycle['total_cost']
        
        print("--- 미실현 손익 ---")
        avg_buy_price = current_cycle['total_cost'] / current_cycle['total_volume']
        print(f"  보유: {coin_balance:.4f} {COIN_NAME}, 평단: {avg_buy_price:,.2f} KRW")
        print(f"  미실현 손익: {unrealized_pnl:,.0f} KRW")
        print("-" * 20)

    total_pnl = final_equity - initial_krw
    total_pnl_percent = (total_pnl / initial_krw) * 100
    
    winning_trades = sum(1 for c in completed_cycles if c.get('is_win', False))
    losing_trades = len(completed_cycles) - winning_trades
    win_rate = (winning_trades / len(completed_cycles) * 100) if completed_cycles else 0

    print(f"초기 자산: {initial_krw:,.0f} KRW")
    print(f"최종 자산: {final_equity:,.0f} KRW")
    print(f"총 손익: {total_pnl:,.0f} KRW ({total_pnl_percent:.2f}%)")
    print(f"완료된 사이클: {len(completed_cycles)}회")
    print(f"승률: {win_rate:.2f}%")
    
    trade_details_list = []
    for cycle in completed_cycles:
        if cycle['total_volume'] > 0:
            avg_buy_price = cycle['total_cost'] / cycle['total_volume']
            duration = None
            if 'exit' in cycle and 'start_time' in cycle:
                duration = cycle['exit']['time'] - cycle['start_time']
            
            for entry in cycle['entries']:
                trade_details_list.append({
                    '사이클 ID': cycle['id'],
                    '시간': entry['time'],
                    '종류': entry['type'],
                    '가격': entry['price'],
                    '수량': entry['volume'],
                    '총 비용': entry['cost'],
                    '실현손익': None,
                    '실현손익(%)': None,
                    '사이클 평단가': None,
                    'duration': None
                })
            
            if 'exit' in cycle:
                trade_details_list.append({
                    '사이클 ID': cycle['id'],
                    '시간': cycle['exit']['time'],
                    '종류': 'sell_exit',
                    '가격': cycle['exit']['price'],
                    '수량': cycle['total_volume'],
                    '총 비용': cycle['total_cost'],
                    '실현손익': cycle.get('pnl', 0),
                    '실현손익(%)': cycle.get('pnl_percent', 0),
                    '사이클 평단가': avg_buy_price,
                    'duration': duration
                })
    
    trades_df = pd.DataFrame(trade_details_list)
    metrics = calculate_performance_metrics(equity_curve, trades_df, cash_usage, initial_krw)
    
    print("-" * 50)
    print("📊 성능 지표")
    print("-" * 50)
    print(f"Sharpe Ratio: {metrics['sharpe_ratio']:.2f}")
    print(f"Max Drawdown: {metrics['max_drawdown']:.2f}%")
    print(f"연율화 변동성: {metrics['volatility']:.2f}%")
    print(f"최대 연속 승: {metrics['max_consecutive_wins']}회")
    print(f"최대 연속 패: {metrics['max_consecutive_losses']}회")
    print("-" * 50)
    print("💰 자본 사용 현황")
    print("-" * 50)
    print(f"최대 사용 현금: {metrics['max_cash_used']:,.0f} KRW ({metrics['max_cash_used_percent']:.2f}%)")
    print(f"평균 사용 현금: {metrics['avg_cash_used']:,.0f} KRW ({metrics['avg_cash_used_percent']:.2f}%)")
    print(f"자본 효율성: {metrics['capital_efficiency']:.2f}")
    print(f"최소 현금 잔고: {cash_usage.min():,.0f} KRW")
    print("="*50)

    timestamp_str = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    filename_prefix = f"backtest_{TICKER}_{timestamp_str}"
    excel_filename = f"{filename_prefix}.xlsx"
    
    summary_df = pd.DataFrame({
        '항목': [
            '테스트 기간', '초기 자산', '최종 자산', 
            '총 손익', '총 손익(%)', '미실현 손익',
            '완료 사이클', '승리', '패배', '승률(%)',
            'Sharpe Ratio', 'Max Drawdown(%)', '연율화 변동성(%)',
            '최대 연속 승', '최대 연속 패',
            '최대 사용 현금', '최대 사용 현금(%)',
            '평균 사용 현금', '평균 사용 현금(%)',
            '최소 현금 잔고', '자본 효율성'
        ],
        '값': [
            f"{df.index.min()} ~ {df.index.max()}",
            initial_krw, final_equity, total_pnl, total_pnl_percent, unrealized_pnl,
            len(completed_cycles), winning_trades, losing_trades, win_rate,
            metrics['sharpe_ratio'], metrics['max_drawdown'], metrics['volatility'],
            metrics['max_consecutive_wins'], metrics['max_consecutive_losses'],
            metrics['max_cash_used'], metrics['max_cash_used_percent'],
            metrics['avg_cash_used'], metrics['avg_cash_used_percent'],
            cash_usage.min(), metrics['capital_efficiency']
        ]
    })
    
    try:
        with pd.ExcelWriter(excel_filename, engine='openpyxl') as writer:
            summary_df.to_excel(writer, sheet_name='백테스트 요약', index=False)
            trades_df.drop('duration', axis=1, errors='ignore').to_excel(
                writer, sheet_name='매매 내역', index=False)
            
            equity_df = pd.DataFrame({
                '시간': equity_curve.index,
                '자산': equity_curve.values
            })
            equity_df.to_excel(writer, sheet_name='자산 곡선', index=False)
            
            cash_df = pd.DataFrame({
                '시간': cash_usage.index,
                '현금 잔고': cash_usage.values,
                '사용률(%)': ((initial_krw - cash_usage) / initial_krw * 100).values
            })
            cash_df.to_excel(writer, sheet_name='현금 사용', index=False)
        
        logger.info(f"결과 저장: '{excel_filename}'")
    except Exception as e:
        logger.error(f"파일 저장 실패: {e}")
    
    try:
        create_visualizations(df, equity_curve, completed_cycles, 
                            cash_usage, initial_krw, filename_prefix)
    except Exception as e:
        logger.error(f"시각화 실패: {e}")


def run_backtest(df: pd.DataFrame):
    """주어진 캔들 데이터를 기반으로 백테스트를 실행합니다."""
    print("\n" + "="*50)
    print(f"백테스트 시작 (마틴게일 전략: 1→2→3→6 Unit)")
    print(f"1 Unit = {UNIT_KRW:,}원")
    print(f"기간: {df.index.min()} ~ {df.index.max()}")
    print("="*50)

    initial_krw = config.initial_capital
    krw_balance = initial_krw
    coin_balance = 0.0
    
    simulated_open_buy_orders: List[Dict] = []
    simulated_open_sell_orders: List[Dict] = []

    total_actions = 0
    completed_cycles = []
    current_cycle = None
    
    equity_times = []
    equity_values = []
    cash_times = []
    cash_values = []

    # 초기 진입 (1 Unit)
    if not df.empty:
        start_price = df.iloc[0]['close']
        current_cycle = {
            'id': 1,
            'entries': [],
            'total_cost': 0,
            'total_volume': 0,
            'start_time': df.index[0]
        }

        buy_funds = UNIT_KRW  # 1 Unit
        buy_volume = buy_funds / start_price
        buy_cost = buy_funds * (1 + TRADING_FEE_RATE)

        krw_balance -= buy_cost
        coin_balance += buy_volume
        
        current_cycle['entries'].append({
            'type': 'market_buy',
            'price': start_price,
            'volume': buy_volume,
            'cost': buy_cost,
            'time': df.index[0],
            'units': 1  # 1 Unit으로 시작
        })
        current_cycle['total_cost'] += buy_cost
        current_cycle['total_volume'] += buy_volume
        total_actions += 1

        avg_buy_price = current_cycle['total_cost'] / current_cycle['total_volume']
        sell_price = adjust_price_to_tick(avg_buy_price * SELL_PROFIT_MARGIN)
        simulated_open_sell_orders = [{'price': sell_price, 'volume': coin_balance}]
        
        # 배수 전략 적용: 2, 3, 6 Unit 매수 주문
        new_buy_orders = calculate_new_buy_prices(avg_buy_price, existing_orders=None)
        simulated_open_buy_orders = []
        for order in new_buy_orders:
            price = order['price']
            units = order['units']
            buy_volume_for_order = (UNIT_KRW * units) / price
            simulated_open_buy_orders.append({
                'price': price,
                'volume': buy_volume_for_order,
                'units': units
            })
        
        equity_times.append(df.index[0])
        equity_values.append(krw_balance + (coin_balance * start_price))
        cash_times.append(df.index[0])
        cash_values.append(krw_balance)

    # 메인 루프
    for index, candle in df.iloc[1:].iterrows():
        current_time = index
        high_price, low_price, close_price = candle['high'], candle['low'], candle['close']

        # Case3: 매도 체결
        if current_cycle and simulated_open_sell_orders and high_price >= simulated_open_sell_orders[0]['price']:
            sell_order = simulated_open_sell_orders.pop(0)
            sell_price = sell_order['price']
            sell_volume = sell_order['volume']

            sell_revenue = (sell_price * sell_volume) * (1 - TRADING_FEE_RATE)
            pnl = sell_revenue - current_cycle['total_cost']
            pnl_percent = (pnl / current_cycle['total_cost']) * 100 if current_cycle['total_cost'] > 0 else 0
            
            current_cycle['exit'] = {'price': sell_price, 'revenue': sell_revenue, 'time': current_time}
            current_cycle['pnl'] = pnl
            current_cycle['pnl_percent'] = pnl_percent
            current_cycle['is_win'] = pnl > 0
            completed_cycles.append(current_cycle)

            krw_balance += sell_revenue
            coin_balance -= sell_volume
            total_actions += 1

            # 새 사이클 시작
            current_cycle = {
                'id': len(completed_cycles) + 1,
                'entries': [],
                'total_cost': 0,
                'total_volume': 0,
                'start_time': current_time
            }
            simulated_open_buy_orders.clear()
            
            # 1 Unit 재진입
            buy_funds = UNIT_KRW
            buy_cost_needed = buy_funds * (1 + TRADING_FEE_RATE)
            
            if krw_balance < buy_cost_needed:
                available_funds = krw_balance / (1 + TRADING_FEE_RATE)
                if available_funds >= 5000:
                    buy_funds = available_funds
                else:
                    current_cycle = None
                    equity_times.append(current_time)
                    equity_values.append(krw_balance)
                    cash_times.append(current_time)
                    cash_values.append(krw_balance)
                    continue
            
            buy_price = close_price
            buy_volume = buy_funds / buy_price
            buy_cost = buy_funds * (1 + TRADING_FEE_RATE)

            krw_balance -= buy_cost
            coin_balance += buy_volume
            
            current_cycle['entries'].append({
                'type': 'market_buy_reentry',
                'price': buy_price,
                'volume': buy_volume,
                'cost': buy_cost,
                'time': current_time,
                'units': 1  # 1 Unit 재진입
            })
            current_cycle['total_cost'] += buy_cost
            current_cycle['total_volume'] += buy_volume
            total_actions += 1
            
            # 배수 전략 적용
            avg_buy_price = current_cycle['total_cost'] / current_cycle['total_volume']
            sell_price = adjust_price_to_tick(avg_buy_price * SELL_PROFIT_MARGIN)
            simulated_open_sell_orders = [{'price': sell_price, 'volume': coin_balance}]
            
            new_buy_orders = calculate_new_buy_prices(avg_buy_price, existing_orders=None)
            for order in new_buy_orders:
                price = order['price']
                units = order['units']
                buy_volume_for_order = (UNIT_KRW * units) / price
                simulated_open_buy_orders.append({
                    'price': price,
                    'volume': buy_volume_for_order,
                    'units': units
                })

        # Case4: 매수 체결
        filled_buy_orders = [o for o in simulated_open_buy_orders if low_price <= o['price']]
        if filled_buy_orders and current_cycle:
            simulated_open_sell_orders.clear()
            filled_count = 0
            
            for order in filled_buy_orders:
                buy_price = order['price']
                buy_volume = order['volume']
                units = order['units']
                buy_cost = (buy_price * buy_volume) * (1 + TRADING_FEE_RATE)

                if krw_balance < buy_cost:
                    continue

                krw_balance -= buy_cost
                coin_balance += buy_volume

                current_cycle['entries'].append({
                    'type': 'limit_buy',
                    'price': buy_price,
                    'volume': buy_volume,
                    'cost': buy_cost,
                    'time': current_time,
                    'units': units  # Unit 수 기록
                })
                current_cycle['total_cost'] += buy_cost
                current_cycle['total_volume'] += buy_volume
                total_actions += 1
                filled_count += 1

            simulated_open_buy_orders = [o for o in simulated_open_buy_orders if o not in filled_buy_orders]
            
            if filled_count > 0 and current_cycle['total_volume'] > 0:
                avg_buy_price = current_cycle['total_cost'] / current_cycle['total_volume']
                new_sell_price = adjust_price_to_tick(avg_buy_price * SELL_PROFIT_MARGIN)
                simulated_open_sell_orders.append({'price': new_sell_price, 'volume': coin_balance})
                
                # 배수 전략으로 추가 매수 주문
                new_buy_orders = calculate_new_buy_prices(avg_buy_price, existing_orders=simulated_open_buy_orders)
                
                for order in new_buy_orders:
                    price = order['price']
                    units = order['units']
                    buy_volume_for_order = (UNIT_KRW * units) / price
                    simulated_open_buy_orders.append({
                        'price': price,
                        'volume': buy_volume_for_order,
                        'units': units
                    })
        
        current_equity = krw_balance + (coin_balance * close_price)
        equity_times.append(current_time)
        equity_values.append(current_equity)
        cash_times.append(current_time)
        cash_values.append(krw_balance)
    
    # 최종 결과 생성
    equity_curve = pd.Series(equity_values, index=pd.DatetimeIndex(equity_times))
    cash_usage = pd.Series(cash_values, index=pd.DatetimeIndex(cash_times))
    
    _generate_and_save_report(
        df=df,
        initial_krw=initial_krw,
        final_krw_balance=krw_balance,
        coin_balance=coin_balance,
        current_cycle=current_cycle,
        completed_cycles=completed_cycles,
        total_actions=total_actions,
        equity_curve=equity_curve,
        cash_usage=cash_usage
    )


def validate_config():
    """설정값 검증"""
    logger.info("설정값 검증 시작...")
    
    try:
        start_dt = pd.to_datetime(BACKTEST_START_DATE)
        end_dt = pd.to_datetime(BACKTEST_END_DATE)
        
        if start_dt >= end_dt:
            logger.error("시작 날짜가 종료 날짜보다 늦습니다.")
            return False
        
        logger.info(f"날짜 범위: {start_dt} ~ {end_dt} ({(end_dt - start_dt).days}일)")
        
    except Exception as e:
        logger.error(f"날짜 형식 오류: {e}")
        return False
    
    valid_intervals = ["minute1", "minute3", "minute5", "minute10", "minute15", 
                      "minute30", "minute60", "minute240", "day"]
    if INTERVAL not in valid_intervals:
        logger.error(f"잘못된 인터벌: {INTERVAL}")
        return False
    
    logger.info("설정값 검증 완료")
    return True


def test_api_connection():
    """API 연결 테스트"""
    logger.info("API 연결 테스트 중...")
    
    try:
        current_price = pyupbit.get_current_price(TICKER)
        
        if current_price is None:
            logger.error(f"API 연결 실패: {TICKER}")
            return False
        
        logger.info(f"API 연결 성공. {TICKER} 현재가: {current_price:,.0f} KRW")
        
        test_ohlcv = pyupbit.get_ohlcv(TICKER, interval=INTERVAL, count=1)
        
        if test_ohlcv is None or test_ohlcv.empty:
            logger.error("OHLCV 데이터 조회 실패")
            return False
        
        logger.info(f"OHLCV 조회 성공. 최근 캔들: {test_ohlcv.index[0]}")
        return True
        
    except Exception as e:
        logger.error(f"API 테스트 오류: {e}")
        return False


if __name__ == "__main__":
    if not validate_config():
        logger.error("설정값 검증 실패")
        exit(1)
    
    if not test_api_connection():
        logger.error("API 연결 실패")
        exit(1)
    
    logger.info("="*50)
    logger.info("백테스트 프로그램 시작")
    logger.info(f"티커: {TICKER}")
    logger.info(f"기간: {BACKTEST_START_DATE} ~ {BACKTEST_END_DATE}")
    logger.info(f"인터벌: {INTERVAL}")
    logger.info(f"1 Unit: {UNIT_KRW:,}원")
    logger.info("="*50)
    
    try:
        df_candles = get_historical_data(
            TICKER, INTERVAL, BACKTEST_START_DATE, BACKTEST_END_DATE, DATA_FILE
        )

        if df_candles.empty:
            logger.error("데이터 없음")
        else:
            logger.info(f"백테스트 실행: {len(df_candles):,}개 캔들")
            run_backtest(df_candles)
            
    except Exception as e:
        logger.error(f"백테스트 실행 오류: {e}")
        import traceback
        logger.error(traceback.format_exc())
