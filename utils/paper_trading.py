import os
import json
import time
import math
import pyupbit
from datetime import datetime
from config import (
    INVESTMENTS,
    MIN_ORDER_KRW,
    STOP_LOSS_PERCENT,
    FEE_RATE,
    get_profit_margin,
    USE_TRAILING_STOP,
    TRAILING_STOP_TRIGGER,
    TRAILING_STOP_DROP
)
from strategy.matingale2x_logic import calculate_new_buy_prices, adjust_price_to_tick
from strategy.hybrid_regime import get_hybrid_regime_and_signals
from utils.db_logger import log_equity_snapshot, log_paper_trade


class PaperAccount:
    """가상매매(모의투자) 계좌 상태를 로컬 JSON 파일 및 SQLite DB로 영속 관리하는 클래스"""

    def __init__(self, market: str):
        self.market = market
        self.ticker = market.split("-")[1]
        self.state_file = f"paper_state_{market.replace('-', '_')}.json"
        
        info = INVESTMENTS.get(market, {"total": 500_000, "unit": 5_000})
        self.initial_capital = float(info["total"])
        self.unit_krw = float(info["unit"])
        self.profit_margin = get_profit_margin(market)
        
        self.state = self._load_or_init_state()
        self.sync_history_to_db()

    def _load_or_init_state(self):
        loaded = None
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
            except Exception as e:
                print(f"[WARN] 가상매매 상태 파일 로드 실패({e}), 초기화합니다.")

        if loaded is not None:
            # initial_price가 누락된 경우 현재가 등으로 보정
            if "initial_price" not in loaded or not loaded["initial_price"]:
                cur = pyupbit.get_current_price(self.market) or loaded.get("avg_buy_price", 0.0)
                loaded["initial_price"] = cur
            # 하이브리드 전략 필드 호환 보정
            if "strategy" not in loaded:
                loaded["strategy"] = "HYBRID"
            if "current_regime" not in loaded:
                loaded["current_regime"] = "UNKNOWN"
            if "active_sub_strategy" not in loaded:
                loaded["active_sub_strategy"] = "MARTINGALE" if loaded.get("coin_balance", 0) > 0 else "WAITING"
            if "regime_indicators" not in loaded:
                loaded["regime_indicators"] = {}
            self._save_state(loaded)
            return loaded

        cur_p = pyupbit.get_current_price(self.market) or 0.0
        initial_state = {
            "market": self.market,
            "strategy": "HYBRID",
            "current_regime": "UNKNOWN",
            "active_sub_strategy": "WAITING",
            "regime_indicators": {},
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "initial_capital": self.initial_capital,
            "initial_price": cur_p,
            "krw_balance": self.initial_capital,
            "coin_balance": 0.0,
            "avg_buy_price": 0.0,
            "total_cost": 0.0,
            "open_orders": [],
            "completed_cycles": 0,
            "wins": 0,
            "losses": 0,
            "realized_pnl": 0.0,
            "trend_highest_price": 0.0,
            "trailing_stop_active": False,
            "trailing_stop_price": 0.0,
            "trade_history": []
        }
        self._save_state(initial_state)
        return initial_state

    def sync_history_to_db(self):
        """JSON 상태 파일의 trade_history 내역을 SQLite DB(paper_trades)와 동기화하고 초기 스냅샷 보장"""
        try:
            import sqlite3
            from utils.db_logger import DB_PATH, init_db
            init_db()
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute("SELECT COUNT(*) FROM paper_trades WHERE market = ?", (self.market,))
            count = c.fetchone()[0]
            conn.close()

            if count == 0 and self.state.get("trade_history"):
                for t in self.state["trade_history"]:
                    t_type = t.get("type", "UNKNOWN")
                    side = "bid" if "BUY" in t_type else "ask"
                    cost_rev = t.get("cost", t.get("revenue", 0.0))
                    pnl = t.get("pnl", 0.0)
                    log_paper_trade(
                        market=self.market,
                        action=t_type,
                        side=side,
                        price=t.get("price", 0.0),
                        volume=t.get("volume", 0.0),
                        cost_or_revenue=cost_rev,
                        pnl=pnl,
                        cycle=self.state.get("completed_cycles", 0),
                        timestamp=t.get("time")
                    )

            # 스냅샷이 하나도 없으면 초기 스냅샷 1건 등록
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute("SELECT COUNT(*) FROM equity_snapshots WHERE market = ?", (self.market,))
            snap_count = c.fetchone()[0]
            conn.close()

            if snap_count == 0:
                cur_p = pyupbit.get_current_price(self.market) or self.state.get("avg_buy_price", 0.0)
                tot_eq = self.state["krw_balance"] + (self.state["coin_balance"] * cur_p)
                unreal_pnl = (self.state["coin_balance"] * cur_p) - self.state.get("total_cost", 0.0)
                log_equity_snapshot(
                    market=self.market,
                    krw_balance=self.state["krw_balance"],
                    coin_balance=self.state["coin_balance"],
                    coin_price=cur_p,
                    total_equity=tot_eq,
                    benchmark_price=cur_p,
                    unrealized_pnl=unreal_pnl
                )
        except Exception as e:
            print(f"[WARN] DB 동기화 실패: {e}")

    def _save_state(self, state=None):
        if state is not None:
            self.state = state
        with open(self.state_file, "w", encoding="utf-8") as f:
            json.dump(self.state, f, indent=2, ensure_ascii=False)

    @property
    def krw_balance(self):
        return self.state["krw_balance"]

    @property
    def coin_balance(self):
        return self.state["coin_balance"]

    @property
    def avg_buy_price(self):
        return self.state["avg_buy_price"]

    @property
    def open_orders(self):
        return self.state["open_orders"]

    def buy_market(self, krw_amount: float, current_price: float):
        """가상 시장가 매수"""
        cost = krw_amount * (1 + FEE_RATE)
        if self.state["krw_balance"] < cost:
            return None

        volume = krw_amount / current_price
        self.state["krw_balance"] -= cost
        
        new_total_cost = self.state["total_cost"] + cost
        new_total_vol = self.state["coin_balance"] + volume
        self.state["coin_balance"] = new_total_vol
        self.state["total_cost"] = new_total_cost
        self.state["avg_buy_price"] = new_total_cost / new_total_vol
        
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.state["trade_history"].append({
            "time": now_str,
            "type": "MARKET_BUY",
            "price": current_price,
            "volume": volume,
            "cost": cost
        })
        self._save_state()
        log_paper_trade(
            market=self.market,
            action="MARKET_BUY",
            side="bid",
            price=current_price,
            volume=volume,
            cost_or_revenue=cost,
            pnl=0.0,
            cycle=self.state["completed_cycles"],
            timestamp=now_str
        )
        return {"price": current_price, "volume": volume, "cost": cost}

    def sell_market(self, volume: float, current_price: float, reason: str = "STOP_LOSS"):
        """가상 시장가 매도 (손절 또는 긴급 매도)"""
        if self.state["coin_balance"] < volume or volume <= 0:
            return None

        revenue = (volume * current_price) * (1 - FEE_RATE)
        pnl = revenue - self.state["total_cost"]
        
        self.state["krw_balance"] += revenue
        self.state["coin_balance"] -= volume
        self.state["total_cost"] = 0.0
        self.state["avg_buy_price"] = 0.0
        self.state["completed_cycles"] += 1
        self.state["losses"] += 1
        self.state["realized_pnl"] += pnl
        
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        action_type = f"MARKET_SELL_{reason}"
        self.state["trade_history"].append({
            "time": now_str,
            "type": action_type,
            "price": current_price,
            "volume": volume,
            "revenue": revenue,
            "pnl": pnl
        })
        self._save_state()
        log_paper_trade(
            market=self.market,
            action=action_type,
            side="ask",
            price=current_price,
            volume=volume,
            cost_or_revenue=revenue,
            pnl=pnl,
            cycle=self.state["completed_cycles"],
            timestamp=now_str
        )
        return {"price": current_price, "revenue": revenue, "pnl": pnl}

    def add_limit_order(self, side: str, price: float, volume: float, units: int = 1):
        """가상 지정가 주문 등록"""
        uuid = f"paper-{side}-{int(time.time()*1000)}"
        order = {
            "uuid": uuid,
            "side": side,  # "bid" (매수) or "ask" (매도)
            "price": float(price),
            "volume": float(volume),
            "units": units,
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
        self.state["open_orders"].append(order)
        self._save_state()
        return order

    def cancel_order(self, uuid: str):
        self.state["open_orders"] = [o for o in self.state["open_orders"] if o["uuid"] != uuid]
        self._save_state()

    def cancel_all_orders(self, side: str = None):
        if side:
            self.state["open_orders"] = [o for o in self.state["open_orders"] if o["side"] != side]
        else:
            self.state["open_orders"] = []
        self._save_state()

    def process_fills(self, current_price: float):
        """현재가 기준으로 가상 지정가 주문들의 체결 여부를 판별합니다."""
        filled_sells = []
        filled_buys = []
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        for order in list(self.state["open_orders"]):
            if order["side"] == "ask" and current_price >= order["price"]:
                # 익절 매도 체결!
                revenue = (order["volume"] * order["price"]) * (1 - FEE_RATE)
                pnl = revenue - self.state["total_cost"]
                
                self.state["krw_balance"] += revenue
                self.state["coin_balance"] = 0.0
                self.state["total_cost"] = 0.0
                self.state["avg_buy_price"] = 0.0
                self.state["completed_cycles"] += 1
                self.state["wins"] += 1
                self.state["realized_pnl"] += pnl
                
                self.state["trade_history"].append({
                    "time": now_str,
                    "type": "TAKE_PROFIT_SELL",
                    "price": order["price"],
                    "volume": order["volume"],
                    "revenue": revenue,
                    "pnl": pnl
                })
                log_paper_trade(
                    market=self.market,
                    action="TAKE_PROFIT_SELL",
                    side="ask",
                    price=order["price"],
                    volume=order["volume"],
                    cost_or_revenue=revenue,
                    pnl=pnl,
                    cycle=self.state["completed_cycles"],
                    timestamp=now_str
                )
                filled_sells.append(order)
                
            elif order["side"] == "bid" and current_price <= order["price"]:
                # 물타기 매수 체결!
                cost = (order["price"] * order["volume"]) * (1 + FEE_RATE)
                if self.state["krw_balance"] >= cost:
                    self.state["krw_balance"] -= cost
                    new_total_cost = self.state["total_cost"] + cost
                    new_total_vol = self.state["coin_balance"] + order["volume"]
                    self.state["coin_balance"] = new_total_vol
                    self.state["total_cost"] = new_total_cost
                    self.state["avg_buy_price"] = new_total_cost / new_total_vol
                    
                    action_type = f"LIMIT_BUY_{order.get('units', 1)}X"
                    self.state["trade_history"].append({
                        "time": now_str,
                        "type": action_type,
                        "price": order["price"],
                        "volume": order["volume"],
                        "cost": cost
                    })
                    log_paper_trade(
                        market=self.market,
                        action=action_type,
                        side="bid",
                        price=order["price"],
                        volume=order["volume"],
                        cost_or_revenue=cost,
                        pnl=0.0,
                        cycle=self.state["completed_cycles"],
                        timestamp=now_str
                    )
                    filled_buys.append(order)

        # 체결된 주문 제거
        filled_uuids = {o["uuid"] for o in filled_sells + filled_buys}
        if filled_uuids:
            self.state["open_orders"] = [o for o in self.state["open_orders"] if o["uuid"] not in filled_uuids]
            self._save_state()

        return filled_sells, filled_buys


def run_paper_trading_loop(market: str = "KRW-SOL"):
    """가상매매(모의투자) 24시간 실시간 실행 루프"""
    acc = PaperAccount(market)
    ticker = acc.ticker
    unit_krw = acc.unit_krw
    profit_margin = acc.profit_margin

    print("=" * 65)
    print(f"[PAPER TRADING] Mode Started: {market}")
    print(f"  - 초기 가상 자본: {acc.initial_capital:,.0f}원")
    print(f"  - 현재 가상 잔고: 원화 {acc.krw_balance:,.0f}원 | {ticker} {acc.coin_balance:.4f}")
    print(f"  - 1 Unit 금액: {unit_krw:,.0f}원")
    print(f"  - 익절 마진: +{(profit_margin - 1) * 100:.2f}% (종목 맞춤 적용)")
    print(f"  - 손절선: {STOP_LOSS_PERCENT * 100:.2f}%")
    print(f"  - 상태 파일: {acc.state_file}")
    print("=" * 65)

    try:
        from utils.bot import send_message
        send_message(
            f"[가상매매 시작] {market}\n"
            f"가상 자본: {acc.krw_balance:,.0f}원 | 1 Unit: {unit_krw:,.0f}원\n"
            f"익절: +{(profit_margin - 1)*100:.2f}% | 손절: {STOP_LOSS_PERCENT*100:.2f}%"
        )
    except Exception:
        pass

    last_snapshot_time = 0
    last_heartbeat_time = 0

    while True:
        try:
            current_price = pyupbit.get_current_price(market)
            if current_price is None:
                print(f"[{time.strftime('%H:%M:%S')}] [WARN] 시세 조회 지연. 5초 후 재시도.")
                time.sleep(5)
                continue

            # 1. 체결 여부 확인
            filled_sells, filled_buys = acc.process_fills(current_price)

            if filled_sells or filled_buys:
                last_snapshot_time = 0  # 체결 발생 시 즉시 스냅샷 기록 유도

            for s in filled_sells:
                msg = f"🎯 [가상 익절 체결] {market} {s['price']:,.0f}원에 전량 매도 완료! (+{(profit_margin - 1)*100:.2f}%)"
                print(f"\n==================================================")
                print(f"[{time.strftime('%H:%M:%S')}] {msg}")
                print(f"   ↳ 실현 손익: {acc.state.get('realized_pnl', 0):+,.0f}원 | 완료 사이클: {acc.state.get('completed_cycles', 0)}회")
                print(f"   ↳ 총 자산: {cur_equity:,.0f}원 ({profit_rate:+.2f}%) | 원화 잔고: {acc.krw_balance:,.0f}원")
                print(f"==================================================\n")
                try:
                    from utils.bot import send_message
                    send_message(msg)
                except Exception:
                    pass

            for b in filled_buys:
                msg = f"💧 [가상 물타기 체결] {market} {b['price']:,.0f}원에 {b.get('units', 1)}배수 체결!"
                print(f"\n==================================================")
                print(f"[{time.strftime('%H:%M:%S')}] {msg}")
                print(f"   ↳ 새 평단가: {acc.avg_buy_price:,.0f}원 | 총 보유수량: {acc.coin_balance:.6f} {ticker}")
                print(f"   ↳ 잔여 원화: {acc.krw_balance:,.0f}원 | 총 자산: {cur_equity:,.0f}원")
                print(f"==================================================\n")
                try:
                    from utils.bot import send_message
                    send_message(msg)
                except Exception:
                    pass

            # 2. 손절(Stop-Loss) 체크
            if acc.coin_balance > 0 and acc.avg_buy_price > 0:
                pnl_rate = (current_price - acc.avg_buy_price) / acc.avg_buy_price
                if pnl_rate <= STOP_LOSS_PERCENT:
                    msg = (
                        f"[가상 STOP-LOSS 발동] {market}\n"
                        f"현재가: {current_price:,.0f}원 | 평단가: {acc.avg_buy_price:,.0f}원\n"
                        f"손실률: {pnl_rate*100:.2f}% (기준: {STOP_LOSS_PERCENT*100:.2f}%)\n"
                        f"보유 수량 전량 시장가 가상 매도 진행."
                    )
                    print(f"\n{msg}")
                    try:
                        from utils.bot import send_message
                        send_message(msg)
                    except Exception:
                        pass
                    
                    acc.cancel_all_orders()
                    acc.sell_market(acc.coin_balance, current_price, reason="STOP_LOSS")
                    last_snapshot_time = 0
                    time.sleep(10)
                    continue

            # 3. 미체결 주문 상태 점검
            sell_orders = [o for o in acc.open_orders if o["side"] == "ask"]
            buy_orders = [o for o in acc.open_orders if o["side"] == "bid"]
            num_sell, num_buy = len(sell_orders), len(buy_orders)

            cur_equity = acc.krw_balance + (acc.coin_balance * current_price)
            profit_rate = ((cur_equity - acc.initial_capital) / acc.initial_capital) * 100

            # 3-1. 자산 스냅샷 기록 (60초 주기 또는 체결 직후)
            now_ts = time.time()
            if now_ts - last_snapshot_time >= 60:
                unrealized_pnl = (acc.coin_balance * current_price) - acc.state.get("total_cost", 0.0)
                try:
                    from utils.real_balance import get_real_coin_status
                    r_status = get_real_coin_status(market)
                    r_eval = r_status.get("current_eval", None)
                except Exception:
                    r_eval = None

                log_equity_snapshot(
                    market=market,
                    krw_balance=acc.krw_balance,
                    coin_balance=acc.coin_balance,
                    coin_price=current_price,
                    total_equity=cur_equity,
                    benchmark_price=current_price,
                    unrealized_pnl=unrealized_pnl,
                    real_dca_eval=r_eval
                )
                last_snapshot_time = now_ts

            # 3-2. 하이브리드 국면 및 지표 갱신 (60초 주기)
            if "last_regime_check_time" not in locals() or (now_ts - last_regime_check_time) >= 60:
                regime_info = get_hybrid_regime_and_signals(market)
                acc.state["current_regime"] = regime_info["regime"]
                acc.state["regime_indicators"] = {
                    "ma5": regime_info["ma5"],
                    "ma20": regime_info["ma20"],
                    "ma200": regime_info["ma200"],
                    "distance_ma200_pct": regime_info["distance_ma200_pct"],
                    "bb_lower": regime_info.get("bb_lower", 0.0),
                    "bb_mid": regime_info.get("bb_mid", 0.0),
                    "ema50": regime_info.get("ema50", 0.0),
                    "cluc_threshold": regime_info.get("cluc_threshold", 0.0),
                    "is_cluc_dip": regime_info.get("is_cluc_dip", False),
                    "signal": regime_info["signal"],
                    "reason": regime_info["reason"]
                }
                acc._save_state()
                last_regime_check_time = now_ts

            # 3-3. 터미널 로깅: 체결 및 상태 변경 시에만 상세 출력 (30분마다 1회 생존 하트비트)
            now_time_sec = time.time()
            if now_time_sec - last_heartbeat_time >= 1800:
                reg_name = "🟢상승장(추세)" if acc.state.get("current_regime") == "BULL" else "🔴하락장(방어)"
                sub_mode = acc.state.get("active_sub_strategy", "WAITING")
                print(f"[{time.strftime('%H:%M:%S')}] [HEARTBEAT] {ticker}: {current_price:,.0f}원 | {reg_name} [{sub_mode}] (자산: {cur_equity:,.0f}원 | 매도 {num_sell}건, 매수 {num_buy}건)")
                last_heartbeat_time = now_time_sec

            # =========================================================
            # 4. 하이브리드 상태 머신 (Hybrid State Machine)
            # =========================================================
            current_regime = acc.state.get("current_regime", "BEAR")
            sub_strategy = acc.state.get("active_sub_strategy", "WAITING")
            hybrid_signal = acc.state.get("regime_indicators", {}).get("signal", "HOLD")

            # ---------------------------------------------------------
            # 국면 A: 상승 국면 (BULL: 200 MA 상회) -> 5/20 MA 추세추종
            # ---------------------------------------------------------
            if current_regime == "BULL":
                # 하락장에서 넘어온 미체결 물타기 매수 취소 (상승장에서는 소액 물타기 방어 불필요)
                if sub_strategy == "MARTINGALE" and num_buy > 0:
                    acc.cancel_all_orders("bid")
                    print(f"[{ticker}] [HYBRID] 🟢 상승 국면 전환: 하락장 마틴게일 매수 대기 주문 취소.")

                # -----------------------------------------------------
                # 신호 0: 다이나믹 트레일링 스탑 체크 (Freqtrade Supertrend/Bandtastic 방식)
                # -----------------------------------------------------
                if sub_strategy == "TREND" and acc.coin_balance > 0 and USE_TRAILING_STOP:
                    cur_highest = max(acc.state.get("trend_highest_price", 0.0), current_price)
                    if cur_highest > acc.state.get("trend_highest_price", 0.0):
                        acc.state["trend_highest_price"] = cur_highest
                        acc._save_state()

                    avg_p = acc.avg_buy_price
                    if avg_p > 0:
                        max_gain = (cur_highest - avg_p) / avg_p
                        # 진입 후 +10% 이상 도달 시 트레일링 스탑 활성화
                        if max_gain >= TRAILING_STOP_TRIGGER:
                            ts_price = adjust_price_to_tick(cur_highest * (1.0 - TRAILING_STOP_DROP), method="floor")
                            if not acc.state.get("trailing_stop_active", False) or acc.state.get("trailing_stop_price", 0.0) != ts_price:
                                acc.state["trailing_stop_active"] = True
                                acc.state["trailing_stop_price"] = ts_price
                                acc._save_state()

                            # 최고가 대비 -3% 이하로 하락 시 조기 익절 청산!
                            if current_price <= ts_price:
                                acc.cancel_all_orders()
                                res = acc.sell_market(acc.coin_balance, current_price, reason="TRAILING_STOP_PROFIT")
                                acc.state["active_sub_strategy"] = "WAITING"
                                acc.state["trend_highest_price"] = 0.0
                                acc.state["trailing_stop_active"] = False
                                acc.state["trailing_stop_price"] = 0.0
                                acc._save_state()

                                realized_gain_pct = ((current_price / avg_p) - 1.0) * 100.0
                                msg = (
                                    f"🎯 [하이브리드 다이나믹 트레일링 스탑 익절] {market}\n"
                                    f"진입가: {avg_p:,.0f}원 | 최고가: {cur_highest:,.0f}원 (+{max_gain * 100:.2f}%)\n"
                                    f"고점 대비 -{TRAILING_STOP_DROP * 100:.1f}% 하락 도달로 조기 익절 완료!\n"
                                    f"매도가: {current_price:,.0f}원 (수익률: {realized_gain_pct:+.2f}%) | 손익: {res.get('pnl', 0):+,.0f}원"
                                )
                                print(f"\n[{time.strftime('%H:%M:%S')}] {msg}")
                                try:
                                    from utils.bot import send_message
                                    send_message(msg)
                                except Exception:
                                    pass
                                time.sleep(5)
                                continue

                # 신호 1: 5/20 MA 골든크로스 매수
                if hybrid_signal == "BUY":
                    if acc.coin_balance <= 0 or (acc.coin_balance * current_price) < MIN_ORDER_KRW:
                        invest_krw = min(acc.krw_balance * 0.98, acc.initial_capital)
                        if invest_krw >= MIN_ORDER_KRW:
                            acc.cancel_all_orders()
                            res = acc.buy_market(invest_krw, current_price)
                            acc.state["active_sub_strategy"] = "TREND"
                            acc.state["trend_highest_price"] = current_price
                            acc.state["trailing_stop_active"] = False
                            acc.state["trailing_stop_price"] = 0.0
                            acc._save_state()
                            msg = (
                                f"🚀 [하이브리드 상승장 진입] {market}\n"
                                f"200 MA 상회 + 5/20 MA 골든크로스 발생!\n"
                                f"매수가: {current_price:,.0f}원 | 매수금: {invest_krw:,.0f}원"
                            )
                            print(f"\n[{time.strftime('%H:%M:%S')}] {msg}")
                            try:
                                from utils.bot import send_message
                                send_message(msg)
                            except Exception:
                                pass
                            time.sleep(5)
                            continue

                # 신호 2: 5/20 MA 데드크로스 청산
                elif hybrid_signal == "SELL":
                    if acc.coin_balance > 0 and sub_strategy == "TREND":
                        acc.cancel_all_orders()
                        res = acc.sell_market(acc.coin_balance, current_price, reason="TREND_DEAD_CROSS")
                        acc.state["active_sub_strategy"] = "WAITING"
                        acc.state["trend_highest_price"] = 0.0
                        acc.state["trailing_stop_active"] = False
                        acc.state["trailing_stop_price"] = 0.0
                        acc._save_state()
                        msg = (
                            f"🛑 [하이브리드 추세 청산] {market}\n"
                            f"5/20 MA 데드크로스 발생으로 전량 청산 완료.\n"
                            f"매도가: {current_price:,.0f}원 | 손익: {res.get('pnl', 0):+,.0f}원"
                        )
                        print(f"\n[{time.strftime('%H:%M:%S')}] {msg}")
                        try:
                            from utils.bot import send_message
                            send_message(msg)
                        except Exception:
                            pass
                        time.sleep(5)
                        continue

                # 이전 마틴게일 잔여 매도 주문이 아직 남아있다면 체결 대기
                if num_sell == 1:
                    time.sleep(5)
                    continue

            # ---------------------------------------------------------
            # 국면 B: 하락 국면 (BEAR: 200 MA 하회) -> 마틴게일 방어 모드
            # ---------------------------------------------------------
            else:
                # 상승장 추세 포지션을 들고 있다가 200선을 깨고 하락장으로 전락한 경우: 즉시 손절/청산
                if sub_strategy == "TREND" and acc.coin_balance > 0:
                    acc.cancel_all_orders()
                    res = acc.sell_market(acc.coin_balance, current_price, reason="BEAR_REGIME_CUT")
                    acc.state["active_sub_strategy"] = "WAITING"
                    acc.state["trend_highest_price"] = 0.0
                    acc.state["trailing_stop_active"] = False
                    acc.state["trailing_stop_price"] = 0.0
                    acc._save_state()
                    msg = (
                        f"🛡️ [하이브리드 하락장 방어 전환] {market}\n"
                        f"200 MA 하향 이탈로 추세 포지션 즉시 청산 후 관망 모드로 전환합니다.\n"
                        f"청산가: {current_price:,.0f}원"
                    )
                    print(f"\n[{time.strftime('%H:%M:%S')}] {msg}")
                    try:
                        from utils.bot import send_message
                        send_message(msg)
                    except Exception:
                        pass
                    time.sleep(5)
                    continue

                # 마틴게일 Case 1 & 2: 정상 대기 (매도 1건, 매수 3건)
                if num_sell == 1:
                    acc.state["active_sub_strategy"] = "MARTINGALE"
                    if num_buy == 3:
                        time.sleep(5)
                        continue

                # 마틴게일 Case 3: 매도 완료 또는 신규 시작 (매도 0건)
                elif num_sell == 0:
                    # ClucMay 과매도 낙주 신호 발생 시에만 1차 진입
                    if hybrid_signal == "MARTINGALE_BUY_DIP":
                        print(f"\n[{time.strftime('%H:%M:%S')}] [ACTION] 📉 하락장 ClucMay 과매도 낙주 포착! 1차 마틴게일 시작.")
                        acc.cancel_all_orders("bid")

                        # 코인이 없으면 1 Unit 시장가 매수
                        if acc.coin_balance <= 0 or (acc.coin_balance * current_price) < MIN_ORDER_KRW:
                            if acc.krw_balance < unit_krw:
                                print(f"[WARN] 가상 잔고 부족 ({acc.krw_balance:,.0f}원 < {unit_krw:,}원). 대기 중...")
                                time.sleep(10)
                                continue
                            res = acc.buy_market(unit_krw, current_price)
                            print(f"  - 1 Unit 가상 시장가 매수 완료: {unit_krw:,.0f}원")

                        avg_price = acc.avg_buy_price
                        quantity = acc.coin_balance
                        print(f"  - 현재 가상 포지션: 평단가 {avg_price:,.0f}원, 보유수량 {quantity:.4f}")

                        # 익절 매도 주문 등록 (+0.5%)
                        sell_p = adjust_price_to_tick(avg_price * profit_margin, method="ceil")
                        acc.add_limit_order("ask", sell_p, quantity)
                        print(f"  - 가상 익절 매도 등록: {sell_p:,.0f}원 (+{(profit_margin - 1)*100:.2f}%)")

                        # 물타기 3단계 등록 (2x, 3x, 6x)
                        plans = calculate_new_buy_prices(avg_buy_price=avg_price, existing_orders=None)
                        for p_dict in plans:
                            p = p_dict["price"]
                            u = p_dict["units"]
                            order_krw = unit_krw * u
                            v = round(order_krw / p, 8)
                            acc.add_limit_order("bid", p, v, units=u)
                            print(f"    - 가상 매수 등록: {p:,.0f}원 | {u} Units ({order_krw:,.0f}원) | 수량: {v}")

                        acc.state["active_sub_strategy"] = "MARTINGALE"
                        acc._save_state()

                        msg = (
                            f"📉 [하락장 ClucMay 과매도 낙주 진입] {market}\n"
                            f"볼린저 밴드 하단-1.5% 이탈 과매도 투매 포착!\n"
                            f"매수가: {current_price:,.0f}원 | 1 Unit: {unit_krw:,}원\n"
                            f"익절 목표: {sell_p:,.0f}원 (+{(profit_margin - 1)*100:.2f}%)"
                        )
                        print(f"\n[{time.strftime('%H:%M:%S')}] {msg}")
                        try:
                            from utils.bot import send_message
                            send_message(msg)
                        except Exception:
                            pass
                        time.sleep(5)
                        continue
                    else:
                        # ClucMay 낙주 신호 미발생: 현금 100% 보존 관망 모드
                        if sub_strategy != "WAITING":
                            acc.state["active_sub_strategy"] = "WAITING"
                            acc.cancel_all_orders("bid")
                            acc._save_state()
                        time.sleep(5)
                        continue

                # 마틴게일 Case 4: 물타기 체결 (매도 1건, 매수 2건 이하)
                elif num_sell == 1 and num_buy <= 2:
                    print(f"\n[{time.strftime('%H:%M:%S')}] [ACTION] 하락장 마틴게일 물타기 체결 감지. 포지션 재조정.")
                    acc.cancel_all_orders("ask")

                    avg_price = acc.avg_buy_price
                    quantity = acc.coin_balance
                    print(f"  - 새 가상 포지션: 새 평단가 {avg_price:,.0f}원, 총 보유수량 {quantity:.4f}")

                    # 새 평단가 기준 익절 매도 등록
                    sell_p = adjust_price_to_tick(avg_price * profit_margin, method="ceil")
                    acc.add_limit_order("ask", sell_p, quantity)
                    print(f"  - 새 가상 익절 매도 등록: {sell_p:,.0f}원")

                    # 부족한 물타기 추가 주문
                    buy_info = [{"price": o["price"], "units": o.get("units", 1)} for o in buy_orders]
                    add_plans = calculate_new_buy_prices(avg_buy_price=avg_price, existing_orders=buy_info)
                    for ap in add_plans:
                        p = ap["price"]
                        u = ap["units"]
                        order_krw = unit_krw * u
                        v = round(order_krw / p, 8)
                        acc.add_limit_order("bid", p, v, units=u)
                        print(f"    - 추가 가상 매수 등록: {p:,.0f}원 | {u} Units ({order_krw:,.0f}원)")

                else:
                    time.sleep(5)
                    continue

        except Exception as e:
            print(f"[{time.strftime('%H:%M:%S')}] [ERROR] 가상매매 루프 에러: {e}")
            time.sleep(10)

        time.sleep(5)
