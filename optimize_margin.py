"""
수익 마진 최적화 스크립트
다양한 sell_profit_margin 값으로 백테스트를 실행하여 최적값을 찾습니다.
"""
import pandas as pd
import numpy as np
from dataclasses import dataclass
import logging
from typing import Dict, List
import time

# 기존 백테스트 모듈에서 필요한 함수들 import
from backtest_main import (
    get_historical_data,
    run_backtest,
    BacktestConfig,
    config
)

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)


def test_margin(margin_value: float, df: pd.DataFrame, original_config: BacktestConfig) -> Dict:
    """
    특정 마진 값으로 백테스트를 실행합니다.
    
    Args:
        margin_value: 테스트할 수익 마진 (예: 1.002 = 0.2%)
        df: 캔들 데이터
        original_config: 원본 설정
    
    Returns:
        백테스트 결과 딕셔너리
    """
    # 임시로 설정 변경
    global config
    config.sell_profit_margin = margin_value
    
    # 백테스트 실행 (결과만 받아오도록 수정 필요)
    # 여기서는 간단히 시뮬레이션만 수행
    
    from backtest_main import (
        UNIT_KRW, TRADING_FEE_RATE, adjust_price_to_tick,
        calculate_new_buy_prices
    )
    
    initial_krw = config.initial_capital
    krw_balance = initial_krw
    coin_balance = 0.0
    
    simulated_open_buy_orders: List[Dict] = []
    simulated_open_sell_orders: List[Dict] = []
    
    completed_cycles = []
    current_cycle = None
    
    equity_values = []
    
    # 초기 진입
    if not df.empty:
        start_price = df.iloc[0]['close']
        current_cycle = {
            'id': 1,
            'entries': [],
            'total_cost': 0,
            'total_volume': 0,
            'start_time': df.index[0]
        }
        
        buy_funds = UNIT_KRW
        buy_volume = buy_funds / start_price
        buy_cost = buy_funds * (1 + TRADING_FEE_RATE)
        
        krw_balance -= buy_cost
        coin_balance += buy_volume
        
        current_cycle['total_cost'] += buy_cost
        current_cycle['total_volume'] += buy_volume
        
        avg_buy_price = current_cycle['total_cost'] / current_cycle['total_volume']
        sell_price = adjust_price_to_tick(avg_buy_price * margin_value)
        simulated_open_sell_orders = [{'price': sell_price, 'volume': coin_balance}]
        
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
        
        equity_values.append(krw_balance + (coin_balance * start_price))
    
    # 메인 루프
    for index, candle in df.iloc[1:].iterrows():
        high_price, low_price, close_price = candle['high'], candle['low'], candle['close']
        
        # Case3: 매도 체결
        if current_cycle and simulated_open_sell_orders and high_price >= simulated_open_sell_orders[0]['price']:
            sell_order = simulated_open_sell_orders.pop(0)
            sell_price = sell_order['price']
            sell_volume = sell_order['volume']
            
            sell_revenue = (sell_price * sell_volume) * (1 - TRADING_FEE_RATE)
            pnl = sell_revenue - current_cycle['total_cost']
            
            current_cycle['pnl'] = pnl
            current_cycle['is_win'] = pnl > 0
            completed_cycles.append(current_cycle)
            
            krw_balance += sell_revenue
            coin_balance -= sell_volume
            
            current_cycle = {
                'id': len(completed_cycles) + 1,
                'entries': [],
                'total_cost': 0,
                'total_volume': 0
            }
            simulated_open_buy_orders.clear()
            
            buy_funds = UNIT_KRW
            buy_cost_needed = buy_funds * (1 + TRADING_FEE_RATE)
            
            if krw_balance < buy_cost_needed:
                available_funds = krw_balance / (1 + TRADING_FEE_RATE)
                if available_funds >= 5000:
                    buy_funds = available_funds
                else:
                    current_cycle = None
                    equity_values.append(krw_balance)
                    continue
            
            buy_price = close_price
            buy_volume = buy_funds / buy_price
            buy_cost = buy_funds * (1 + TRADING_FEE_RATE)
            
            krw_balance -= buy_cost
            coin_balance += buy_volume
            
            current_cycle['total_cost'] += buy_cost
            current_cycle['total_volume'] += buy_volume
            
            avg_buy_price = current_cycle['total_cost'] / current_cycle['total_volume']
            sell_price = adjust_price_to_tick(avg_buy_price * margin_value)
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
            
            for order in filled_buy_orders:
                buy_price = order['price']
                buy_volume = order['volume']
                units = order['units']
                buy_cost = (buy_price * buy_volume) * (1 + TRADING_FEE_RATE)
                
                if krw_balance < buy_cost:
                    continue
                
                krw_balance -= buy_cost
                coin_balance += buy_volume
                current_cycle['total_cost'] += buy_cost
                current_cycle['total_volume'] += buy_volume
            
            simulated_open_buy_orders = [o for o in simulated_open_buy_orders if o not in filled_buy_orders]
            
            if current_cycle['total_volume'] > 0:
                avg_buy_price = current_cycle['total_cost'] / current_cycle['total_volume']
                new_sell_price = adjust_price_to_tick(avg_buy_price * margin_value)
                simulated_open_sell_orders.append({'price': new_sell_price, 'volume': coin_balance})
                
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
        
        equity_values.append(krw_balance + (coin_balance * close_price))
    
    # 결과 계산
    final_equity = krw_balance + (coin_balance * df.iloc[-1]['close'])
    total_pnl = final_equity - initial_krw
    total_pnl_percent = (total_pnl / initial_krw) * 100
    
    winning_trades = sum(1 for c in completed_cycles if c.get('is_win', False))
    win_rate = (winning_trades / len(completed_cycles) * 100) if completed_cycles else 0
    
    # Sharpe Ratio 계산
    equity_series = pd.Series(equity_values)
    daily_returns = equity_series.pct_change().dropna()
    sharpe = 0
    if len(daily_returns) > 0 and daily_returns.std() > 0:
        sharpe = np.sqrt(365) * daily_returns.mean() / daily_returns.std()
    
    # Max Drawdown 계산
    cumulative = (1 + daily_returns).cumprod()
    running_max = cumulative.expanding().max()
    drawdown = (cumulative - running_max) / running_max
    max_dd = drawdown.min() * 100
    
    return {
        'margin': margin_value,
        'margin_percent': (margin_value - 1) * 100,
        'total_pnl': total_pnl,
        'total_pnl_percent': total_pnl_percent,
        'num_cycles': len(completed_cycles),
        'win_rate': win_rate,
        'sharpe': sharpe,
        'max_drawdown': max_dd
    }


def optimize_margin():
    """다양한 마진 값으로 백테스트를 실행하여 최적값을 찾습니다."""
    
    print("="*80)
    print("수익 마진 최적화 시작")
    print("="*80)
    
    # 데이터 로드
    logger.info("데이터 로딩 중...")
    df = get_historical_data(
        config.ticker,
        config.interval,
        config.start_date,
        config.end_date,
        config.data_file
    )
    
    if df.empty:
        logger.error("데이터 없음")
        return
    
    logger.info(f"데이터 준비 완료: {len(df):,}개 캔들")
    
    # 테스트할 마진 범위
    margins = [
        1.001,   # 0.1%
        1.002,   # 0.2%
        1.003,   # 0.3%
        1.004,   # 0.4%
        1.005,   # 0.5%
        1.0075,  # 0.75%
        1.01,    # 1.0%
        1.015,   # 1.5%
        1.02,    # 2.0%
        1.025,   # 2.5%
        1.03,    # 3.0%
        1.04,    # 4.0%
        1.05,    # 5.0%
        1.06,    # 6.0%
        1.07,    # 7.0%
        1.08,    # 8.0%
    ]
    
    results = []
    
    print(f"\n총 {len(margins)}개 마진 테스트 시작...\n")
    
    for i, margin in enumerate(margins, 1):
        margin_percent = (margin - 1) * 100
        print(f"[{i}/{len(margins)}] 마진 {margin_percent:.2f}% 테스트 중...", end=' ')
        
        try:
            result = test_margin(margin, df, config)
            results.append(result)
            print(f"✅ 수익률: {result['total_pnl_percent']:.2f}%, Sharpe: {result['sharpe']:.2f}")
        except Exception as e:
            print(f"❌ 오류: {e}")
            continue
    
    # 결과를 DataFrame으로 변환
    results_df = pd.DataFrame(results)
    
    # 결과 정렬 (수익률 기준)
    results_df = results_df.sort_values('total_pnl_percent', ascending=False)
    
    print("\n" + "="*80)
    print("최적화 결과 (수익률 순)")
    print("="*80)
    print(results_df.to_string(index=False))
    
    # 상위 5개 출력
    print("\n" + "="*80)
    print("🏆 TOP 5 수익률")
    print("="*80)
    top5 = results_df.head(5)
    for idx, row in top5.iterrows():
        print(f"{row['margin_percent']:.2f}%: 수익 {row['total_pnl_percent']:.2f}%, "
              f"Sharpe {row['sharpe']:.2f}, DD {row['max_drawdown']:.2f}%, "
              f"거래 {row['num_cycles']}회")
    
    # Sharpe Ratio 기준 상위 5개
    print("\n" + "="*80)
    print("🏆 TOP 5 Sharpe Ratio (위험 대비 수익)")
    print("="*80)
    top5_sharpe = results_df.nlargest(5, 'sharpe')
    for idx, row in top5_sharpe.iterrows():
        print(f"{row['margin_percent']:.2f}%: Sharpe {row['sharpe']:.2f}, "
              f"수익 {row['total_pnl_percent']:.2f}%, DD {row['max_drawdown']:.2f}%")
    
    # 균형잡힌 설정 추천 (Sharpe > 0.8, 수익률 상위)
    print("\n" + "="*80)
    print("🎯 추천 설정 (Sharpe > 0.8)")
    print("="*80)
    recommended = results_df[results_df['sharpe'] > 0.8].head(3)
    if len(recommended) > 0:
        for idx, row in recommended.iterrows():
            print(f"✅ {row['margin_percent']:.2f}%: 수익 {row['total_pnl_percent']:.2f}%, "
                  f"Sharpe {row['sharpe']:.2f}, DD {row['max_drawdown']:.2f}%")
    else:
        print("Sharpe > 0.8인 설정이 없습니다. 상위 결과를 참고하세요.")
    
    # CSV로 저장
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    filename = f"margin_optimization_{timestamp}.csv"
    results_df.to_csv(filename, index=False)
    print(f"\n📊 결과 저장: {filename}")


if __name__ == "__main__":
    optimize_margin()
