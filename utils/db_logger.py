import sqlite3
from datetime import datetime, timedelta

DB_PATH = "trade_history.db"

def _get_send_message():
    try:
        from utils.bot import send_message
        return send_message
    except Exception:
        return None

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    # 1. 실거래 및 일반 거래 기록
    c.execute('''
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            ticker TEXT,
            side TEXT,
            volume REAL,
            price REAL,
            strategy TEXT
        )
    ''')
    # 2. 시계열 자산 추이 스냅샷 (Equity Curve, MDD, B&H 분석용)
    c.execute('''
        CREATE TABLE IF NOT EXISTS equity_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            market TEXT,
            krw_balance REAL,
            coin_balance REAL,
            coin_price REAL,
            total_equity REAL,
            benchmark_price REAL,
            unrealized_pnl REAL,
            real_dca_eval REAL
        )
    ''')
    try:
        c.execute('ALTER TABLE equity_snapshots ADD COLUMN real_dca_eval REAL')
    except Exception:
        pass
    # 3. 가상매매 상세 거래 내역 (승률, 손익비, 마틴게일 단계 분석용)
    c.execute('''
        CREATE TABLE IF NOT EXISTS paper_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            market TEXT,
            action TEXT,
            side TEXT,
            price REAL,
            volume REAL,
            cost_or_revenue REAL,
            pnl REAL,
            cycle INTEGER
        )
    ''')
    conn.commit()
    conn.close()

def log_equity_snapshot(market: str, krw_balance: float, coin_balance: float, coin_price: float, total_equity: float, benchmark_price: float = None, unrealized_pnl: float = 0.0, real_dca_eval: float = None):
    """시계열 자산 스냅샷을 DB에 기록합니다."""
    init_db()
    if benchmark_price is None:
        benchmark_price = coin_price
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        INSERT INTO equity_snapshots (timestamp, market, krw_balance, coin_balance, coin_price, total_equity, benchmark_price, unrealized_pnl, real_dca_eval)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        market, krw_balance, coin_balance, coin_price, total_equity, benchmark_price, unrealized_pnl, real_dca_eval
    ))
    conn.commit()
    conn.close()

def log_paper_trade(market: str, action: str, side: str, price: float, volume: float, cost_or_revenue: float, pnl: float = 0.0, cycle: int = 0, timestamp: str = None):
    """가상매매 체결 내역을 DB에 기록합니다."""
    init_db()
    if timestamp is None:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        INSERT INTO paper_trades (timestamp, market, action, side, price, volume, cost_or_revenue, pnl, cycle)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        timestamp, market, action, side, price, volume, cost_or_revenue, pnl, cycle
    ))
    conn.commit()
    conn.close()

def log_trade(ticker, side, volume, price, strategy):
    init_db()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        INSERT INTO trades (timestamp, ticker, side, volume, price, strategy)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        ticker, side, volume, price, strategy
    ))
    conn.commit()
    conn.close()

def get_last_trade_time(ticker, strategy):
    init_db()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        SELECT timestamp, side FROM trades
        WHERE ticker = ? AND strategy = ?
        ORDER BY timestamp DESC LIMIT 1
    ''', (ticker, strategy))
    row = c.fetchone()
    conn.close()

    if row:
        last_time = datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S")
        return last_time, row[1]
    else:
        return datetime.min, None

def send_strategy_summary():
    init_db()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute("SELECT strategy, side, MAX(timestamp), SUM(price * volume) FROM trades GROUP BY strategy, side")
    rows = c.fetchall()
    conn.close()

    send_msg = _get_send_message()
    if not send_msg:
        return

    if not rows:
        send_msg("📋 거래 요약 없음 (아직 거래 없음)")
        return

    message = "📊 전략별 최근 거래 요약:\n"
    for strategy, side, last_time, total in rows:
        message += f"• {strategy} - {side.upper()} 총액: {total:,.0f}원\n   마지막: {last_time}\n"

    send_msg(message)
    
def log_trade_reason(ticker, side, strategy, reason):
    with open("trade_reason_log.csv", "a", encoding="utf-8") as f:
        f.write(f"{datetime.now()},{ticker},{side},{strategy},\"{reason}\"\n")

