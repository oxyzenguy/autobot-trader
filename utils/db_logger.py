import sqlite3
from datetime import datetime, timedelta
from autobot_trader.telegram_bot import send_message


DB_PATH = "trade_history.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
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
    conn.commit()
    conn.close()

def log_trade(ticker, side, volume, price, strategy):
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
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute("SELECT strategy, side, MAX(timestamp), SUM(price * volume) FROM trades GROUP BY strategy, side")
    rows = c.fetchall()
    conn.close()

    if not rows:
        send_message("📋 거래 요약 없음 (아직 거래 없음)")
        return

    message = "📊 전략별 최근 거래 요약:\n"
    for strategy, side, last_time, total in rows:
        message += f"• {strategy} - {side.upper()} 총액: {total:,.0f}원\n   마지막: {last_time}\n"

    send_message(message)
    
def log_trade_reason(ticker, side, strategy, reason):
    # 예시: SQLite INSERT 또는 CSV append
    with open("trade_reason_log.csv", "a", encoding="utf-8") as f:
        f.write(f"{datetime.now()},{ticker},{side},{strategy},\"{reason}\"\n")
