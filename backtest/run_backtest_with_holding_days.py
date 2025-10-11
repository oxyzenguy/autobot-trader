
def run_backtest(strategy_func, strategy_name, count=365, market="KRW-BTC"):
    import pyupbit
    import pandas as pd
    from datetime import datetime

    df = pyupbit.get_ohlcv(market, interval="day", count=count)
    if df is None or df.empty:
        print("❌ 데이터 로드 실패")
        return []

    signals = strategy_func(df)
    trades = []
    positions = []
    investment = 5000

    for sig in signals:
        i = sig["index"]
        date = df.index[i]
        price = sig["price"]
        action = sig["action"]

        if action == "buy":
            trades.append({
                "date": date.strftime("%Y-%m-%d"),
                "type": "buy",
                "price": price
            })
            positions.append({
                "buy_price": price,
                "buy_index": i,
                "buy_date": date
            })
        elif action == "sell" and positions:
            matched = positions.pop(0)
            profit_rate = (price - matched["buy_price"]) / matched["buy_price"]
            earned = round(profit_rate * investment, 2)
            holding_days = (date - matched["buy_date"]).days

            trades.append({
                "date": date.strftime("%Y-%m-%d"),
                "type": "sell",
                "price": price,
                "profit": round(profit_rate * 100, 2),
                "earned": earned,
                "holding_days": holding_days
            })

    # 저장
    df_trades = pd.DataFrame(trades)
    df_trades.to_csv(f"C:/Users/oxyze/autobot-trader/backtest/trades_{strategy_name}.csv", index=False, encoding="utf-8-sig")
    print(f"📄 거래 내역 CSV 저장 완료: trades_{strategy_name}.csv")
    return trades
