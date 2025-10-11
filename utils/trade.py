def buy_market(upbit, ticker, amount):
    return upbit.buy_market_order(ticker, amount)

def sell_market(upbit, ticker, volume):
    return upbit.sell_market_order(ticker, volume)

def buy_limit(upbit, ticker, price, volume):
    return upbit.buy_limit_order(ticker, price, volume)

def sell_limit(upbit, ticker, price, volume):
    return upbit.sell_limit_order(ticker, price, volume)

def get_order_volume(amount, price):
    # 수수료 고려하여 소수점 조정 (0.05% 수수료 적용 예)
    fee_rate = 0.0005
    return round((amount * (1 - fee_rate)) / price, 8)
