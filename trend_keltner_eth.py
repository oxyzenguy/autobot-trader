#!/usr/bin/env python3
"""
trend_keltner_eth.py
====================
Standalone 4H Keltner Channel Breakout Trend Daemon for KRW-ETH:
- Strategy: 4H Keltner Channel Breakout (20 EMA + 2.0 ATR + 200 EMA Macro Filter)
- Rule:
  1. Macro Trend: 4H Close > 200 EMA
  2. Entry: 4H Completed Close > Keltner Upper Band (20 EMA + 2.0 ATR)
  3. Exit: 4H Completed Close < 20 EMA
  4. Emergency SL: Current Price <= Entry Price * -10.0%
- Handover Guard:
  - Protects PROTECTED_BALANCES["KRW-ETH"] (0.40756151 ETH)
  - Automatically waits until main bot's 18-step exit (3,552,000 KRW basket ask) completes
"""

import fcntl
import json
import logging
import math
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, Optional, Tuple

import pandas as pd

# Set up project base path
BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from config import (
    PROTECTED_BALANCES,
    MIN_ORDER_KRW,
    INVESTMENTS,
    get_upbit_client,
)
from utils.db_logger import log_real_trade, init_db
from strategy.keltner_trend import (
    calculate_keltner_channel,
    get_keltner_signals,
    fetch_4h_candles_upbit,
)

MARKET = "KRW-ETH"
TICKER = "ETH"
STATE_FILE = BASE_DIR / "trend_keltner_state_KRW_ETH.json"
MAIN_BOT_STATE_FILE = BASE_DIR / "real_strategy_state_KRW_ETH.json"
LOCK_FILE = Path(tempfile.gettempdir()) / "trend_keltner_KRW_ETH.lock"
DB_PATH = BASE_DIR / "trade_history.db"

# Strategy Parameters
DEFAULT_BUDGET_KRW = 500_000.0  # Default trade allocation
EMERGENCY_SL_PCT = 0.10        # -10.0% Hard Stop Loss
POLL_INTERVAL_SEC = 10.0       # 10s evaluation loop


def safe_get_current_price(market: str, retries: int = 3, delay: float = 0.5) -> Optional[float]:
    import pyupbit
    for i in range(retries):
        try:
            p = pyupbit.get_current_price(market)
            if p is not None and not isinstance(p, dict):
                p_float = float(p)
                if p_float > 0:
                    return p_float
        except Exception:
            pass
        if i < retries - 1:
            time.sleep(delay)
    return None


def send_telegram_alert(message: str) -> None:
    try:
        from utils.bot import send_message
        send_message(message)
    except Exception as e:
        print(f"[WARN] 텔레그램 알림 발송 실패: {e}")


def acquire_process_lock():
    lock_fp = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock_fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return lock_fp
    except (BlockingIOError, IOError):
        print(f"[CRITICAL] {MARKET} 4H 켈트너 프로세스가 이미 실행 중입니다. 중복 가동을 차단합니다.")
        sys.exit(1)


class KeltnerTrendRunner:
    def __init__(
        self,
        market: str = MARKET,
        allocated_budget: float = DEFAULT_BUDGET_KRW,
        emergency_sl_pct: float = EMERGENCY_SL_PCT,
        poll_interval: float = POLL_INTERVAL_SEC,
        state_file: Path = STATE_FILE
    ):
        self.market = market
        self.ticker = market.split("-")[1]
        self.allocated_budget = allocated_budget
        self.emergency_sl_pct = emergency_sl_pct
        self.poll_interval = poll_interval
        self.state_file = state_file
        self.upbit = None
        self.last_heartbeat_time = 0.0
        self.state = self.load_state()

    def load_state(self) -> Dict[str, Any]:
        if self.state_file.exists():
            try:
                with open(self.state_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                print(f"[WARN] 상태 파일 로드 실패 ({e}), 기본값으로 초기화합니다.")
        return {
            "market": self.market,
            "strategy": "4H_KELTNER_CHANNEL_BREAKOUT",
            "in_position": False,
            "position_volume": 0.0,
            "entry_price": 0.0,
            "entry_cost_krw": 0.0,
            "entry_time": None,
            "peak_price": 0.0,
            "last_evaluated_candle_dt": None,
            "trade_count": 0,
            "win_count": 0,
            "realized_pnl_krw": 0.0,
            "waiting_for_main_bot_exit": True
        }

    def save_state(self) -> bool:
        tmp_file = self.state_file.with_suffix(".tmp")
        try:
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(self.state, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            tmp_file.replace(self.state_file)
            return True
        except Exception as e:
            print(f"[ERROR] 상태 파일 저장 실패: {e}")
            return False

    def is_main_bot_exit_completed(self) -> bool:
        """
        메인봇의 기존 18단 평단 탈출 주문(3,552,000원 바스켓 매도)이
        완전히 체결되어 봇 잔고가 0이 되었는지 안전하게 확인합니다.
        """
        if not MAIN_BOT_STATE_FILE.exists():
            return True
        try:
            with open(MAIN_BOT_STATE_FILE, "r", encoding="utf-8") as f:
                mb_state = json.load(f)
            bot_qty = float(mb_state.get("bot_quantity", 0.0) or 0.0)
            tranches = mb_state.get("tranches", [])
            # 봇 수량이 0이거나 미미하고 트랜치가 비어있으면 탈출 완료
            if bot_qty <= 0.0001 or len(tranches) == 0:
                return True
            return False
        except Exception as e:
            print(f"[WARN] 메인봇 상태 파일 확인 중 예외 발생: {e}")
            return False

    def get_safe_balances(self) -> Tuple[float, float]:
        """
        거래소 잔고를 조회하고 보호 자산(PROTECTED_BALANCES)을 엄격히 차감합니다.
        반환: (가용 KRW, 봇 가용 ETH)
        """
        try:
            krw = float(self.upbit.get_balance("KRW") or 0.0)
            raw_coin = float(self.upbit.get_balance(self.ticker) or 0.0)
            protected = float(PROTECTED_BALANCES.get(self.market, 0.0))
            safe_coin = max(0.0, raw_coin - protected)
            return krw, safe_coin
        except Exception as e:
            print(f"[ERROR] 잔고 조회 실패: {e}")
            return 0.0, 0.0

    def execute_buy(self, curr_price: float, reason: str) -> bool:
        avail_krw, _ = self.get_safe_balances()
        order_amount = min(self.allocated_budget, avail_krw)
        
        if order_amount < MIN_ORDER_KRW:
            print(f"[{self.market}] ⚠️ 주문 가능 예수금({order_amount:,.0f}원)이 최소주문({MIN_ORDER_KRW:,}원) 미만으로 매수 보류.")
            return False

        print(f"\n[{self.market}] 🟢 [4H 켈트너 매수 신호 포착] {order_amount:,.0f}원 시장가 매수 집행 (사유: {reason})")
        try:
            res = self.upbit.buy_market_order(self.market, order_amount)
        except Exception as e:
            print(f"[{self.market}] [ERROR] 시장가 매수 주문 API 실패: {e}")
            return False

        if not isinstance(res, dict) or not res.get("uuid"):
            print(f"[{self.market}] [ERROR] 매수 주문 접수 실패: {res}")
            return False

        uuid = res["uuid"]
        time.sleep(1.0)
        
        # Check order settlement
        order_info = None
        for _ in range(5):
            try:
                order_info = self.upbit.get_order(uuid)
                if isinstance(order_info, dict) and order_info.get("state") in ("done", "cancel"):
                    break
            except Exception:
                pass
            time.sleep(1.0)

        if not order_info or order_info.get("state") != "done":
            print(f"[{self.market}] [WARN] 매수 체결 즉시 확인 미완료 ({uuid}); 다음 틱에서 재정산.")
            return False

        executed_vol = float(order_info.get("executed_volume", 0.0) or 0.0)
        executed_funds = float(order_info.get("trades", [{}])[0].get("funds", 0.0) or (executed_vol * curr_price))
        fee = float(order_info.get("paid_fee", 0.0) or (executed_funds * 0.0005))
        avg_p = executed_funds / executed_vol if executed_vol > 0 else curr_price

        # Update state
        self.state["in_position"] = True
        self.state["position_volume"] = executed_vol
        self.state["entry_price"] = avg_p
        self.state["entry_cost_krw"] = executed_funds
        self.state["entry_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.state["peak_price"] = avg_p
        self.save_state()

        # Log trade to SQLite
        try:
            log_real_trade(
                market=self.market,
                ticker=self.ticker,
                side="bid",
                action="4H_KELTNER_BUY",
                price=avg_p,
                volume=executed_vol,
                cost_or_revenue=executed_funds,
                pnl=0.0,
                strategy="KELTNER_4H_TREND",
                order_uuid=uuid,
                db_path=str(DB_PATH)
            )
        except Exception as e:
            print(f"[{self.market}] [WARN] DB 매매 기록 실패: {e}")

        # Telegram Alert
        msg = (
            f"🟢 <b>[4H 켈트너 돌파 매수 체결]</b> {self.market}\n"
            f"• 매수가: {avg_p:,.0f}원 | 수량: {executed_vol:.6f} {self.ticker}\n"
            f"• 투입금액: {executed_funds:,.0f}원 (수수료: {fee:,.0f}원)\n"
            f"• 진입사유: {reason}\n"
            f"• 청산기준: 4시간봉 종가 < 20 EMA 이탈 시 또는 -10% 긴급손절"
        )
        print(f"[{self.market}] {msg}")
        send_telegram_alert(msg)
        return True

    def execute_sell(self, curr_price: float, reason: str, is_emergency_sl: bool = False) -> bool:
        pos_vol = float(self.state.get("position_volume", 0.0))
        _, safe_coin = self.get_safe_balances()
        sell_vol = min(pos_vol, safe_coin)

        if sell_vol <= 0 or (sell_vol * curr_price) < MIN_ORDER_KRW:
            print(f"[{self.market}] ⚠️ 매도 대상 수량 부족 또는 최소주문 미달 (pos_vol={pos_vol}, safe_coin={safe_coin}).")
            self.state["in_position"] = False
            self.state["position_volume"] = 0.0
            self.save_state()
            return False

        label = "긴급 손절" if is_emergency_sl else "추세 둔화 익절/청산"
        print(f"\n[{self.market}] 🔴 [4H 켈트너 {label}] {sell_vol:.6f} {self.ticker} 시장가 매도 집행 (사유: {reason})")
        try:
            res = self.upbit.sell_market_order(self.market, sell_vol)
        except Exception as e:
            print(f"[{self.market}] [ERROR] 시장가 매도 주문 API 실패: {e}")
            return False

        if not isinstance(res, dict) or not res.get("uuid"):
            print(f"[{self.market}] [ERROR] 매도 주문 접수 실패: {res}")
            return False

        uuid = res["uuid"]
        time.sleep(1.0)

        # Check order settlement
        order_info = None
        for _ in range(5):
            try:
                order_info = self.upbit.get_order(uuid)
                if isinstance(order_info, dict) and order_info.get("state") in ("done", "cancel"):
                    break
            except Exception:
                pass
            time.sleep(1.0)

        if not order_info or order_info.get("state") != "done":
            print(f"[{self.market}] [WARN] 매도 체결 즉시 확인 미완료 ({uuid}); 다음 틱에서 재정산.")
            return False

        executed_vol = float(order_info.get("executed_volume", 0.0) or sell_vol)
        executed_funds = float(order_info.get("trades", [{}])[0].get("funds", 0.0) or (executed_vol * curr_price))
        fee = float(order_info.get("paid_fee", 0.0) or (executed_funds * 0.0005))
        avg_exit_p = executed_funds / executed_vol if executed_vol > 0 else curr_price

        entry_cost = float(self.state.get("entry_cost_krw", 0.0))
        entry_p = float(self.state.get("entry_price", 0.0))
        net_revenue = executed_funds - fee
        net_pnl = net_revenue - entry_cost
        pnl_pct = (net_pnl / entry_cost * 100.0) if entry_cost > 0 else 0.0

        # Update stats
        self.state["in_position"] = False
        self.state["position_volume"] = 0.0
        self.state["entry_price"] = 0.0
        self.state["entry_cost_krw"] = 0.0
        self.state["peak_price"] = 0.0
        self.state["trade_count"] = self.state.get("trade_count", 0) + 1
        if net_pnl > 0:
            self.state["win_count"] = self.state.get("win_count", 0) + 1
        self.state["realized_pnl_krw"] = self.state.get("realized_pnl_krw", 0.0) + net_pnl
        self.save_state()

        # Log trade to SQLite
        try:
            log_real_trade(
                market=self.market,
                ticker=self.ticker,
                side="ask",
                action="4H_KELTNER_EMERGENCY_SL" if is_emergency_sl else "4H_KELTNER_EXIT",
                price=avg_exit_p,
                volume=executed_vol,
                cost_or_revenue=executed_funds,
                pnl=net_pnl,
                strategy="KELTNER_4H_TREND",
                order_uuid=uuid,
                db_path=str(DB_PATH)
            )
        except Exception as e:
            print(f"[{self.market}] [WARN] DB 매매 기록 실패: {e}")

        # Telegram Alert
        emoji = "🔴" if net_pnl < 0 else "🎯"
        msg = (
            f"{emoji} <b>[4H 켈트너 {label} 완료]</b> {self.market}\n"
            f"• 매도가: {avg_exit_p:,.0f}원 (진입가: {entry_p:,.0f}원)\n"
            f"• 수량: {executed_vol:.6f} {self.ticker} | 회수액: {net_revenue:,.0f}원\n"
            f"• 실현손익: <b>{net_pnl:+,.0f}원</b> ({pnl_pct:+.2f}%)\n"
            f"• 사유: {reason}\n"
            f"• 누적 통계: {self.state['trade_count']}전 {self.state['win_count']}승 | 누적 PnL: {self.state['realized_pnl_krw']:+,.0f}원"
        )
        print(f"[{self.market}] {msg}")
        send_telegram_alert(msg)
        return True

    def get_current_price(self) -> Optional[float]:
        try:
            if self.upbit and hasattr(self.upbit, "get_current_price"):
                p = self.upbit.get_current_price(self.market)
                if p is not None and not isinstance(p, dict) and float(p) > 0:
                    return float(p)
        except Exception:
            pass
        return safe_get_current_price(self.market)

    def run_tick(self) -> None:
        # 1. Check current market price
        curr_price = self.get_current_price()
        if curr_price is None or curr_price <= 0:
            return

        in_pos = self.state.get("in_position", False)

        # 2. High-Frequency: Check Emergency Stop Loss (-10%) if in position
        if in_pos:
            entry_p = float(self.state.get("entry_price", 0.0))
            peak_p = float(self.state.get("peak_price", entry_p))
            if curr_price > peak_p:
                self.state["peak_price"] = curr_price
                self.save_state()

            # Hard Stop Loss Check (-10%)
            if entry_p > 0 and curr_price <= entry_p * (1.0 - self.emergency_sl_pct):
                sl_reason = f"평단가({entry_p:,.0f}원) 대비 -{self.emergency_sl_pct*100:.1f}% 긴급손절선 터치 ({curr_price:,.0f}원)"
                self.execute_sell(curr_price, sl_reason, is_emergency_sl=True)
                return

        # 3. 4H Candle Evaluation
        df_4h = fetch_4h_candles_upbit(self.market, count=220)
        if df_4h is None or len(df_4h) < 200:
            return

        # Upbit returns newest last; iloc[-1] is currently active forming candle, iloc[-2] is the last completed candle
        completed_candle = df_4h.iloc[-2]
        completed_dt_str = str(completed_candle["dt"])

        last_eval_dt = self.state.get("last_evaluated_candle_dt")

        # Evaluate on every new 4H candle close
        if completed_dt_str != last_eval_dt:
            # We compute indicators on all completed candles (excluding active forming candle)
            closed_candles_df = df_4h.iloc[:-1].copy()
            sig_info = get_keltner_signals(closed_candles_df)

            if sig_info.get("valid", False):
                c_close = sig_info["curr_price"]
                ema20 = sig_info["ema20"]
                ema200 = sig_info["ema200"]
                upper_b = sig_info["upper_band"]
                signal = sig_info["signal"]
                reason = sig_info["reason"]

                print(f"\n[{datetime.now().strftime('%H:%M:%S')}] 📊 [4H 캔들 마감 감지] {completed_dt_str}")
                print(f" • 종가: {c_close:,.0f}원 | 20선: {ema20:,.0f}원 | 상단: {upper_b:,.0f}원 | 200선: {ema200:,.0f}원")
                print(f" • 판정: {signal} ({reason})")

                # Case A: In position -> Check 20 EMA exit
                if in_pos:
                    if sig_info.get("is_exit", False):
                        exit_reason = f"4H 종가({c_close:,.0f}원)가 20 EMA({ema20:,.0f}원) 하향 이탈"
                        self.execute_sell(curr_price, exit_reason)
                # Case B: Not in position -> Check Breakout Entry
                else:
                    if not self.is_main_bot_exit_completed():
                        print(f"[{self.market}] ⏳ 메인봇 기존 18단 평단 탈출(3,552,000원) 대기 중... 켈트너 신규 매수를 보류합니다.")
                    elif signal == "BUY":
                        self.execute_buy(curr_price, reason)

            self.state["last_evaluated_candle_dt"] = completed_dt_str
            self.save_state()

        # 4. Periodic Heartbeat logging (every 30 mins)
        now_ts = time.time()
        if now_ts - self.last_heartbeat_time >= 1800:
            self.last_heartbeat_time = now_ts
            pos_desc = f"보유 중 ({self.state.get('position_volume', 0.0):.6f} {self.ticker} @ {self.state.get('entry_price', 0.0):,.0f}원)" if in_pos else "미보유 (대기 중)"
            mb_status = "완료/준비됨" if self.is_main_bot_exit_completed() else "메인봇 탈출 대기 중"
            print(f"[{datetime.now().strftime('%H:%M:%S')}] 💓 [HEARTBEAT] 4H 켈트너 ETH: 현재가 {curr_price:,.0f}원 | 포지션: {pos_desc} | 인계상태: {mb_status}")

    def run(self) -> None:
        init_db(str(DB_PATH))
        self.upbit = get_upbit_client()

        print("=" * 70)
        print(f"🚀 [AutoBot Trader] 4H 켈트너 채널 돌파 독립 데몬 가동: {self.market}")
        print(f" • 타임프레임: 4시간봉 (240m)")
        print(f" • 대세 필터: 4H 종가 > 200 EMA")
        print(f" • 켈트너 밴드: 20 EMA +/- 2.0 * ATR(14)")
        print(f" • 청산 조건: 4H 종가 < 20 EMA 이탈 (추세 둔화 시 손익 확정)")
        print(f" • 긴급 손절: -{self.emergency_sl_pct * 100:.1f}% Stop-Loss")
        print(f" • 운용 예산: {self.allocated_budget:,.0f} KRW")
        print(f" • 보호 자산: {PROTECTED_BALANCES.get(self.market, 0.0)} {self.ticker} (완전 격리)")
        print(f" • 인계 게이트: 메인봇 18단 평단 탈출(3,552,000원 매도) 체결 시 자동 개시")
        print("=" * 70)

        send_telegram_alert(
            f"🚀 <b>[4H 켈트너 독립 데몬 가동]</b> {self.market}\n"
            f"• 전략: 4H 켈트너 채널 돌파 (20 EMA + 2.0 ATR + 200 EMA)\n"
            f"• 할당예산: {self.allocated_budget:,.0f}원 | 보호자산: {PROTECTED_BALANCES.get(self.market, 0.0)} {self.ticker}\n"
            f"• 메인봇 3,552,000원 바스켓 매도 체결 시 실전 진입이 자동 인계됩니다."
        )

        while True:
            try:
                self.run_tick()
            except Exception as e:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] [ERROR] 틱 루프 예외: {e}")
            time.sleep(self.poll_interval)


def main():
    _lock = acquire_process_lock()
    runner = KeltnerTrendRunner()
    runner.run()


if __name__ == "__main__":
    main()
