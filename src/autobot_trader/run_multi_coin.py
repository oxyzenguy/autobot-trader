# run_multi_coin.py - 전략별 모니터링 명령어 확장 포함 (정렬된 함수 순서)

import time
import schedule
import pyupbit
import os
import pandas as pd
from dotenv import load_dotenv
from datetime import datetime, timedelta
from inspect import signature
import sqlite3
from collections import defaultdict

from autobot_trader.strategies.moving_average import get_moving_average_signal
from autobot_trader.strategies.rsi import get_rsi_signal
from autobot_trader.strategies.bollinger import get_bollinger_signal
from autobot_trader.strategies.trend_following import get_trend_following_signal
from autobot_trader.strategies.grid_trading import get_grid_trading_signal
from autobot_trader.strategies.volatility_breakout import get_volatility_breakout_signal
from autobot_trader.strategies.momentum import get_momentum_signal

from autobot_trader.telegram_bot import send_message, listen_for_commands
from autobot_trader.log_signal import log_signal
from autobot_trader.order_executor import market_buy, market_sell
from autobot_trader.db_logger import log_trade, init_db, get_last_trade_time, log_trade_reason

load_dotenv()
ACCESS_KEY = os.getenv("UPBIT_ACCESS_KEY")
SECRET_KEY = os.getenv("UPBIT_SECRET_KEY")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
upbit = pyupbit.Upbit(ACCESS_KEY, SECRET_KEY)
init_db()

STRATEGY_BUDGETS = {
    "moving_average": 10000,
    "rsi": 8000,
    "bollinger": 12000,
    "trend_following": 15000,
    "grid_trading": 10000,
    "volatility_breakout": 10000,
    "momentum": 10000
}

STRATEGY_INTERVALS = {
    "rsi": "minute1",
    "bollinger": "minute15",
    "moving_average": "minute30",
    "trend_following": "minute60",
    "grid_trading": "minute1",
    "volatility_breakout": "day",
    "momentum": "minute15"
}

DUPLICATE_BUY_COOLDOWN = {
    "moving_average": 30,
    "rsi": 10,
    "bollinger": 30,
    "trend_following": 60,
    "grid_trading": 5,
    "volatility_breakout": 1440,
    "momentum": 30
}

TAKE_PROFIT = 0.05
STOP_LOSS = -0.03
POSITION_HISTORY = {}
TRADE_REASON_LOG = "trade_reason_log.csv"

def get_dynamic_budget(strategy, base_budget):
    try:
        total_base = sum(STRATEGY_BUDGETS.values())
        krw_balance = upbit.get_balance("KRW")
        if krw_balance is None or total_base == 0:
            return base_budget
        scale = min(1.0, krw_balance / total_base)
        dynamic = int(base_budget * scale)
        print(f"[BUDGET] {strategy}: 동적 예산 {dynamic:,}원 (보유 KRW: {krw_balance:,})")
        return dynamic
    except Exception as e:
        print(f"[BUDGET] 예산 계산 오류: {e}")
        return base_budget

def handle_command(command):
    if command == "/내포지션":
        if not POSITION_HISTORY:
            send_message("📭 현재 보유 중인 포지션이 없습니다.")
            return
        msg = "📦 <b>보유 포지션 현황</b>\n"
        for key, (price, volume) in POSITION_HISTORY.items():
            msg += f"• {key} | 매수가: {price:,.0f} | 수량: {volume:.4f}\n"
        send_message(msg)

    elif command == "/실현손익":
        conn = sqlite3.connect("trade_history.db")
        c = conn.cursor()
        c.execute("SELECT strategy, side, price * volume FROM trades")
        rows = c.fetchall()
        pnl = defaultdict(lambda: {"buy": 0, "sell": 0})
        for strategy, side, value in rows:
            pnl[strategy][side] += value
        conn.close()
        msg = "💹 <b>전략별 실현 손익</b>\n"
        for strategy, data in pnl.items():
            gain = data["sell"] - data["buy"]
            msg += f"• {strategy}: {gain:,.0f}원 (매수 {data['buy']:,.0f} / 매도 {data['sell']:,.0f})\n"
        send_message(msg)

    elif command == "/이익랭킹":
        conn = sqlite3.connect("trade_history.db")
        c = conn.cursor()
        c.execute("SELECT strategy, side, price * volume FROM trades")
        rows = c.fetchall()
        pnl = defaultdict(lambda: {"buy": 0, "sell": 0})
        for strategy, side, value in rows:
            pnl[strategy][side] += value
        ranking = sorted(pnl.items(), key=lambda kv: kv[1]["sell"] - kv[1]["buy"], reverse=True)
        msg = "🏆 <b>전략 이익 랭킹</b>\n"
        for i, (strategy, data) in enumerate(ranking, 1):
            gain = data["sell"] - data["buy"]
            msg += f"{i}. {strategy}: {gain:,.0f}원\n"
        send_message(msg)

    elif command == "/다음매수예정":
        msg = "⏳ <b>전략별 다음 매수 가능 시점</b>\n"
        for strategy in STRATEGY_BUDGETS:
            last_time, last_side = get_last_trade_time("KRW-BTC", strategy)
            if last_side != "buy":
                continue
            cooldown = DUPLICATE_BUY_COOLDOWN.get(strategy, 30)
            delta = datetime.now() - last_time
            remain = cooldown - (delta.total_seconds() / 60)
            if remain > 0:
                msg += f"• {strategy}: {remain:.1f}분 후\n"
            else:
                msg += f"• {strategy}: 가능\n"
        send_message(msg)
    
    elif command == "/현금잔고":
        krw = upbit.get_balance("KRW")
        msg = f"💰 <b>현재 보유 현금</b>\n{krw:,.0f}원" if krw is not None else "잔고 조회 실패"
        send_message(msg)
        return

    elif command == "/전략예산":
        msg = "📊 <b>전략별 현재 적용 예산</b>\n"
        for strategy, base in STRATEGY_BUDGETS.items():
            dynamic = get_dynamic_budget(strategy, base)
            msg += f"• {strategy}: {dynamic:,}원 (기준: {base:,})\n"
        send_message(msg)
        return

def run_strategy(name, func, ticker):
    print(f"\n⏱️ [{name}] 전략 실행 중... ({ticker})")
    try:
        interval = STRATEGY_INTERVALS.get(name, "day")
        df = pyupbit.get_ohlcv(ticker, interval=interval, count=50)
        if df is None or len(df) < 2:
            print("❌ OHLCV 데이터 부족")
            return

        budget = get_dynamic_budget(name, STRATEGY_BUDGETS.get(name, 10000))
        result = func(df, amount=budget)
        if result is None or "signal" not in result:
            print(f"💤 [{name}] 시그널 없음")
            return

        signal = result["signal"]
        reason = result.get("reason", "N/A")
        amount = result.get("amount", budget)

        price = pyupbit.get_current_price(ticker)
        if price is None:
            print("❌ 가격 조회 실패")
            return

        last_time, last_side = get_last_trade_time(ticker, name)
        cooldown = DUPLICATE_BUY_COOLDOWN.get(name, 30)
        time_diff = (datetime.now() - last_time).total_seconds() / 60

        if signal == "buy" and last_side == "buy" and time_diff < cooldown:
            print(f"⛔ {name} 최근 매수({time_diff:.1f}분 전), 재매수 차단")
            return

        if signal == "buy":
            if amount < 5000:
                print("⛔ 최소 주문 금액 미만")
                return
            result_order = market_buy(ticker, amount)
            if result_order:
                volume = float(result_order['executed_volume'])
                log_trade(ticker, "buy", volume, price, name)
                log_trade_reason(ticker, "buy", name, reason)
                log_signal(name, ticker, signal, price)
                POSITION_HISTORY[name + ticker] = (price, volume)
                send_message(f"📈 <b>[{name}] {ticker} 매수 완료</b>\n수량: {volume} ({amount:,}원)\n이유: {reason}")

        elif signal == "sell":
            balance = upbit.get_balance(ticker)
            if balance is None or balance < 0.0001:
                print("⛔ 매도할 잔고 부족")
                return
            key = name + ticker
            buy_price, volume = POSITION_HISTORY.get(key, (None, None))
            if buy_price:
                pnl = (price - buy_price) / buy_price
                if pnl < STOP_LOSS:
                    print(f"📉 [{name}] 손절 조건 실행: {pnl*100:.2f}%")
                elif pnl > TAKE_PROFIT:
                    print(f"💰 [{name}] 익절 조건 실행: {pnl*100:.2f}%")
            result_order = market_sell(ticker, balance)
            if result_order:
                log_trade(ticker, "sell", balance, price, name)
                log_trade_reason(ticker, "sell", name, reason)
                log_signal(name, ticker, signal, price)
                send_message(f"📉 <b>[{name}] {ticker} 매도 완료</b>\n수량: {balance}\n이유: {reason}")
                POSITION_HISTORY.pop(key, None)

    except Exception as e:
        send_message(f"⚠️ <b>[{name}] 전략 실행 오류 ({ticker})</b>: {e}")


def schedule_strategies():
    print("🛠️ 전략별 실행 주기 스케줄링...")
    strategies = {
        "moving_average": get_moving_average_signal,
        "rsi": get_rsi_signal,
        "bollinger": get_bollinger_signal,
        "trend_following": get_trend_following_signal,
        "grid_trading": get_grid_trading_signal,
        "volatility_breakout": get_volatility_breakout_signal,
        "momentum": get_momentum_signal
    }
    tickers = ["KRW-BTC", "KRW-ETH"]

    for name, func in strategies.items():
        for ticker in tickers:
            if name in ["grid_trading", "rsi"]:
                schedule.every(1).minutes.do(run_strategy, name, func, ticker)
            elif name in ["bollinger", "momentum"]:
                schedule.every(15).minutes.do(run_strategy, name, func, ticker)
            elif name in ["moving_average", "trend_following"]:
                schedule.every(30).minutes.do(run_strategy, name, func, ticker)
            elif name == "volatility_breakout":
                schedule.every().day.at("09:01").do(run_strategy, name, func, ticker)
                
def main():
    print("🚀 전략 다중 자동매매 루프 시작")
    schedule_strategies()
    listen_for_commands(handle_command)
    while True:
        schedule.run_pending()
        time.sleep(1)

if __name__ == "__main__":
    main()
