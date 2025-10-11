from datetime import datetime
from autobot_trader.util.trade import buy_market

def get_signal(df, amount=10000, prev_positions=None):
    signals, updated_positions = get_all_signals(df, prev_positions or [])
    if not signals:
        return None, updated_positions

    latest = signals[-1]
    if latest['action'] == 'buy':
        return {
            'signal': 'buy',
            'reason': f"DCA 조건부 매수 (가격: {latest['price']})",
            'amount': amount,
            'price': latest['price']
        }, updated_positions

    elif latest['action'] == 'sell':
        return {
            'signal': 'sell',
            'reason': f"DCA 조건부 매도 (가격: {latest['price']})"
        }, updated_positions

    return None, updated_positions

def get_all_signals(df, positions):
    signals = []
    today_str = datetime.now().strftime('%Y-%m-%d')

    for i in range(1, len(df)):
        prev_close = df['close'].iloc[i - 1]
        today_date = df.index[i]
        today_close = df['close'].iloc[i]
        today_high = df['high'].iloc[i]
        buy_price = round(prev_close * 0.9995, 2)

        # 중복 매수 방지
        if any(p['buy_date'] == today_date.strftime('%Y-%m-%d') for p in positions):
            continue

        if today_close < prev_close:
            signals.append({
                'action': 'buy',
                'price': buy_price,
                'index': i,
                'date': today_date
            })
            positions.append({
                'buy_price': buy_price,
                'buy_date': today_date.strftime('%Y-%m-%d')
            })

        sell_targets = []
        for pos in positions:
            target_sell = round(pos['buy_price'] * 1.05, 2)
            buy_date = datetime.strptime(pos['buy_date'], '%Y-%m-%d')
            holding_days = (today_date - buy_date).days
            profit = (today_close - pos['buy_price']) / pos['buy_price']

            if today_high >= target_sell or (holding_days >= 30 and profit > 0):
                signals.append({
                    'action': 'sell',
                    'price': target_sell if today_high >= target_sell else today_close,
                    'index': i,
                    'date': today_date
                })
                sell_targets.append(pos)

        positions = [p for p in positions if p not in sell_targets]

    return signals, positions
