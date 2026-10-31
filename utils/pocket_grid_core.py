"""
========================================================================================
🚀 [AutoBot Trader] 업비트 서브포켓(G60) 실거래 그리드 엔진 (Pocket Grid Core Engine)
========================================================================================
Model G60 실거래 아키텍처 (안전 재감사 R1~R4 하드닝 패치 적용):
- 대상 마켓: KRW-XRP, KRW-SOL, KRW-ETH
- 1회 주문 단위: 10,000 KRW (예산 한도 검증 필수)
- 그리드 간격: step_pct (0.50%)
- 1회 익절 목표: profit_pct (+1.00% ~ +1.20%)
- 5단계 스쿼드 바스켓 이중 익절: 5단 단위 묶음 평단(VWAP) 대비 +1.00% 터치 시 스쿼드 일괄 익절
- 자산 격리: 서브포켓(G60) 전용 API 키를 사용하여 메인 계좌와 100% 독립 운용
- 멱등적 단일 정산 경계(Unified Idempotent Settlement): 동기 체결과 비동기 재시작 복구가 동일한 정산 로직 공유
- 스쿼드 다중 그리드 정산(Keyed Allocation): 복수 그리드 일괄 매도 시 allocation_id 키 분리를 통해 UNIQUE 제약 충돌 방지
- 미확정 주문 틱 격리: 주문 후 타임아웃/미확정 발생 시 즉시 해당 틱 종료(추가 주문 전면 차단)
- 부분체결 수량 보존(Volume Conservation): 부분 매도 시 미체결 잔여 수량 및 원가 장부 보존
- 엄격한 Fail-Closed 지속성: 유효하지 않은 회계 데이터 감지 시 임의 삭제 없이 trading_halted 격리
- 완전한 수수료 차감: 매수 수수료 및 매도 수수료 모두 실현 손익에서 정확히 차감
========================================================================================
"""

import os
import sys
import time
import json
import math
import sqlite3
import tempfile
import atexit
import requests
from datetime import datetime
from typing import Dict, List, Any, Optional, Tuple

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from utils.pocket_api import UpbitPocketClient
from utils.bot import send_message

DB_FILE = os.path.join(BASE_DIR, "pocket_trade_history.db")


class PocketGridRunner:
    def __init__(
        self,
        market: str = "KRW-XRP",
        step_pct: float = 0.005,
        profit_pct: float = 0.010,
        squad_tp_pct: float = 0.010,
        max_levels: int = 30,
        unit_krw: float = 10_000.0,
        grid_budget: float = 300_000.0,
        poll_interval_sec: int = 5,
        exit_only: bool = False,
        basket_exit_pct: Optional[float] = None,
        auto_reallocate_eth: bool = True
    ):
        self.market = market
        self.ticker = market.replace("KRW-", "")
        self.step_pct = step_pct
        self.profit_pct = profit_pct
        self.squad_tp_pct = squad_tp_pct
        self.squad_size = 5
        self.min_squad_tranches_for_tp = 2
        self.max_levels = max_levels
        self.unit_krw = unit_krw
        self.grid_budget = grid_budget
        self.poll_interval_sec = poll_interval_sec
        self.exit_only = exit_only
        self.basket_exit_pct = basket_exit_pct
        self.auto_reallocate_eth = auto_reallocate_eth

        safe_tag = market.replace("-", "_")
        self.state_file = os.path.join(BASE_DIR, f"pocket_grid_state_{safe_tag}.json")
        self.lock_file = os.path.join(BASE_DIR, f".pocket_grid_{safe_tag}.lock")
        self.db_file = DB_FILE
        self._lock_fd = None

        self.client = UpbitPocketClient()

    def get_effective_budget_and_levels(self) -> Tuple[float, int]:
        """
        ETH 청산 완료 시 매도 회수금을 SOL/XRP 예산으로 50:50 동적 배분 (기본: 300,000원 -> +106,000원 = 406,000원 / 40단)
        """
        if not getattr(self, "auto_reallocate_eth", True) or self.market not in ("KRW-SOL", "KRW-XRP"):
            return self.grid_budget, self.max_levels

        eth_state_path = os.path.join(BASE_DIR, "pocket_grid_state_KRW_ETH.json")
        if not os.path.exists(eth_state_path):
            return self.grid_budget, self.max_levels

        try:
            with open(eth_state_path, "r", encoding="utf-8") as f:
                eth_state = json.load(f)

            is_eth_retired = (
                eth_state.get("exit_only", False) and
                len(eth_state.get("active_grids", [])) == 0 and
                eth_state.get("basket_exit_settled", False)
            )
            if is_eth_retired:
                rev = float(eth_state.get("last_basket_revenue", 0.0) or 0.0)
                extra = round(rev / 2.0, -3) if rev > 0 else 106_000.0
                effective_budget = self.grid_budget + extra
                effective_levels = int(effective_budget // self.unit_krw)
                return effective_budget, effective_levels
        except Exception:
            pass

        return self.grid_budget, self.max_levels

    def acquire_lock(self) -> bool:
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
        """데이터베이스 초기화 및 스키마 검증/마이그레이션 (order_uuid 단독 UNIQUE가 아닌 allocation_id UNIQUE)"""
        conn = None
        try:
            conn = sqlite3.connect(self.db_file, timeout=10.0)
            c = conn.cursor()
            c.execute('''
                CREATE TABLE IF NOT EXISTS pocket_trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT,
                    market TEXT,
                    side TEXT,
                    action TEXT,
                    price REAL,
                    volume REAL,
                    cost_or_revenue REAL,
                    fee REAL,
                    pnl REAL,
                    level INTEGER,
                    strategy TEXT,
                    order_uuid TEXT,
                    allocation_id TEXT UNIQUE
                )
            ''')
            conn.commit()

            c.execute("PRAGMA table_info(pocket_trades)")
            cols = [info[1] for info in c.fetchall()]
            if "allocation_id" not in cols:
                c.execute('''
                    CREATE TABLE pocket_trades_new (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp TEXT,
                        market TEXT,
                        side TEXT,
                        action TEXT,
                        price REAL,
                        volume REAL,
                        cost_or_revenue REAL,
                        fee REAL,
                        pnl REAL,
                        level INTEGER,
                        strategy TEXT,
                        order_uuid TEXT,
                        allocation_id TEXT UNIQUE
                    )
                ''')
                c.execute('''
                    INSERT INTO pocket_trades_new (id, timestamp, market, side, action, price, volume, cost_or_revenue, fee, pnl, level, strategy, order_uuid, allocation_id)
                    SELECT id, timestamp, market, side, action, price, volume, cost_or_revenue, fee, pnl, level, strategy, order_uuid,
                           COALESCE(order_uuid || ':lvl_' || level, order_uuid)
                    FROM pocket_trades
                ''')
                c.execute("DROP TABLE pocket_trades")
                c.execute("ALTER TABLE pocket_trades_new RENAME TO pocket_trades")
                conn.commit()
        finally:
            if conn:
                conn.close()

    def log_trades_batch(self, trades: List[Dict[str, Any]]):
        """다중 실체결 DB 저널을 단일 SQLite 트랜잭션으로 원자적 기록 (Fail-Closed, 중복 allocation_id 스킵)"""
        if not trades:
            return
        conn = None
        try:
            conn = sqlite3.connect(self.db_file, timeout=10.0)
            c = conn.cursor()
            for t in trades:
                alloc_id = t.get("allocation_id") or f"{t.get('order_uuid')}:lvl_{t.get('level')}_{t.get('action')}"
                c.execute("SELECT id FROM pocket_trades WHERE allocation_id = ?", (alloc_id,))
                if c.fetchone():
                    continue

                now_str = t.get("timestamp") or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                c.execute('''
                    INSERT INTO pocket_trades (timestamp, market, side, action, price, volume, cost_or_revenue, fee, pnl, level, strategy, order_uuid, allocation_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (now_str, self.market, t["side"], t["action"], t["price"], t["volume"], t["cost_or_revenue"], t["fee"], t["pnl"], t["level"], t.get("strategy", "POCKET_G60"), t.get("order_uuid", ""), alloc_id))
            conn.commit()
        finally:
            if conn:
                conn.close()

    def log_trade(
        self,
        side: str,
        action: str,
        price: float,
        volume: float,
        cost_or_revenue: float,
        fee: float,
        pnl: float,
        level: int,
        strategy: str = "POCKET_G60",
        order_uuid: str = "",
        allocation_id: Optional[str] = None,
        timestamp: Optional[str] = None
    ):
        """단일 실체결 DB 저널 기록 (단일 트랜잭션 위임)"""
        self.log_trades_batch([{
            "side": side,
            "action": action,
            "price": price,
            "volume": volume,
            "cost_or_revenue": cost_or_revenue,
            "fee": fee,
            "pnl": pnl,
            "level": level,
            "strategy": strategy,
            "order_uuid": order_uuid,
            "allocation_id": allocation_id,
            "timestamp": timestamp
        }])

    def send_telegram(self, msg: str):
        # 단위 테스트(unittest 등) 실행 중이거나 테스트 환경변수 설정 시 실제 텔레그램 발송 원천 차단
        if (
            os.environ.get("AUTOTRADE_ENV") == "test"
            or os.environ.get("TESTING") == "1"
            or ("unittest" in sys.modules and any("test" in arg.lower() for arg in sys.argv))
        ):
            return
        try:
            send_message(msg)
        except Exception as e:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] [{self.market}] 텔레그램 발송 실패: {e}")

    def load_state(self) -> Dict[str, Any]:
        """상태 파일 로드 (Fail-Closed: 손상된 JSON 또는 유효하지 않은 회계 데이터 감지 시 임의 삭제 없이 안전 중단)"""
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, "r", encoding="utf-8") as f:
                    s = json.load(f)

                # 필수 키 및 구조 검증
                if not isinstance(s, dict) or "market" not in s:
                    print(f"[CRITICAL] [{self.market}] 상태 파일 구조 비정상: 필수 필드 누락")
                    return {
                        "market": self.market,
                        "trading_halted": True,
                        "active_grids": [],
                        "settled_orders": [],
                        "error": "MISSING_REQUIRED_FIELDS"
                    }

                # 마켓 식별자 일치 검증
                if s.get("market") != self.market:
                    print(f"[CRITICAL] [{self.market}] 상태 파일 마켓 불일치 ({s.get('market')} != {self.market}). 안전 중단합니다.")
                    s["trading_halted"] = True
                    s["error"] = f"MARKET_MISMATCH: {s.get('market')} != {self.market}"
                    return s

                # active_grids 필수 존재 및 타입 엄격 검증 (누락 시 기본값 대체 거부)
                if "active_grids" not in s or not isinstance(s.get("active_grids"), list):
                    print(f"[CRITICAL] [{self.market}] 상태 파일 내 active_grids 필드 누락 또는 비정상 타입. 임의 초기화를 거부하고 안전 중단합니다.")
                    s["trading_halted"] = True
                    s["error"] = "MISSING_OR_INVALID_ACTIVE_GRIDS"
                    return s

                # 상위 회계 필드 건전성 검증
                budget = s.get("allocated_budget")
                if budget is not None:
                    try:
                        b_val = float(budget)
                        if not (math.isfinite(b_val) and b_val >= 0):
                            s["trading_halted"] = True
                            s["error"] = f"INVALID_BUDGET: {budget}"
                            return s
                    except (ValueError, TypeError):
                        s["trading_halted"] = True
                        s["error"] = f"INVALID_BUDGET: {budget}"
                        return s

                pnl = s.get("realized_pnl")
                if pnl is not None:
                    try:
                        p_val = float(pnl)
                        if not math.isfinite(p_val):
                            s["trading_halted"] = True
                            s["error"] = f"INVALID_REALIZED_PNL: {pnl}"
                            return s
                    except (ValueError, TypeError):
                        s["trading_halted"] = True
                        s["error"] = f"INVALID_REALIZED_PNL: {pnl}"
                        return s

                fees = s.get("total_fees")
                if fees is not None:
                    try:
                        f_val = float(fees)
                        if not (math.isfinite(f_val) and f_val >= 0):
                            s["trading_halted"] = True
                            s["error"] = f"INVALID_TOTAL_FEES: {fees}"
                            return s
                    except (ValueError, TypeError):
                        s["trading_halted"] = True
                        s["error"] = f"INVALID_TOTAL_FEES: {fees}"
                        return s

                # 그리드별 회계 데이터 건전성 엄격 검증
                levels_seen = set()
                for g in s["active_grids"]:
                    if not isinstance(g, dict):
                        s["trading_halted"] = True
                        s["error"] = "INVALID_GRID_RECORD"
                        return s

                    lvl = g.get("lvl")
                    if not isinstance(lvl, int) or lvl < 1:
                        s["trading_halted"] = True
                        s["error"] = f"INVALID_GRID_LEVEL: {lvl}"
                        return s

                    if lvl in levels_seen:
                        print(f"[CRITICAL] [{self.market}] 중복 그리드 레벨 감지 (lvl={lvl}). 안전 중단합니다.")
                        s["trading_halted"] = True
                        s["error"] = f"DUPLICATE_GRID_LEVEL: lvl {lvl}"
                        return s
                    levels_seen.add(lvl)

                    try:
                        buy_p = float(g.get("buy_p", 0.0) or 0.0)
                        vol = float(g.get("vol", 0.0) or 0.0)
                        cost = float(g.get("cost", 0.0) or 0.0)
                        fee = float(g.get("fee", 0.0) or 0.0)
                        tp_p = float(g.get("tp_p", 0.0) or 0.0)
                    except (ValueError, TypeError) as e:
                        s["trading_halted"] = True
                        s["error"] = f"INVALID_GRID_FIELD_TYPE: lvl {lvl}: {e}"
                        return s

                    if not (math.isfinite(buy_p) and buy_p > 0 and
                            math.isfinite(vol) and vol > 0 and
                            math.isfinite(cost) and cost > 0 and
                            math.isfinite(fee) and fee >= 0 and
                            math.isfinite(tp_p) and tp_p > 0):
                        print(f"[CRITICAL] [{self.market}] 비정상 그리드 데이터 감지 (lvl={lvl}, buy_p={buy_p}, vol={vol}, cost={cost}, fee={fee}, tp_p={tp_p}). 임의 삭제를 거부하고 안전 중단합니다.")
                        s["trading_halted"] = True
                        s["error"] = f"INVALID_GRID_ACCOUNTING: lvl {lvl} has non-positive or non-finite values"
                        return s

                s.setdefault("settled_orders", [])
                if s.get("exit_only"):
                    self.exit_only = True
                if s.get("basket_exit_pct") is not None:
                    self.basket_exit_pct = float(s["basket_exit_pct"])
                return s

            except Exception as e:
                print(f"[CRITICAL] [{self.market}] 상태 파일 로드/파싱 실패: {e}. 임의 초기화를 거부하고 안전 중단 플래그를 설정합니다.")
                return {
                    "market": self.market,
                    "trading_halted": True,
                    "active_grids": [],
                    "settled_orders": [],
                    "error": f"CORRUPT_STATE_JSON: {e}"
                }

        # 상태 파일이 전혀 존재하지 않는 신규 생성 시에만 초기 상태 생성
        initial_state = {
            "market": self.market,
            "strategy": "POCKET_G60",
            "mode": f"EXIT_ONLY_BASKET_{int((self.basket_exit_pct or 0.01)*100)}PCT" if (self.exit_only and self.basket_exit_pct) else "POCKET_G60",
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "allocated_budget": self.grid_budget,
            "realized_pnl": 0.0,
            "total_fees": 0.0,
            "win_count": 0,
            "total_trades": 0,
            "base_price": 0.0,
            "active_grids": [],
            "pending_order": None,
            "settled_orders": [],
            "trading_halted": False,
            "exit_only": self.exit_only,
            "basket_exit_pct": self.basket_exit_pct,
            "last_update": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
        self.save_state(initial_state)
        return initial_state

    def save_state(self, state: Dict[str, Any]):
        state["last_update"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        state_dir = os.path.dirname(self.state_file)
        fd, temp_path = tempfile.mkstemp(dir=state_dir, prefix=f".pocket_state_{self.ticker}_{os.getpid()}_", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_path, self.state_file)
            temp_path = None
        finally:
            if temp_path and os.path.exists(temp_path):
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass

    def get_current_price(self) -> Optional[float]:
        try:
            url = f"https://api.upbit.com/v1/ticker?markets={self.market}"
            r = requests.get(url, timeout=3)
            if r.status_code == 200:
                data = r.json()
                if data and len(data) > 0:
                    p = float(data[0].get("trade_price", 0.0) or 0.0)
                    if math.isfinite(p) and p > 0:
                        return p
        except Exception:
            pass
        return None

    def settle_buy_order(
        self,
        state: Dict[str, Any],
        parsed: Dict[str, Any],
        pending: Dict[str, Any],
        now_str: str
    ) -> Dict[str, Any]:
        """매수 체결에 대한 단일 멱등 정산 경계 (동기 실행 및 비동기 복구 공용, 재생 멱등성 보장)"""
        order_uuid = parsed.get("uuid", "")
        ident_val = pending.get("identifier") or parsed.get("identifier")

        # 0. 재생 멱등성 확인 (P1-V3-1 방어: 기정산 주문 replay 시 상태 불변)
        settled_orders = state.setdefault("settled_orders", [])
        is_already_settled = False
        if order_uuid and order_uuid in settled_orders:
            is_already_settled = True
        if ident_val and ident_val in settled_orders:
            is_already_settled = True

        if is_already_settled:
            if state.get("pending_order"):
                p_uuid = state["pending_order"].get("uuid")
                p_ident = state["pending_order"].get("identifier")
                if (order_uuid and p_uuid == order_uuid) or (ident_val and p_ident == ident_val):
                    state["pending_order"] = None
                    self.save_state(state)
            return state

        lvl = pending.get("lvl", 1)
        sq = pending.get("squad", (lvl - 1) // self.squad_size + 1)
        buy_p = float(parsed.get("price", 0.0) or 0.0)
        vol = float(parsed.get("volume", 0.0) or 0.0)
        cost = float(parsed.get("funds", 0.0) or 0.0)
        fee = float(parsed.get("fee", 0.0) or 0.0)
        tp_p = buy_p * (1.0 + self.profit_pct)
        alloc_id = f"{order_uuid}:buy_lvl_{lvl}"

        # 1. DB 저널 기록 (멱등성 보장)
        self.log_trade(
            side="bid",
            action=f"POCKET_BUY_LVL_{lvl}",
            price=buy_p,
            volume=vol,
            cost_or_revenue=cost,
            fee=fee,
            pnl=0.0,
            level=lvl,
            strategy="POCKET_G60",
            order_uuid=order_uuid,
            allocation_id=alloc_id,
            timestamp=now_str
        )

        # 2. 상태 장부(active_grids) 반영 (중복 추가 방지)
        active = state.setdefault("active_grids", [])
        if not any(g.get("order_uuid") == order_uuid for g in active):
            grid_item = {
                "lvl": lvl,
                "squad": sq,
                "buy_p": buy_p,
                "vol": vol,
                "cost": cost,
                "fee": fee,
                "tp_p": tp_p,
                "order_uuid": order_uuid,
                "buy_time": now_str
            }
            active.append(grid_item)
            active.sort(key=lambda x: x["lvl"])

        # 3. 정산 완료 주문 등록 (재생 멱등성 보장)
        if order_uuid and order_uuid not in settled_orders:
            settled_orders.append(order_uuid)
        if ident_val and ident_val not in settled_orders:
            settled_orders.append(ident_val)
        if len(settled_orders) > 1000:
            state["settled_orders"] = settled_orders[-1000:]

        # 4. 미확정 체크포인트 해제 및 원자적 저장
        state["pending_order"] = None
        self.save_state(state)

        # 5. 텔레그램 실체결 알림 발송
        try:
            active_cnt = len(state.get("active_grids", []))
            p_str = f"{buy_p:,.0f}원" if buy_p >= 100 else f"{buy_p:,.2f}원"
            tp_str = f"{tp_p:,.0f}원" if tp_p >= 100 else f"{tp_p:,.2f}원"
            _, eff_max_levels = self.get_effective_budget_and_levels()
            self.send_telegram(
                f"🔵 <b>[G60 실거래 매수]</b> {self.market} (Lvl {lvl} / {sq}스쿼드)\n"
                f"• 체결단가: <b>{p_str}</b> | 체결수량: {vol:.6f} {self.ticker}\n"
                f"• 매수금액: {cost:,.0f}원 (수수료: {fee:,.1f}원)\n"
                f"• 개별 목표가: <b>{tp_str}</b> (+{self.profit_pct * 100:.1f}%)\n"
                f"• 보유 그리드: <b>{active_cnt}/{eff_max_levels}단</b>"
            )
        except Exception as e_tg:
            print(f"[{self.market}] 텔레그램 매수 알림 오류: {e_tg}")

        return state

    def settle_sell_order(
        self,
        state: Dict[str, Any],
        parsed: Dict[str, Any],
        pending: Dict[str, Any],
        now_str: str
    ) -> Dict[str, Any]:
        """매도 체결에 대한 단일 멱등 정산 경계 (개별/스쿼드 일원화, 수량 보존, 중복 UUID 분리, 재생 멱등성 보장)"""
        order_uuid = parsed.get("uuid", "")
        ident_val = pending.get("identifier") or parsed.get("identifier")

        # 0. 재생 멱등성 확인 (P1-V3-1 방어: 이미 반영된 매도 주문의 중복 정산 원천 차단)
        settled_orders = state.setdefault("settled_orders", [])
        is_already_settled = False
        if order_uuid and order_uuid in settled_orders:
            is_already_settled = True
        if ident_val and ident_val in settled_orders:
            is_already_settled = True

        if is_already_settled:
            if state.get("pending_order"):
                p_uuid = state["pending_order"].get("uuid")
                p_ident = state["pending_order"].get("identifier")
                if (order_uuid and p_uuid == order_uuid) or (ident_val and p_ident == ident_val):
                    state["pending_order"] = None
                    self.save_state(state)
            return state

        exit_p = float(parsed.get("price", 0.0) or 0.0)
        exec_v = float(parsed.get("volume", 0.0) or 0.0)
        actual_rev = float(parsed.get("funds", 0.0) or 0.0)
        sell_fee = float(parsed.get("fee", 0.0) or 0.0)
        exit_type = pending.get("exit_type", "INDIVIDUAL")
        sq = pending.get("squad", 1)
        target_grids = pending.get("target_grids", [])

        if not target_grids:
            lvl = pending.get("lvl", 1)
            target_g = next((x for x in state.get("active_grids", []) if x["lvl"] == lvl), None)
            if target_g:
                target_grids = [target_g.copy()]
            else:
                target_grids = [{"lvl": lvl, "vol": exec_v, "cost": actual_rev, "fee": 0.0}]

        total_target_vol = sum(float(x["vol"]) for x in target_grids)
        if total_target_vol <= 0:
            state["pending_order"] = None
            if order_uuid and order_uuid not in settled_orders:
                settled_orders.append(order_uuid)
            if ident_val and ident_val not in settled_orders:
                settled_orders.append(ident_val)
            self.save_state(state)
            return state

        ratio = min(1.0, exec_v / total_target_vol)
        allocated_cost_total = sum(float(x["cost"]) for x in target_grids) * ratio
        allocated_buy_fee_total = sum(float(x.get("fee", 0.0) or 0.0) for x in target_grids) * ratio
        net_pnl_total = actual_rev - allocated_cost_total - allocated_buy_fee_total - sell_fee

        # 1. DB 저널 기록 (그리드별 Keyed Allocation: order_uuid:sell_lvl_{lvl}, 단일 SQLite 트랜잭션 기록)
        trade_records = []
        for g_target in target_grids:
            g_lvl = g_target["lvl"]
            alloc_id = f"{order_uuid}:sell_lvl_{g_lvl}"
            g_v = float(g_target["vol"])
            g_portion = g_v / total_target_vol if total_target_vol > 0 else 0.0
            sold_g_vol = g_v * ratio
            sold_g_cost = float(g_target["cost"]) * ratio
            sold_g_fee = float(g_target.get("fee", 0.0) or 0.0) * ratio
            allocated_rev = actual_rev * g_portion
            allocated_sell_fee = sell_fee * g_portion
            allocated_net_pnl = net_pnl_total * g_portion

            if exit_type == "BASKET":
                action_label = f"POCKET_BASKET_EXIT_LVL_{g_lvl}"
                strat_label = "POCKET_BASKET_EXIT"
            elif exit_type == "SQUAD":
                action_label = f"POCKET_SQUAD_{sq}_TP_LVL_{g_lvl}"
                strat_label = f"SQUAD_{sq}_BASKET"
            else:
                action_label = f"POCKET_TP_LVL_{g_lvl}"
                strat_label = "POCKET_G60"

            if ratio < 1.0 - 1e-8:
                action_label += "_PARTIAL"

            trade_records.append({
                "side": "ask",
                "action": action_label,
                "price": exit_p,
                "volume": sold_g_vol,
                "cost_or_revenue": allocated_rev,
                "fee": allocated_sell_fee,
                "pnl": allocated_net_pnl,
                "level": g_lvl,
                "strategy": strat_label,
                "order_uuid": order_uuid,
                "allocation_id": alloc_id,
                "timestamp": now_str
            })

        self.log_trades_batch(trade_records)

        # 2. 상태 장부(active_grids) 수량/원가 비례 차감 및 잔여 보존 (멱등성 보장)
        active = state.get("active_grids", [])
        target_lvls = {x["lvl"] for x in target_grids}
        surviving = []
        for g in active:
            if g["lvl"] in target_lvls:
                tgt = next((x for x in target_grids if x["lvl"] == g["lvl"]), None)
                if tgt:
                    g_v = float(g["vol"])
                    sold_v = float(tgt["vol"]) * ratio
                    rem_v = max(0.0, g_v - sold_v)
                    if rem_v > 1e-8:
                        g["vol"] = rem_v
                        g["cost"] = max(0.0, float(g["cost"]) - float(tgt["cost"]) * ratio)
                        g["fee"] = max(0.0, float(g.get("fee", 0.0) or 0.0) - float(tgt.get("fee", 0.0) or 0.0) * ratio)
                        surviving.append(g)
                else:
                    surviving.append(g)
            else:
                surviving.append(g)

        state["active_grids"] = surviving
        state["realized_pnl"] = state.get("realized_pnl", 0.0) + net_pnl_total
        state["total_fees"] = state.get("total_fees", 0.0) + (allocated_buy_fee_total + sell_fee)
        state["total_trades"] = state.get("total_trades", 0) + len(target_grids)
        if net_pnl_total > 0:
            state["win_count"] = state.get("win_count", 0) + len(target_grids)

        if exit_type == "BASKET":
            state["basket_exit_settled"] = True
            state["last_basket_revenue"] = actual_rev
            state["last_basket_profit"] = net_pnl_total
            state["last_basket_exit_time"] = now_str
            state["exit_only"] = True

        # 3. 정산 완료 주문 등록 (재생 멱등성 보장)
        if order_uuid and order_uuid not in settled_orders:
            settled_orders.append(order_uuid)
        if ident_val and ident_val not in settled_orders:
            settled_orders.append(ident_val)
        if len(settled_orders) > 1000:
            state["settled_orders"] = settled_orders[-1000:]

        # 4. 미확정 체크포인트 해제 및 원자적 저장
        state["pending_order"] = None
        self.save_state(state)

        # 5. 텔레그램 실체결 알림 발송
        try:
            rem_cnt = len(state.get("active_grids", []))
            p_str = f"{exit_p:,.0f}원" if exit_p >= 100 else f"{exit_p:,.2f}원"
            pnl_pct = (net_pnl_total / allocated_cost_total * 100.0) if allocated_cost_total > 0 else 0.0
            eff_budget, eff_max_lvls = self.get_effective_budget_and_levels()

            if exit_type == "BASKET":
                per_coin_extra = round(actual_rev / 2.0, -3)
                self.send_telegram(
                    f"🏁 <b>[G60 실거래 바스켓 일괄 탈출 완료]</b> {self.market}\n"
                    f"• 매도단가: <b>{p_str}</b> | 매도수량: {exec_v:.6f} {self.ticker}\n"
                    f"• 총 매도대금: <b>{actual_rev:,.0f}원</b> (투입원금 {allocated_cost_total:,.0f}원)\n"
                    f"• 실현손익: <b>{net_pnl_total:+,.0f}원</b> ({pnl_pct:+.2f}%)\n"
                    f"• 누적 손익: <b>{state.get('realized_pnl', 0.0):+,.0f}원</b> ({state.get('win_count', 0)}회 청산)\n"
                    f"• 💰 <b>SOL / XRP 예산 배분</b>: 각 <b>+{per_coin_extra:,.0f}원</b> 예산 증액 적용!"
                )
            elif exit_type == "SQUAD":
                self.send_telegram(
                    f"🟢 <b>[G60 실거래 스쿼드 익절]</b> {self.market} (스쿼드 {sq} 바스켓)\n"
                    f"• 매도단가: <b>{p_str}</b> | 매도수량: {exec_v:.6f} {self.ticker}\n"
                    f"• 매도대금: {actual_rev:,.0f}원 (투입원금 {allocated_cost_total:,.0f}원)\n"
                    f"• 실현손익: <b>{net_pnl_total:+,.0f}원</b> ({pnl_pct:+.2f}%)\n"
                    f"• 누적 손익: <b>{state.get('realized_pnl', 0.0):+,.0f}원</b> ({state.get('win_count', 0)}회 익절)\n"
                    f"• 잔여 그리드: <b>{rem_cnt}/{eff_max_lvls}단</b>"
                )
            else:
                lvl_val = target_grids[0]["lvl"] if target_grids else pending.get("lvl", 1)
                self.send_telegram(
                    f"🟢 <b>[G60 실거래 개별 익절]</b> {self.market} (Lvl {lvl_val} 짤짤이)\n"
                    f"• 매도단가: <b>{p_str}</b> | 매도수량: {exec_v:.6f} {self.ticker}\n"
                    f"• 매도대금: {actual_rev:,.0f}원 (투입원금 {allocated_cost_total:,.0f}원)\n"
                    f"• 실현손익: <b>{net_pnl_total:+,.0f}원</b> ({pnl_pct:+.2f}%)\n"
                    f"• 누적 손익: <b>{state.get('realized_pnl', 0.0):+,.0f}원</b> ({state.get('win_count', 0)}회 익절)\n"
                    f"• 잔여 그리드: <b>{rem_cnt}/{eff_max_lvls}단</b>"
                )
        except Exception as e_tg:
            print(f"[{self.market}] 텔레그램 매도 알림 오류: {e_tg}")

        return state

    def _parse_order_execution(self, order_info: Dict[str, Any], req_vol: Optional[float] = None) -> Optional[Dict[str, Any]]:
        """체결 응답 파싱 (Mock 환경에서도 실제 파서 로직 보장)"""
        if hasattr(self.client, "parse_order_execution"):
            res = self.client.parse_order_execution(order_info, req_vol=req_vol)
            if isinstance(res, dict):
                return res
        return UpbitPocketClient.parse_order_execution(self.client, order_info, req_vol=req_vol)

    def reconcile_pending_order(self, state: Dict[str, Any], pending: Dict[str, Any], now_str: str) -> Dict[str, Any]:
        """미확정 주문(pending_order)을 거래소와 대사하여 체결 반영 또는 정리"""
        uuid_val = pending.get("uuid")
        ident_val = pending.get("identifier")
        action = pending.get("action")
        req_vol = pending.get("target_vol")

        # 선행 멱등성 검사: 이미 정산 완료된 주문에 대한 재대사 요청인 경우 No-op
        settled_orders = state.get("settled_orders", [])
        if (uuid_val and uuid_val in settled_orders) or (ident_val and ident_val in settled_orders):
            print(f"[{self.market}] ℹ️ 이미 정산 완료된 주문({uuid_val or ident_val}) 대사 요청 -> No-op")
            if state.get("pending_order") == pending:
                state["pending_order"] = None
                self.save_state(state)
            return state

        try:
            order_info = None
            if uuid_val:
                order_info = self.client.get_order(uuid_val)
            elif ident_val:
                order_info = self.client.get_order_by_identifier(ident_val)

            if not order_info:
                print(f"[{self.market}] ⏳ 미확정 주문({ident_val or uuid_val}) 거래소 조회 미확인 (미결 상태 유지) -> 보류 지속")
                return state

            # 주문 정보 파싱
            parsed = self._parse_order_execution(order_info, req_vol=req_vol)
            if isinstance(parsed, dict):
                status = parsed.get("status")
                if status in ("FILLED", "PARTIAL"):
                    if action == "BUY":
                        state = self.settle_buy_order(state, parsed, pending, now_str)
                        print(f"[{self.market}] ✅ 미확정 매수 주문 대사 체결 완료: {parsed.get('uuid')}")
                    elif action == "SELL":
                        state = self.settle_sell_order(state, parsed, pending, now_str)
                        print(f"[{self.market}] ✅ 미확정 매도 주문 대사 체결 완료: {parsed.get('uuid')}")
                    return state
                elif status == "CANCELLED":
                    print(f"[{self.market}] ℹ️ 미확정 주문({ident_val or uuid_val}) 거래소 체결 없이 취소 확인 -> 보류 해제")
                    state["pending_order"] = None
                    self.save_state(state)
                    return state

            print(f"[{self.market}] ⏳ 미확정 주문({uuid_val or ident_val}) 거래소 체결 대기 중...")
            return state

        except Exception as e:
            print(f"[{self.market}] ⚠️ 미확정 주문 대사 중 오류: {e}")
            return state

    def execute_live_buy(
        self,
        lvl: int,
        squad: int,
        target_p: float,
        now_str: str,
        state: Optional[Dict[str, Any]] = None
    ) -> Optional[Dict[str, Any]]:
        """실거래 시장가 매수 주문 집행 및 체결 대사 (Durable Pending Intent & Checkpoint)"""
        # 0. 선행 미확정 주문 검증 (P1-R4 방어)
        if state is not None and state.get("pending_order"):
            raise RuntimeError(f"[{self.market}] 기존 미확정 주문이 해결되지 않아 신규 매수 불가")

        # 0-B. exit_only 방어
        if self.exit_only:
            raise RuntimeError(f"[{self.market}] exit_only 모드에서는 신규 매수가 불가합니다")

        eff_budget, _ = self.get_effective_budget_and_levels()
        # 1. 예산 한도 철저 검증
        if self.unit_krw > eff_budget:
            print(f"[{self.market}] ⚠️ 1회 주문 단위({self.unit_krw:,.0f}원)가 전체 배정 예산({eff_budget:,.0f}원) 초과")
            return None

        # 2. 포켓 원화 잔고 확인
        krw_bal = self.client.get_balance("KRW")
        if krw_bal < self.unit_krw:
            print(f"[{self.market}] ⚠️ 포켓 잔여 원화 부족으로 매수 보류: 현재 {krw_bal:,.0f}원 < 필요 {self.unit_krw:,.0f}원")
            return None

        # 3. Durable pending intent 생성 및 저장
        ident = f"G60_{self.ticker}_B_{lvl}_{int(time.time()*1000)}"
        pending = {
            "identifier": ident,
            "action": "BUY",
            "lvl": lvl,
            "squad": squad,
            "target_cost": self.unit_krw,
            "created_at": now_str,
            "uuid": None,
            "status": "SUBMITTING"
        }
        if state is not None:
            state["pending_order"] = pending
            self.save_state(state)

        try:
            print(f"[{self.market}] 🛒 [실거래 매수 요청] Lvl {lvl} (10,000원 투입, ident: {ident})...")
            res = self.client.buy_market_order_and_wait(self.market, self.unit_krw, identifier=ident)
            if res.get("status") in ("FILLED", "PARTIAL"):
                if state is not None:
                    self.settle_buy_order(state, res, pending, now_str)
                    grid_item = next((g for g in state["active_grids"] if g.get("order_uuid") == res["uuid"]), None)
                    return grid_item
                else:
                    buy_p = float(res["price"])
                    vol = float(res["volume"])
                    cost = float(res["funds"])
                    fee = float(res["fee"])
                    ouuid = res["uuid"]
                    tp_p = buy_p * (1.0 + self.profit_pct)
                    self.log_trade("bid", f"POCKET_BUY_LVL_{lvl}", buy_p, vol, cost, fee, 0.0, lvl, "POCKET_G60", ouuid, f"{ouuid}:buy_lvl_{lvl}", now_str)
                    return {
                        "lvl": lvl, "squad": squad, "buy_p": buy_p, "vol": vol,
                        "cost": cost, "fee": fee, "tp_p": tp_p, "order_uuid": ouuid,
                        "buy_time": now_str
                    }
            elif res.get("status") == "CANCELLED":
                if state is not None:
                    state["pending_order"] = None
                    self.save_state(state)
                return None

        except Exception as e:
            print(f"[{self.market}] ❌ 실거래 매수 실패/대기: {e}")
            raise

        return None

    def execute_live_sell(
        self,
        vol: float,
        label: str,
        lvl: int = 1,
        exit_type: str = "INDIVIDUAL",
        squad: int = 1,
        target_grids: Optional[List[Dict[str, Any]]] = None,
        state: Optional[Dict[str, Any]] = None
    ) -> Optional[Dict[str, Any]]:
        """실거래 시장가 매도 주문 집행 및 체결 대사 (Durable Pending Intent & Checkpoint)"""
        # 0. 선행 미확정 주문 검증 (P1-R4 방어: 미확정 주문 존재 시 추가 매도 원천 차단)
        if state is not None and state.get("pending_order"):
            raise RuntimeError(f"[{self.market}] 기존 미확정 주문이 해결되지 않아 신규 매도 불가")

        if vol <= 0:
            print(f"[{self.market}] ⚠️ 매도 요청 수량이 0 이하입니다: {vol}")
            return None

        targets = target_grids or [{"lvl": lvl, "vol": vol, "cost": 0.0, "fee": 0.0}]
        ident = f"G60_{self.ticker}_S_{lvl}_{int(time.time()*1000)}"
        pending = {
            "identifier": ident,
            "action": "SELL",
            "exit_type": exit_type,
            "lvl": lvl,
            "squad": squad,
            "target_vol": vol,
            "target_grids": targets,
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "uuid": None,
            "status": "SUBMITTING"
        }
        if state is not None:
            state["pending_order"] = pending
            self.save_state(state)

        try:
            print(f"[{self.market}] 💰 [실거래 매도 요청] {label} ({vol:.8f} {self.ticker}, ident: {ident})...")
            res = self.client.sell_market_order_and_wait(self.market, vol, identifier=ident)
            if res.get("status") in ("FILLED", "PARTIAL"):
                if state is not None:
                    self.settle_sell_order(state, res, pending, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                return res
            elif res.get("status") == "CANCELLED":
                if state is not None:
                    state["pending_order"] = None
                    self.save_state(state)
                return None

        except Exception as e:
            print(f"[{self.market}] ❌ 실거래 매도 실패 ({label}): {e}")
            raise

        return None

    def process_tick(self, state: Dict[str, Any], curr_p: float, now_str: str) -> Dict[str, Any]:
        """단일 틱 처리 엔진"""
        if state.get("trading_halted"):
            print(f"[{self.market}] ⛔ 트레이딩 비활성화/중단 상태 (trading_halted=True)")
            return state

        # 0. 미확정 주문 대사 (Pending Order Reconciliation)
        pending = state.get("pending_order")
        if pending:
            state = self.reconcile_pending_order(state, pending, now_str)
            if state.get("pending_order"):
                print(f"[{self.market}] ⏳ 미확정 주문 대사 진행 중으로 새 주문 제출 보류")
                return state

        active_grids = state.get("active_grids", [])
        base_p = float(state.get("base_price", 0.0) or 0.0)

        # =========================================================================
        # 1-0. 바스켓 일괄 탈출 체크 (exit_only / basket_exit_pct 모드인 경우)
        # =========================================================================
        exit_occurred = False
        if self.basket_exit_pct is not None and len(active_grids) > 0:
            total_vol = sum(float(g["vol"]) for g in active_grids)
            total_cost = sum(float(g["cost"]) for g in active_grids)
            if total_vol > 0:
                vwap = total_cost / total_vol
                target_basket_tp = vwap * (1.0 + self.basket_exit_pct)
                if curr_p >= target_basket_tp:
                    try:
                        sell_res = self.execute_live_sell(
                            vol=total_vol,
                            label=f"전체 바스켓 일괄탈출 (+{self.basket_exit_pct * 100:.1f}%)",
                            lvl=active_grids[0]["lvl"],
                            exit_type="BASKET",
                            squad=0,
                            target_grids=[{"lvl": g["lvl"], "vol": g["vol"], "cost": g["cost"], "fee": g.get("fee", 0.0)} for g in active_grids],
                            state=state
                        )
                        exit_occurred = True
                    except Exception as se:
                        print(f"[{self.market}] 바스켓 일괄 매도 실행 중 예외 (해당 틱 즉시 종료): {se}")
                        return state

                    if state.get("pending_order"):
                        return state

        # =========================================================================
        # 1-A. 스쿼드 바스켓 일괄 익절 체크 (+1.0%) - 일반 모드에서만 동작
        # =========================================================================
        elif self.basket_exit_pct is None:
            squads_held = {}
            for g in active_grids:
                sq = g.get("squad", (g["lvl"] - 1) // self.squad_size + 1)
                squads_held.setdefault(sq, []).append(g)

            squads_to_exit = set()
            squad_exit_targets = {}
            for sq, g_list in sorted(squads_held.items()):
                if len(g_list) >= self.min_squad_tranches_for_tp:
                    sq_vol = sum(float(x["vol"]) for x in g_list)
                    sq_cost = sum(float(x["cost"]) for x in g_list)
                    if sq_vol > 0:
                        sq_vwap = sq_cost / sq_vol
                        sq_target = sq_vwap * (1.0 + self.squad_tp_pct)
                        if sq_vwap > 0 and sq_target > 0 and curr_p >= sq_target:
                            squads_to_exit.add(sq)
                            squad_exit_targets[sq] = (sq_vwap, sq_target, sq_vol, sq_cost)

            if squads_to_exit:
                for sq in sorted(squads_to_exit):
                    g_list = squads_held[sq]
                    sq_vwap, sq_target, sq_vol, sq_cost = squad_exit_targets[sq]
                    try:
                        sell_res = self.execute_live_sell(
                            vol=sq_vol,
                            label=f"스쿼드 {sq} 바스켓",
                            lvl=g_list[0]["lvl"],
                            exit_type="SQUAD",
                            squad=sq,
                            target_grids=[{"lvl": g["lvl"], "vol": g["vol"], "cost": g["cost"], "fee": g.get("fee", 0.0)} for g in g_list],
                            state=state
                        )
                        exit_occurred = True
                    except Exception as se:
                        print(f"[{self.market}] 스쿼드 {sq} 매도 실행 중 예외 (해당 틱 즉시 종료): {se}")
                        return state

                    # 스쿼드 매도 주문이 나갔거나 타임아웃된 경우 동일 틱 내 다른 주문 시도 차단
                    if state.get("pending_order"):
                        return state

            # =========================================================================
            # 1-B. 잔여 그리드 개별 짤짤이 익절 (+1.0%)
            # =========================================================================
            for g in list(state.get("active_grids", [])):
                tp_p = float(g.get("tp_p", 0.0) or 0.0)
                buy_p = float(g.get("buy_p", 0.0) or 0.0)

                # P0: 단가 또는 목표가가 0 이하인 경우 절대 매도 트리거 발동 금지
                if tp_p > 0 and buy_p > 0 and curr_p >= tp_p:
                    try:
                        sell_res = self.execute_live_sell(
                            vol=g["vol"],
                            label=f"레벨 {g['lvl']} 개별익절",
                            lvl=g["lvl"],
                            exit_type="INDIVIDUAL",
                            squad=g.get("squad", 1),
                            target_grids=[{"lvl": g["lvl"], "vol": g["vol"], "cost": g["cost"], "fee": g.get("fee", 0.0)}],
                            state=state
                        )
                        exit_occurred = True
                    except Exception as se:
                        print(f"[{self.market}] 개별 매도 실행 중 예외 (해당 틱 즉시 종료): {se}")
                        return state

                    # 매도 주문이 미결 상태이면 동일 틱 내 다른 주문 시도 차단
                    if state.get("pending_order"):
                        return state

        # 익절 매도가 집행된 틱에서는 동일 틱 내 신규 사이클 매수 진입을 유보하고 안전하게 상태 반환
        if exit_occurred:
            return state

        # exit_only 모드일 경우 신규 매수 (1단계 진입 및 물타기) 전면 차단
        if self.exit_only:
            return state

        eff_budget, eff_max_levels = self.get_effective_budget_and_levels()
        state["allocated_budget"] = eff_budget

        # =========================================================================
        # 2. 보유 포지션이 0개일 때: 새 사이클 시작 (1단계 진입)
        # =========================================================================
        if len(state["active_grids"]) == 0:
            if self.unit_krw > eff_budget:
                print(f"[{self.market}] ⚠️ 주문 단위({self.unit_krw:,.0f}원)가 배정 예산({eff_budget:,.0f}원)을 초과하여 진입을 차단합니다.")
                return state

            state["base_price"] = curr_p
            base_p = curr_p

            try:
                grid_item = self.execute_live_buy(lvl=1, squad=1, target_p=curr_p, now_str=now_str, state=state)
            except Exception as e:
                print(f"[{self.market}] 1단계 매수 보류 (해당 틱 즉시 종료): {e}")
                return state

        # =========================================================================
        # 3. 하락 시 다음 그리드 매수 진입 (0.50% 간격)
        # =========================================================================
        elif len(state["active_grids"]) > 0:
            existing_lvls = {g["lvl"] for g in state["active_grids"]}
            max_filled_lvl = max(existing_lvls) if existing_lvls else 0
            next_lvl = max_filled_lvl + 1

            if next_lvl <= eff_max_levels and next_lvl not in existing_lvls:
                target_dip_p = base_p * (1.0 - (next_lvl - 1) * self.step_pct)
                if base_p > 0 and target_dip_p > 0 and curr_p <= target_dip_p:
                    curr_cost_sum = sum(float(g["cost"]) for g in state["active_grids"])
                    if curr_cost_sum + self.unit_krw <= eff_budget:
                        sq_id = (next_lvl - 1) // self.squad_size + 1
                        try:
                            grid_item = self.execute_live_buy(lvl=next_lvl, squad=sq_id, target_p=target_dip_p, now_str=now_str, state=state)
                        except Exception as e:
                            print(f"[{self.market}] {next_lvl}단계 매수 보류 (해당 틱 즉시 종료): {e}")
                            return state

        return state

        return state

    def run(self):
        if not self.acquire_lock():
            print(f"[CRITICAL] [{self.market}] 이미 다른 실거래 G60 프로세스가 가동 중입니다. 종료합니다.")
            sys.exit(1)

        atexit.register(self.release_lock)
        self.init_db()
        state = self.load_state()

        if state.get("trading_halted"):
            print(f"[CRITICAL] [{self.market}] 상태 파일에 비정상 오류 플래그(trading_halted=True)가 설정되어 거래가 중단되었습니다. 수동 점검이 필요합니다.")
            sys.exit(1)

        eff_budget, eff_max_levels = self.get_effective_budget_and_levels()
        if self.exit_only:
            state["exit_only"] = True
            if self.basket_exit_pct is not None:
                state["basket_exit_pct"] = self.basket_exit_pct
            state["mode"] = f"EXIT_ONLY_BASKET_{int((self.basket_exit_pct or 0.01)*100)}PCT"
            self.save_state(state)

        # 신규 목표 익절폭(profit_pct) 설정에 맞춰 기존 active_grids의 개별 tp_p 동기화 (파라미터 변경 시 신속 반영)
        updated_tp = False
        for g in state.get("active_grids", []):
            buy_p = float(g.get("buy_p", 0.0) or 0.0)
            if buy_p > 0:
                expected_tp = buy_p * (1.0 + self.profit_pct)
                if abs(float(g.get("tp_p", 0.0) or 0.0) - expected_tp) > 1e-4:
                    g["tp_p"] = expected_tp
                    updated_tp = True
        if updated_tp:
            self.save_state(state)

        # 1. 기동 시 미확정 주문 대사 우선 수행 (P1-R3: 장부 불일치 판정 전 pending 활동 복구)
        pending = state.get("pending_order")
        if pending:
            print(f"[{self.market}] 🔄 기동 시 미확정 주문 대사 수행 중: {pending}")
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            state = self.reconcile_pending_order(state, pending, now_str)
            if state.get("pending_order"):
                print(f"[CRITICAL] [{self.market}] 기동 시 미확정 주문이 해결되지 않았습니다 ({state['pending_order']}). 안전을 위해 기동을 중단합니다.")
                sys.exit(1)

        # 2. 거래소 실자산 대사 (복구된 상태 기준)
        krw_bal = self.client.get_balance("KRW")
        coin_bal = self.client.get_balance(self.ticker)
        state_vol = sum(float(g["vol"]) for g in state.get("active_grids", []))
        vol_diff = abs(coin_bal - state_vol)
        if vol_diff > 1e-4 and (coin_bal > 1e-4 or state_vol > 1e-4):
            print(f"[CRITICAL] [{self.market}] 거래소 코인 잔고({coin_bal:.8f} {self.ticker})와 장부 보유량({state_vol:.8f} {self.ticker})이 불일치합니다! 차이: {vol_diff:.8f}. 안전을 위해 가동을 중단합니다.")
            sys.exit(1)

        if self.exit_only and self.basket_exit_pct is not None:
            tot_vol = sum(float(g["vol"]) for g in state.get("active_grids", []))
            tot_cost = sum(float(g["cost"]) for g in state.get("active_grids", []))
            vwap = tot_cost / tot_vol if tot_vol > 0 else 0.0
            target_tp = vwap * (1.0 + self.basket_exit_pct)
            print("=" * 75)
            print(f"🛑 [실거래 포켓 G60 EXIT_ONLY 바스켓 모드] {self.market}")
            print(f" • 포켓 가용 잔고: {krw_bal:,.0f} KRW")
            print(f" • 포켓 코인 잔고: {coin_bal:.8f} {self.ticker} (장부 보유: {state_vol:.8f})")
            print(f" • 보유 그리드:   {len(state.get('active_grids', []))}단 (총 매수원금: {tot_cost:,.0f} KRW)")
            print(f" • 매수 평단가:   {vwap:,.2f} KRW")
            print(f" • 바스켓 목표가: {target_tp:,.0f} KRW (+{self.basket_exit_pct * 100:.2f}% 일괄 매도)")
            print(f" • 신규 매수:     🚫 전면 차단 (1단계 진입 및 물타기 중단)")
            print(f" • 청산 후 조치:  💰 매도 대금 SOL / XRP 예산으로 50:50 배분")
            print("=" * 75)

            self.send_telegram(
                f"🛑 <b>[실거래 포켓 G60 EXIT_ONLY 바스켓 모드 가동]</b> {self.market}\n"
                f"• 보유 그리드: <b>{len(state.get('active_grids', []))}단</b> ({tot_vol:.6f} {self.ticker})\n"
                f"• 매수 평단가: <b>{vwap:,.0f}원</b> (총 투입: {tot_cost:,.0f}원)\n"
                f"• 바스켓 목표가: <b>{target_tp:,.0f}원</b> (+{self.basket_exit_pct * 100:.1f}%)\n"
                f"• 🚫 신규 매수/추가 물타기 전면 중단\n"
                f"• 🎯 목표가 터치 시 21단 전량 시장가 매도 후 SOL/XRP 예산으로 전환"
            )
        else:
            print("=" * 75)
            print(f"🚀 [실거래 포켓 G60 가동] {self.market} 순수 5단 독립 스쿼드 그리드")
            print(f" • 포켓 가용 잔고: {krw_bal:,.0f} KRW")
            print(f" • 포켓 코인 잔고: {coin_bal:.8f} {self.ticker} (장부 보유: {state_vol:.8f})")
            print(f" • 배정 운용 예산: {eff_budget:,.0f} KRW (최대 {eff_max_levels}단계)")
            print(f" • 1회 주문 단위:  {self.unit_krw:,.0f} KRW (시장가 주문)")
            print(f" • 그리드 매수간격: {self.step_pct * 100:.2f}%")
            print(f" • 개별 짤짤이 익절: +{self.profit_pct * 100:.2f}%")
            print(f" • 스쿼드 바스켓:  +{self.squad_tp_pct * 100:.2f}% (5단 단위 묶음 익절)")
            print("=" * 75)

            self.send_telegram(
                f"🚀 <b>[실거래 포켓 G60 가동 시작]</b> {self.market}\n"
                f"• 포켓 잔고: {krw_bal:,.0f}원 (배정 예산: {eff_budget:,.0f}원 / {eff_max_levels}단계)\n"
                f"• 코인 보유: {coin_bal:.8f} {self.ticker}\n"
                f"• 주문 단위: 10,000원 | 간격: {self.step_pct*100:.2f}% | 익절: +{self.profit_pct*100:.2f}%\n"
                f"• 📦 5단 스쿼드 바스켓 일괄 익절 탑재\n"
                f"• 메인 계좌와 100% 완전 격리 실거래 시작!"
            )

        last_heartbeat = 0
        try:
            while True:
                try:
                    curr_p = self.get_current_price()
                    if curr_p is not None:
                        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        state = self.process_tick(state, curr_p, now_str)
                        self.save_state(state)

                        now_ts = time.time()
                        if now_ts - last_heartbeat >= 30:
                            grids_count = len(state.get("active_grids", []))
                            lvls = [g["lvl"] for g in state.get("active_grids", [])]
                            eff_budget, eff_max_lvls = self.get_effective_budget_and_levels()
                            if self.exit_only and self.basket_exit_pct is not None:
                                if grids_count > 0:
                                    tot_vol = sum(float(g["vol"]) for g in state.get("active_grids", []))
                                    tot_cost = sum(float(g["cost"]) for g in state.get("active_grids", []))
                                    vwap = tot_cost / tot_vol if tot_vol > 0 else 0.0
                                    target_tp = vwap * (1.0 + self.basket_exit_pct)
                                    gap_pct = (curr_p / target_tp - 1.0) * 100.0
                                    print(f"[{datetime.now().strftime('%H:%M:%S')}] [{self.market}] 💓 하트비트 [EXIT_ONLY] | 현재가: {curr_p:,.0f}원 | 평단: {vwap:,.0f}원 | 목표가(+{self.basket_exit_pct*100:.1f}%): {target_tp:,.0f}원 (괴리: {gap_pct:+.2f}%) | 보유: {tot_vol:.6f} {self.ticker} ({grids_count}단, {tot_cost:,.0f}원) | 신규 매수 차단 중")
                                else:
                                    print(f"[{datetime.now().strftime('%H:%M:%S')}] [{self.market}] 💓 하트비트 [EXIT_ONLY] | 전량 청산 완료 (포지션 0개, 가동 종료/대기 중)")
                            else:
                                print(f"[{datetime.now().strftime('%H:%M:%S')}] [{self.market}] 💓 하트비트 | 현재가: {curr_p:,.0f}원 | 보유그리드: {grids_count}/{eff_max_lvls}단 (차수: {lvls}) | 배정예산: {eff_budget:,.0f}원 | 누적익절: {state.get('win_count', 0)}회 (+{state.get('realized_pnl', 0):,.0f}원)")
                            last_heartbeat = now_ts

                except Exception as inner_e:
                    print(f"[{self.market}] ⚠️ 틱 처리 오류: {inner_e}")

                time.sleep(self.poll_interval_sec)

        except KeyboardInterrupt:
            print(f"[{self.market}] 사용자에 의해 실거래 봇 정지.")
        finally:
            self.release_lock()
