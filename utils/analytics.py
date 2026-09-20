import os
import json
import sqlite3
import pandas as pd
import numpy as np
import pyupbit
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional
from utils.db_logger import DB_PATH


class PerformanceAnalyzer:
    """
    트레이딩 봇의 8대 핵심 퀀트 성과 지표 계산 엔진:
    1. 수익률 (Cumulative & Daily Return)
    2. MDD (Maximum Drawdown & Underwater Plot)
    3. 승률 (Win Rate)
    4. 평균 수익/손실 및 손익비 (Win/Loss Ratio & Profit Factor)
    5. 연속 손실 및 마틴게일 단계 분석 (Consecutive Losses & Step Distribution)
    6. 거래 빈도 및 사이클 보유 시간 (Trade Frequency & Holding Time)
    7. Buy & Hold 대비 성과 (Benchmark vs Strategy & Alpha)
    8. 시장 상승/하락/횡보 국면별 성과 (Market Regime Analysis)
    """

    def __init__(self, market: str, initial_capital: float = 1000000.0):
        self.market = market
        self.ticker = market.split("-")[1] if "-" in market else market
        self.initial_capital = initial_capital
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.state_file = os.path.join(base_dir, f"paper_state_{market.replace('-', '_')}.json")
        self._load_metadata()

    def _load_metadata(self):
        """JSON 상태 파일에서 초기 자본 및 기준점 메타데이터 확인"""
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.initial_capital = float(data.get("initial_capital", self.initial_capital))
                    self.initial_price = float(data.get("initial_price", 0.0))
            except Exception:
                self.initial_price = 0.0
        else:
            self.initial_price = 0.0

    def get_equity_snapshots(self) -> pd.DataFrame:
        """DB에서 시계열 자산 스냅샷 조회 (SHOWDOWN_START_TIME 기점 이후 데이터만 필터)"""
        try:
            from config import SHOWDOWN_START_TIME
            conn = sqlite3.connect(DB_PATH)
            query = """
                SELECT timestamp, krw_balance, coin_balance, coin_price, total_equity, benchmark_price, unrealized_pnl, real_dca_eval
                FROM equity_snapshots
                WHERE market = ? AND timestamp >= ?
                ORDER BY timestamp ASC
            """
            df = pd.read_sql_query(query, conn, params=(self.market, SHOWDOWN_START_TIME))
            conn.close()

            if df.empty:
                # 데이터가 없는 경우 기준 시점 기본값 1행 생성
                cur_p = pyupbit.get_current_price(self.market) or 100000.0
                df = pd.DataFrame([{
                    "timestamp": SHOWDOWN_START_TIME,
                    "krw_balance": self.initial_capital,
                    "coin_balance": 0.0,
                    "coin_price": cur_p,
                    "total_equity": self.initial_capital,
                    "benchmark_price": cur_p,
                    "unrealized_pnl": 0.0,
                    "real_dca_eval": None
                }])

            df["timestamp"] = pd.to_datetime(df["timestamp"])
            return df
        except Exception as e:
            print(f"[ERROR] get_equity_snapshots 오류: {e}")
            return pd.DataFrame()

    def get_trades(self) -> pd.DataFrame:
        """가상매매 체결 내역 조회 (SHOWDOWN_START_TIME 기점 이후 체결만 필터)"""
        df_db = pd.DataFrame()
        try:
            from config import SHOWDOWN_START_TIME
            if os.path.exists(DB_PATH):
                conn = sqlite3.connect(DB_PATH)
                query = """
                    SELECT timestamp, action, side, price, volume, cost_or_revenue, pnl, cycle
                    FROM paper_trades
                    WHERE market = ? AND timestamp >= ?
                    ORDER BY timestamp ASC
                """
                df_db = pd.read_sql_query(query, conn, params=(self.market, SHOWDOWN_START_TIME))
                conn.close()
        except Exception as e:
            print(f"[WARN] DB get_trades 조회 실패: {e}")

        # DB에 데이터가 존재하면 사용
        if not df_db.empty:
            df_db["timestamp"] = pd.to_datetime(df_db["timestamp"])
            return df_db

        # DB가 비어있거나 파일이 없을 경우 paper_state_*.json의 trade_history를 fallback 로드
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    history = data.get("trade_history", [])
                    if history:
                        rows = []
                        cycle = 0
                        for t in history:
                            act = t.get("type", "UNKNOWN")
                            side = "ask" if "SELL" in act else "bid"
                            price = float(t.get("price", 0.0))
                            volume = float(t.get("volume", 0.0))
                            cost_or_rev = float(t.get("cost", t.get("revenue", 0.0)))
                            pnl = float(t.get("pnl", 0.0))
                            if "SELL" in act:
                                cycle += 1
                            rows.append({
                                "timestamp": pd.to_datetime(t.get("time")),
                                "action": act,
                                "side": side,
                                "price": price,
                                "volume": volume,
                                "cost_or_revenue": cost_or_rev,
                                "pnl": pnl,
                                "cycle": cycle
                            })
                        return pd.DataFrame(rows)
            except Exception as e:
                print(f"[WARN] JSON trade_history 로드 실패: {e}")

        return pd.DataFrame()

    # -------------------------------------------------------------
    # 1. 수익률 & 2. MDD
    # -------------------------------------------------------------
    def calculate_returns_and_mdd(self, df_equity: pd.DataFrame) -> Dict[str, Any]:
        """누적 수익률, 현재 평가손익, MDD, Underwater 시리즈 계산"""
        if df_equity.empty:
            return {
                "current_equity": self.initial_capital,
                "total_return_pct": 0.0,
                "total_pnl": 0.0,
                "peak_equity": self.initial_capital,
                "current_drawdown_pct": 0.0,
                "mdd_pct": 0.0,
                "df_mdd": pd.DataFrame()
            }

        df = df_equity.copy()
        current_equity = float(df["total_equity"].iloc[-1])
        total_pnl = current_equity - self.initial_capital
        total_return_pct = (total_pnl / self.initial_capital) * 100.0

        # Peak & Drawdown
        df["peak"] = df["total_equity"].cummax()
        df["drawdown_pct"] = (df["total_equity"] - df["peak"]) / df["peak"] * 100.0
        
        peak_equity = float(df["peak"].max())
        mdd_pct = float(df["drawdown_pct"].min())
        current_drawdown_pct = float(df["drawdown_pct"].iloc[-1])

        return {
            "current_equity": current_equity,
            "total_return_pct": total_return_pct,
            "total_pnl": total_pnl,
            "peak_equity": peak_equity,
            "current_drawdown_pct": current_drawdown_pct,
            "mdd_pct": mdd_pct,
            "df_mdd": df
        }

    # -------------------------------------------------------------
    # 3. 승률 & 4. 평균 손익 / 손익비
    # -------------------------------------------------------------
    def calculate_trade_performance(self, df_trades: pd.DataFrame) -> Dict[str, Any]:
        """승률, 총 이익, 총 손실, 평균 손익, 손익비(Profit Factor & Win/Loss Ratio) 계산"""
        if df_trades.empty:
            return {
                "total_closed_trades": 0,
                "wins": 0,
                "losses": 0,
                "win_rate": 0.0,
                "total_profit": 0.0,
                "total_loss": 0.0,
                "net_profit": 0.0,
                "avg_win": 0.0,
                "avg_loss": 0.0,
                "win_loss_ratio": 0.0,
                "profit_factor": 0.0
            }

        # 청산 거래(매도 체결: TAKE_PROFIT or STOP_LOSS)
        closed_trades = df_trades[df_trades["side"] == "ask"].copy()
        total_closed = len(closed_trades)

        if total_closed == 0:
            return {
                "total_closed_trades": 0,
                "wins": 0,
                "losses": 0,
                "win_rate": 0.0,
                "total_profit": 0.0,
                "total_loss": 0.0,
                "net_profit": 0.0,
                "avg_win": 0.0,
                "avg_loss": 0.0,
                "win_loss_ratio": 0.0,
                "profit_factor": 0.0
            }

        wins_df = closed_trades[closed_trades["pnl"] > 0]
        losses_df = closed_trades[closed_trades["pnl"] < 0]

        wins = len(wins_df)
        losses = len(losses_df)
        win_rate = (wins / total_closed * 100.0) if total_closed > 0 else 0.0

        total_profit = float(wins_df["pnl"].sum())
        total_loss = float(abs(losses_df["pnl"].sum()))
        net_profit = total_profit - total_loss

        avg_win = float(wins_df["pnl"].mean()) if wins > 0 else 0.0
        avg_loss = float(abs(losses_df["pnl"].mean())) if losses > 0 else 0.0

        win_loss_ratio = (avg_win / avg_loss) if avg_loss > 0 else (avg_win if avg_win > 0 else 0.0)
        profit_factor = (total_profit / total_loss) if total_loss > 0 else (total_profit if total_profit > 0 else 1.0)

        return {
            "total_closed_trades": total_closed,
            "wins": wins,
            "losses": losses,
            "win_rate": win_rate,
            "total_profit": total_profit,
            "total_loss": total_loss,
            "net_profit": net_profit,
            "avg_win": avg_win,
            "avg_loss": avg_loss,
            "win_loss_ratio": win_loss_ratio,
            "profit_factor": profit_factor
        }

    # -------------------------------------------------------------
    # 5. 연속 손실 & 마틴게일 단계 분석
    # -------------------------------------------------------------
    def calculate_consecutive_losses_and_martingale(self, df_trades: pd.DataFrame) -> Dict[str, Any]:
        """역대 최다 연속 손실, 현재 연속 손실, 마틴게일 물타기 단계별 도달 빈도"""
        default_res = {
            "max_consecutive_losses": 0,
            "current_consecutive_losses": 0,
            "max_consecutive_wins": 0,
            "martingale_steps": {"1X": 0, "2X": 0, "3X": 0, "6X": 0, "Other": 0}
        }
        if df_trades.empty:
            return default_res

        # 청산 거래 기준 연속 손익 계산
        closed = df_trades[df_trades["side"] == "ask"].sort_values("timestamp")
        
        max_losses = 0
        curr_losses = 0
        max_wins = 0
        curr_wins = 0

        for pnl in closed["pnl"]:
            if pnl < 0:
                curr_losses += 1
                curr_wins = 0
                max_losses = max(max_losses, curr_losses)
            elif pnl > 0:
                curr_wins += 1
                curr_losses = 0
                max_wins = max(max_wins, curr_wins)
            else:
                curr_losses = 0
                curr_wins = 0

        # 마틴게일 단계별 진입 빈도 분석
        buy_trades = df_trades[df_trades["side"] == "bid"]
        steps = {"1X": 0, "2X": 0, "3X": 0, "6X": 0, "Other": 0}
        for act in buy_trades["action"]:
            act_str = str(act).upper()
            if "BUY_1X" in act_str or "MARKET_BUY" in act_str:
                steps["1X"] += 1
            elif "2X" in act_str:
                steps["2X"] += 1
            elif "3X" in act_str:
                steps["3X"] += 1
            elif "6X" in act_str:
                steps["6X"] += 1
            else:
                steps["Other"] += 1

        return {
            "max_consecutive_losses": max_losses,
            "current_consecutive_losses": curr_losses,
            "max_consecutive_wins": max_wins,
            "martingale_steps": steps
        }

    # -------------------------------------------------------------
    # 6. 거래 빈도 및 평균 보유 시간
    # -------------------------------------------------------------
    def calculate_trade_frequency(self, df_trades: pd.DataFrame) -> Dict[str, Any]:
        """일평균 거래 횟수, 총 거래 횟수, 사이클 평균 보유 기간"""
        if df_trades.empty:
            return {
                "total_orders": 0,
                "daily_trade_frequency": 0.0,
                "avg_cycle_duration_hours": 0.0,
                "days_active": 1
            }

        total_orders = len(df_trades)
        min_time = df_trades["timestamp"].min()
        max_time = df_trades["timestamp"].max()
        days_span = max(1, (max_time - min_time).total_seconds() / 86400.0)
        daily_freq = round(total_orders / days_span, 2)

        # 사이클별 보유 기간 계산
        durations = []
        for cycle_id, group in df_trades.groupby("cycle"):
            if len(group) >= 2:
                start_t = group["timestamp"].min()
                end_t = group["timestamp"].max()
                dur_hrs = (end_t - start_t).total_seconds() / 3600.0
                if dur_hrs > 0:
                    durations.append(dur_hrs)

        avg_dur = round(float(np.mean(durations)), 2) if durations else 0.0

        return {
            "total_orders": total_orders,
            "daily_trade_frequency": daily_freq,
            "avg_cycle_duration_hours": avg_dur,
            "days_active": round(days_span, 1)
        }

    # -------------------------------------------------------------
    # 7. Buy & Hold 대비 성과 (Benchmark vs Strategy & Alpha)
    # -------------------------------------------------------------
    def calculate_buy_and_hold(self, df_equity: pd.DataFrame) -> Dict[str, Any]:
        """동일 자본을 코인에 Buy & Hold 했을 때와의 성과 비교 곡선 및 Alpha 산출"""
        if df_equity.empty:
            return {
                "bnh_return_pct": 0.0,
                "alpha_pct": 0.0,
                "strategy_return_pct": 0.0,
                "df_comparison": pd.DataFrame()
            }

        df = df_equity.copy()
        first_price = self.initial_price if self.initial_price > 0 else df["coin_price"].iloc[0]
        if first_price <= 0:
            first_price = df["coin_price"].iloc[0]

        # B&H 자산 가치 = 초기 자본 * (해당 시점 가격 / 시작 시점 가격)
        df["bnh_equity"] = self.initial_capital * (df["coin_price"] / first_price)
        df["bnh_return_pct"] = (df["bnh_equity"] - self.initial_capital) / self.initial_capital * 100.0
        df["strategy_return_pct"] = (df["total_equity"] - self.initial_capital) / self.initial_capital * 100.0
        df["alpha_pct"] = df["strategy_return_pct"] - df["bnh_return_pct"]

        # 실제 코인모으기(실계좌) 평가가치 성장 곡선 (가상 100만원 환산치)
        if "real_dca_eval" in df.columns and df["real_dca_eval"].notna().any():
            valid_dca = df["real_dca_eval"].dropna()
            if not valid_dca.empty and valid_dca.iloc[0] > 0:
                base_dca = valid_dca.iloc[0]
                df["real_dca_equity"] = self.initial_capital * (df["real_dca_eval"] / base_dca)
                df["real_dca_return_pct"] = (df["real_dca_eval"] - base_dca) / base_dca * 100.0

        bnh_return = float(df["bnh_return_pct"].iloc[-1])
        strat_return = float(df["strategy_return_pct"].iloc[-1])
        alpha = strat_return - bnh_return

        return {
            "bnh_return_pct": bnh_return,
            "strategy_return_pct": strat_return,
            "alpha_pct": alpha,
            "df_comparison": df
        }

    # -------------------------------------------------------------
    # 8. 시장 국면(상승/하락/횡보)별 성과 분석
    # -------------------------------------------------------------
    def analyze_market_regime(self, df_trades: pd.DataFrame) -> Dict[str, Any]:
        """
        업비트 캔들 데이터를 통해 시장 국면(상승, 하락, 횡보)을 정의하고,
        각 국면에서 발생한 매매의 성과(거래 수, 승률, 평균 손익)를 비교 분석
        """
        try:
            # 최근 1시간봉 200개 조회
            candles = pyupbit.get_ohlcv(self.market, interval="minute60", count=200)
            if candles is None or len(candles) < 30:
                candles = pyupbit.get_ohlcv(self.market, interval="day", count=60)
        except Exception:
            candles = None

        regimes_summary = {
            "Bull": {"trades": 0, "wins": 0, "losses": 0, "win_rate": 0.0, "total_pnl": 0.0, "avg_pnl": 0.0},
            "Bear": {"trades": 0, "wins": 0, "losses": 0, "win_rate": 0.0, "total_pnl": 0.0, "avg_pnl": 0.0},
            "Sideways": {"trades": 0, "wins": 0, "losses": 0, "win_rate": 0.0, "total_pnl": 0.0, "avg_pnl": 0.0}
        }

        if candles is None or candles.empty:
            return {"summary": regimes_summary, "current_regime": "Unknown", "df_candles": pd.DataFrame()}

        # 지표 계산: SMA 20, SMA 60, 기울기
        candles["sma20"] = candles["close"].rolling(window=20).mean()
        candles["sma60"] = candles["close"].rolling(window=min(60, len(candles))).mean()
        candles["slope20"] = candles["sma20"].diff(3) / candles["sma20"] * 100.0

        def classify_row(row):
            if pd.isna(row["sma20"]) or pd.isna(row["sma60"]):
                return "Sideways"
            # 상승장: SMA20 > SMA60 및 기울기 양수
            if row["sma20"] > row["sma60"] and row["slope20"] > 0.1:
                return "Bull"
            # 하락장: SMA20 < SMA60 및 기울기 음수
            elif row["sma20"] < row["sma60"] and row["slope20"] < -0.1:
                return "Bear"
            else:
                return "Sideways"

        candles["regime"] = candles.apply(classify_row, axis=1)
        current_regime = candles["regime"].iloc[-1]

        # 거래 내역을 캔들 국면에 매핑
        if not df_trades.empty and "timestamp" in df_trades.columns:
            closed = df_trades[df_trades["side"] == "ask"].copy()
            for _, trade in closed.iterrows():
                t_time = trade["timestamp"]
                # 거래 시점과 가장 가까운 이전 캔들 찾기
                sub = candles[candles.index <= t_time]
                regime = sub["regime"].iloc[-1] if not sub.empty else "Sideways"

                pnl = float(trade.get("pnl", 0.0))
                regimes_summary[regime]["trades"] += 1
                regimes_summary[regime]["total_pnl"] += pnl
                if pnl > 0:
                    regimes_summary[regime]["wins"] += 1
                elif pnl < 0:
                    regimes_summary[regime]["losses"] += 1

            for reg, stats in regimes_summary.items():
                if stats["trades"] > 0:
                    stats["win_rate"] = round((stats["wins"] / stats["trades"]) * 100.0, 1)
                    stats["avg_pnl"] = round(stats["total_pnl"] / stats["trades"], 0)

        return {
            "summary": regimes_summary,
            "current_regime": current_regime,
            "df_candles": candles
        }

    # -------------------------------------------------------------
    # 종합 요약 보고서
    # -------------------------------------------------------------
    def get_full_analysis(self) -> Dict[str, Any]:
        """8대 지표를 통합 산출하여 딕셔너리로 반환"""
        df_equity = self.get_equity_snapshots()
        df_trades = self.get_trades()

        returns_mdd = self.calculate_returns_and_mdd(df_equity)
        trade_perf = self.calculate_trade_performance(df_trades)
        cons_losses = self.calculate_consecutive_losses_and_martingale(df_trades)
        trade_freq = self.calculate_trade_frequency(df_trades)
        bnh = self.calculate_buy_and_hold(df_equity)
        regimes = self.analyze_market_regime(df_trades)

        try:
            from utils.real_balance import get_real_coin_status
            real_dca = get_real_coin_status(self.market)
        except Exception:
            real_dca = {}

        return {
            "market": self.market,
            "ticker": self.ticker,
            "initial_capital": self.initial_capital,
            "returns_mdd": returns_mdd,
            "trade_perf": trade_perf,
            "cons_losses": cons_losses,
            "trade_freq": trade_freq,
            "buy_and_hold": bnh,
            "regimes": regimes,
            "real_dca": real_dca,
            "raw_equity": df_equity,
            "raw_trades": df_trades
        }
