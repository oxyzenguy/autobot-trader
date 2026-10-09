"""
Unit Tests for AG-04-R1: Confirmed Candle Input Contract Verification
Validates that:
1. Forming/incomplete candles are excluded based on candle end time vs observation time across all paths.
2. Results of Hybrid Regime and Adaptive Grid are invariant to extreme fluctuations in the forming candle.
3. Normal fetch, Cache hit, Fetch failure, and Exception fallback paths strictly maintain the confirmed candle contract.
4. Already confirmed data is never double-stripped (idempotent).
5. Time boundary transitions (e.g. at exact close second) behave deterministically.
6. Missing bars (gaps) do not overwrite explicit candle interval.
7. UTC timestamp column is parsed as UTC and compared consistently against KST observation time.
8. Multiple unclosed/future candles are all excluded (per-row filtering).
9. Malformed/invalid timestamp data raises an error and prevents unvalidated signal generation.
10. Insufficient data and empty inputs return safe fallbacks without exceptions.
"""
import unittest
from unittest.mock import patch
import datetime
import numpy as np
import pandas as pd

from strategy.hybrid_regime import (
    filter_confirmed_candles,
    get_hybrid_regime_and_signals,
    _CANDLE_CACHE,
    KST,
)
from strategy.adaptive_grid import (
    get_recent_candles,
    get_market_regime_and_natr,
)


class TestConfirmedCandleContract(unittest.TestCase):

    def setUp(self):
        # Clear candle cache before each test
        _CANDLE_CACHE.clear()
        
        # Generate 210 sequential 1-hour candles
        # Starting 2026-10-01 00:00:00 KST
        np.random.seed(123)
        self.num_bars = 210
        self.start_dt = pd.Timestamp("2026-10-01 00:00:00")
        self.dt_index = pd.date_range(self.start_dt, periods=self.num_bars, freq="1h")
        
        base = 3_000_000.0
        closes = [base]
        for _ in range(self.num_bars - 1):
            change = np.random.normal(0, 0.005)
            closes.append(closes[-1] * (1.0 + change))
        closes = np.array(closes)
        highs = closes * 1.005
        lows = closes * 0.995
        opens = closes * 1.001
        vols = np.random.uniform(10, 50, self.num_bars)
        
        self.df_confirmed = pd.DataFrame({
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": vols
        }, index=self.dt_index)
        
        # Last candle in df_confirmed starts at self.dt_index[-1] (2026-10-09 17:00:00) and ends 1h later (18:00:00)
        self.last_candle_start = self.dt_index[-1]
        self.last_candle_end = self.last_candle_start + pd.Timedelta(hours=1)

        # Freeze observation instant to 17:45:00 KST (forming candle active)
        self.frozen_obs_time = self.last_candle_start + pd.Timedelta(minutes=45)
        self.frozen_obs_timestamp = self.frozen_obs_time.tz_localize(KST).timestamp()
        self._time_patcher = patch("time.time", return_value=self.frozen_obs_timestamp)
        self._time_patcher.start()

    def tearDown(self):
        self._time_patcher.stop()
        _CANDLE_CACHE.clear()

    def test_filter_confirmed_candles_excludes_forming_candle(self):
        """관측시각이 마지막 봉 종료시각 미만이면 마지막 미완성봉을 제외해야 합니다."""
        obs_time = self.last_candle_start + pd.Timedelta(minutes=30)
        filtered = filter_confirmed_candles(self.df_confirmed, obs_time=obs_time)
        
        self.assertEqual(len(filtered), self.num_bars - 1)
        self.assertEqual(filtered.index[-1], self.dt_index[-2])

    def test_filter_confirmed_candles_preserves_already_confirmed_data(self):
        """이미 확정된 데이터는 여러 번 필터링해도 마지막 봉이 중복 제거되지 않아야 합니다."""
        obs_time = self.last_candle_start + pd.Timedelta(minutes=30)
        confirmed_once = filter_confirmed_candles(self.df_confirmed, obs_time=obs_time)
        self.assertEqual(len(confirmed_once), self.num_bars - 1)
        
        # 2차 필터링: 이미 확정된 데이터를 동일 관측시각에 재필터링 -> 길이 유지 (멱등성)
        confirmed_twice = filter_confirmed_candles(confirmed_once, obs_time=obs_time)
        self.assertEqual(len(confirmed_twice), len(confirmed_once))
        self.assertEqual(confirmed_twice.index[-1], confirmed_once.index[-1])

    def test_filter_confirmed_candles_synthetic_range_index_preserved(self):
        """Datetime 정보가 없는 합성/테스트 데이터는 이미 확정된 것으로 취급하여 보존해야 합니다."""
        df_synthetic = pd.DataFrame({
            "open": [100, 200, 300],
            "close": [105, 205, 305],
            "high": [110, 210, 310],
            "low": [95, 195, 295],
            "volume": [1, 2, 3]
        })
        filtered = filter_confirmed_candles(df_synthetic)
        self.assertEqual(len(filtered), 3)

    def test_time_boundary_closure(self):
        """정확한 마감 시각 경계에서 마감 전/후 판정이 결정론적으로 동작해야 합니다."""
        # 1초 전 (03:59:59.999): 미완성봉 -> 제외
        just_before = self.last_candle_end - pd.Timedelta(milliseconds=1)
        res_before = filter_confirmed_candles(self.df_confirmed, obs_time=just_before)
        self.assertEqual(len(res_before), self.num_bars - 1)
        
        # 정확히 마감 시각 (04:00:00.000): 확정봉 -> 보존
        exact_close = self.last_candle_end
        res_exact = filter_confirmed_candles(self.df_confirmed, obs_time=exact_close)
        self.assertEqual(len(res_exact), self.num_bars)
        
        # 1초 후 (04:00:01.000): 확정봉 -> 보존
        just_after = self.last_candle_end + pd.Timedelta(seconds=1)
        res_after = filter_confirmed_candles(self.df_confirmed, obs_time=just_after)
        self.assertEqual(len(res_after), self.num_bars)

    def test_forming_candle_wild_fluctuation_invariance(self):
        """진행 중인 미완성봉의 시세/거래량이 극단적으로 급변해도 확정봉 지표 결과는 100% 불변이어야 합니다."""
        obs_time = self.last_candle_start + pd.Timedelta(minutes=45)
        
        # 기본 미완성봉이 포함된 원본 데이터
        base_df = self.df_confirmed.copy()
        
        # Case A: 미완성봉 폭락 (-90% flash crash)
        df_crash = base_df.copy()
        df_crash.loc[self.last_candle_start, "close"] = base_df.loc[self.last_candle_start, "close"] * 0.1
        df_crash.loc[self.last_candle_start, "low"] = base_df.loc[self.last_candle_start, "low"] * 0.05
        df_crash.loc[self.last_candle_start, "volume"] = 1_000_000.0
        
        # Case B: 미완성봉 폭등 (+500% spike)
        df_surge = base_df.copy()
        df_surge.loc[self.last_candle_start, "close"] = base_df.loc[self.last_candle_start, "close"] * 5.0
        df_surge.loc[self.last_candle_start, "high"] = base_df.loc[self.last_candle_start, "high"] * 6.0
        df_surge.loc[self.last_candle_start, "volume"] = 5_000_000.0
        
        # Case C: 미완성봉 횡보
        df_normal = base_df.copy()
        
        # 1. Hybrid Regime 결과 비교 (동일한 obs_time 전달)
        res_normal = get_hybrid_regime_and_signals("KRW-SOL", df=df_normal, obs_time=obs_time)
        res_crash = get_hybrid_regime_and_signals("KRW-SOL", df=df_crash, obs_time=obs_time)
        res_surge = get_hybrid_regime_and_signals("KRW-SOL", df=df_surge, obs_time=obs_time)
        
        self.assertEqual(res_normal["regime"], res_crash["regime"])
        self.assertEqual(res_normal["regime"], res_surge["regime"])
        self.assertEqual(res_normal["signal"], res_crash["signal"])
        self.assertEqual(res_normal["signal"], res_surge["signal"])
        self.assertAlmostEqual(res_normal["curr_price"], res_crash["curr_price"], places=4)
        self.assertAlmostEqual(res_normal["curr_price"], res_surge["curr_price"], places=4)
        self.assertAlmostEqual(res_normal["ma200"], res_crash["ma200"], places=4)
        self.assertAlmostEqual(res_normal["ma200"], res_surge["ma200"], places=4)
        
        # 2. Adaptive Grid NATR 및 200MA 결과 비교 (동일한 obs_time 전달)
        natr_normal = get_market_regime_and_natr(df_normal, obs_time=obs_time)
        natr_crash = get_market_regime_and_natr(df_crash, obs_time=obs_time)
        natr_surge = get_market_regime_and_natr(df_surge, obs_time=obs_time)
        
        self.assertAlmostEqual(natr_normal["curr_natr"], natr_crash["curr_natr"], places=6)
        self.assertAlmostEqual(natr_normal["curr_natr"], natr_surge["curr_natr"], places=6)
        self.assertAlmostEqual(natr_normal["curr_price"], natr_crash["curr_price"], places=4)
        self.assertAlmostEqual(natr_normal["curr_price"], natr_surge["curr_price"], places=4)
        self.assertAlmostEqual(natr_normal["ma200"], natr_crash["ma200"], places=4)
        self.assertAlmostEqual(natr_normal["ma200"], natr_surge["ma200"], places=4)
        self.assertEqual(natr_normal["volatility_regime"], natr_crash["volatility_regime"])
        self.assertEqual(natr_normal["volatility_regime"], natr_surge["volatility_regime"])

    def test_four_paths_contract_consistency(self):
        """정상 조회, 캐시 히트, 조회 실패 폴백, 예외 발생 폴백 모두 동일한 확정봉을 반환해야 합니다."""
        market = "KRW-SOL"
        raw_candles = self.df_confirmed.copy()
        obs_time = self.frozen_obs_time
        
        # Mocking pyupbit.get_ohlcv to return raw_candles with forming candle
        with patch("pyupbit.get_ohlcv", return_value=raw_candles):
            # 1. 정상 조회 (Normal Fetch)
            res1 = get_hybrid_regime_and_signals(market, obs_time=obs_time)
            self.assertIn("ma200", res1)
            self.assertGreater(res1["curr_price"], 0)
            self.assertIn(res1["regime"], ["BULL", "BEAR", "HOLD"])
            
            # Adaptive get_recent_candles 확인 (동일 obs_time)
            candles_adaptive_1 = get_recent_candles(market, obs_time=obs_time)
            self.assertIsNotNone(candles_adaptive_1)
            self.assertEqual(len(candles_adaptive_1), self.num_bars - 1)
            self.assertEqual(candles_adaptive_1.index[-1], self.dt_index[-2])
            
            # 2. 캐시 히트 경로 (Cache Hit, 50초 이내)
            res2 = get_hybrid_regime_and_signals(market, obs_time=obs_time)
            self.assertAlmostEqual(res1["curr_price"], res2["curr_price"], places=4)
            self.assertAlmostEqual(res1["ma200"], res2["ma200"], places=4)
            
            candles_adaptive_2 = get_recent_candles(market, obs_time=obs_time)
            self.assertEqual(len(candles_adaptive_2), self.num_bars - 1)
            self.assertEqual(candles_adaptive_2.index[-1], self.dt_index[-2])
            
        # 3. 조회 실패 폴백 경로 (Fetch Failure: get_ohlcv returns None)
        # 50초 경과 시뮬레이션을 위해 timestamp 조작
        _CANDLE_CACHE[market]["timestamp"] = 0.0
        with patch("pyupbit.get_ohlcv", return_value=None):
            res3 = get_hybrid_regime_and_signals(market, obs_time=obs_time)
            self.assertAlmostEqual(res1["curr_price"], res3["curr_price"], places=4)
            self.assertAlmostEqual(res1["ma200"], res3["ma200"], places=4)
            
            candles_adaptive_3 = get_recent_candles(market, obs_time=obs_time)
            self.assertEqual(len(candles_adaptive_3), self.num_bars - 1)
            self.assertEqual(candles_adaptive_3.index[-1], self.dt_index[-2])
            
        # 4. 예외 발생 폴백 경로 (Exception Fallback: get_ohlcv raises Exception)
        _CANDLE_CACHE[market]["timestamp"] = 0.0
        with patch("pyupbit.get_ohlcv", side_effect=RuntimeError("Upbit API Network Timeout")):
            res4 = get_hybrid_regime_and_signals(market, obs_time=obs_time)
            self.assertAlmostEqual(res1["curr_price"], res4["curr_price"], places=4)
            self.assertAlmostEqual(res1["ma200"], res4["ma200"], places=4)
            
            candles_adaptive_4 = get_recent_candles(market, obs_time=obs_time)
            self.assertEqual(len(candles_adaptive_4), self.num_bars - 1)
            self.assertEqual(candles_adaptive_4.index[-1], self.dt_index[-2])

    def test_missing_hour_gap_preserves_confirmed_candles(self):
        """누락 봉이 있어도 1시간봉은 60분 간격으로 계산되어 정상 마감된 봉을 유지해야 합니다."""
        # 01:00, 03:00 시작 (02:00 누락), 관측시각 04:30
        df = pd.DataFrame(
            {'close': [100.0, 101.0]},
            index=pd.to_datetime(['2026-10-09 01:00:00', '2026-10-09 03:00:00'])
        )
        obs_time = pd.Timestamp('2026-10-09 04:30:00')
        res = filter_confirmed_candles(df, obs_time=obs_time, interval_minutes=60)
        self.assertEqual(len(res), 2)
        self.assertEqual(res.index[-1], pd.Timestamp('2026-10-09 03:00:00'))

    def test_utc_column_interpreted_and_compared_consistently(self):
        """candle_date_time_utc 열은 UTC로 해석되어 KST 관측시각과 일관되게 비교되어야 합니다."""
        # UTC 17:00, 18:00 (Oct 8) -> KST 02:00, 03:00 (Oct 9)
        # 관측시각 KST 03:30 -> KST 02:00봉(03:00 마감)은 확정, KST 03:00봉(04:00 마감)은 미완성
        df = pd.DataFrame({
            'candle_date_time_utc': ['2026-10-08 17:00:00', '2026-10-08 18:00:00'],
            'close': [100.0, 101.0]
        })
        obs_time = pd.Timestamp('2026-10-09 03:30:00')
        res = filter_confirmed_candles(df, obs_time=obs_time, interval_minutes=60)
        self.assertEqual(len(res), 1)
        self.assertEqual(res.iloc[0]['candle_date_time_utc'], '2026-10-08 17:00:00')

    def test_multiple_unclosed_future_candles_filtered(self):
        """마지막 한 행만 제거하는 것이 아니라 관측시각 이후의 모든 미완성/미래 봉을 필터링해야 합니다."""
        # 01:00, 02:00, 03:00, 04:00 봉, 관측시각 02:30
        # 01:00봉(02:00 마감)만 확정이고 02:00, 03:00, 04:00봉은 모두 제외되어야 함
        df = pd.DataFrame(
            {'close': [100.0, 101.0, 102.0, 103.0]},
            index=pd.to_datetime(['2026-10-09 01:00:00', '2026-10-09 02:00:00', '2026-10-09 03:00:00', '2026-10-09 04:00:00'])
        )
        obs_time = pd.Timestamp('2026-10-09 02:30:00')
        res = filter_confirmed_candles(df, obs_time=obs_time, interval_minutes=60)
        self.assertEqual(len(res), 1)
        self.assertEqual(res.index[-1], pd.Timestamp('2026-10-09 01:00:00'))

    def test_invalid_timestamp_raises_and_prevents_signals(self):
        """타임스탬프 오류 시 원본 캔들로 우회 계산하지 않고 오류를 전파하여 신규 주문을 차단해야 합니다."""
        df_invalid = pd.DataFrame({
            'candle_date_time_kst': ['invalid_timestamp'] * 25,
            'open': [100.0] * 24 + [10000.0],
            'high': [100.0] * 24 + [10000.0],
            'low': [100.0] * 24 + [10000.0],
            'close': [100.0] * 24 + [10000.0],
            'volume': [10.0] * 25
        })

        # 1. filter_confirmed_candles는 예외 발생
        with self.assertRaises(ValueError):
            filter_confirmed_candles(df_invalid)

        # 2. Adaptive get_market_regime_and_natr는 원본을 소비하지 않고 curr_price=0.0, regime=HOLD 반환
        res_adaptive = get_market_regime_and_natr(df_invalid)
        self.assertEqual(res_adaptive["curr_price"], 0.0)
        self.assertEqual(res_adaptive["regime"], "HOLD")
        self.assertIsNone(res_adaptive["is_bull"])
        self.assertIn("확정봉 필터링 실패", res_adaptive["reason"])

        # 3. Hybrid get_hybrid_regime_and_signals도 HOLD 및 curr_price=0.0 반환하여 신규 주문 방지
        res_hybrid = get_hybrid_regime_and_signals("KRW-SOL", df=df_invalid)
        self.assertEqual(res_hybrid["curr_price"], 0.0)
        self.assertEqual(res_hybrid["regime"], "HOLD")
        self.assertEqual(res_hybrid["signal"], "HOLD")
        self.assertIn("확정봉 필터링 실패", res_hybrid["reason"])

        # 4. get_recent_candles는 캐시 데이터 오류 시 None 반환
        _CANDLE_CACHE["KRW-SOL"] = {"df": df_invalid, "timestamp": self.frozen_obs_timestamp}
        candles_recent = get_recent_candles("KRW-SOL")
        self.assertIsNone(candles_recent)

    def test_repeated_filtering_idempotence(self):
        """동일 관측시각으로 여러 번 반복 필터링해도 데이터가 점진적으로 유실되지 않아야 합니다."""
        obs_time = self.last_candle_start + pd.Timedelta(minutes=30)
        res1 = filter_confirmed_candles(self.df_confirmed, obs_time=obs_time)
        res2 = filter_confirmed_candles(res1, obs_time=obs_time)
        res3 = filter_confirmed_candles(res2, obs_time=obs_time)
        
        self.assertEqual(len(res1), len(res2))
        self.assertEqual(len(res2), len(res3))
        pd.testing.assert_frame_equal(res1, res3)

    def test_insufficient_data_and_empty_inputs_return_safe_fallbacks(self):
        """데이터 부족 및 빈 데이터프레임 입력 시 예외를 발생시키지 않고 안전한 폴백을 반환해야 합니다."""
        # 1. 빈 데이터프레임
        empty_df = pd.DataFrame()
        filtered_empty = filter_confirmed_candles(empty_df)
        self.assertTrue(filtered_empty.empty)
        
        # 2. None 입력
        self.assertIsNone(filter_confirmed_candles(None))
        
        # 3. 데이터 부족한 5개 봉
        short_df = self.df_confirmed.iloc[:5].copy()
        res_short = get_hybrid_regime_and_signals("KRW-SOL", df=short_df)
        self.assertEqual(res_short["regime"], "HOLD")
        self.assertIn("부족", res_short["reason"])
        
        # 4. Adaptive Grid 데이터 부족 (<20봉)
        res_adaptive_short = get_market_regime_and_natr(short_df)
        self.assertFalse(res_adaptive_short.get("candles_valid", True))
        self.assertEqual(res_adaptive_short["regime"], "HOLD")
        self.assertIsNone(res_adaptive_short["curr_natr"])
        self.assertEqual(res_adaptive_short["volatility_regime"], "INVALID")

    def test_validate_candles_numerical_rejects_nan_inf_and_inverted_prices(self):
        """수치 결측, 무한대, 음수/0 가격, 고가<저가 역전 등 비정상 캔들은 명시적 invalid로 처리해야 합니다."""
        base_df = self.df_confirmed.copy()

        # 1. high/low NaN 주입
        df_nan = base_df.copy()
        df_nan.loc[:, ["high", "low"]] = float("nan")
        with self.assertRaises(ValueError):
            filter_confirmed_candles(df_nan, obs_time=self.frozen_obs_time)
        res_nan_h = get_hybrid_regime_and_signals("KRW-SOL", df=df_nan, obs_time=self.frozen_obs_time)
        self.assertFalse(res_nan_h["candles_valid"])
        self.assertEqual(res_nan_h["regime"], "HOLD")
        res_nan_a = get_market_regime_and_natr(df_nan, obs_time=self.frozen_obs_time)
        self.assertFalse(res_nan_a["candles_valid"])
        self.assertIsNone(res_nan_a["curr_natr"])

        # 2. close 무한대(inf) 주입 (확정된 봉에 주입)
        df_inf = base_df.copy()
        df_inf.loc[df_inf.index[0], "close"] = float("inf")
        with self.assertRaises(ValueError):
            filter_confirmed_candles(df_inf, obs_time=self.frozen_obs_time)

        # 3. 0 이하 비양수 가격 주입
        df_neg = base_df.copy()
        df_neg.loc[df_neg.index[0], "close"] = -100.0
        with self.assertRaises(ValueError):
            filter_confirmed_candles(df_neg, obs_time=self.frozen_obs_time)

        # 4. 고가(high) < 저가(low) 역전 주입
        df_inv = base_df.copy()
        df_inv.loc[df_inv.index[0], "high"] = 50.0
        df_inv.loc[df_inv.index[0], "low"] = 150.0
        with self.assertRaises(ValueError):
            filter_confirmed_candles(df_inv, obs_time=self.frozen_obs_time)

        # 5. 거래량 음수 주입
        df_neg_vol = base_df.copy()
        df_neg_vol.loc[df_neg_vol.index[0], "volume"] = -1.0
        with self.assertRaises(ValueError):
            filter_confirmed_candles(df_neg_vol, obs_time=self.frozen_obs_time)

    def test_zero_volatility_flat_price_produces_valid_regime_and_natr_zero(self):
        """정상적인 0변동성(평탄 가격) 데이터는 올바르게 유효(candles_valid=True)로 판정되고 NATR 0.0을 산출해야 합니다."""
        n_bars = 220
        dts = [
            (self.frozen_obs_time - pd.Timedelta(hours=n_bars - i)).strftime("%Y-%m-%d %H:%M:%S")
            for i in range(n_bars)
        ]
        flat_df = pd.DataFrame({
            "candle_date_time_kst": dts,
            "open": [100.0] * n_bars,
            "high": [100.0] * n_bars,
            "low": [100.0] * n_bars,
            "close": [100.0] * n_bars,
            "volume": [10.0] * n_bars
        })

        filtered = filter_confirmed_candles(flat_df, obs_time=self.frozen_obs_time)
        self.assertEqual(len(filtered), n_bars)

        res_a = get_market_regime_and_natr(flat_df, obs_time=self.frozen_obs_time)
        self.assertTrue(res_a["candles_valid"])
        self.assertEqual(res_a["curr_natr"], 0.0)

        # 0변동성 시 calculate_adaptive_buy_prices는 최소 간격(min_drop_pct=0.006)으로 정상 계산
        from strategy.adaptive_grid import calculate_adaptive_buy_prices
        orders = calculate_adaptive_buy_prices(base_price=100.0, natr=0.0, num_orders=3)
        self.assertEqual(len(orders), 3)
        self.assertLess(orders[0]["price"], 100.0)


if __name__ == "__main__":
    unittest.main()
