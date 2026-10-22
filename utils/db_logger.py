import os
import json
import sqlite3
from datetime import datetime
from typing import Dict, Any, List, Optional

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, "trade_history.db")


def _get_send_message():
    try:
        from utils.bot import send_message
        return send_message
    except Exception:
        return None


def init_db(db_path: Optional[str] = None):
    """모든 실전 트레이딩, 전략 히스토리, 계좌 스냅샷 테이블 초기화 및 마이그레이션"""
    conn = sqlite3.connect(db_path or DB_PATH)
    c = conn.cursor()

    # 1. 실전 매매 체결 기록 (trades)
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

    # 기존 trades 테이블에 신규 컬럼 안전 추가 (마이그레이션)
    columns_to_add = [
        ("market", "TEXT"),
        ("action", "TEXT"),
        ("cost_or_revenue", "REAL"),
        ("pnl", "REAL DEFAULT 0.0"),
        ("cycle", "INTEGER DEFAULT 0"),
        ("order_uuid", "TEXT"),
        ("strategy_id", "TEXT")
    ]
    for col_name, col_type in columns_to_add:
        try:
            c.execute(f"ALTER TABLE trades ADD COLUMN {col_name} {col_type}")
        except Exception:
            pass

    # 1-1. 주문 체결 내구성 체크포인트 테이블 (order_execution_checkpoints)
    c.execute('''
        CREATE TABLE IF NOT EXISTS order_execution_checkpoints (
            order_uuid TEXT PRIMARY KEY,
            market TEXT,
            credited_volume REAL DEFAULT 0.0,
            credited_funds REAL DEFAULT 0.0,
            last_updated TEXT,
            terminal INTEGER DEFAULT 0
        )
    ''')

    # 2. 1~3달 주기 전략 교체 및 성과 평가를 위한 전략 히스토리 테이블 (strategy_history)
    c.execute('''
        CREATE TABLE IF NOT EXISTS strategy_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            strategy_id TEXT UNIQUE,
            strategy_name TEXT,
            market TEXT,
            start_time TEXT,
            end_time TEXT,
            initial_capital REAL,
            final_capital REAL,
            realized_pnl REAL,
            return_pct REAL,
            win_rate REAL,
            trade_count INTEGER,
            mdd REAL,
            profit_factor REAL,
            parameters_json TEXT,
            status TEXT DEFAULT 'ACTIVE',
            notes TEXT
        )
    ''')

    # 3. 실계좌 전체 자산 시계열 스냅샷 (total_account_snapshots)
    c.execute('''
        CREATE TABLE IF NOT EXISTS total_account_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            total_equity REAL,
            total_cost REAL,
            unrealized_pnl REAL,
            return_pct REAL,
            krw_balance REAL,
            coin_eval REAL,
            coins_json TEXT
        )
    ''')

    # 4. 개별 전략별 시계열 자산 추이 스냅샷 (strategy_snapshots)
    c.execute('''
        CREATE TABLE IF NOT EXISTS strategy_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            strategy_id TEXT,
            market TEXT,
            strategy_equity REAL,
            invested_krw REAL,
            cash_balance REAL,
            coin_balance REAL,
            coin_price REAL,
            realized_pnl REAL,
            unrealized_pnl REAL,
            return_pct REAL
        )
    ''')

    # 기존 레거시 테이블 보존 (필요 시)
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


# =============================================================================
# 실전 매매 기록 함수
# =============================================================================
def log_real_trade(
    market: str,
    ticker: str,
    side: str,
    action: str,
    price: float,
    volume: float,
    cost_or_revenue: float,
    pnl: float = 0.0,
    cycle: int = 0,
    strategy: str = "MARTINGALE_2X",
    strategy_id: str = None,
    order_uuid: str = None,
    timestamp: str = None,
    cumulative_volume: float = None,
    cumulative_funds: float = None,
    terminal: bool = None,
    idempotent: bool = False,
    db_path: Optional[str] = None
):
    """실제 체결된 거래 내역을 trades 테이블 및 체크포인트 테이블에 안전하게 기록합니다.

    idempotent=True 이고 order_uuid와 cumulative_volume/funds가 주어지면, 하나의
    BEGIN IMMEDIATE 트랜잭션 안에서 DB 기준값(체크포인트·기존 bid 기록 중 큰 값) 조회,
    미반영 차액 계산, trades 삽입, 체크포인트 갱신을 모두 수행한다. 동시 재처리가 있어도
    차액만 정확히 한 번 기록되며, 차액이 없으면 아무 행도 쓰지 않는다.
    반환: {"recorded": bool, "delta_volume", "delta_funds", "base_volume", "base_funds"} (idempotent 모드),
    그 외에는 None.
    """
    path = db_path or DB_PATH
    init_db(path)
    if timestamp is None:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    result = None
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            c = conn.cursor()
            if idempotent and order_uuid and cumulative_volume is not None and cumulative_funds is not None:
                c.execute(
                    "SELECT credited_volume, credited_funds FROM order_execution_checkpoints WHERE order_uuid = ?",
                    (order_uuid,),
                )
                row = c.fetchone()
                cp_vol, cp_funds = (float(row[0] or 0.0), float(row[1] or 0.0)) if row else (0.0, 0.0)
                c.execute(
                    "SELECT COALESCE(SUM(volume), 0.0), COALESCE(SUM(cost_or_revenue), 0.0) "
                    "FROM trades WHERE order_uuid = ? AND side = ?",
                    (order_uuid, side),
                )
                tr_vol, tr_funds = (float(x or 0.0) for x in c.fetchone())
                base_vol, base_funds = max(cp_vol, tr_vol), max(cp_funds, tr_funds)
                delta_vol = float(cumulative_volume) - base_vol
                delta_funds = float(cumulative_funds) - base_funds
                recorded = delta_vol > 1e-12 and delta_funds > 0
                if recorded:
                    c.execute('''
                        INSERT INTO trades (
                            timestamp, ticker, market, side, action, price, volume,
                            cost_or_revenue, pnl, cycle, strategy, strategy_id, order_uuid
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        timestamp, ticker, market, side, action, delta_funds / delta_vol, delta_vol,
                        delta_funds, pnl, cycle, strategy, strategy_id, order_uuid
                    ))
                if recorded or terminal:
                    new_vol = max(base_vol, float(cumulative_volume))
                    new_funds = max(base_funds, float(cumulative_funds))
                    c.execute('''
                        INSERT INTO order_execution_checkpoints (
                            order_uuid, market, credited_volume, credited_funds, last_updated, terminal
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        ON CONFLICT(order_uuid) DO UPDATE SET
                            credited_volume = MAX(credited_volume, excluded.credited_volume),
                            credited_funds = MAX(credited_funds, excluded.credited_funds),
                            last_updated = excluded.last_updated,
                            terminal = MAX(terminal, excluded.terminal)
                    ''', (order_uuid, market, new_vol, new_funds, timestamp, 1 if terminal else 0))
                result = {
                    "recorded": recorded,
                    "delta_volume": delta_vol if recorded else 0.0,
                    "delta_funds": delta_funds if recorded else 0.0,
                    "base_volume": base_vol,
                    "base_funds": base_funds,
                }
            else:
                c.execute('''
                    INSERT INTO trades (
                        timestamp, ticker, market, side, action, price, volume,
                        cost_or_revenue, pnl, cycle, strategy, strategy_id, order_uuid
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    timestamp, ticker, market, side, action, price, volume,
                    cost_or_revenue, pnl, cycle, strategy, strategy_id, order_uuid
                ))

                if order_uuid and (cumulative_volume is not None or cumulative_funds is not None):
                    cum_vol = cumulative_volume if cumulative_volume is not None else volume
                    cum_funds = cumulative_funds if cumulative_funds is not None else cost_or_revenue
                    is_term = 1 if terminal else 0
                    c.execute('''
                        INSERT INTO order_execution_checkpoints (
                            order_uuid, market, credited_volume, credited_funds, last_updated, terminal
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        ON CONFLICT(order_uuid) DO UPDATE SET
                            credited_volume = excluded.credited_volume,
                            credited_funds = excluded.credited_funds,
                            last_updated = excluded.last_updated,
                            terminal = excluded.terminal
                    ''', (order_uuid, market, cum_vol, cum_funds, timestamp, is_term))
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()

    if result is not None and not result["recorded"]:
        return result

    # 체결 즉시 비동기로 계좌 스냅샷 기록 및 클라우드 동기화 트리거
    try:
        from utils.sync_manager import trigger_snapshot_sync
        trigger_snapshot_sync()
    except Exception:
        pass
    return result


def get_order_settled_totals(order_uuid: str, db_path: Optional[str] = None) -> Dict[str, Any]:
    """
    주문 UUID에 대해 DB에 이미 기록된 누적 체결 수량과 체결 금액을 반환합니다.
    order_execution_checkpoints 테이블과 trades 테이블 모두를 조회하여
    내구성 있게 커밋된 최대 누적값을 안전하게 도출합니다.
    """
    path = db_path or DB_PATH
    if not os.path.exists(path):
        return {"credited_volume": 0.0, "credited_funds": 0.0, "terminal": False}

    conn = sqlite3.connect(path)
    c = conn.cursor()
    cp_vol, cp_funds, cp_term = 0.0, 0.0, False
    tr_vol, tr_funds = 0.0, 0.0
    try:
        c.execute(
            "SELECT credited_volume, credited_funds, terminal FROM order_execution_checkpoints WHERE order_uuid = ?",
            (order_uuid,)
        )
        row = c.fetchone()
        if row:
            cp_vol = float(row[0] or 0.0)
            cp_funds = float(row[1] or 0.0)
            cp_term = bool(row[2])
    except sqlite3.OperationalError:
        pass

    try:
        c.execute(
            "SELECT COALESCE(SUM(volume), 0.0), COALESCE(SUM(cost_or_revenue), 0.0) "
            "FROM trades WHERE order_uuid = ? AND side = 'bid'",
            (order_uuid,)
        )
        row = c.fetchone()
        if row:
            tr_vol = float(row[0] or 0.0)
            tr_funds = float(row[1] or 0.0)
    except sqlite3.OperationalError:
        pass

    conn.close()

    return {
        "credited_volume": max(cp_vol, tr_vol),
        "credited_funds": max(cp_funds, tr_funds),
        "terminal": cp_term
    }


def record_order_terminal_checkpoint(
    order_uuid: str,
    market: str,
    cumulative_volume: float = 0.0,
    cumulative_funds: float = 0.0,
    db_path: Optional[str] = None
) -> None:
    """체결 증가분 없이 종결된 주문(또는 잔여 미체결 취소 등)의 체크포인트를 DB에 기록합니다."""
    path = db_path or DB_PATH
    init_db(path)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect(path, timeout=30)
    try:
        with conn:
            c = conn.cursor()
            c.execute('''
                INSERT INTO order_execution_checkpoints (
                    order_uuid, market, credited_volume, credited_funds, last_updated, terminal
                ) VALUES (?, ?, ?, ?, ?, 1)
                ON CONFLICT(order_uuid) DO UPDATE SET
                    credited_volume = MAX(credited_volume, excluded.credited_volume),
                    credited_funds = MAX(credited_funds, excluded.credited_funds),
                    last_updated = excluded.last_updated,
                    terminal = 1
            ''', (order_uuid, market, cumulative_volume, cumulative_funds, timestamp))
    finally:
        conn.close()


def log_trade(ticker, side, volume, price, strategy):
    """레거시 호환용 거래 기록 함수"""
    market = f"KRW-{ticker}" if not ticker.startswith("KRW-") else ticker
    clean_ticker = ticker.replace("KRW-", "")
    cost = volume * price
    log_real_trade(
        market=market,
        ticker=clean_ticker,
        side=side,
        action="TRADE",
        price=price,
        volume=volume,
        cost_or_revenue=cost,
        strategy=strategy
    )


# =============================================================================
# 전체 계좌 및 전략별 시계열 스냅샷 기록 함수
# =============================================================================
def log_total_account_snapshot(
    total_equity: float,
    total_cost: float,
    unrealized_pnl: float,
    return_pct: float,
    krw_balance: float,
    coin_eval: float,
    coins_json: str = "{}"
):
    """업비트 전체 계좌 총 평가액 및 자산 구성을 스냅샷으로 기록합니다."""
    init_db()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        INSERT INTO total_account_snapshots (
            timestamp, total_equity, total_cost, unrealized_pnl,
            return_pct, krw_balance, coin_eval, coins_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        total_equity, total_cost, unrealized_pnl,
        return_pct, krw_balance, coin_eval, coins_json
    ))
    conn.commit()
    conn.close()


def log_strategy_snapshot(
    strategy_id: str,
    market: str,
    strategy_equity: float,
    invested_krw: float,
    cash_balance: float,
    coin_balance: float,
    coin_price: float,
    realized_pnl: float,
    unrealized_pnl: float,
    return_pct: float
):
    """개별 가동 전략의 시계열 성과 스냅샷을 기록합니다."""
    init_db()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        INSERT INTO strategy_snapshots (
            timestamp, strategy_id, market, strategy_equity, invested_krw,
            cash_balance, coin_balance, coin_price, realized_pnl,
            unrealized_pnl, return_pct
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        strategy_id, market, strategy_equity, invested_krw,
        cash_balance, coin_balance, coin_price, realized_pnl,
        unrealized_pnl, return_pct
    ))
    conn.commit()
    conn.close()


# =============================================================================
# 전략 히스토리 (1~3달 평가 및 보관/불러오기) 관리 함수
# =============================================================================
def save_or_update_strategy(
    strategy_id: str,
    strategy_name: str,
    market: str,
    start_time: str,
    initial_capital: float,
    parameters_dict: Dict[str, Any],
    status: str = "ACTIVE",
    end_time: Optional[str] = None,
    final_capital: Optional[float] = None,
    realized_pnl: float = 0.0,
    return_pct: float = 0.0,
    win_rate: float = 0.0,
    trade_count: int = 0,
    mdd: float = 0.0,
    profit_factor: float = 0.0,
    notes: Optional[str] = None
):
    """전략 메타데이터 및 성과 지표를 strategy_history에 저장하거나 갱신합니다."""
    init_db()
    param_str = json.dumps(parameters_dict, ensure_ascii=False)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute("SELECT id FROM strategy_history WHERE strategy_id = ?", (strategy_id,))
    row = c.fetchone()

    if row:
        c.execute('''
            UPDATE strategy_history SET
                strategy_name = ?, market = ?, start_time = ?, end_time = ?,
                initial_capital = ?, final_capital = ?, realized_pnl = ?,
                return_pct = ?, win_rate = ?, trade_count = ?, mdd = ?,
                profit_factor = ?, parameters_json = ?, status = ?, notes = ?
            WHERE strategy_id = ?
        ''', (
            strategy_name, market, start_time, end_time,
            initial_capital, final_capital, realized_pnl,
            return_pct, win_rate, trade_count, mdd,
            profit_factor, param_str, status, notes, strategy_id
        ))
    else:
        c.execute('''
            INSERT INTO strategy_history (
                strategy_id, strategy_name, market, start_time, end_time,
                initial_capital, final_capital, realized_pnl, return_pct,
                win_rate, trade_count, mdd, profit_factor, parameters_json,
                status, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            strategy_id, strategy_name, market, start_time, end_time,
            initial_capital, final_capital, realized_pnl, return_pct,
            win_rate, trade_count, mdd, profit_factor, param_str,
            status, notes
        ))

    conn.commit()
    conn.close()


def archive_strategy(strategy_id: str, notes: Optional[str] = None):
    """현재 가동 중인 전략을 종료 및 보관(ARCHIVED) 처리합니다."""
    init_db()
    end_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    if notes:
        c.execute('''
            UPDATE strategy_history
            SET status = 'ARCHIVED', end_time = ?, notes = ?
            WHERE strategy_id = ?
        ''', (end_time, notes, strategy_id))
    else:
        c.execute('''
            UPDATE strategy_history
            SET status = 'ARCHIVED', end_time = ?
            WHERE strategy_id = ?
        ''', (end_time, strategy_id))
    conn.commit()
    conn.close()


def get_active_strategies() -> List[Dict[str, Any]]:
    """현재 활성화(가동 중) 상태인 전략 목록을 반환합니다."""
    init_db()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM strategy_history WHERE status = 'ACTIVE' ORDER BY start_time ASC")
    rows = c.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_strategy_history(include_active: bool = True) -> List[Dict[str, Any]]:
    """과거 및 현재 전략 전체 히스토리 목록을 반환합니다."""
    init_db()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    if include_active:
        c.execute("SELECT * FROM strategy_history ORDER BY id DESC")
    else:
        c.execute("SELECT * FROM strategy_history WHERE status = 'ARCHIVED' ORDER BY id DESC")
    rows = c.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_strategy_by_id(strategy_id: str) -> Optional[Dict[str, Any]]:
    """전략 ID로 특정 전략의 상세 정보 및 파라미터를 조회합니다."""
    init_db()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM strategy_history WHERE strategy_id = ?", (strategy_id,))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None


# 레거시 호환용 함수
def log_equity_snapshot(market: str, krw_balance: float, coin_balance: float, coin_price: float, total_equity: float, benchmark_price: float = None, unrealized_pnl: float = 0.0, real_dca_eval: float = None):
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
