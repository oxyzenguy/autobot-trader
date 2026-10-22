"""
Unit Tests for Adaptive Grid Strategy Module (strategy/adaptive_grid.py)
"""
import unittest
import numpy as np
import pandas as pd
from strategy.adaptive_grid import (
    calculate_natr_series,
    get_market_regime_and_natr,
    get_dynamic_tp_margins,
    calculate_adaptive_buy_prices,
    calculate_basket_sell_price,
    DEFAULT_STEP_SCALE,
    DEFAULT_BULL_BASKET_TP,
    DEFAULT_BEAR_BASKET_TP,
)


class TestAdaptiveGrid(unittest.TestCase):

    def setUp(self):
        # 300개의 샘플 1시간봉 데이터 생성
        np.random.seed(42)
        base = 3_000_000.0
        closes = [base]
        for _ in range(299):
            change = np.random.normal(0, 0.01)
            closes.append(closes[-1] * (1.0 + change))
        closes = np.array(closes)
        highs = closes * (1.0 + np.random.uniform(0.002, 0.015, len(closes)))
        lows = closes * (1.0 - np.random.uniform(0.002, 0.015, len(closes)))
        
        self.df = pd.DataFrame({
            'open': closes,
            'high': highs,
            'low': lows,
            'close': closes,
            'volume': np.random.uniform(10, 100, len(closes))
        })

    def test_calculate_natr_series(self):
        natr = calculate_natr_series(self.df, window=24)
        self.assertEqual(len(natr), len(self.df))
        self.assertFalse(natr.isna().any())
        self.assertTrue((natr > 0).all())
        
        # 빈 데이터프레임 방어
        empty_natr = calculate_natr_series(pd.DataFrame())
        self.assertTrue(empty_natr.empty)

    def test_get_market_regime_and_natr(self):
        reg = get_market_regime_and_natr(self.df, rolling_window_days=10)
        self.assertIn("is_bull", reg)
        self.assertIn("regime", reg)
        self.assertIn(reg["regime"], ["BULL", "BEAR"])
        self.assertIn("curr_natr", reg)
        self.assertIn("volatility_regime", reg)
        self.assertIn(reg["volatility_regime"], ["LOW", "NORMAL", "HIGH"])
        self.assertGreater(reg["curr_natr"], 0.0)
        self.assertGreaterEqual(reg["q75"], reg["q25"])

    def test_dynamic_tp_margins(self):
        bull_basket, bull_tranche = get_dynamic_tp_margins(is_bull=True)
        bear_basket, bear_tranche = get_dynamic_tp_margins(is_bull=False)
        
        # 상승장 바스켓은 하락장 바스켓보다 크다
        self.assertAlmostEqual(bull_basket, 1.0 + DEFAULT_BULL_BASKET_TP)
        self.assertAlmostEqual(bear_basket, 1.0 + DEFAULT_BEAR_BASKET_TP)
        self.assertGreater(bull_basket, bear_basket)
        self.assertGreater(bull_tranche, bear_tranche)

    def test_calculate_adaptive_buy_prices_step_scale(self):
        base_p = 3_700_000.0
        natr = 0.009 # 0.9%
        
        # 1.0x (고정) vs 1.25x (스케일 가속) 비교
        orders_fixed = calculate_adaptive_buy_prices(
            base_price=base_p, natr=natr, current_step=0, num_orders=3, step_scale=1.00
        )
        orders_scaled = calculate_adaptive_buy_prices(
            base_price=base_p, natr=natr, current_step=0, num_orders=3, step_scale=1.25
        )
        
        self.assertEqual(len(orders_fixed), 3)
        self.assertEqual(len(orders_scaled), 3)
        
        # 스쿼드 배수: [1, 1, 2]
        self.assertEqual([o['units'] for o in orders_scaled], [1, 1, 2])
        self.assertEqual([o['cost_krw'] for o in orders_scaled], [10000.0, 10000.0, 20000.0])
        
        # 차수가 깊어질수록 간격이 1.25배씩 가속되어야 함
        drop_0 = orders_scaled[0]['drop_pct']
        drop_1 = orders_scaled[1]['drop_pct']
        drop_2 = orders_scaled[2]['drop_pct']
        
        self.assertAlmostEqual(drop_1, drop_0 * 1.25, places=5)
        self.assertAlmostEqual(drop_2, drop_0 * (1.25 ** 2), places=5)
        
        # 스케일 적용 시 마지막 호가는 고정보다 더 낮아야 함 (더 넓게 벌려 매수)
        self.assertLess(orders_scaled[-1]['price'], orders_fixed[-1]['price'])
        
        # 호가 단위(1,000원) 준수 확인
        for o in orders_scaled:
            self.assertEqual(o['price'] % 1000.0, 0.0)

    def test_calculate_basket_sell_price_min_profit(self):
        avg_p = 3_600_000.0
        sell_bull = calculate_basket_sell_price(avg_p, is_bull=True)
        sell_bear = calculate_basket_sell_price(avg_p, is_bull=False)
        
        # 상승장 매도가가 하락장 매도가보다 높음
        self.assertGreater(sell_bull, sell_bear)
        
        # 어떤 경우에도 거래소 수수료(0.10%) 이상의 순이익 보장선 상회
        min_p = avg_p * (1.0 + 0.0005 * 2 + 0.0005)
        self.assertGreaterEqual(sell_bear, min_p)
        self.assertGreaterEqual(sell_bull, min_p)
        
        # 호가 단위 올림 확인 (3,600,000 기준 1,000원 단위)
        self.assertEqual(sell_bull % 1000.0, 0.0)
        self.assertEqual(sell_bear % 1000.0, 0.0)

    def test_feature_flag_config_defaults(self):
        from config import (
            is_adaptive_grid_enabled,
            is_adaptive_grid_shadow_enabled,
            USE_ADAPTIVE_GRID,
            ENABLE_ADAPTIVE_GRID_SHADOW
        )
        # 1. ETH 및 SOL은 C-E 실전 활성화(True), BTC는 False
        self.assertTrue(is_adaptive_grid_enabled("KRW-ETH"))
        self.assertTrue(is_adaptive_grid_enabled("KRW-SOL"))
        self.assertFalse(is_adaptive_grid_enabled("KRW-BTC"))
        self.assertTrue(USE_ADAPTIVE_GRID.get("KRW-ETH", False))
        self.assertTrue(USE_ADAPTIVE_GRID.get("KRW-SOL", False))
        self.assertFalse(USE_ADAPTIVE_GRID.get("KRW-BTC", False))

        # 2. 섀도우 모드는 ETH 실전 가동으로 OFF 전환, SOL/BTC 기본 OFF
        self.assertFalse(is_adaptive_grid_shadow_enabled("KRW-ETH"))
        self.assertFalse(is_adaptive_grid_shadow_enabled("KRW-SOL"))
        self.assertFalse(is_adaptive_grid_shadow_enabled("KRW-BTC"))

    def test_format_shadow_mode_log(self):
        from strategy.adaptive_grid import format_shadow_mode_log
        log_out = format_shadow_mode_log(
            market="KRW-ETH",
            current_price=3_650_000.0,
            avg_price=3_700_000.0,
            current_step=4,
            legacy_buy_price=3_674_100.0,
            legacy_tp_price=3_718_500.0,
            df_candles=self.df
        )
        self.assertIn("[SHADOW][ETH]", log_out)
        self.assertIn("Regime       :", log_out)
        self.assertIn("NATR         :", log_out)
        self.assertIn("Q25/Q75      :", log_out)
        self.assertIn("Volatility   :", log_out)
        self.assertIn("Step         : 4", log_out)
        self.assertIn("Base Drop    :", log_out)
        self.assertIn("Step Scale   : 1.25^4", log_out)
        self.assertIn("Effective    :", log_out)
        self.assertIn("Legacy Buy   : 3,674,100", log_out)
        self.assertIn("Adaptive Buy :", log_out)
        self.assertIn("Basket TP    :", log_out)
        self.assertIn("Tranche TP   :", log_out)

    def test_get_recent_candles_safety(self):
        from strategy.adaptive_grid import get_recent_candles
        # allow_network=False일 때 캐시가 없으면 None을 즉시 반환하고 네트워크 호출 금지
        res = get_recent_candles("KRW-NONEXISTENT", allow_network=False)
        self.assertIsNone(res)


if __name__ == '__main__':
    unittest.main()
