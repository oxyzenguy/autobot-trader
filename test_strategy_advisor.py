import os
import json
import unittest
import tempfile
import shutil
from pathlib import Path
from unittest.mock import patch, MagicMock
import pandas as pd
import numpy as np

import strategy.strategy_advisor as sa
from strategy.tradingview_indicators import (
    calculate_supertrend,
    calculate_wavetrend,
    calculate_ttm_squeeze,
    add_tradingview_indicators
)


class TestStrategyAdvisor(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.original_proposal_file = sa.PROPOSAL_FILE
        self.original_config_path = sa.CONFIG_PATH

        self.test_proposal_file = Path(self.temp_dir) / "test_proposals.json"
        self.test_config_path = Path(self.temp_dir) / "test_config.py"

        sa.PROPOSAL_FILE = self.test_proposal_file
        sa.CONFIG_PATH = self.test_config_path

    def tearDown(self):
        sa.PROPOSAL_FILE = self.original_proposal_file
        sa.CONFIG_PATH = self.original_config_path
        shutil.rmtree(self.temp_dir)

    def test_tradingview_indicators_calculation(self):
        dates = pd.date_range("2026-01-01", periods=50, freq="1h")
        data = {
            "high": [100.0 + (i % 5) for i in range(50)],
            "low": [95.0 - (i % 3) for i in range(50)],
            "close": [98.0 + (i % 4) for i in range(50)],
            "volume": [1000.0 + (i * 10) for i in range(50)]
        }
        df = pd.DataFrame(data, index=dates)

        # 1. SuperTrend
        st_line, st_dir = calculate_supertrend(df, period=10, multiplier=3.0)
        self.assertEqual(len(st_line), 50)
        self.assertIn(st_dir.iloc[-1], [-1, 1])

        # 2. WaveTrend
        wt1, wt2, wt_cross = calculate_wavetrend(df, n1=10, n2=21, oversold=-53.0)
        self.assertEqual(len(wt1), 50)
        self.assertIsInstance(bool(wt_cross.iloc[-1]), bool)

        # 3. TTM Squeeze
        squeeze = calculate_ttm_squeeze(df, period=20, bb_mult=2.0, kc_mult=1.5)
        self.assertEqual(len(squeeze), 50)

        # 4. add_tradingview_indicators
        df_augmented = add_tradingview_indicators(df)
        self.assertIn("supertrend", df_augmented.columns)
        self.assertIn("supertrend_dir", df_augmented.columns)
        self.assertIn("wt1", df_augmented.columns)
        self.assertIn("is_wt_oversold_cross", df_augmented.columns)
        self.assertIn("is_squeeze_on", df_augmented.columns)

    def test_analyze_market_volatility_with_synthetic_candles(self):
        dates = pd.date_range("2026-01-01", periods=60, freq="1h")
        data = {
            "high": [100.0 + (i % 3) for i in range(60)],
            "low": [98.0 - (i % 2) for i in range(60)],
            "close": [99.0 + (i % 2) for i in range(60)],
            "volume": [1000.0] * 60
        }
        df = pd.DataFrame(data, index=dates)

        with patch("strategy.strategy_advisor.load_historical_candles", return_value=df):
            with patch("pyupbit.get_ohlcv", return_value=None):
                metrics = sa.analyze_market_volatility("KRW-SOL")

        self.assertEqual(metrics["market"], "KRW-SOL")
        self.assertIn("atr_pct", metrics)
        self.assertIn("range_24h_pct", metrics)
        self.assertIn("volatility_regime", metrics)
        self.assertIn("recommendation_hint", metrics)
        self.assertIsInstance(metrics["atr_pct"], float)

    def test_build_telegram_strategy_briefing_autopilot_badges(self):
        proposals = {
            "KRW-SOL": {
                "market": "KRW-SOL",
                "volatility": {
                    "volatility_regime": "극소 변동성 횡보장",
                    "atr_pct": 0.35,
                    "range_24h_pct": 1.25,
                    "recommendation_hint": "익절 마진을 축소하여 회전율을 높이세요."
                },
                "should_propose": True,
                "action": "AUTO_APPLIED",
                "current": {
                    "profit_margin_pct": 0.5,
                    "down_pct_val": 4.0,
                    "market_strat": "PURE_MARTINGALE",
                    "return_pct": 2.0,
                    "mdd_pct": 5.0,
                    "trades_count": 500
                },
                "recommended": {
                    "profit_margin_pct": 0.3,
                    "down_pct_val": 3.0,
                    "market_strat": "PURE_MARTINGALE",
                    "trend_indicator": "SUPERTREND",
                    "return_pct": 2.5,
                    "mdd_pct": 4.8,
                    "trades_count": 550,
                    "max_capital": 200000
                },
                "trade_boost_pct": 10.0
            },
            "KRW-ETH": {
                "market": "KRW-ETH",
                "volatility": {
                    "volatility_regime": "정상 변동성 구간",
                    "atr_pct": 1.2,
                    "range_24h_pct": 3.5,
                    "recommendation_hint": "상승 추세 전환 검토"
                },
                "should_propose": True,
                "action": "REQUIRE_APPROVAL",
                "current": {
                    "profit_margin_pct": 0.8,
                    "down_pct_val": 2.0,
                    "market_strat": "PURE_MARTINGALE",
                    "return_pct": 0.5,
                    "mdd_pct": 6.0,
                    "trades_count": 200
                },
                "recommended": {
                    "profit_margin_pct": 0.5,
                    "down_pct_val": 2.0,
                    "market_strat": "HYBRID",
                    "return_pct": 3.5,
                    "mdd_pct": 5.0,
                    "trades_count": 260,
                    "max_capital": 300000
                },
                "trade_boost_pct": 30.0
            }
        }

        briefing = sa.build_telegram_strategy_briefing(proposals)
        self.assertIn("AutoBot 일일 퀀트 전략 리뷰", briefing)
        self.assertIn("안전 자율주행: 자동 튜닝 완료", briefing)
        self.assertIn("전략 모델 변경 제안 - 승인 필요", briefing)
        self.assertIn("/전략승인 ETH", briefing)
        self.assertIn("[TV 슈퍼트렌드]", briefing)

    def test_apply_strategy_approval_success(self):
        proposals = {
            "KRW-SOL": {
                "market": "KRW-SOL",
                "should_propose": True,
                "current": {
                    "profit_margin": 1.005,
                    "down_pct": 0.040,
                    "market_strat": "HYBRID"
                },
                "recommended": {
                    "profit_margin": 1.003,
                    "down_pct": 0.030,
                    "market_strat": "PURE_MARTINGALE"
                }
            }
        }
        with open(self.test_proposal_file, "w", encoding="utf-8") as pf:
            json.dump(proposals, pf)

        config_content = (
            'PROFIT_MARGINS = {\n'
            '    "KRW-BTC": 1.000,\n'
            '    "KRW-ETH": 1.008,\n'
            '    "KRW-SOL": 1.005,\n'
            '}\n'
            'MAGIC_SPLIT_DOWN_PCTS = {\n'
            '    "KRW-BTC": 0.050,  # BTC\n'
            '    "KRW-ETH": 0.020,  # ETH\n'
            '    "KRW-SOL": 0.040,  # SOL\n'
            '}\n'
            'MARKET_STRATEGIES = {\n'
            '    "KRW-BTC": "HYBRID",\n'
            '    "KRW-ETH": "PURE_MARTINGALE",\n'
            '    "KRW-SOL": "HYBRID",\n'
            '}\n'
        )
        with open(self.test_config_path, "w", encoding="utf-8") as cf:
            cf.write(config_content)

        ok, msg = sa.apply_strategy_approval("SOL")
        self.assertTrue(ok)
        self.assertIn("전략 최적화 적용 완료", msg)
        self.assertIn("KRW-SOL", msg)

        with open(self.test_config_path, "r", encoding="utf-8") as cf:
            updated = cf.read()
            self.assertIn('"KRW-SOL": 1.003', updated)
            self.assertIn('"KRW-SOL": 0.030,  # SOL', updated)
            self.assertIn('"KRW-SOL": "PURE_MARTINGALE"', updated)

    def test_apply_strategy_approval_unknown_market(self):
        proposals = {
            "KRW-SOL": {
                "market": "KRW-SOL",
                "recommended": {"profit_margin": 1.003, "down_pct": 0.03, "market_strat": "HYBRID"}
            }
        }
        with open(self.test_proposal_file, "w", encoding="utf-8") as pf:
            json.dump(proposals, pf)

        ok, msg = sa.apply_strategy_approval("DOGE")
        self.assertFalse(ok)
        self.assertIn("제안이 없습니다", msg)

    def test_apply_strategy_approval_no_file(self):
        if self.test_proposal_file.exists():
            self.test_proposal_file.unlink()

        ok, msg = sa.apply_strategy_approval("SOL")
        self.assertFalse(ok)
        self.assertIn("등록된 최신 전략 제안서가 없습니다", msg)


if __name__ == "__main__":
    unittest.main()
