# ✅ 통합 전략: trend_combined (OR 조건 기반)
def get_signal(df):
    result = get_trend_combined_signal(df)
    if isinstance(result, dict):
        return [result]  # 백테스트 시스템이 for-loop로 처리 가능하게 리스트로 반환
    return []

# ✅ 통합 전략: trend_combined (OR 조건 기반)
def get_trend_combined_signal(df, amount=15000):
    if df is None or len(df) < 100:
        return None

    df = df.copy()
    short_ma = df["close"].rolling(window=20).mean()
    long_ma = df["close"].rolling(window=100).mean()
    ma5 = df["close"].rolling(window=5).mean()
    ma20 = df["close"].rolling(window=20).mean()
    price = df["close"].iloc[-1]

    # RSI 계산
    delta = df['close'].diff()
    gain = delta.where(delta > 0, 0)
    loss = -delta.where(delta < 0, 0)
    avg_gain = gain.rolling(window=14).mean()
    avg_loss = loss.rolling(window=14).mean()
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))

    curr_rsi = rsi.iloc[-1]
    prev_rsi = rsi.iloc[-2]

    prev_short = short_ma.iloc[-2]
    curr_short = short_ma.iloc[-1]
    prev_long = long_ma.iloc[-2]
    curr_long = long_ma.iloc[-1]
    prev_ma5 = ma5.iloc[-2]
    curr_ma5 = ma5.iloc[-1]
    prev_ma20 = ma20.iloc[-2]
    curr_ma20 = ma20.iloc[-1]

    # ✅ 하나라도 만족 시 매수
    buy_signal = (
        (prev_short <= prev_long and curr_short > curr_long and price > curr_long) or
        (prev_ma5 < prev_ma20 and curr_ma5 > curr_ma20 and price > curr_ma5) or
        (prev_rsi < 60 and curr_rsi > 60)
    )

    sell_signal = (
        (prev_short >= prev_long and curr_short < curr_long and price < curr_long) or
        (prev_ma5 > prev_ma20 and curr_ma5 < curr_ma20 and price < curr_ma5) or
        (prev_rsi > 70 and curr_rsi < 70)
    )

    if buy_signal:
        return {
            "signal": "buy",
            "reason": "조건 중 하나 이상 만족 (OR 조건)",
            "amount": amount
        }
    elif sell_signal:
        return {
            "signal": "sell",
            "reason": "조건 중 하나 이상 하락 전환",
            "amount": None
        }

    return None


def get_signal(df):
    result = get_trend_combined_signal(df)
    if isinstance(result, dict):
        return [result]
    return []


# ✅ 유지 전략: rsi 그대로 사용
# ✅ 유지 전략: magicsplit 그대로 사용
