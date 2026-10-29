#!/usr/bin/env python3
"""
포켓 전용 바스켓 리셋 전략 코어 모듈 (PocketBasketResetRunner)
- 1.8% 하락 매수 / 0.8% 전량 익절 리셋 (200단 버퍼)
- 독립된 서브포켓 API(UpbitPocketClient)를 사용하여 메인 계좌와 완전 분리 운용
- 거래소 실시간 지정가 주문(매도 1건 + 매수 3건) 및 100% 현금 리셋 사이클
"""

import os
import sys
import time
import math
import json
import sqlite3
import tempfile
from datetime import datetime
from typing import Dict, List, Optional, Tuple, Any

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from utils.pocket_api import UpbitPocketClient

try:
    from utils.bot import send_message as _send_telegram_msg
except Exception:
    _send_telegram_msg = None

DB_FILE = os.path.join(BASE_DIR, "pocket_trade_history.db")


def get_upbit_tick_size(price: float) -> float:
    """업비트 원화(KRW) 마켓 공식 호가 단위"""
    if price >= 2_000_000:
        return 1000.0
    elif price >= 1_000_000:
        return 1000.0
    elif price >= 500_000:
        return 500.0
    elif price >= 100_000:
        return 100.0
    elif price >= 50_000:
        return 50.0
    elif price >= 10_000:
        return 10.0
    elif price >= 5_000:
        return 5.0
    elif price >= 1_000:
        return 1.0
    elif price >= 100:
        return 1.0
    elif price >= 10:
        return 0.1
    elif price >= 1:
        return 0.01
    else:
        return 0.001


def adjust_price_to_tick(price: float, method: str = "floor") -> float:
    """호가 단위에 맞춰 올림/내림/반올림 조정"""
    tick = get_upbit_tick_size(price)
    if method == "ceil":
        func = math.ceil
    elif method == "round":
        func = round
    else:
        func = math.floor
    adjusted = func(price / tick) * tick
    if tick >= 1.0:
        return float(int(round(adjusted)))
    return round(adjusted, 4)


class PocketBasketResetRunner:
    def __init__(
        self,
        market: str = "KRW-ETH",
        step_pct: float = 0.018,        # 1.8% 하락 매수 간격
        profit_pct: float = 0.008,      # 0.8% 전량 익절 마진
        unit_krw: float = 10_000.0,     # 1Unit 10,000원
        max_levels: int = 200,          # 최대 200단 한도
        max_open_buys: int = 3,         # 하단 대기 매수 주문 수
        poll_interval_sec: float = 2.0, # 폴링 대기 주기 (초)
        pocket_name: str = "pocket2_basket",  # 포켓 식별 태그 (장부 및 DB 격리)
        env_prefix: str = "UPBIT_POCKET2"     # 서브포켓 API 키 prefix
    ):
        self.market = market
        self.ticker = market.replace("KRW-", "")
        self.step_pct = step_pct
        self.profit_pct = profit_pct
        self.unit_krw = unit_krw
        self.max_levels = max_levels
        self.max_open_buys = max_open_buys
        self.poll_interval_sec = poll_interval_sec
        self.pocket_name = pocket_name

        safe_tag = market.replace("-", "_")
        self.state_file = os.path.join(BASE_DIR, f"{pocket_name}_state_{safe_tag}.json")
        self.lock_file = os.path.join(BASE_DIR, f".{pocket_name}_{safe_tag}.lock")
        self.db_file = os.path.join(BASE_DIR, f"{pocket_name}_trade_history.db")
        self._lock_fd = None

        self.client = UpbitPocketClient(env_prefix=env_prefix)
        self.init_db()

    def acquire_lock(self) -> bool:
        """중복 프로세스 기동 방지 파일 락"""
        try:
            import fcntl
            self._lock_fd = open(self.lock_file, "w")
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._lock_fd.write(f"{os.getpid()}\n")
            self._lock_fd.flush()
            return True
        except (IOError, OSError):
            return False

    def release_lock(self) -> None:
        if self._lock_fd:
            try:
                import fcntl
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
                self._lock_fd.close()
            except (IOError, OSError):
                pass
            self._lock_fd = None

    def init_db(self) -> None:
        """포켓 전용 SQLite 데이터베이스 테이블 초기화"""
        try:
            with sqlite3.connect(self.db_file, timeout=10.0) as conn:
                c = conn.cursor()
                c.execute("""
                    CREATE TABLE IF NOT EXISTS pocket_trades (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp TEXT NOT NULL,
                        market TEXT NOT NULL,
                        side TEXT NOT NULL,
                        action TEXT NOT NULL,
                        price REAL NOT NULL,
                        volume REAL NOT NULL,
                        cost_or_revenue REAL NOT NULL,
                        fee REAL NOT NULL,
                        pnl REAL NOT NULL,
                        cycle INTEGER NOT NULL,
                        order_uuid TEXT,
                        strategy TEXT NOT NULL,
                        created_at TEXT DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                c.execute("CREATE INDEX IF NOT EXISTS idx_pocket_trades_mkt ON pocket_trades(market, timestamp)")
                conn.commit()
        except Exception as e:
            print(f"[{self.market}] ⚠️ DB 초기화 오류: {e}")

    def send_telegram(self, msg: str) -> None:
        """텔레그램 메시지 발송 헬퍼"""
        if _send_telegram_msg:
            try:
                _send_telegram_msg(msg)
            except Exception as e:
                print(f"[{self.market}] ⚠️ 텔레그램 알림 실패: {e}")

    def log_trade(
        self,
        side: str,
        action: str,
        price: float,
        volume: float,
        cost_or_rev: float,
        fee: float,
        pnl: float,
        cycle: int,
        order_uuid: str = ""
    ) -> None:
        """체결 내역 DB 로깅"""
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            with sqlite3.connect(self.db_file, timeout=10.0) as conn:
                c = conn.cursor()
                c.execute("""
                    INSERT INTO pocket_trades 
                    (timestamp, market, side, action, price, volume, cost_or_revenue, fee, pnl, cycle, order_uuid, strategy)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (now_str, self.market, side, action, price, volume, cost_or_rev, fee, pnl, cycle, order_uuid, "POCKET_BASKET_18_08"))
                conn.commit()
        except Exception as e:
            print(f"[{self.market}] ⚠️ DB 로깅 오류: {e}")

    def load_state(self) -> Dict[str, Any]:
        """원자적 상태 파일 로드"""
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                print(f"[{self.market}] ⚠️ 상태 파일 읽기 오류: {e}")
        return self._create_empty_state()

    def _create_empty_state(self) -> Dict[str, Any]:
        return {
            "market": self.market,
            "strategy": "POCKET_BASKET_RESET_18_08",
            "step_pct": self.step_pct,
            "profit_pct": self.profit_pct,
            "unit_krw": self.unit_krw,
            "max_levels": self.max_levels,
            "cycle_count": 0,
            "total_realized_pnl": 0.0,
            "total_fees": 0.0,
            "win_count": 0,
            "active_levels": 0,
            "total_cost": 0.0,
            "held_volume": 0.0,
            "vwap": 0.0,
            "base_entry_price": 0.0,
            "pending_sell": None,   # {'uuid': ..., 'price': ..., 'volume': ...}
            "pending_buys": {},     # {uuid: {'price': ..., 'volume': ..., 'level': ...}}
            "last_update": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }

    def save_state(self, state: Dict[str, Any]) -> None:
        """원자적 임시파일 교체 저장 (fsync)"""
        state["last_update"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        dir_name = os.path.dirname(self.state_file)
        tmp_fd, tmp_path = tempfile.mkstemp(dir=dir_name, prefix=".pocket_basket_tmp_")
        try:
            with open(tmp_fd, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self.state_file)
        except Exception as e:
            if os.path.exists(tmp_path):
                try: os.remove(tmp_path)
                except OSError: pass
            print(f"[{self.market}] ⚠️ 상태 저장 실패: {e}")

    def cancel_all_open_orders(self) -> None:
        """거래소에 걸려있는 해당 마켓 미체결 주문 전량 취소"""
        try:
            open_orders = self.client.get_open_orders(self.market)
            for o in open_orders:
                ouuid = o.get("uuid")
                if ouuid:
                    try:
                        self.client.cancel_order(ouuid)
                        time.sleep(0.1)
                    except Exception as e:
                        print(f"[{self.market}] 주문 취소 예외 ({ouuid}): {e}")
        except Exception as e:
            print(f"[{self.market}] 미체결 목록 조회 실패: {e}")

    def start_new_cycle(self, state: Dict[str, Any]) -> None:
        """
        신규 사이클 시작:
        1. 잔여 미체결 주문 전량 취소
        2. 시장가 1Unit 매수 (새 진입)
        3. 평단가 +0.8% 매도 주문 등록
        4. 평단가 대비 -1.8%, -3.6%, -5.3% 매수 주문 3건 등록
        """
        krw_bal = self.client.get_balance("KRW")
        if krw_bal < self.unit_krw:
            print(f"[{self.market}] ⏳ 원화 잔고 부족: 현재 {krw_bal:,.0f}원 < 1Unit({self.unit_krw:,.0f}원). 입금 대기 중...")
            return

        print(f"\n[{self.market}] 🚀 [새 사이클 시작] 이전 주문 정리 및 1Unit 시장가 신규 진입...")
        self.cancel_all_open_orders()
        time.sleep(0.5)

        # 1Unit 시장가 매수
        ident = f"init_buy_{int(time.time()*1000)}"
        buy_res = self.client.buy_market_order_and_wait(self.market, self.unit_krw, identifier=ident)
        
        buy_p = float(buy_res["price"])
        vol = float(buy_res["volume"])
        cost = float(buy_res["funds"])
        fee = float(buy_res["fee"])
        ouuid = buy_res["uuid"]

        state["cycle_count"] += 1
        curr_cycle = state["cycle_count"]
        state["active_levels"] = 1
        state["total_cost"] = cost
        state["held_volume"] = vol
        state["vwap"] = buy_p
        state["base_entry_price"] = buy_p
        state["total_fees"] += fee

        self.log_trade("bid", "BASKET_INITIAL_BUY", buy_p, vol, cost, fee, 0.0, curr_cycle, ouuid)
        print(f"[{self.market}] 🟢 1Unit 매수 체결 완료: {vol:.6f} {self.ticker} @ {buy_p:,.0f}원 (투입: {cost:,.0f}원)")

        # 지정가 매도 등록 (평단가 +0.8%)
        self._register_basket_sell(state)

        # 지정가 매수 3건 등록
        state["pending_buys"] = {}
        self._replenish_buy_orders(state)
        self.save_state(state)

        sell_info = state.get("pending_sell") or {}
        tp_price = sell_info.get("price", buy_p * (1.0 + self.profit_pct))
        self.send_telegram(
            f"🚀 <b>[Cascade {self.ticker} 1단 신규 진입]</b> (사이클 {curr_cycle}회차)\n"
            f"• 체결: {vol:.6f} {self.ticker} @ {buy_p:,.0f}원 ({cost:,.0f}원)\n"
            f"• 평단: <b>{buy_p:,.0f}원</b> | 전량 익절가: <b>{tp_price:,.0f}원</b> (+{self.profit_pct*100:.1f}%)\n"
            f"• 하단 대기 매수 3건 등록 완료 (1.8% 간격 200단 버퍼)"
        )

    def _register_basket_sell(self, state: Dict[str, Any]) -> None:
        """보유 전량에 대해 평단가 대비 +0.8% 지정가 매도 등록"""
        vwap = state["vwap"]
        vol = state["held_volume"]
        if vol <= 0 or vwap <= 0:
            return

        target_tp_p = adjust_price_to_tick(vwap * (1.0 + self.profit_pct), method="ceil")
        ident = f"tp_sell_{int(time.time()*1000)}"

        try:
            res = self.client.create_limit_order(self.market, "ask", vol, target_tp_p, identifier=ident)
            sell_uuid = res.get("uuid")
            state["pending_sell"] = {
                "uuid": sell_uuid,
                "price": target_tp_p,
                "volume": vol
            }
            pnl_expected = (target_tp_p * vol * 0.9995) - state["total_cost"]
            print(f"[{self.market}] 🎯 [바스켓 매도 등록] {vol:.6f} {self.ticker} @ {target_tp_p:,.0f}원 (목표마진 +{self.profit_pct*100:.1f}%, 예상익: {pnl_expected:+,.0f}원)")
        except Exception as e:
            print(f"[{self.market}] ⚠️ 바스켓 매도 주문 등록 실패: {e}")

    def _replenish_buy_orders(self, state: Dict[str, Any]) -> None:
        """하단 매수 주문을 항상 max_open_buys(3개)로 보충"""
        current_active = state["active_levels"]
        if current_active >= self.max_levels:
            print(f"[{self.market}] 🛑 최대 단수({self.max_levels}단)에 도달하여 추가 매수 주문을 생성하지 않습니다.")
            return

        pending_buys = state.get("pending_buys", {})
        needed = self.max_open_buys - len(pending_buys)
        if needed <= 0:
            return

        # 기준 가격 계산 (기존 대기 매수 중 최저가, 없으면 base_entry_price)
        if pending_buys:
            min_existing_p = min(info["price"] for info in pending_buys.values())
        else:
            min_existing_p = state["base_entry_price"]

        for i in range(1, needed + 1):
            target_lvl = current_active + len(pending_buys) + 1
            if target_lvl > self.max_levels:
                break

            krw_bal = self.client.get_balance("KRW")
            if krw_bal < 5000:
                print(f"[{self.market}] ⚠️ 가용 원화 부족으로 Lvl {target_lvl} 매수 예약 보류 (가용: {krw_bal:,.0f}원)")
                break

            target_p = adjust_price_to_tick(min_existing_p * ((1.0 - self.step_pct) ** i), method="floor")
            # 1Unit(10,000원)에 해당하는 매수 수량 계산
            req_vol = math.floor(((self.unit_krw * 0.9995) / target_p) * 1e8) / 1e8
            if req_vol <= 0 or (req_vol * target_p) < 5000:
                continue

            ident = f"ladder_buy_l{target_lvl}_{int(time.time()*1000)}"
            try:
                res = self.client.create_limit_order(self.market, "bid", req_vol, target_p, identifier=ident)
                b_uuid = res.get("uuid")
                pending_buys[b_uuid] = {
                    "price": target_p,
                    "volume": req_vol,
                    "level": target_lvl
                }
                print(f"[{self.market}] 📥 [지정가 매수 예약] Lvl {target_lvl} : {req_vol:.6f} {self.ticker} @ {target_p:,.0f}원 (10,000원 대기)")
                time.sleep(0.1)
            except Exception as e:
                print(f"[{self.market}] ⚠️ 지정가 매수 등록 실패: {e}")

        state["pending_buys"] = pending_buys

    def run(self) -> None:
        """메인 실거래 루프"""
        if not self.acquire_lock():
            print(f"[{self.market}] ❌ 이미 실행 중인 프로세스가 존재합니다. 기동을 중단합니다.")
            return

        print("\n" + "=" * 80)
        print(f"🤖 [포켓 바스켓 리셋 봇] 가동 시작: {self.market}")
        print(f" • 간격: {self.step_pct*100:.1f}% 하락 매수 | 익절: +{self.profit_pct*100:.1f}% 전량 청산 후 리셋")
        print(f" • 1회 단위: {self.unit_krw:,.0f}원 | 최대 단수: {self.max_levels}단 | 대기 주문: {self.max_open_buys}개")
        print(f" • 서브포켓 격리 DB: {self.db_file}")
        print("=" * 80)

        try:
            state = self.load_state()

            while True:
                # 포지션이 없거나 최초 상태라면 신규 사이클 즉시 시작
                if state["held_volume"] <= 0 or state["pending_sell"] is None:
                    self.start_new_cycle(state)
                    if state["held_volume"] <= 0:
                        time.sleep(self.poll_interval_sec * 5)
                        continue

                time.sleep(self.poll_interval_sec)

                try:
                    open_orders = self.client.get_open_orders(self.market)
                except Exception as e:
                    print(f"[{self.market}] ⚠️ 미체결 주문 조회 실패 (재시도 대기): {e}")
                    continue

                open_uuids = set(o.get("uuid") for o in open_orders if o.get("uuid"))
                sell_info = state.get("pending_sell")
                pending_buys = state.get("pending_buys", {})

                # -------------------------------------------------------------
                # Case 1: 매도 주문 체결 확인 (바스켓 전량 익절 탈출 달성!)
                # -------------------------------------------------------------
                if sell_info and sell_info.get("uuid") not in open_uuids:
                    # 매도 주문 상세 조회로 체결 확인
                    sell_uuid = sell_info["uuid"]
                    try:
                        order_detail = self.client.get_order(sell_uuid)
                        parsed = self.client.parse_order_execution(order_detail)
                    except Exception as e:
                        print(f"[{self.market}] 매도 체결 확인 예외: {e}")
                        parsed = None

                    if parsed and parsed.get("status") in ("FILLED", "PARTIAL"):
                        sold_vol = parsed["volume"]
                        exec_funds = parsed["funds"]
                        fee = parsed["fee"]
                        pnl = exec_funds - state["total_cost"] - fee
                        state["total_realized_pnl"] += pnl
                        state["total_fees"] += fee
                        state["win_count"] += 1
                        curr_cycle = state["cycle_count"]

                        print("\n" + "🎉" * 35)
                        print(f"[{self.market}] 🎯 [바스켓 전량 익절 완료!] 사이클 {curr_cycle}회차 탈출!")
                        print(f" • 매도 체결가: {parsed['price']:,.0f}원 | 체결 수량: {sold_vol:.6f}")
                        print(f" • 회수 금액: {exec_funds:,.0f}원 | 실현 손익: {pnl:+,.0f}원 (누적: {state['total_realized_pnl']:+,.0f}원)")
                        print("🎉" * 35 + "\n")

                        self.log_trade("ask", "BASKET_RESET_SELL", parsed["price"], sold_vol, exec_funds, fee, pnl, curr_cycle, sell_uuid)
                        self.send_telegram(
                            f"🎉 <b>[Cascade {self.ticker} 바스켓 전량 익절 리셋!]</b>\n"
                            f"• 사이클: <b>{curr_cycle}회차 전량 익절 탈출</b>\n"
                            f"• 매도 체결: {sold_vol:.6f} {self.ticker} @ {parsed['price']:,.0f}원 (회수: {exec_funds:,.0f}원)\n"
                            f"• 실현 손익: <b>{pnl:+,.0f}원</b> (누적: <b>{state['total_realized_pnl']:+,.0f}원</b> | {state['win_count']}회 익절)\n"
                            f"• 100% 현금 리셋 완료 ➔ 사이클 {curr_cycle + 1}회차 1Unit 즉시 재진입"
                        )

                        # 전량 청산 완료 -> 즉시 새 사이클 시작 (리셋)
                        state["held_volume"] = 0.0
                        state["total_cost"] = 0.0
                        state["active_levels"] = 0
                        state["pending_sell"] = None
                        self.start_new_cycle(state)
                        continue

                # -------------------------------------------------------------
                # Case 2: 매수 주문 체결 확인 (1개 이상의 매수 주문 체결)
                # -------------------------------------------------------------
                filled_buy_uuids = [u for u in pending_buys if u not in open_uuids]
                if filled_buy_uuids:
                    print(f"\n[{self.market}] 📥 [지정가 매수 체결 감지] {len(filled_buy_uuids)}건 체결!")
                    for u in filled_buy_uuids:
                        try:
                            o_detail = self.client.get_order(u)
                            b_parsed = self.client.parse_order_execution(o_detail)
                        except Exception as e:
                            print(f"[{self.market}] 매수 체결 대사 예외: {e}")
                            b_parsed = None

                        if b_parsed and b_parsed.get("status") in ("FILLED", "PARTIAL"):
                            b_vol = b_parsed["volume"]
                            b_cost = b_parsed["funds"]
                            b_fee = b_parsed["fee"]
                            b_price = b_parsed["price"]
                            b_lvl = pending_buys[u].get("level", state["active_levels"] + 1)

                            state["held_volume"] += b_vol
                            state["total_cost"] += b_cost
                            state["total_fees"] += b_fee
                            state["active_levels"] = max(state["active_levels"] + 1, b_lvl)
                            state["vwap"] = state["total_cost"] / state["held_volume"]

                            self.log_trade("bid", f"BASKET_LADDER_BUY_LVL_{b_lvl}", b_price, b_vol, b_cost, b_fee, 0.0, state["cycle_count"], u)
                            print(f"[{self.market}]   - Lvl {b_lvl} 체결: {b_vol:.6f} @ {b_price:,.0f}원 (투입 {b_cost:,.0f}원) ➔ 새 평단: {state['vwap']:,.0f}원 (총 {state['active_levels']}단)")

                        # 대기 목록에서 제거
                        pending_buys.pop(u, None)

                    # 기존 매도 주문 취소 후, 새 평단가(+0.8%)로 전량 매도 재등록
                    if sell_info and sell_info.get("uuid") in open_uuids:
                        try:
                            self.client.cancel_order(sell_info["uuid"])
                            time.sleep(0.2)
                        except Exception as e:
                            print(f"[{self.market}] 기존 매도 주문 취소 예외: {e}")
                    
                    self._register_basket_sell(state)

                    # 하단 매수 주문 3개로 보충
                    self._replenish_buy_orders(state)
                    self.save_state(state)

                    sell_res = state.get("pending_sell") or {}
                    new_tp = sell_res.get("price", state["vwap"] * (1.0 + self.profit_pct))
                    self.send_telegram(
                        f"📥 <b>[Cascade {self.ticker} 매수 체결]</b>\n"
                        f"• 체결: <b>총 {len(filled_buy_uuids)}건</b> (현재 <b>{state['active_levels']}/{self.max_levels}단</b>)\n"
                        f"• 누적 보유: {state['held_volume']:.6f} {self.ticker} (투입 {state['total_cost']:,.0f}원)\n"
                        f"• 새 매수평단: <b>{state['vwap']:,.0f}원</b>\n"
                        f"• 새 목표 익절가: <b>{new_tp:,.0f}원</b> (+{self.profit_pct*100:.1f}%)"
                    )
                    continue

                # -------------------------------------------------------------
                # Case 3: 매수 주문 부족 시 보충 (정상 대기 상태)
                # -------------------------------------------------------------
                if len(pending_buys) < self.max_open_buys and state["active_levels"] < self.max_levels:
                    self._replenish_buy_orders(state)
                    self.save_state(state)

        except KeyboardInterrupt:
            print(f"\n[{self.market}] 🛑 사용자에 의해 프로세스가 중단되었습니다.")
        finally:
            self.release_lock()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Pocket Basket Reset Runner")
    parser.add_argument("--market", type=str, default="KRW-ETH", help="Target Market (e.g. KRW-ETH)")
    parser.add_argument("--step", type=float, default=0.018, help="Drop step pct (default: 0.018 = 1.8%)")
    parser.add_argument("--profit", type=float, default=0.008, help="Profit pct (default: 0.008 = 0.8%)")
    parser.add_argument("--unit", type=float, default=10000.0, help="Unit KRW (default: 10,000)")
    parser.add_argument("--max-levels", type=int, default=200, help="Max levels (default: 200)")
    args = parser.parse_args()

    runner = PocketBasketResetRunner(
        market=args.market,
        step_pct=args.step,
        profit_pct=args.profit,
        unit_krw=args.unit,
        max_levels=args.max_levels
    )
    runner.run()
