
def analyze_by_period(trades, period='M'):
    """
    trades: List of {'date': 'YYYY-MM-DD', 'type': 'buy'|'sell', 'price': float, 'profit': float, 'earned': float}
    period: 'M' for month, 'Q' for quarter, 'Y' for year
    returns: pandas.DataFrame with period-wise aggregated performance
    """
    df = pd.DataFrame(trades)
    if df.empty or 'date' not in df.columns:
        return pd.DataFrame()

    df['date'] = pd.to_datetime(df['date'])
    df.set_index('date', inplace=True)

    sells = df[df['type'] == 'sell'].copy()
    sells['profit'] = pd.to_numeric(sells['profit'], errors='coerce').fillna(0)
    sells['earned'] = pd.to_numeric(sells['earned'], errors='coerce').fillna(0)

    grouped = sells.resample(period).agg({
        'profit': ['sum', 'count', 'mean'],
        'earned': 'sum'
    })

    grouped.columns = ['total_profit(%)', 'trade_count', 'avg_profit(%)', 'total_earned(₩)']
    grouped.index.name = 'period'
    return grouped.reset_index()
