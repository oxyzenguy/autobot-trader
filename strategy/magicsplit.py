from datetime import datetime

VARIANT_CONFIGS = {
    "conservative": {
        "split_steps": 5,
        "down_pct": 0.03,
        "profit_pct": 0.03,
        "max_holding_days": 20
    },
    "balanced": {
        "split_steps": 7,
        "down_pct": 0.05,
        "profit_pct": 0.05,
        "max_holding_days": 30
    },
    "aggressive": {
        "split_steps": 10,
        "down_pct": 0.07,
        "profit_pct": 0.10,
        "max_holding_days": 45
    }
}

def get_signal(df, variant="balanced", amount=10000, prev_positions=None):
    signals, updated_positions = get_all_signals(df, variant, prev_positions or [])
    if not signals:
        return None, updated_positions

    latest = signals[-1]
    if latest['action'] == 'buy':
        return {
            "signal": "buy",
            "reason": f"[{variant}] 조건 매수 (step: {latest.get('step')})",
            "amount": amount,
            "price": latest["price"]
        }, updated_positions

    elif latest['action'] == 'sell':
        return {
            "signal": "sell",
            "reason": f"[{variant}] 조건 매도 (step: {latest.get('step')})",
            "price": latest["price"]
        }, updated_positions

    return None, updated_positions

def get_all_signals(df, variant="balanced", positions=[]):
    config = VARIANT_CONFIGS[variant]

    split_steps = config["split_steps"]
    down_pct = config["down_pct"]
    profit_pct = config["profit_pct"]
    max_holding_days = config["max_holding_days"]

    signals = []

    for i in range(1, len(df)):
        prev_close = df['close'].iloc[i - 1]
        today_date = df.index[i]
        today_low = df['low'].iloc[i]
        today_high = df['high'].iloc[i]
        today_close = df['close'].iloc[i]

        if len(positions) == 0:
            buy_price = prev_close
            signals.append({'action': 'buy', 'price': buy_price, 'index': i, 'date': today_date, 'step': 1})
            positions.append({'buy_price': buy_price, 'buy_date': today_date.strftime('%Y-%m-%d'), 'step': 1})
            continue

        last_buy = positions[-1]
        next_buy_price = round(last_buy['buy_price'] * (1 - down_pct), 2)
        if today_low <= next_buy_price and len(positions) < split_steps:
            step = len(positions) + 1
            signals.append({'action': 'buy', 'price': next_buy_price, 'index': i, 'date': today_date, 'step': step})
            positions.append({'buy_price': next_buy_price, 'buy_date': today_date.strftime('%Y-%m-%d'), 'step': step})

        sell_targets = []
        for pos in positions:
            target_sell = round(pos['buy_price'] * (1 + profit_pct), 2)
            buy_date = datetime.strptime(pos['buy_date'], '%Y-%m-%d')
            holding_days = (today_date - buy_date).days
            profit = (today_close - pos['buy_price']) / pos['buy_price']

            if today_high >= target_sell or (holding_days >= max_holding_days and profit > 0):
                signals.append({
                    'action': 'sell',
                    'price': target_sell if today_high >= target_sell else today_close,
                    'index': i,
                    'date': today_date,
                    'step': pos['step']
                })
                sell_targets.append(pos)

        positions = [p for p in positions if p not in sell_targets]

    return signals, positions

__all__ = ['get_signal', 'VARIANT_CONFIGS']
