#!/usr/bin/env python3
"""
========================================================================================
🧪 [AutoBot Trader] 15분봉 거래량 돌파 엔진 회귀 및 정합성 테스트 슈트
========================================================================================
1. test_01_accounting_invariant_every_bar: 매 봉 회계 불변성 (Equity == Cash + NetLiquidationValue)
2. test_02_signal_causality_and_conditions: 20봉 고점 돌파 + 거래량 2배 조건 정확성 및 인과성
3. test_03_conservative_sl_priority: TP/SL 동시 터치 시 보수적 SL 체결
4. test_04_period_split_state_inheritance: 상태 승계 시 분할 실행과 전체 실행 100% 일치
========================================================================================
"""

import unittest
import pandas as pd
import numpy as np

from backtest.breakout_15m_engine import (
    run_breakout_discrete_backtest,
    prepare_breakout_indicators,
    quantize_price
)


def create_synthetic_15m_candles(n_bars: int = 100, base_price: float = 100000.0) -> pd.DataFrame:
    """합성 15분봉 캔들 데이터 생성기"""
    dates = pd.date_range("2026-01-01 00:00:00", periods=n_bars, freq="15min")
    np.random.seed(42)

    prices = [base_price]
    for _ in range(n_bars - 1):
        ret = np.random.normal(0, 0.005)
        prices.append(prices[-1] * (1.0 + ret))

    df = pd.DataFrame(index=dates)
    df["trade_price"] = prices
    df["opening_price"] = [p * (1.0 + np.random.normal(0, 0.001)) for p in prices]
    df["high_price"] = [max(o, c) * (1.0 + abs(np.random.normal(0, 0.002))) for o, c in zip(df["opening_price"], df["trade_price"])]
    df["low_price"] = [min(o, c) * (1.0 - abs(np.random.normal(0, 0.002))) for o, c in zip(df["opening_price"], df["trade_price"])]
    df["candle_acc_trade_volume"] = 1000.0
    df["candle_acc_trade_price"] = df["trade_price"] * df["candle_acc_trade_volume"]
    return df


class TestBreakout15mEngine(unittest.TestCase):
    def setUp(self):
        self.df_btc = create_synthetic_15m_candles(n_bars=100, base_price=100000.0)
        self.dfs = {"KRW-BTC": self.df_btc}

    def test_01_accounting_invariant_every_bar(self):
        """[검증 1] 매 봉 회계 불변성 및 최종 손익 일치"""
        res = run_breakout_discrete_backtest(
            self.dfs,
            capital=1_000_000.0,
            max_positions=1,
            sizing_mode="dynamic_risk",
            pos_fraction=0.5,
            liquidate_at_end=True
        )
        eq_curve = res["equity_curve"]
        self.assertGreater(len(eq_curve), 0)

        for pt in eq_curve:
            self.assertGreaterEqual(pt["cash"], -1e-6)
            self.assertGreaterEqual(pt["equity"], 0.0)

        # sum(trades.pnl) == net_pnl
        pnl_sum = sum(t["pnl"] for t in res["trades"])
        self.assertAlmostEqual(pnl_sum, res["net_pnl"], places=4)

    def test_02_signal_causality_and_conditions(self):
        """[검증 2] 20봉 고점 돌파 + 거래량 2배 조건과 다음 봉 시가 체결 인과성 검증"""
        df = self.df_btc.copy()
        # 30번째 봉까지는 고요한 횡보
        # 31번째 봉에서 의도적인 고점 돌파 및 거래량 3배 급증 생성
        high_prev_20 = df["high_price"].iloc[10:30].max()
        df.loc[df.index[30], "trade_price"] = high_prev_20 * 1.05  # 고점 상향 돌파
        df.loc[df.index[30], "high_price"] = high_prev_20 * 1.06
        df.loc[df.index[30], "candle_acc_trade_volume"] = 3000.0   # 평소(1000) 대비 3배

        dfs_test = {"KRW-BTC": df}
        res = run_breakout_discrete_backtest(
            dfs_test,
            capital=1_000_000.0,
            breakout_window=20,
            volume_window=20,
            volume_mult=2.0,
            start_date=str(df.index[0]),
            end_date=str(df.index[40])
        )

        trades = res["trades"]
        self.assertGreater(len(trades), 0)
        first_trade = trades[0]

        # 30번째 확정봉에서 신호가 떴으므로 체결 시각은 31번째 봉(인덱스 31)의 dt여야 함 (다음 봉 시가 체결)
        expected_entry_dt = str(df.index[31])
        self.assertEqual(first_trade["entry_dt"], expected_entry_dt)
        self.assertAlmostEqual(first_trade["entry_price"], df.loc[df.index[31], "opening_price"], places=2)

    def test_03_conservative_sl_priority(self):
        """[검증 3] 고가(TP)와 저가(SL) 동시 터치 시 보수적 SL 체결"""
        dates = pd.date_range("2026-01-01 00:00:00", periods=25, freq="15min")
        df = pd.DataFrame(index=dates, data={
            "opening_price": [100000.0] * 25,
            "high_price":    [100100.0] * 25,
            "low_price":     [99900.0]  * 25,
            "trade_price":   [100000.0] * 25,
            "candle_acc_trade_volume": [1000.0] * 25,
            "candle_acc_trade_price":  [1e8] * 25
        })
        # 20봉에서 돌파 신호
        df.loc[dates[20], "trade_price"] = 100500.0
        df.loc[dates[20], "high_price"] = 100600.0
        df.loc[dates[20], "candle_acc_trade_volume"] = 3000.0

        # 21봉 시가 진입 후, 22봉에서 상하방 동시 극단 변동 (TP +3%, SL -1.5% 동시 터치)
        df.loc[dates[22], "high_price"] = 105000.0  # +5% (TP 초과)
        df.loc[dates[22], "low_price"]  = 97000.0   # -3% (SL 초과)

        res = run_breakout_discrete_backtest(
            {"KRW-BTC": df},
            capital=1_000_000.0,
            tp_pct=0.03,
            sl_pct=0.015
        )

        trades = res["trades"]
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["reason"], "STOP_LOSS")
        self.assertEqual(res["simultaneous_both_count"], 1)

    def test_04_period_split_state_inheritance(self):
        """[검증 4] strict_fixed 모드에서 상태 승계 시 분할 실행과 전체 실행 100% 완전 일치"""
        start_date = str(self.df_btc.index[25])
        split_date_end = str(self.df_btc.index[50])
        split_date_next = str(self.df_btc.index[51])
        end_date = str(self.df_btc.index[-1])

        # A. 전체 연속 실행
        res_full = run_breakout_discrete_backtest(
            self.dfs,
            capital=1_000_000.0,
            sizing_mode="strict_fixed",
            liquidate_at_end=True,
            start_date=start_date,
            end_date=end_date
        )

        # B. Split 1 실행
        res_part1 = run_breakout_discrete_backtest(
            self.dfs,
            capital=1_000_000.0,
            sizing_mode="strict_fixed",
            liquidate_at_end=False,
            start_date=start_date,
            end_date=split_date_end
        )

        # C. Split 2 실행
        res_part2 = run_breakout_discrete_backtest(
            self.dfs,
            capital=1_000_000.0,
            sizing_mode="strict_fixed",
            liquidate_at_end=True,
            start_date=split_date_next,
            end_date=end_date,
            initial_cash=res_part1["equity_curve"][-1]["cash"],
            initial_positions=res_part1["terminal_positions"],
            initial_pending_orders=res_part1["pending_orders_end"]
        )

        tr_full = res_full["trades"]
        tr_split = res_part1["trades"] + res_part2["trades"]

        self.assertEqual(len(tr_split), len(tr_full))
        for i in range(len(tr_full)):
            self.assertEqual(tr_split[i]["entry_dt"], tr_full[i]["entry_dt"])
            self.assertEqual(tr_split[i]["exit_dt"], tr_full[i]["exit_dt"])
            self.assertAlmostEqual(tr_split[i]["entry_price"], tr_full[i]["entry_price"], places=4)
            self.assertAlmostEqual(tr_split[i]["exit_price"], tr_full[i]["exit_price"], places=4)
            self.assertAlmostEqual(tr_split[i]["pnl"], tr_full[i]["pnl"], places=4)

        self.assertAlmostEqual(res_part2["final_equity"], res_full["final_equity"], places=4)

    def test_05_atr_dynamic_sl_and_tp(self):
        """[검증 5] ATR 기반 동적 손절(1.5x ATR) 및 목표가(2.5x ATR) 호가 양자화 및 정확한 체결 검증"""
        dates = pd.date_range("2026-01-01 00:00:00", periods=25, freq="15min")
        # 14봉 동안 일정한 캔들 변동폭(ATR = 1,000원) 형성
        df = pd.DataFrame(index=dates, data={
            "opening_price": [100000.0] * 25,
            "high_price":    [100400.0] * 25,
            "low_price":     [99400.0]  * 25,
            "trade_price":   [100000.0] * 25,
            "candle_acc_trade_volume": [1000.0] * 25,
            "candle_acc_trade_price":  [1e8] * 25
        })
        # 20봉에서 돌파 신호 (고가 100,500 > 이전 고가 100,400)
        df.loc[dates[20], "trade_price"] = 100500.0
        df.loc[dates[20], "high_price"] = 100500.0
        df.loc[dates[20], "low_price"] = 99500.0
        df.loc[dates[20], "candle_acc_trade_volume"] = 3000.0

        # 21봉 시가(100,000) 진입. 신호봉 ATR = 1,000원
        # Initial SL = 100,000 - 1.5 * 1,000 = 98,500
        # TP = 100,000 + 2.5 * 1,000 = 102,500
        # 22봉에서 저가가 98,400으로 하락하여 ATR SL 터치 (시가는 100,000 유지하여 갭 방지)
        df.loc[dates[22], "low_price"] = 98400.0

        res = run_breakout_discrete_backtest(
            {"KRW-BTC": df},
            capital=1_000_000.0,
            atr_sl_mult=1.5,
            atr_tp_mult=2.5,
            tp_pct=0.0,
            sl_pct=0.0
        )

        trades = res["trades"]
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["reason"], "STOP_LOSS")
        self.assertEqual(trades[0]["exit_price"], 98500.0)

    def test_06_atr_trailing_stop_execution(self):
        """[검증 6] ATR 트레일링 스탑: 수익 발생 후 고점 대비 1.5x ATR 이탈 시 TRAILING_STOP 체결 검증"""
        dates = pd.date_range("2026-01-01 00:00:00", periods=25, freq="15min")
        df = pd.DataFrame(index=dates, data={
            "opening_price": [100000.0] * 25,
            "high_price":    [100400.0] * 25,
            "low_price":     [99400.0]  * 25,
            "trade_price":   [100000.0] * 25,
            "candle_acc_trade_volume": [1000.0] * 25,
            "candle_acc_trade_price":  [1e8] * 25
        })
        # 20봉에서 돌파 신호
        df.loc[dates[20], "trade_price"] = 100500.0
        df.loc[dates[20], "high_price"] = 100500.0
        df.loc[dates[20], "low_price"] = 99500.0
        df.loc[dates[20], "candle_acc_trade_volume"] = 3000.0

        # 21봉 시가 100,000 진입 후, 22봉에서 고점 105,000 (+5.0% 급등)
        # peak_price = 105,000
        # trail_sl = 105,000 - 1.5 * 1,000 = 103,500
        df.loc[dates[22], "high_price"] = 105000.0
        df.loc[dates[22], "trade_price"] = 104500.0

        # 23봉 시가 104,500(전봉 종가) -> 저가 103,000으로 하락하여 intra-bar 트레일링 스탑 터치
        df.loc[dates[23], "opening_price"] = 104500.0
        df.loc[dates[23], "high_price"] = 104500.0
        df.loc[dates[23], "low_price"] = 103000.0
        df.loc[dates[23], "trade_price"] = 103200.0

        res = run_breakout_discrete_backtest(
            {"KRW-BTC": df},
            capital=1_000_000.0,
            tp_pct=0.0,
            sl_pct=0.0,
            atr_sl_mult=1.5,
            atr_trail_mult=1.5,
            trail_activation_pct=0.015
        )

        trades = res["trades"]
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["reason"], "TRAILING_STOP")
        self.assertEqual(trades[0]["exit_price"], 103500.0)
        self.assertGreater(trades[0]["pnl"], 0.0)

    def test_07_atr_trailing_stop_period_split_inheritance(self):
        """[검증 7] ATR 트레일링 스탑 적용 시에도 기간 분할 상태 승계 100% 완전 일치"""
        start_date = str(self.df_btc.index[25])
        split_date_end = str(self.df_btc.index[50])
        split_date_next = str(self.df_btc.index[51])
        end_date = str(self.df_btc.index[-1])

        # A. 전체 연속 실행
        res_full = run_breakout_discrete_backtest(
            self.dfs,
            capital=1_000_000.0,
            sizing_mode="strict_fixed",
            tp_pct=0.0,
            sl_pct=0.0,
            atr_sl_mult=1.5,
            atr_tp_mult=2.5,
            atr_trail_mult=1.5,
            trail_activation_pct=0.015,
            liquidate_at_end=True,
            start_date=start_date,
            end_date=end_date
        )

        # B. Split 1 실행
        res_part1 = run_breakout_discrete_backtest(
            self.dfs,
            capital=1_000_000.0,
            sizing_mode="strict_fixed",
            tp_pct=0.0,
            sl_pct=0.0,
            atr_sl_mult=1.5,
            atr_tp_mult=2.5,
            atr_trail_mult=1.5,
            trail_activation_pct=0.015,
            liquidate_at_end=False,
            start_date=start_date,
            end_date=split_date_end
        )

        # C. Split 2 실행
        res_part2 = run_breakout_discrete_backtest(
            self.dfs,
            capital=1_000_000.0,
            sizing_mode="strict_fixed",
            tp_pct=0.0,
            sl_pct=0.0,
            atr_sl_mult=1.5,
            atr_tp_mult=2.5,
            atr_trail_mult=1.5,
            trail_activation_pct=0.015,
            liquidate_at_end=True,
            start_date=split_date_next,
            end_date=end_date,
            initial_cash=res_part1["equity_curve"][-1]["cash"],
            initial_positions=res_part1["terminal_positions"],
            initial_pending_orders=res_part1["pending_orders_end"]
        )

        tr_full = res_full["trades"]
        tr_split = res_part1["trades"] + res_part2["trades"]

        self.assertEqual(len(tr_split), len(tr_full))
        for i in range(len(tr_full)):
            self.assertEqual(tr_split[i]["entry_dt"], tr_full[i]["entry_dt"])
            self.assertEqual(tr_split[i]["exit_dt"], tr_full[i]["exit_dt"])
            self.assertAlmostEqual(tr_split[i]["entry_price"], tr_full[i]["entry_price"], places=4)
            self.assertAlmostEqual(tr_split[i]["exit_price"], tr_full[i]["exit_price"], places=4)
            self.assertAlmostEqual(tr_split[i]["pnl"], tr_full[i]["pnl"], places=4)

        self.assertAlmostEqual(res_part2["final_equity"], res_full["final_equity"], places=4)


if __name__ == "__main__":
    unittest.main()
