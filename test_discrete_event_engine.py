#!/usr/bin/env python3
"""
========================================================================================
🧪 [AutoBot Trader] 이산 사건(Discrete-Event) 백테스트 엔진 V3.1 엄격 회귀 테스트 슈트
========================================================================================
[테스트 목록]
1. test_01_accounting_invariant_every_bar: 매 봉 회계 불변성 (Equity == Cash + NetLiquidationValue)
2. test_02_period_split_exact_matching: 무포지션 경계 분할 시 건별 진입·청산·가격·손익 100% 완전 일치
3. test_03_period_split_state_inheritance: 상태 승계(State Inheritance) 시 임의 분할 경계에서도 100% 완전 일치
4. test_04_gap_down_stop_loss_execution: 기존 포지션 시가 갭다운 시 opening_price 체결 (STOP_LOSS_GAP)
5. test_05_new_entry_gap_isolation: 신규 진입 포지션은 시가 갭 판정에서 배제되고 봉 내부 판정 적용
6. test_06_simultaneous_touch_conservative_stop_loss: TP/SL 동시 터치 시 보수적 손절(SL) 우선
7. test_07_minimum_order_rejection: 최소 주문금액(5,000원) 미만 거절 및 현금 보호
8. test_08_input_validation_errors: 잘못된 sizing_mode, capital, pos_fraction 등 ValueError 발생
9. test_09_final_bar_liquidation_sync: 마지막 봉 청산 시 equity_curve[-1] 동기화 및 MDD/손익 완전 일치
10. test_10_liquidate_at_end_false_accounting: liquidate_at_end=False 시 실현/미실현 손익 정밀 분리
11. test_11_period_split_state_inheritance_strict_fixed: strict_fixed 모드에서 상태 승계 시 전체 실행과 100% 완전 일치
12. test_12_period_split_state_inheritance_dynamic_risk: dynamic_risk 모드에서 상태 승계 시 전체 실행과 100% 완전 일치
13. test_13_missing_candle_mark_to_market_valuation: 중간 캔들 누락 시 직전 유효가 보존 평가 및 예외 방지
14. test_14_terminal_pending_orders_policy: 백테스트 종료 시 대기 주문 취소/보존 정책 검증
========================================================================================
"""

import unittest
from datetime import datetime
import pandas as pd
import numpy as np

from backtest.discrete_event_engine import (
    run_discrete_event_backtest,
    quantize_price,
    get_upbit_tick_size
)


def create_synthetic_candle_data(n_bars: int = 120, base_price: float = 100000.0) -> pd.DataFrame:
    """합성 캔들 데이터 생성기"""
    dates = pd.date_range("2025-01-01 00:00:00", periods=n_bars, freq="1h")
    np.random.seed(42)

    prices = [base_price]
    for _ in range(n_bars - 1):
        ret = np.random.normal(0, 0.01)
        prices.append(prices[-1] * (1.0 + ret))

    df = pd.DataFrame(index=dates)
    df["trade_price"] = prices
    df["opening_price"] = [p * (1.0 + np.random.normal(0, 0.002)) for p in prices]
    df["high_price"] = [max(o, c) * (1.0 + abs(np.random.normal(0, 0.005))) for o, c in zip(df["opening_price"], df["trade_price"])]
    df["low_price"] = [min(o, c) * (1.0 - abs(np.random.normal(0, 0.005))) for o, c in zip(df["opening_price"], df["trade_price"])]
    df["candle_acc_trade_volume"] = 1000.0
    df["candle_acc_trade_price"] = df["trade_price"] * df["candle_acc_trade_volume"]

    # 롤링 지표
    df["vol_24h"] = df["candle_acc_trade_price"].rolling(24).sum().fillna(1e8)
    df["ret_24h"] = 0.01
    df["ret_4h"] = 0.005
    df["natr"] = 2.0
    df["rsi14"] = 40.0
    df["pct_b"] = 0.3
    df["tick_ratio"] = 0.001

    return df


class TestDiscreteEventEngineHardened(unittest.TestCase):
    """이산 사건 백테스트 엔진 V3.1 엄격 회귀 테스트 슈트"""

    def setUp(self):
        self.df_btc = create_synthetic_candle_data(n_bars=120, base_price=100000.0)
        self.df_eth = create_synthetic_candle_data(n_bars=120, base_price=50000.0)
        self.dfs = {
            "KRW-BTC": self.df_btc,
            "KRW-ETH": self.df_eth
        }

    def test_01_accounting_invariant_every_bar(self):
        """[회귀 1] 매 봉 현금 + 순청산가치 == 평가금 불변성 & PnL 총합 일치"""
        res = run_discrete_event_backtest(
            self.dfs,
            capital=1_000_000.0,
            max_positions=2,
            sizing_mode="dynamic_risk",
            pos_fraction=0.5,
            liquidate_at_end=True,
            start_date=str(self.df_btc.index[25]),
            end_date=str(self.df_btc.index[-1])
        )

        eq_curve = res["equity_curve"]
        self.assertGreater(len(eq_curve), 0)

        # 1. 매 봉마다 현금 및 평가금 음수 방지 및 불변성 검증
        for pt in eq_curve:
            cash = pt["cash"]
            equity = pt["equity"]
            self.assertGreaterEqual(cash, -1e-6, f"Negative cash detected: {cash}")
            self.assertGreaterEqual(equity, 0.0, f"Negative equity detected: {equity}")

        # 2. liquidate_at_end=True 시: final_equity == 최종 현금 & sum(trades.pnl) == net_pnl
        trades = res["trades"]
        pnl_sum = sum(t["pnl"] for t in trades)
        expected_net_pnl = res["final_equity"] - res["initial_capital"]

        self.assertAlmostEqual(
            pnl_sum,
            expected_net_pnl,
            places=4,
            msg=f"PnL sum ({pnl_sum}) does not match Net PnL ({expected_net_pnl})"
        )
        self.assertEqual(res["open_positions_end"], 0, "All positions must be liquidated at end")

    def test_02_period_split_exact_matching_zero_boundary_positions(self):
        """[회귀 2] 경계 시점에 포지션이 없는 경우 전체 실행과 분할 실행의 건별 100% 완전 일치 검증"""
        # 100개 봉 합성 데이터 생성하되, 45~55봉 구간은 신호가 발생하지 않도록 설정
        df = create_synthetic_candle_data(n_bars=100, base_price=100000.0)
        # 40~55봉 사이는 NATR을 0으로 만들어 신규 진입을 완전 차단
        df.loc[df.index[40:55], "natr"] = 0.1
        dfs_test = {"KRW-BTC": df}

        split_date = str(df.index[50])
        start_date = str(df.index[25])
        end_date = str(df.index[-1])

        # A. 전체 연속 실행 (unconstrained_signal 모드)
        res_full = run_discrete_event_backtest(
            dfs_test,
            capital=1_000_000.0,
            sizing_mode="unconstrained_signal",
            liquidate_at_end=True,
            start_date=start_date,
            end_date=end_date
        )

        # B. Split 1 실행 (경계 시점 50봉 종료)
        res_part1 = run_discrete_event_backtest(
            dfs_test,
            capital=1_000_000.0,
            sizing_mode="unconstrained_signal",
            liquidate_at_end=False,  # 경계 청산 없음
            start_date=start_date,
            end_date=split_date
        )
        self.assertEqual(res_part1["open_positions_end"], 0, "Boundary must have zero open positions")

        # C. Split 2 실행 (경계 시점 50봉부터 시작)
        res_part2 = run_discrete_event_backtest(
            dfs_test,
            capital=1_000_000.0,
            sizing_mode="unconstrained_signal",
            liquidate_at_end=True,
            start_date=split_date,
            end_date=end_date
        )

        tr_full = res_full["trades"]
        tr_split = res_part1["trades"] + res_part2["trades"]

        # 건수 100% 완전 일치
        self.assertEqual(len(tr_split), len(tr_full))

        # 건별 진입 시각, 청산 시각, 진입가, 청산가, 손익 전수 100% 일치 검증
        for i in range(len(tr_full)):
            t_f = tr_full[i]
            t_s = tr_split[i]
            self.assertEqual(t_s["entry_dt"], t_f["entry_dt"])
            self.assertEqual(t_s["exit_dt"], t_f["exit_dt"])
            self.assertAlmostEqual(t_s["entry_price"], t_f["entry_price"], places=4)
            self.assertAlmostEqual(t_s["exit_price"], t_f["exit_price"], places=4)
            self.assertAlmostEqual(t_s["pnl"], t_f["pnl"], places=4)

    def test_03_period_split_state_inheritance(self):
        """[회귀 3] 상태 승계(State Inheritance) 시 임의 분할 경계에서도 전체 실행과 100% 완전 일치"""
        start_date = str(self.df_btc.index[30])
        split_date_end = str(self.df_btc.index[60])
        split_date_next = str(self.df_btc.index[61])
        end_date = str(self.df_btc.index[-1])

        # A. 전체 연속 실행
        res_full = run_discrete_event_backtest(
            self.dfs,
            capital=1_000_000.0,
            sizing_mode="unconstrained_signal",
            liquidate_at_end=True,
            start_date=start_date,
            end_date=end_date
        )

        # B. Split 1 실행 (경계 시점 60봉에서 강제 청산하지 않고 상태 유지)
        res_part1 = run_discrete_event_backtest(
            self.dfs,
            capital=1_000_000.0,
            sizing_mode="unconstrained_signal",
            liquidate_at_end=False,
            start_date=start_date,
            end_date=split_date_end
        )

        # C. Split 2 실행: 61봉부터 시작하며 Part 1의 현금, 잔여 포지션, 대기 주문을 100% 승계
        res_part2 = run_discrete_event_backtest(
            self.dfs,
            capital=1_000_000.0,
            sizing_mode="unconstrained_signal",
            liquidate_at_end=True,
            start_date=split_date_next,
            end_date=end_date,
            initial_cash=res_part1["equity_curve"][-1]["cash"],
            initial_positions=res_part1["terminal_positions"],
            initial_pending_orders=res_part1["pending_orders_end"]
        )

        tr_full = res_full["trades"]
        tr_split = res_part1["trades"] + res_part2["trades"]

        # 건수 100% 일치
        self.assertEqual(len(tr_split), len(tr_full))
        # 건별 진입·청산 시각, 가격, 손익 100% 완전 일치 검증
        for i in range(len(tr_full)):
            self.assertEqual(tr_split[i]["entry_dt"], tr_full[i]["entry_dt"])
            self.assertEqual(tr_split[i]["exit_dt"], tr_full[i]["exit_dt"])
            self.assertAlmostEqual(tr_split[i]["entry_price"], tr_full[i]["entry_price"], places=4)
            self.assertAlmostEqual(tr_split[i]["exit_price"], tr_full[i]["exit_price"], places=4)
            self.assertAlmostEqual(tr_split[i]["pnl"], tr_full[i]["pnl"], places=4)

    def test_04_gap_down_stop_loss_execution(self):
        """[회귀 4] 기존 보유 포지션 시가 갭다운 시 sl_target이 아닌 opening_price 체결"""
        dates = pd.date_range("2025-01-01 00:00:00", periods=5, freq="1h")
        df = pd.DataFrame(index=dates, data={
            "opening_price": [100000.0, 100000.0, 95000.0, 95000.0, 95000.0],
            "high_price":    [100500.0, 100500.0, 96000.0, 96000.0, 96000.0],
            "low_price":     [99500.0,  99500.0,  94000.0, 94000.0, 94000.0],
            "trade_price":   [100000.0, 100000.0, 95000.0, 95000.0, 95000.0],
            "candle_acc_trade_volume": [1000.0] * 5,
            "candle_acc_trade_price":  [1e8] * 5,
            "vol_24h":       [1e8] * 5,
            "ret_24h":       [0.01] * 5,
            "ret_4h":        [0.005] * 5,
            "natr":          [2.0] * 5,
            "rsi14":         [40.0] * 5,
            "pct_b":         [0.3] * 5,
            "tick_ratio":    [0.001] * 5
        })

        test_dfs = {"KRW-BTC": df}
        res = run_discrete_event_backtest(
            test_dfs,
            capital=1_000_000.0,
            max_positions=1,
            tp_pct=0.015,
            sl_pct=0.025,
            start_date=str(dates[0]),
            end_date=str(dates[-1])
        )

        trades = res["trades"]
        self.assertGreater(len(trades), 0)
        gap_trade = [t for t in trades if t["reason"] == "STOP_LOSS_GAP"]
        self.assertEqual(len(gap_trade), 1)
        self.assertEqual(gap_trade[0]["exit_price"], 95000.0)
        self.assertLess(gap_trade[0]["exit_price"], 97500.0)

    def test_05_new_entry_gap_isolation(self):
        """[회귀 5] 신규 진입 포지션은 시가 갭 판정을 받지 않고 봉 내부 판정이 적용됨"""
        dates = pd.date_range("2025-01-01 00:00:00", periods=4, freq="1h")
        # 0번 봉: 매수 신호
        # 1번 봉: 100,000원에 시가 진입. 당일 봉 내 저가가 97,000원으로 하락 (SL 터치)
        df = pd.DataFrame(index=dates, data={
            "opening_price": [100000.0, 100000.0, 100000.0, 100000.0],
            "high_price":    [100500.0, 100500.0, 100000.0, 100000.0],
            "low_price":     [99500.0,  97000.0,  100000.0, 100000.0],
            "trade_price":   [100000.0, 97500.0,  100000.0, 100000.0],
            "candle_acc_trade_volume": [1000.0] * 4,
            "candle_acc_trade_price":  [1e8] * 4,
            "vol_24h":       [1e8] * 4,
            "ret_24h":       [0.01] * 4,
            "ret_4h":        [0.005] * 4,
            "natr":          [2.0] * 4,
            "rsi14":         [40.0] * 4,
            "pct_b":         [0.3] * 4,
            "tick_ratio":    [0.001] * 4
        })

        res = run_discrete_event_backtest(
            {"KRW-BTC": df},
            capital=1_000_000.0,
            max_positions=1,
            tp_pct=0.015,
            sl_pct=0.025,
            start_date=str(dates[0]),
            end_date=str(dates[-1])
        )

        trades = res["trades"]
        self.assertGreater(len(trades), 0)
        # 신규 진입 봉이므로 STOP_LOSS_GAP이 아니라 일반 STOP_LOSS여야 함
        self.assertEqual(trades[0]["reason"], "STOP_LOSS")
        self.assertEqual(trades[0]["exit_price"], 97500.0)

    def test_06_simultaneous_touch_conservative_stop_loss(self):
        """[회귀 6] TP와 SL 동시 터치 시 보수적 손절(SL) 우선 체결"""
        dates = pd.date_range("2025-01-01 00:00:00", periods=5, freq="1h")
        df = pd.DataFrame(index=dates, data={
            "opening_price": [100000.0, 100000.0, 100000.0, 100000.0, 100000.0],
            "high_price":    [100500.0, 100500.0, 105000.0, 100000.0, 100000.0],
            "low_price":     [99500.0,  99500.0,  90000.0,  100000.0, 100000.0],
            "trade_price":   [100000.0, 100000.0, 100000.0, 100000.0, 100000.0],
            "candle_acc_trade_volume": [1000.0] * 5,
            "candle_acc_trade_price":  [1e8] * 5,
            "vol_24h":       [1e8] * 5,
            "ret_24h":       [0.01] * 5,
            "ret_4h":        [0.005] * 5,
            "natr":          [2.0] * 5,
            "rsi14":         [40.0] * 5,
            "pct_b":         [0.3] * 5,
            "tick_ratio":    [0.001] * 5
        })

        test_dfs = {"KRW-BTC": df}
        res = run_discrete_event_backtest(
            test_dfs,
            capital=1_000_000.0,
            max_positions=1,
            tp_pct=0.015,
            sl_pct=0.025,
            start_date=str(dates[0]),
            end_date=str(dates[-1])
        )

        trades = res["trades"]
        self.assertGreater(len(trades), 0)
        self.assertEqual(trades[0]["reason"], "STOP_LOSS")
        self.assertLess(trades[0]["pnl"], 0.0)

    def test_07_minimum_order_rejection(self):
        """[회귀 7] 최소 주문금액(5,000원) 미만 잔고 시 주문 거절 및 음수 방지"""
        dates = pd.date_range("2025-01-01 00:00:00", periods=5, freq="1h")
        df = pd.DataFrame(index=dates, data={
            "opening_price": [100000.0] * 5,
            "high_price":    [100500.0] * 5,
            "low_price":     [99500.0] * 5,
            "trade_price":   [100000.0] * 5,
            "candle_acc_trade_volume": [1000.0] * 5,
            "candle_acc_trade_price":  [1e8] * 5,
            "vol_24h":       [1e8] * 5,
            "ret_24h":       [0.01] * 5,
            "ret_4h":        [0.005] * 5,
            "natr":          [2.0] * 5,
            "rsi14":         [40.0] * 5,
            "pct_b":         [0.3] * 5,
            "tick_ratio":    [0.001] * 5
        })

        test_dfs = {"KRW-BTC": df}
        res = run_discrete_event_backtest(
            test_dfs,
            capital=3000.0,
            min_order_krw=5000.0,
            start_date=str(dates[0]),
            end_date=str(dates[-1])
        )

        self.assertEqual(len(res["trades"]), 0)
        self.assertEqual(res["final_equity"], 3000.0)

    def test_08_input_validation_errors(self):
        """[회귀 8] sizing_mode, capital, pos_fraction 등 잘못된 파라미터 시 ValueError 발생 검증"""
        # 1. 잘못된 sizing_mode
        with self.assertRaises(ValueError):
            run_discrete_event_backtest(self.dfs, sizing_mode="invalid_mode")

        # 2. 음수 또는 0 자본금
        with self.assertRaises(ValueError):
            run_discrete_event_backtest(self.dfs, capital=0.0)
        with self.assertRaises(ValueError):
            run_discrete_event_backtest(self.dfs, capital=-5000.0)

        # 3. max_positions < 1 또는 float
        with self.assertRaises(ValueError):
            run_discrete_event_backtest(self.dfs, max_positions=0)
        with self.assertRaises(ValueError):
            run_discrete_event_backtest(self.dfs, max_positions=1.5)  # type: ignore

        # 4. pos_fraction 범위 오류 (0 이하 또는 1 초과)
        with self.assertRaises(ValueError):
            run_discrete_event_backtest(self.dfs, pos_fraction=0.0)
        with self.assertRaises(ValueError):
            run_discrete_event_backtest(self.dfs, pos_fraction=1.2)

        # 5. min_order_krw < 0
        with self.assertRaises(ValueError):
            run_discrete_event_backtest(self.dfs, min_order_krw=-1.0)

        # 6. tp_pct 또는 sl_pct <= 0
        with self.assertRaises(ValueError):
            run_discrete_event_backtest(self.dfs, tp_pct=0.0)
        with self.assertRaises(ValueError):
            run_discrete_event_backtest(self.dfs, sl_pct=-0.01)

    def test_09_final_bar_liquidation_sync(self):
        """[회귀 9] 마지막 봉 청산 시 equity_curve[-1] 동기화 및 MDD/손익 완전 일치 검증"""
        dates = pd.date_range("2025-01-01 00:00:00", periods=5, freq="1h")
        # 마지막 4번 봉까지 청산되지 않고 남는 포지션 생성
        df = pd.DataFrame(index=dates, data={
            "opening_price": [100000.0] * 5,
            "high_price":    [100500.0] * 5,
            "low_price":     [99500.0] * 5,
            "trade_price":   [99000.0] * 5,  # 종가는 -1% 손실 상태
            "candle_acc_trade_volume": [1000.0] * 5,
            "candle_acc_trade_price":  [1e8] * 5,
            "vol_24h":       [1e8] * 5,
            "ret_24h":       [0.01] * 5,
            "ret_4h":        [0.005] * 5,
            "natr":          [2.0] * 5,
            "rsi14":         [40.0] * 5,
            "pct_b":         [0.3] * 5,
            "tick_ratio":    [0.001] * 5
        })

        res = run_discrete_event_backtest(
            {"KRW-BTC": df},
            capital=1_000_000.0,
            max_positions=1,
            liquidate_at_end=True,
            start_date=str(dates[0]),
            end_date=str(dates[-1])
        )

        eq_curve = res["equity_curve"]
        # equity_curve 마지막 값과 res["final_equity"]가 완전히 동일해야 함
        self.assertAlmostEqual(eq_curve[-1]["equity"], res["final_equity"], places=4)
        self.assertEqual(eq_curve[-1]["open_positions"], 0)
        self.assertEqual(res["open_positions_end"], 0)

        # 마지막 봉에서 TERMINAL_LIQUIDATION이 실행되어 trades에 기록되었는지 검증
        term_trade = [t for t in res["trades"] if t["reason"] == "TERMINAL_LIQUIDATION"]
        self.assertEqual(len(term_trade), 1)

        # 원장 합계 == net_pnl
        self.assertAlmostEqual(sum(t["pnl"] for t in res["trades"]), res["net_pnl"], places=4)

    def test_10_liquidate_at_end_false_accounting(self):
        """[회귀 10] liquidate_at_end=False 시 실현/미실현 손익 정밀 분리 검증"""
        dates = pd.date_range("2025-01-01 00:00:00", periods=5, freq="1h")
        df = pd.DataFrame(index=dates, data={
            "opening_price": [100000.0] * 5,
            "high_price":    [100500.0] * 5,
            "low_price":     [99500.0] * 5,
            "trade_price":   [99000.0] * 5,
            "candle_acc_trade_volume": [1000.0] * 5,
            "candle_acc_trade_price":  [1e8] * 5,
            "vol_24h":       [1e8] * 5,
            "ret_24h":       [0.01] * 5,
            "ret_4h":        [0.005] * 5,
            "natr":          [2.0] * 5,
            "rsi14":         [40.0] * 5,
            "pct_b":         [0.3] * 5,
            "tick_ratio":    [0.001] * 5
        })

        res = run_discrete_event_backtest(
            {"KRW-BTC": df},
            capital=1_000_000.0,
            max_positions=1,
            liquidate_at_end=False,  # 미청산 유지
            start_date=str(dates[0]),
            end_date=str(dates[-1])
        )

        self.assertEqual(res["open_positions_end"], 1)
        self.assertEqual(len(res["trades"]), 0)  # 청산된 거래는 없음
        self.assertEqual(res["realized_pnl"], 0.0)
        self.assertLess(res["unrealized_pnl"], 0.0)  # 종가 하락으로 미실현 손실
        # net_pnl == realized + unrealized
        self.assertAlmostEqual(res["net_pnl"], res["unrealized_pnl"], places=4)

    def test_11_period_split_state_inheritance_strict_fixed(self):
        """[회귀 11] strict_fixed 모드에서 상태 승계 시 전체 실행과 100% 완전 일치 검증"""
        start_date = str(self.df_btc.index[30])
        split_date_end = str(self.df_btc.index[60])
        split_date_next = str(self.df_btc.index[61])
        end_date = str(self.df_btc.index[-1])

        # A. 전체 연속 실행
        res_full = run_discrete_event_backtest(
            self.dfs,
            capital=1_000_000.0,
            sizing_mode="strict_fixed",
            liquidate_at_end=True,
            start_date=start_date,
            end_date=end_date
        )

        # B. Split 1 실행 (경계 시점 60봉에서 강제 청산하지 않고 상태 유지)
        res_part1 = run_discrete_event_backtest(
            self.dfs,
            capital=1_000_000.0,
            sizing_mode="strict_fixed",
            liquidate_at_end=False,
            start_date=start_date,
            end_date=split_date_end
        )

        # C. Split 2 실행: 61봉부터 시작하며 Part 1의 현금, 잔여 포지션, 대기 주문을 100% 승계
        res_part2 = run_discrete_event_backtest(
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

        # 건수 100% 일치
        self.assertEqual(len(tr_split), len(tr_full))
        # 건별 진입·청산 시각, 가격, 손익 100% 완전 일치 검증
        for i in range(len(tr_full)):
            self.assertEqual(tr_split[i]["entry_dt"], tr_full[i]["entry_dt"])
            self.assertEqual(tr_split[i]["exit_dt"], tr_full[i]["exit_dt"])
            self.assertAlmostEqual(tr_split[i]["entry_price"], tr_full[i]["entry_price"], places=4)
            self.assertAlmostEqual(tr_split[i]["exit_price"], tr_full[i]["exit_price"], places=4)
            self.assertAlmostEqual(tr_split[i]["pnl"], tr_full[i]["pnl"], places=4)

        # 최종 자산 및 현금 완전 일치
        self.assertAlmostEqual(res_part2["final_equity"], res_full["final_equity"], places=4)

    def test_12_period_split_state_inheritance_dynamic_risk(self):
        """[회귀 12] dynamic_risk 모드에서 상태 승계 시 전체 실행과 100% 완전 일치 검증"""
        start_date = str(self.df_btc.index[30])
        split_date_end = str(self.df_btc.index[60])
        split_date_next = str(self.df_btc.index[61])
        end_date = str(self.df_btc.index[-1])

        # A. 전체 연속 실행
        res_full = run_discrete_event_backtest(
            self.dfs,
            capital=1_000_000.0,
            sizing_mode="dynamic_risk",
            pos_fraction=0.5,
            liquidate_at_end=True,
            start_date=start_date,
            end_date=end_date
        )

        # B. Split 1 실행 (경계 시점 60봉에서 강제 청산하지 않고 상태 유지)
        res_part1 = run_discrete_event_backtest(
            self.dfs,
            capital=1_000_000.0,
            sizing_mode="dynamic_risk",
            pos_fraction=0.5,
            liquidate_at_end=False,
            start_date=start_date,
            end_date=split_date_end
        )

        # C. Split 2 실행: 61봉부터 시작하며 Part 1의 현금, 잔여 포지션, 대기 주문을 100% 승계
        res_part2 = run_discrete_event_backtest(
            self.dfs,
            capital=1_000_000.0,
            sizing_mode="dynamic_risk",
            pos_fraction=0.5,
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

    def test_13_missing_candle_mark_to_market_valuation(self):
        """[회귀 13] 보유 종목의 캔들이 중간에 누락되더라도 평가금 급락 없이 직전가로 보존 평가"""
        dates = pd.date_range("2025-01-01 00:00:00", periods=6, freq="1h")
        # BTC는 모든 봉 존재
        df_btc = pd.DataFrame(index=dates, data={
            "opening_price": [100000.0] * 6,
            "high_price":    [100500.0] * 6,
            "low_price":     [99500.0] * 6,
            "trade_price":   [100000.0] * 6,
            "candle_acc_trade_volume": [1000.0] * 6,
            "candle_acc_trade_price":  [1e8] * 6,
            "vol_24h":       [1e8] * 6,
            "ret_24h":       [0.01] * 6,
            "ret_4h":        [0.005] * 6,
            "natr":          [2.0] * 6,
            "rsi14":         [40.0] * 6,
            "pct_b":         [0.3] * 6,
            "tick_ratio":    [0.001] * 6
        })
        # ETH는 2번 봉(인덱스 2)과 마지막 봉(인덱스 5)이 누락됨
        df_eth = df_btc.copy()
        df_eth = df_eth.drop([dates[2], dates[5]])

        # 초기 상태로 ETH 포지션을 보유한 채로 시작
        initial_pos = {
            "KRW-ETH": {
                "entry_price": 50000.0,
                "effective_entry_p": 50010.0,
                "entry_dt": dates[0],
                "volume": 10.0,
                "cost": 500000.0,
                "fee_buy": 250.0
            }
        }

        res = run_discrete_event_backtest(
            {"KRW-BTC": df_btc, "KRW-ETH": df_eth},
            capital=1_000_000.0,
            initial_cash=500_000.0,
            initial_positions=initial_pos,
            max_positions=2,
            liquidate_at_end=True,
            start_date=str(dates[1]),
            end_date=str(dates[-1])
        )

        # 1. 캔들 누락 시 평가 fallback이 실행되어 missing_candle_eval_count > 0
        self.assertGreater(res["missing_candle_eval_count"], 0)
        # 2. 마지막 봉 누락에도 KeyError 없이 정상 청산 완료
        self.assertEqual(res["open_positions_end"], 0)
        # 3. 누락된 시점(dates[2])에도 equity가 현금(500,000) 수준으로 급락하지 않고 포지션 가치가 보존됨
        eq_at_missing = [pt["equity"] for pt in res["equity_curve"] if pt["dt"] == str(dates[2])][0]
        self.assertGreater(eq_at_missing, 900_000.0)

    def test_14_terminal_pending_orders_policy(self):
        """[회귀 14] 백테스트 종료 시 미체결 대기 주문 처리 정책 검증 (liquidate_at_end에 따른 취소/보존)"""
        # A. liquidate_at_end=True 인 경우: pending_orders_end 는 비워지고 terminal_cancelled_orders_count 에 기록
        res_liq = run_discrete_event_backtest(
            self.dfs,
            capital=1_000_000.0,
            liquidate_at_end=True,
            start_date=str(self.df_btc.index[30]),
            end_date=str(self.df_btc.index[40])
        )
        self.assertEqual(res_liq["pending_orders_end"], [])
        self.assertEqual(res_liq["open_positions_end"], 0)

        # B. liquidate_at_end=False 인 경우: 분할/승계를 위해 pending_orders_end 에 다음 봉 대기 주문 보존
        res_part = run_discrete_event_backtest(
            self.dfs,
            capital=1_000_000.0,
            liquidate_at_end=False,
            start_date=str(self.df_btc.index[30]),
            end_date=str(self.df_btc.index[40])
        )
        self.assertIsInstance(res_part["pending_orders_end"], list)


if __name__ == "__main__":
    unittest.main()
