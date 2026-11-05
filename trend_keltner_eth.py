#!/usr/bin/env python3
"""
trend_keltner_eth.py
====================
Standalone 4H Keltner Channel Breakout Trend Daemon with 25% 4-Stage Pyramiding for KRW-ETH:
- Strategy: 4H Keltner Channel Breakout (20 EMA + 2.0 ATR + 200 EMA Macro Filter)
- Pyramiding Scaling In:
  * Stage 1 (25%): 4H Completed Close > Keltner Upper Band AND Close > 200 EMA
  * Stage 2 (25%): 4H Completed Close >= Stage 1 Buy Price + 1.5% AND Close > 20 EMA
  * Stage 3 (25%): 4H Completed Close >= Stage 2 Buy Price + 1.5% AND Close > 20 EMA
  * Stage 4 (25%): 4H Completed Close >= Stage 3 Buy Price + 1.5% AND Close > 20 EMA (100% full weight)
- Exit Rules:
  * Trend Weakening Exit: 4H Completed Close < 20 EMA -> Sell ALL accumulated volume (100% cash)
  * Hard Emergency Stop Loss: Current Price <= VWAP Entry Price * -10.0% -> Sell ALL accumulated volume
- Handover Guard:
  * Protects PROTECTED_BALANCES["KRW-ETH"] (0.40756151 ETH)
  * Automatically waits until main bot's 18-step exit (3,552,000 KRW basket ask) completes
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
DEFAULT_BUDGET_KRW = 500_000.0  # 총 운용 예산 (기본 50만 원)
MAX_STAGES = 4                  # 25%씩 4단계 피라미딩
STAGE_FRACTION = 0.25           # 회당 25% (12.5만 원)
PYRAMID_STEP_PCT = 0.015        # 직전 매수가 대비 +1.5% 상승 확인 시 다음 차수 진입
EMERGENCY_SL_PCT = 0.10         # 가중 평단가 대비 -10.0% 긴급손절
POLL_INTERVAL_SEC = 10.0        # 10초 주기 루프


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
        max_stages: int = MAX_STAGES,
        stage_fraction: float = STAGE_FRACTION,
        pyramid_step_pct: float = PYRAMID_STEP_PCT,
        emergency_sl_pct: float = EMERGENCY_SL_PCT,
        poll_interval: float = POLL_INTERVAL_SEC,
        state_file: Path = STATE_FILE,
    ):
        self.market = market
        self.ticker = market.split("-")[1]
        self.allocated_budget = allocated_budget
        self.max_stages = max_stages
        self.stage_fraction = stage_fraction
        self.pyramid_step_pct = pyramid_step_pct
        self.emergency_sl_pct = emergency_sl_pct
        self.poll_interval = poll_interval
        self.state_file = state_file
        self.upbit = None
        self.last_heartbeat_time = 0.0
        self.state = self.load_state()
        if not self.state_file.exists():
            self.save_state()

    def load_state(self) -> Dict[str, Any]:
        if self.state_file.exists():
            try:
                with open(self.state_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    data.setdefault("current_stage", 1 if data.get("in_position") else 0)
                    data.setdefault("max_stages", self.max_stages)
                    data.setdefault("last_buy_price", data.get("entry_price", 0.0))
                    data.setdefault("tranches", [])
                    return data
            except Exception as e:
                print(f"[WARN] 상태 파일 로드 실패 ({e}), 기본값으로 초기화합니다.")

        return {
            "market": self.market,
            "strategy": "4H_KELTNER_CHANNEL_BREAKOUT_25PCT_PYRAMID",
            "in_position": False,
            "current_stage": 0,
            "max_stages": self.max_stages,
            "position_volume": 0.0,
            "entry_price": 0.0,
            "last_buy_price": 0.0,
            "entry_cost_krw": 0.0,
            "entry_time": None,
            "peak_price": 0.0,
            "last_evaluated_candle_dt": None,
            "trade_count": 0,
            "win_count": 0,
            "realized_pnl_krw": 0.0,
            "waiting_for_main_bot_exit": True,
            "tranches": [],
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

    def execute_buy(self, curr_price: float, reason: str, stage: int = 1) -> bool:
        avail_krw, _ = self.get_safe_balances()

        # 25% 회당 투입 금액 계산
        if stage >= self.max_stages:
            spent_so_far = float(self.state.get("entry_cost_krw", 0.0))
            target_amount = max(0.0, self.allocated_budget - spent_so_far)
            if target_amount < MIN_ORDER_KRW:
                target_amount = round(self.allocated_budget * self.stage_fraction)
        else:
            target_amount = round(self.allocated_budget * self.stage_fraction)

        order_amount = min(target_amount, avail_krw)

        if order_amount < MIN_ORDER_KRW:
            print(
                f"[{self.market}] ⚠️ 주문 가능 예수금({order_amount:,.0f}원)이 "
                f"최소주문({MIN_ORDER_KRW:,}원) 미만으로 {stage}차 매수 보류."
            )
            return False

        label = "1차 돌파 매수 (25%)" if stage == 1 else f"{stage}/4차 불타기 추가 매수 (25%)"
        print(f"\n[{self.market}] 🟢 [4H 켈트너 {label}] {order_amount:,.0f}원 시장가 매수 집행 (사유: {reason})")
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

        # 체결 정산 확인
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
        now_time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        if stage == 1:
            total_vol = executed_vol
            total_funds = executed_funds
            new_vwap = avg_p
        else:
            prior_vol = float(self.state.get("position_volume", 0.0))
            prior_funds = float(self.state.get("entry_cost_krw", 0.0))
            total_vol = round(prior_vol + executed_vol, 8)
            total_funds = prior_funds + executed_funds
            new_vwap = total_funds / total_vol if total_vol > 0 else avg_p

        # State 갱신
        self.state["in_position"] = True
        self.state["current_stage"] = stage
        self.state["position_volume"] = total_vol
        self.state["entry_price"] = new_vwap
        self.state["last_buy_price"] = avg_p
        self.state["entry_cost_krw"] = total_funds
        if stage == 1:
            self.state["entry_time"] = now_time_str
        self.state["peak_price"] = max(float(self.state.get("peak_price", avg_p) or avg_p), avg_p)

        tranche_meta = {
            "stage": stage,
            "volume": executed_vol,
            "price": avg_p,
            "funds": executed_funds,
            "fee": fee,
            "time": now_time_str,
            "uuid": uuid,
        }
        self.state.setdefault("tranches", []).append(tranche_meta)
        self.save_state()

        # SQLite 매매 기록
        try:
            log_real_trade(
                market=self.market,
                ticker=self.ticker,
                side="bid",
                action=f"4H_KELTNER_BUY_ST{stage}",
                price=avg_p,
                volume=executed_vol,
                cost_or_revenue=executed_funds,
                pnl=0.0,
                strategy="KELTNER_4H_TREND",
                order_uuid=uuid,
                db_path=str(DB_PATH),
            )
        except Exception as e:
            print(f"[{self.market}] [WARN] DB 매매 기록 실패: {e}")

        # 텔레그램 알림
        if stage == 1:
            msg = (
                f"🟢 <b>[4H 켈트너 1/4차 돌파 매수 체결 (25%)]</b> {self.market}\n"
                f"• 매수가: {avg_p:,.0f}원 | 수량: {executed_vol:.6f} {self.ticker}\n"
                f"• 투입금액: {executed_funds:,.0f}원 (총 예산: {self.allocated_budget:,.0f}원)\n"
                f"• 진입사유: {reason}\n"
                f"• 다음 2차 불타기: 4H 종가 >= {avg_p * (1.0 + self.pyramid_step_pct):,.0f}원 (+{self.pyramid_step_pct*100:.1f}%) 확인 시\n"
                f"• 청산기준: 4시간봉 종가 < 20 EMA 이탈 시 또는 가중평단 대비 -10% 긴급손절"
            )
        else:
            msg = (
                f"🔥 <b>[4H 켈트너 {stage}/4차 불타기 매수 체결 (25%)]</b> {self.market}\n"
                f"• 체결단가: {avg_p:,.0f}원 | 체결수량: {executed_vol:.6f} {self.ticker}\n"
                f"• 가중평단(VWAP): <b>{new_vwap:,.0f}원</b> | 총 누적수량: <b>{total_vol:.6f} {self.ticker}</b>\n"
                f"• 누적 투입액: {total_funds:,.0f}원 / {self.allocated_budget:,.0f}원 ({stage * 25}% 비중 완성)\n"
                f"• 사유: {reason}\n"
                f"• 청산기준: 4시간봉 종가 < 20 EMA 이탈 시 전량 익절"
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
            self.state["current_stage"] = 0
            self.state["position_volume"] = 0.0
            self.state["tranches"] = []
            self.save_state()
            return False

        label = "긴급 손절" if is_emergency_sl else "추세 둔화 전량 청산"
        curr_stage = int(self.state.get("current_stage", 1))
        print(f"\n[{self.market}] 🔴 [4H 켈트너 {label}] {sell_vol:.6f} {self.ticker} 시장가 전량 매도 집행 (사유: {reason})")
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

        # 체결 정산 확인
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
        self.state["current_stage"] = 0
        self.state["position_volume"] = 0.0
        self.state["entry_price"] = 0.0
        self.state["last_buy_price"] = 0.0
        self.state["entry_cost_krw"] = 0.0
        self.state["peak_price"] = 0.0
        self.state["tranches"] = []
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
                db_path=str(DB_PATH),
            )
        except Exception as e:
            print(f"[{self.market}] [WARN] DB 매매 기록 실패: {e}")

        # Telegram Alert
        emoji = "🔴" if net_pnl < 0 else "🎯"
        msg = (
            f"{emoji} <b>[4H 켈트너 {label} 완료]</b> {self.market} ({curr_stage}/4차 비중 일괄 청산)\n"
            f"• 매도가: {avg_exit_p:,.0f}원 (가중평단: {entry_p:,.0f}원)\n"
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
        # 1. 실시간 현재가 확인
        curr_price = self.get_current_price()
        if curr_price is None or curr_price <= 0:
            return

        in_pos = self.state.get("in_position", False)
        current_stage = int(self.state.get("current_stage", 0))

        # 2. 고주파: 포지션 보유 중 긴급 손절(-10%) 및 최고가 추적
        if in_pos:
            entry_p = float(self.state.get("entry_price", 0.0))
            peak_p = float(self.state.get("peak_price", entry_p) or entry_p)
            if curr_price > peak_p:
                self.state["peak_price"] = curr_price
                self.save_state()

            # 가중 평단가 대비 -10% 긴급손절선
            if entry_p > 0 and curr_price <= entry_p * (1.0 - self.emergency_sl_pct):
                sl_reason = f"가중평단가({entry_p:,.0f}원) 대비 -{self.emergency_sl_pct*100:.1f}% 긴급손절선 터치 ({curr_price:,.0f}원)"
                self.execute_sell(curr_price, sl_reason, is_emergency_sl=True)
                return

        # 3. 4시간봉 확정 캔들 평가
        df_4h = fetch_4h_candles_upbit(self.market, count=220)
        if df_4h is None or len(df_4h) < 200:
            return

        completed_candle = df_4h.iloc[-2]
        completed_dt_str = str(completed_candle["dt"])
        last_eval_dt = self.state.get("last_evaluated_candle_dt")

        # 4시간봉 마감 시점 신호 판정
        if completed_dt_str != last_eval_dt:
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
                print(f" • 판정: {signal} ({reason}) | 현재 스테이지: {current_stage}/{self.max_stages}")

                # 케이스 A: 포지션 보유 중
                if in_pos:
                    # A-1. 20 EMA 이탈 시 전량 청산
                    if sig_info.get("is_exit", False):
                        exit_reason = f"4H 종가({c_close:,.0f}원)가 20 EMA({ema20:,.0f}원) 하향 이탈"
                        self.execute_sell(curr_price, exit_reason)
                    # A-2. 추세 유지 중 + 4차 미달성 시 불타기(피라미딩 2~4차) 판정
                    elif current_stage < self.max_stages:
                        last_buy_p = float(self.state.get("last_buy_price", 0.0) or self.state.get("entry_price", 0.0))
                        pyramid_target_p = last_buy_p * (1.0 + self.pyramid_step_pct)
                        if c_close >= pyramid_target_p and c_close > ema20:
                            gain_pct = ((c_close - last_buy_p) / last_buy_p * 100.0)
                            pyramid_reason = (
                                f"4H 종가({c_close:,.0f}원)가 직전 매수가({last_buy_p:,.0f}원) 대비 "
                                f"+{gain_pct:.2f}% 상승 확인 (기준 +{self.pyramid_step_pct*100:.1f}%)"
                            )
                            self.execute_buy(curr_price, pyramid_reason, stage=current_stage + 1)

                # 케이스 B: 미보유 상태 -> 1차 25% 돌파 매수 진입 판정
                else:
                    if not self.is_main_bot_exit_completed():
                        print(f"[{self.market}] ⏳ 메인봇 기존 18단 평단 탈출(3,552,000원) 대기 중... 켈트너 신규 매수를 보류합니다.")
                    elif signal == "BUY":
                        self.execute_buy(curr_price, reason, stage=1)

            self.state["last_evaluated_candle_dt"] = completed_dt_str
            self.save_state()

        # 4. 정기 하트비트 (30분 주기)
        now_ts = time.time()
        if now_ts - self.last_heartbeat_time >= 1800:
            self.last_heartbeat_time = now_ts
            stage_str = f"{current_stage}/{self.max_stages}차 ({current_stage * 25}%)" if in_pos else "미보유 (대기)"
            pos_desc = f"보유 중 ({self.state.get('position_volume', 0.0):.6f} {self.ticker} @ {self.state.get('entry_price', 0.0):,.0f}원, {stage_str})" if in_pos else "미보유 (대기 중)"
            mb_status = "완료/준비됨" if self.is_main_bot_exit_completed() else "메인봇 탈출 대기 중"
            print(f"[{datetime.now().strftime('%H:%M:%S')}] 💓 [HEARTBEAT] 4H 켈트너 ETH: 현재가 {curr_price:,.0f}원 | 포지션: {pos_desc} | 인계상태: {mb_status}")

    def run(self) -> None:
        init_db(str(DB_PATH))
        self.upbit = get_upbit_client()

        print("=" * 70)
        print(f"🚀 [AutoBot Trader] 4H 켈트너 25% 4단계 피라미딩 불타기 독립 데몬 가동: {self.market}")
        print(f" • 타임프레임: 4시간봉 (240m)")
        print(f" • 대세 필터: 4H 종가 > 200 EMA")
        print(f" • 켈트너 밴드: 20 EMA +/- 2.0 * ATR(14)")
        print(f" • 진입 방식: 25%씩 최대 4단계 피라미딩 (순추세 불타기)")
        print(f"   - 1차: 상단 돌파 시 25% (12.5만 원)")
        print(f"   - 2~4차: 직전 매수가 대비 +{self.pyramid_step_pct*100:.1f}% 추가 상승 시마다 25% 불타기")
        print(f" • 청산 조건: 4H 종가 < 20 EMA 이탈 시 전량 청산")
        print(f" • 긴급 손절: 가중평단가 대비 -{self.emergency_sl_pct * 100:.1f}% Hard SL")
        print(f" • 운용 예산: {self.allocated_budget:,.0f} KRW (1차당 {self.allocated_budget * self.stage_fraction:,.0f}원)")
        print(f" • 보호 자산: {PROTECTED_BALANCES.get(self.market, 0.0)} {self.ticker} (완전 격리)")
        print(f" • 인계 게이트: 메인봇 18단 평단 탈출(3,552,000원 매도) 체결 시 자동 개시")
        print("=" * 70)

        send_telegram_alert(
            f"🚀 <b>[4H 켈트너 25% 피라미딩 독립 데몬 가동]</b> {self.market}\n"
            f"• 전략: 4H 켈트너 채널 돌파 + 25% 4단계 불타기 (Pyramiding)\n"
            f"• 1차: 돌파 시 25% ➔ 2~4차: +1.5% 상승 시마다 25%씩 추가 탑승\n"
            f"• 청산: 4H 종가 < 20 EMA 이탈 시 전량 익절/청산\n"
            f"• 할당예산: {self.allocated_budget:,.0f}원 (회당 12.5만 원)"
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
