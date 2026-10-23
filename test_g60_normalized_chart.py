"""
G60 서브포켓 3종 통합 정규화(%) 수익률 및 매매 타점 오버레이 차트 단위 테스트
"""

from __future__ import annotations

import unittest
from typing import Dict, Any, List

import numpy as np
import pandas as pd
import plotly.graph_objects as go

from dashboard import build_g60_normalized_figure, format_g60_action


class TestG60NormalizedChart(unittest.TestCase):
    def setUp(self):
        self.markets = [
            {"market": "KRW-ETH", "ticker": "ETH", "name": "이더리움", "icon": "💎"},
            {"market": "KRW-SOL", "ticker": "SOL", "name": "솔라나", "icon": "🟣"},
            {"market": "KRW-XRP", "ticker": "XRP", "name": "리플", "icon": "🌊"},
        ]

    def _mock_ohlcv(self, market: str, interval: str = "minute60", count: int = 72) -> pd.DataFrame:
        dates = pd.date_range("2026-10-21 00:00:00", periods=count, freq="1h")
        base = 3000000.0 if "ETH" in market else (140000.0 if "SOL" in market else 2000.0)
        t = np.linspace(0, 4, count)
        prices = base * (1.0 + 0.05 * np.sin(t))
        return pd.DataFrame({
            "open": prices,
            "high": prices * 1.01,
            "low": prices * 0.99,
            "close": prices,
            "volume": np.full(count, 100.0)
        }, index=dates)

    def test_format_g60_action(self):
        self.assertEqual("매수 (Lvl 1)", format_g60_action({"action": "POCKET_BUY_LVL_1", "side": "bid", "level": 1}))
        self.assertEqual("매수 (Lvl 3)", format_g60_action({"action": "POCKET_BUY_LVL_3", "side": "bid", "level": 3}))
        self.assertEqual("개별 익절", format_g60_action({"action": "POCKET_TP_LVL_1", "side": "ask"}))
        self.assertEqual("스쿼드 익절", format_g60_action({"action": "POCKET_SQUAD_TP_LVL_2", "side": "ask"}))

    def test_build_g60_normalized_figure_empty_data(self):
        fig = build_g60_normalized_figure(
            g60_data=[],
            ohlcv_fetcher=lambda m, **kwargs: pd.DataFrame()
        )
        self.assertIsInstance(fig, go.Figure)
        self.assertEqual(0, len(fig.data))

    def test_build_g60_normalized_figure_with_trades(self):
        eth_trades = pd.DataFrame([
            {"timestamp": "2026-10-21 05:00:00", "market": "KRW-ETH", "side": "bid", "price": 3020000.0, "volume": 0.003, "action": "POCKET_BUY_LVL_1", "level": 1},
            {"timestamp": "2026-10-21 12:00:00", "market": "KRW-ETH", "side": "ask", "price": 3080000.0, "volume": 0.003, "action": "POCKET_TP_LVL_1", "pnl": 180.0, "level": 1},
        ])
        sol_trades = pd.DataFrame([
            {"timestamp": "2026-10-21 06:00:00", "market": "KRW-SOL", "side": "bid", "price": 141000.0, "volume": 0.07, "action": "POCKET_BUY_LVL_1", "level": 1},
        ])
        xrp_trades = pd.DataFrame()

        g60_data = [
            {**self.markets[0], "trades_df": eth_trades},
            {**self.markets[1], "trades_df": sol_trades},
            {**self.markets[2], "trades_df": xrp_trades},
        ]

        fig = build_g60_normalized_figure(
            g60_data=g60_data,
            interval="minute60",
            count=72,
            show_trades=True,
            show_zero_line=True,
            ohlcv_fetcher=self._mock_ohlcv
        )

        self.assertIsInstance(fig, go.Figure)
        # Traces expected:
        # 3 line traces (ETH, SOL, XRP)
        # + ETH buys, ETH sells
        # + SOL buys
        # = 6 traces total
        self.assertEqual(6, len(fig.data))

        trace_names = [t.name for t in fig.data]
        self.assertTrue(any("ETH" in n for n in trace_names))
        self.assertTrue(any("SOL" in n for n in trace_names))
        self.assertTrue(any("XRP" in n for n in trace_names))
        self.assertTrue(any("ETH 매수" in n for n in trace_names))
        self.assertTrue(any("ETH 익절" in n for n in trace_names))
        self.assertTrue(any("SOL 매수" in n for n in trace_names))

        # Check Y values are percentages (around -10% to +10%, not millions)
        eth_line = fig.data[0]
        self.assertAlmostEqual(0.0, eth_line.y[0], places=5)
        for y_val in eth_line.y:
            self.assertTrue(-20.0 < y_val < 20.0)

    def test_build_g60_normalized_figure_hide_trades(self):
        g60_data = [{**m, "trades_df": pd.DataFrame()} for m in self.markets]
        fig = build_g60_normalized_figure(
            g60_data=g60_data,
            interval="minute60",
            count=72,
            show_trades=False,
            show_zero_line=False,
            ohlcv_fetcher=self._mock_ohlcv
        )
        # Only 3 line traces
        self.assertEqual(3, len(fig.data))


if __name__ == "__main__":
    unittest.main()
