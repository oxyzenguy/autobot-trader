# ✅ 전략: Bollinger Band + RSI 평균회귀 전략
import pandas as pd

def get_bollinger_rsi_signal(df, rsi_threshold=40, stddev=2, holding_limit=10):
    signals = []
    position = None  # 현재 보유 포지션

    df = df.copy()
    df['ma20'] = df['close'].rolling(window=20).mean()
    df['stddev'] = df['close'].rolling(window=20).std()
    df['upper'] = df['ma20'] + stddev * df['stddev']
    df['lower'] = df['ma20'] - stddev * df['stddev']

    # RSI 계산
    delta = df['close'].diff()
    gain = delta.where(delta > 0, 0)
    loss = -delta.where(delta < 0, 0)
    avg_gain = gain.rolling(window=14).mean()
    avg_loss = loss.rolling(window=14).mean()
    rs = avg_gain / avg_loss
    df['rsi'] = 100 - (100 / (1 + rs))

    for i in range(20, len(df)):
        price = df['close'].iloc[i]
        ma20 = df['ma20'].iloc[i]
        lower = df['lower'].iloc[i]
        rsi = df['rsi'].iloc[i]
        date = df.index[i]

        # 진입 조건: 밴드 하단 이하 + RSI 과매도
        if position is None and price < lower and rsi < rsi_threshold:
            position = {
                'buy_price': price,
                'buy_index': i,
                'buy_date': date
            }
            signals.append({
                'action': 'buy',
                'price': price,
                'index': i,
                'date': date
            })

        # 청산 조건: 20일 이평 도달 또는 보유기간 초과
        elif position is not None:
            holding_days = (date - position['buy_date']).days
            if price >= ma20 or holding_days > holding_limit:
                signals.append({
                    'action': 'sell',
                    'price': price,
                    'index': i,
                    'date': date
                })
                position = None

    return signals

def get_signal(df):
    return get_bollinger_rsi_signal(df)
