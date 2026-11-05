#!/usr/bin/env python3
"""
legacy_eth_trailing_exit.py
===========================
이전 보유 이더리움(0.40756151 ETH) 전용 10% 분할 트레일링 스탑 익절 데몬:
- 대상: 이전 고유 보유분 0.40756151 ETH
- 전략: A안 (취득 원가 본전 3,620,000원 기준 10% 분할 트레일링 스탑)
- 룰:
  1. 발동 하한선(Floor & Trigger): 현재가 >= 3,620,000원
     (3,620,000원 미만에서는 일체 매도 금지, 손절 원천 방지)
  2. 트레일링 스탑: 3,620,000원 돌파 후 형성되는 최고가(Peak) 추적
     - 최고가 대비 -1.5% 반락 시 시장가 매도 (단, 매도가격은 3,620,000원 이상 보장)
     - 최고가 형성 중 3,620,000원 미만으로 가격이 밀리면 트레일링 즉시 정지 및 대기
  3. 분할 규모: 총 10회 (회당 10% = 0.04075615 ETH, 마지막 10차수는 잔여 전량)
  4. 매도 간격(쿨다운): 1회 매도 체결 후 12시간 쿨다운 (12시간 동안 추가 매도 대기)
  5. 격리 원칙: 메인봇의 지정가 주문(0.11659862 ETH @ 3,552,000원)을 침범하지 않음
"""

import fcntl
import json
import logging
import os
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional, Tuple

# Base directory setup
BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from config import (
    MIN_ORDER_KRW,
    get_upbit_client,
)
from utils.db_logger import log_real_trade, init_db

MARKET = "KRW-ETH"
TICKER = "ETH"
TRIGGER_PRICE = 3_620_000.0       # A안 진짜 취득원가 본전 기준선 (362만 원)
TRAILING_DROP_PCT = 0.015         # 최고가 대비 -1.5% 콜백
COOLDOWN_HOURS = 12.0             # 1회 체결 후 12시간 쿨다운
TOTAL_INITIAL_QTY = 0.40756151    # 총 정리 대상 레거시 이더리움 수량
TOTAL_TRANCHES = 10               # 10회 분할 매도
TRANCHE_PCT = 0.10                # 회당 10% (0.04075615 ETH)

STATE_FILE = BASE_DIR / "legacy_eth_trailing_state.json"
LOCK_FILE = Path(tempfile.gettempdir()) / "legacy_eth_trailing_exit.lock"
DB_PATH = BASE_DIR / "trade_history.db"
POLL_INTERVAL_SEC = 10.0          # 10초 주기 실시간 가격 및 상태 감시


def safe_get_current_price(market: str = MARKET, retries: int = 3, delay: float = 0.5) -> Optional[float]:
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
        print(f"[WARN] 텔레그램 발송 실패: {e}")


def acquire_process_lock():
    lock_fp = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock_fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return lock_fp
    except (BlockingIOError, IOError):
        print(f"[CRITICAL] {MARKET} 레거시 ETH 트레일링 익절 데몬이 이미 실행 중입니다. 중복 가동 차단.")
        sys.exit(1)


class LegacyEthTrailingRunner:
    def __init__(
        self,
        market: str = MARKET,
        trigger_price: float = TRIGGER_PRICE,
        trailing_drop_pct: float = TRAILING_DROP_PCT,
        cooldown_hours: float = COOLDOWN_HOURS,
        total_initial_qty: float = TOTAL_INITIAL_QTY,
        total_tranches: int = TOTAL_TRANCHES,
        poll_interval: float = POLL_INTERVAL_SEC,
        state_file: Path = STATE_FILE,
    ):
        self.market = market
        self.ticker = market.split("-")[1]
        self.trigger_price = trigger_price
        self.trailing_drop_pct = trailing_drop_pct
        self.cooldown_hours = cooldown_hours
        self.total_initial_qty = total_initial_qty
        self.total_tranches = total_tranches
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
                print(f"[WARN] 상태 파일 읽기 실패 ({e}), 기본값 초기화합니다.")

        return {
            "market": self.market,
            "strategy": "LEGACY_ETH_10PCT_TRAILING_EXIT",
            "trigger_price": self.trigger_price,
            "trailing_drop_pct": self.trailing_drop_pct,
            "cooldown_hours": self.cooldown_hours,
            "total_initial_qty": self.total_initial_qty,
            "total_tranches": self.total_tranches,
            "completed_tranches": 0,
            "cumulative_executed_volume": 0.0,
            "cumulative_executed_funds": 0.0,
            "remaining_qty": self.total_initial_qty,
            "current_status": "WAITING_TRIGGER",  # WAITING_TRIGGER | TRAILING | COOLDOWN | COMPLETED
            "active_trailing": False,
            "peak_price": 0.0,
            "last_executed_time": None,
            "cooldown_until": 0.0,
            "history": [],
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

    def get_current_price(self) -> Optional[float]:
        try:
            if self.upbit and hasattr(self.upbit, "get_current_price"):
                p = self.upbit.get_current_price(self.market)
                if p is not None and not isinstance(p, dict) and float(p) > 0:
                    return float(p)
        except Exception:
            pass
        return safe_get_current_price(self.market)

    def get_available_legacy_balance(self) -> float:
        """
        거래소에서 미체결(locked)을 제외한 실제 주문 가능(free) 잔고 조회
        """
        try:
            if self.upbit:
                free_coin = float(self.upbit.get_balance(self.ticker) or 0.0)
                return free_coin
        except Exception as e:
            print(f"[ERROR] 잔고 조회 실패: {e}")
        return 0.0

    def execute_tranche_sell(self, curr_price: float, peak_price: float) -> bool:
        """
        10% 분할 매도 집행 (시장가)
        """
        completed = int(self.state.get("completed_tranches", 0))
        next_tranche = completed + 1
        rem_qty = float(self.state.get("remaining_qty", 0.0))

        # 매도 수량 계산 (10회차는 잔여 전량 일괄 매도하여 소수점 오차 제거)
        if next_tranche >= self.total_tranches:
            target_vol = rem_qty
        else:
            std_vol = round(self.total_initial_qty * (1.0 / self.total_tranches), 8)
            target_vol = min(std_vol, rem_qty)

        # 업비트 가용 잔고 안전 확인
        free_coin = self.get_available_legacy_balance()
        if free_coin < target_vol * 0.99:
            print(f"[{self.market}] ⚠️ 업비트 가용 잔고({free_coin:.6f} ETH)가 목표 매도 수량({target_vol:.6f} ETH)보다 부족합니다.")
            target_vol = min(target_vol, free_coin)

        if target_vol <= 0 or (target_vol * curr_price) < MIN_ORDER_KRW:
            print(f"[{self.market}] ⚠️ 매도 대상 수량 부족 또는 최소주문금액({MIN_ORDER_KRW:,}원) 미달로 매도 중단.")
            return False

        drop_pct_from_peak = ((curr_price - peak_price) / peak_price * 100.0) if peak_price > 0 else 0.0
        print(f"\n[{self.market}] 🎯 [이전 ETH {next_tranche}/{self.total_tranches}차 분할 익절] {target_vol:.8f} {self.ticker} 시장가 매도 집행")
        print(f" • 고점: {peak_price:,.0f}원 ➔ 현재가: {curr_price:,.0f}원 ({drop_pct_from_peak:+.2f}%)")

        try:
            res = self.upbit.sell_market_order(self.market, target_vol)
        except Exception as e:
            print(f"[{self.market}] [ERROR] 시장가 매도 주문 실패: {e}")
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
            print(f"[{self.market}] [WARN] 매도 체결 확인 미완료 ({uuid}). 다음 틱에서 재정산 필요.")
            return False

        executed_vol = float(order_info.get("executed_volume", 0.0) or target_vol)
        executed_funds = float(order_info.get("trades", [{}])[0].get("funds", 0.0) or (executed_vol * curr_price))
        fee = float(order_info.get("paid_fee", 0.0) or (executed_funds * 0.0005))
        avg_exit_p = executed_funds / executed_vol if executed_vol > 0 else curr_price
        net_revenue = executed_funds - fee

        # 원초 취득원가(3,620,000원) 대비 손익 계산
        cost_basis = executed_vol * self.trigger_price
        net_pnl = net_revenue - cost_basis
        pnl_pct = (net_pnl / cost_basis * 100.0) if cost_basis > 0 else 0.0

        now_time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cooldown_target_ts = time.time() + (self.cooldown_hours * 3600.0)

        # 상태 업데이트
        self.state["completed_tranches"] = next_tranche
        self.state["cumulative_executed_volume"] = float(self.state.get("cumulative_executed_volume", 0.0)) + executed_vol
        self.state["cumulative_executed_funds"] = float(self.state.get("cumulative_executed_funds", 0.0)) + net_revenue
        new_rem = max(0.0, round(self.total_initial_qty - self.state["cumulative_executed_volume"], 8))
        self.state["remaining_qty"] = new_rem
        self.state["active_trailing"] = False
        self.state["peak_price"] = 0.0
        self.state["last_executed_time"] = now_time_str

        if next_tranche >= self.total_tranches or new_rem <= 0.0001:
            self.state["current_status"] = "COMPLETED"
            self.state["cooldown_until"] = 0.0
        else:
            self.state["current_status"] = "COOLDOWN"
            self.state["cooldown_until"] = cooldown_target_ts

        history_item = {
            "tranche": next_tranche,
            "volume": executed_vol,
            "price": avg_exit_p,
            "gross_funds": executed_funds,
            "net_revenue": net_revenue,
            "fee": fee,
            "peak_price": peak_price,
            "pnl": net_pnl,
            "pnl_pct": pnl_pct,
            "time": now_time_str,
            "uuid": uuid,
        }
        self.state.setdefault("history", []).append(history_item)
        self.save_state()

        # SQLite 영구 기록
        try:
            log_real_trade(
                market=self.market,
                ticker=self.ticker,
                side="ask",
                action=f"LEGACY_ETH_TRAILING_EXIT_{next_tranche}OF10",
                price=avg_exit_p,
                volume=executed_vol,
                cost_or_revenue=executed_funds,
                pnl=net_pnl,
                strategy="LEGACY_ETH_TRAILING_EXIT",
                order_uuid=uuid,
                db_path=str(DB_PATH),
            )
        except Exception as e:
            print(f"[{self.market}] [WARN] DB 매매 기록 실패: {e}")

        # 텔레그램 알림
        cooldown_desc = (
            f"• 다음 12시간 동안 쿨다운 대기 후 {next_tranche + 1}차 감시를 재개합니다."
            if self.state["current_status"] == "COOLDOWN"
            else "🎉 <b>이전 보유 이더리움 10회 분할 매도가 전량 완료되었습니다!</b>"
        )
        msg = (
            f"🎯 <b>[이전 ETH 10% 트레일링 익절 완료]</b> {self.market} ({next_tranche}/{self.total_tranches}차)\n"
            f"• 체결단가: {avg_exit_p:,.0f}원 (고점: {peak_price:,.0f}원 | {drop_pct_from_peak:+.2f}%)\n"
            f"• 체결수량: {executed_vol:.8f} {self.ticker} | 회수액: {net_revenue:,.0f}원\n"
            f"• 본전(362만) 대비 손익: <b>{net_pnl:+,.0f}원</b> ({pnl_pct:+.2f}%)\n"
            f"• 잔여 물량: <b>{new_rem:.8f} {self.ticker}</b>\n"
            f"{cooldown_desc}"
        )
        print(f"[{self.market}] {msg}")
        send_telegram_alert(msg)
        return True

    def run_tick(self) -> None:
        completed = int(self.state.get("completed_tranches", 0))
        rem_qty = float(self.state.get("remaining_qty", self.total_initial_qty))

        # 1. 완료 상태 체크
        if completed >= self.total_tranches or rem_qty <= 0.0001:
            if self.state.get("current_status") != "COMPLETED":
                self.state["current_status"] = "COMPLETED"
                self.save_state()
            return

        now_ts = time.time()
        cooldown_until = float(self.state.get("cooldown_until", 0.0) or 0.0)

        # 2. 쿨다운 상태 체크
        if now_ts < cooldown_until:
            rem_sec = int(cooldown_until - now_ts)
            hrs = rem_sec // 3600
            mins = (rem_sec % 3600) // 60
            self.state["current_status"] = "COOLDOWN"

            # 30분 주기 하트비트
            if now_ts - self.last_heartbeat_time >= 1800:
                self.last_heartbeat_time = now_ts
                print(
                    f"[{datetime.now().strftime('%H:%M:%S')}] ⏳ [COOLDOWN] 이전 ETH {completed}/{self.total_tranches}차 완료. "
                    f"다음 {completed + 1}차 감시까지 {hrs}시간 {mins}분 대기 중 (잔여: {rem_qty:.6f} ETH)"
                )
            return

        # 쿨다운 종료 감지
        if self.state.get("current_status") == "COOLDOWN" and now_ts >= cooldown_until:
            print(f"\n[{datetime.now().strftime('%H:%M:%S')}] 🔔 12시간 쿨다운 종료! {completed + 1}차 트레일링 감시를 재개합니다.")
            self.state["current_status"] = "WAITING_TRIGGER"
            self.state["active_trailing"] = False
            self.state["peak_price"] = 0.0
            self.save_state()

        # 3. 현재 시세 조회
        curr_price = self.get_current_price()
        if curr_price is None or curr_price <= 0:
            return

        is_active = self.state.get("active_trailing", False)
        peak_p = float(self.state.get("peak_price", 0.0) or 0.0)

        # 4. 트레일링 비활성 상태: 트리거(3,620,000원) 돌파 확인
        if not is_active:
            if curr_price >= self.trigger_price:
                self.state["active_trailing"] = True
                self.state["peak_price"] = curr_price
                self.state["current_status"] = "TRAILING"
                self.save_state()

                msg = (
                    f"🚀 <b>[이전 ETH {completed + 1}/{self.total_tranches}차 트레일링 스탑 개시]</b> {self.market}\n"
                    f"• 현재가: {curr_price:,.0f}원이 본전 기준선({self.trigger_price:,.0f}원)을 돌파하였습니다!\n"
                    f"• 고점 추적을 시작하며, 최고가 대비 -{self.trailing_drop_pct * 100:.1f}% 반락 시 10%({round(self.total_initial_qty * TRANCHE_PCT, 8):.8f} ETH)를 시장가 익절합니다."
                )
                print(f"\n[{datetime.now().strftime('%H:%M:%S')}] {msg}")
                send_telegram_alert(msg)
            else:
                self.state["current_status"] = "WAITING_TRIGGER"
                # 30분 주기 하트비트
                if now_ts - self.last_heartbeat_time >= 1800:
                    self.last_heartbeat_time = now_ts
                    dist_to_trigger = ((self.trigger_price - curr_price) / curr_price * 100.0)
                    print(
                        f"[{datetime.now().strftime('%H:%M:%S')}] 💓 [HEARTBEAT] 이전 ETH 감시: 현재가 {curr_price:,.0f}원 "
                        f"(362만 본전까지 {dist_to_trigger:+.2f}%) | 진행: {completed}/{self.total_tranches}차 완료 | 잔여: {rem_qty:.6f} ETH"
                    )
            return

        # 5. 트레일링 활성 상태 (고점 추적 및 콜백 판정)
        # 5-1. 신규 고점 갱신
        if curr_price > peak_p:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] 🔥 [고점 갱신] {peak_p:,.0f}원 ➔ {curr_price:,.0f}원 (스탑라인 상향)")
            self.state["peak_price"] = curr_price
            peak_p = curr_price
            self.save_state()

        # 5-2. 절대 원금 하한선(3,620,000원) 아래로 반락 시 트레일링 일시 중지 (원금 손실 방어)
        if curr_price < self.trigger_price:
            print(
                f"\n[{datetime.now().strftime('%H:%M:%S')}] ⚠️ 현재가({curr_price:,.0f}원)가 본전 하한선({self.trigger_price:,.0f}원) "
                f"아래로 반락하여 {completed + 1}차 트레일링을 일시 중지하고 재돌파를 대기합니다."
            )
            self.state["active_trailing"] = False
            self.state["peak_price"] = 0.0
            self.state["current_status"] = "WAITING_TRIGGER"
            self.save_state()
            return

        # 5-3. 트레일링 스탑 라인 판정
        stop_line = peak_p * (1.0 - self.trailing_drop_pct)
        # 매도는 반드시 3,620,000원 이상에서만 집행
        effective_stop = max(stop_line, self.trigger_price)

        if curr_price <= effective_stop:
            print(
                f"\n[{datetime.now().strftime('%H:%M:%S')}] 🎯 트레일링 스탑 터치! "
                f"현재가({curr_price:,.0f}원) <= 스탑라인({effective_stop:,.0f}원, 고점 {peak_p:,.0f}원 대비 -{self.trailing_drop_pct * 100:.1f}%)"
            )
            self.execute_tranche_sell(curr_price, peak_p)

    def run(self) -> None:
        init_db(str(DB_PATH))
        self.upbit = get_upbit_client()

        print("=" * 75)
        print(f"🚀 [AutoBot Trader] 이전 보유 ETH 10% 분할 트레일링 익절 데몬 가동")
        print(f" • 대상 마켓: {self.market}")
        print(f" • 총 대상 물량: {self.total_initial_qty:.8f} {self.ticker}")
        print(f" • 분할 방식: 10%씩 총 {self.total_tranches}회 분할 (회당 약 {round(self.total_initial_qty * TRANCHE_PCT, 8):.8f} ETH)")
        print(f" • 발동 기준선(Floor): {self.trigger_price:,.0f}원 (원초 취득원가 본전, 이하 매도 금지)")
        print(f" • 트레일링 콜백: 고점 대비 -{self.trailing_drop_pct * 100:.1f}%")
        print(f" • 매도 후 쿨다운: {self.cooldown_hours:.0f}시간 대기 (충분한 호흡 분할 매도)")
        print(f" • 현재 진행 상태: {self.state.get('completed_tranches', 0)}/{self.total_tranches}차 완료 | 잔여: {self.state.get('remaining_qty', self.total_initial_qty):.8f} ETH")
        print("=" * 75)

        send_telegram_alert(
            f"🚀 <b>[이전 보유 ETH 10% 분할 트레일링 익절 데몬 가동]</b>\n"
            f"• 대상 물량: {self.total_initial_qty:.8f} ETH (10회 분할 매도)\n"
            f"• 발동 기준선: {self.trigger_price:,.0f}원 (본전 이하 매도 원천 차단)\n"
            f"• 트레일링 콜백: 고점 대비 -1.5% | 체결 후 12시간 쿨다운\n"
            f"• 3,620,000원 돌파 시 1차 10% 고점 추적이 자동 시작됩니다."
        )

        while True:
            try:
                self.run_tick()
            except Exception as e:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] [ERROR] 틱 루프 예외: {e}")
            time.sleep(self.poll_interval)


def main():
    _lock = acquire_process_lock()
    runner = LegacyEthTrailingRunner()
    runner.run()


if __name__ == "__main__":
    main()
