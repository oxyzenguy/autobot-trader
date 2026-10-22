"""Offline discovery/filter/ownership tests; never contact the exchange."""
import sys
import unittest
if sys.version_info < (3, 12):
    raise unittest.SkipTest("Separate multiagent runtime requires Python 3.12")
from datetime import datetime, timezone
import json
from unittest.mock import patch
from pathlib import Path
from tempfile import TemporaryDirectory
from contextlib import redirect_stdout
from io import StringIO
from pydantic import ValidationError
from ma_trader.feed import PublicFeed
from ma_trader.settings import Limits, Settings, UniverseSettings
from ma_trader.universe import TradingUniverse
from ma_trader.broker import BrokerUncertain
from ma_trader.demo import paper_settings
from ma_trader.ledger import Ledger


class PublicFake:
    def __init__(self):
        self.now = 1791909000.
        self.calls = []
        self.bad_discovery = False
        self.missing_ticker = False
        self.reverse_rank = False
        self.bad_cost = set()
        self.signal = False
        self.slippage = set()
        self.pairs = [{"market": m, "market_event": {"warning": False, "caution": {"PRICE_FLUCTUATIONS": False}}}
                      for m in ("KRW-BTC", "KRW-ETH", "KRW-SOL", "KRW-USDT") + tuple(f"KRW-C{i:02}" for i in range(25))]

    def call(self, method, request):
        path, params = request
        self.calls.append((method, path, params.copy()))
        if path == "/v1/market/all":
            return "not-json" if self.bad_discovery else json.dumps(self.pairs)
        markets = params.get("markets", params.get("market", "")).split(",")
        if path == "/v1/ticker":
            rows = [{"market": m, "acc_trade_price_24h": (2e10 + int(m[-2:]) * 1e8 if self.reverse_rank else 2e10 - int(m[-2:]) * 1e8) if m.startswith("KRW-C") else 1e12} for m in markets]
            return json.dumps(rows[:-1] if self.missing_ticker else rows)
        if path == "/v1/orderbook":
            return json.dumps([{"market": m, "timestamp": self.now * 1000,
                "orderbook_units": ([{"bid_price":1000,"ask_price":1001,"ask_size":1},{"bid_price":1000,"ask_price":1010,"ask_size":10000}]
                                    if m in self.slippage else [{"bid_price": 1000, "ask_price": 1010 if m in self.bad_cost else 1001, "ask_size": 10000}])} for m in markets])
        if path == "/v1/candles/minutes/60":
            return json.dumps([{"candle_date_time_utc": datetime.fromtimestamp(self.now-i*3600,timezone.utc).isoformat(),
                "trade_price": 997 if self.signal and i==1 else 1000 + i % 2} for i in range(1,31)])
        raise AssertionError(path)


class UniverseTests(unittest.TestCase):
    def setUp(self):
        self.http = PublicFake()
        self.settings = Settings(limits=Limits(max_order_krw=300000,allowed_markets=()), universe=UniverseSettings(enabled=True))

    def test_rank_by_krw_funds_after_exclusions_and_warning_filter(self):
        # Given: high-turnover protected/stable assets and warned/delisting markets.
        self.http.pairs[4]["market_event"]["warning"] = True
        self.http.pairs[5]["market_event"]["caution"]["PRICE_FLUCTUATIONS"] = True
        self.http.pairs[6]["delisting_date"] = "2026-10-20"
        del self.http.pairs[7]["market_event"]
        universe = TradingUniverse(self.http)
        # When: the deterministic ranking is refreshed.
        with patch("ma_trader.universe.time.sleep"):
            selected = universe.current(self.settings,self.http.now)
        # Then: exclusions occur before the top20 cutoff and never receive ownership.
        self.assertEqual(tuple(f"KRW-C{i:02}" for i in range(4,24)), selected)
        self.assertEqual(1, universe.exclusions["stablecoin"])

    def test_refresh_holds_for_30_minutes_then_replaces_ranking(self):
        # Given: a successful ranking and later reversed trading amounts.
        universe=TradingUniverse(self.http)
        with patch("ma_trader.universe.time.sleep"):
            before=universe.current(self.settings,self.http.now)
            self.http.reverse_rank=True
            # When: read before and at the scheduled refresh boundary.
            held=universe.current(self.settings,self.http.now+1799)
            after=universe.current(self.settings,self.http.now+1800)
        # Then: no per-tick ranking churn occurs.
        self.assertEqual(before,held)
        self.assertEqual("KRW-C24",after[0])
        self.assertEqual(2,sum(path=="/v1/market/all" for _,path,_ in self.http.calls))

    def test_filters_backfill_until_ten_analysis_markets_with_batched_books(self):
        # Given: the highest-ranked two markets have prohibitive spreads.
        self.http.bad_cost={"KRW-C00","KRW-C01"}
        feed=PublicFeed(self.settings,self.http)
        # When: the public scan runs.
        with patch("ma_trader.feed.time.sleep"), patch("ma_trader.feed.time.time",return_value=self.http.now):
            snapshots=feed.read(self.http.now)
        # Then: lower-ranked candidates replace them, within the top20 pool.
        self.assertEqual(tuple(f"KRW-C{i:02}" for i in range(2,12)),feed.analysis_markets)
        self.assertEqual(10,len(snapshots))
        self.assertEqual(feed.analysis_markets,feed.execution_settings().limits.allowed_markets)
        self.assertEqual(1,sum(path=="/v1/orderbook" for _,path,_ in self.http.calls))

    def test_removed_or_excluded_owned_asset_remains_observed(self):
        # Given: a protected-name asset already owned by the separate paper ledger.
        feed=PublicFeed(self.settings,self.http)
        self.http.bad_cost={"KRW-ETH"}
        # When: discovery excludes ETH from new entry but the ledger requests management.
        with patch("ma_trader.feed.time.sleep"),patch("ma_trader.feed.time.time",return_value=self.http.now):
            snapshots=feed.read(self.http.now,("KRW-ETH",))
        # Then: owned quotes are preserved without extending entry permissions.
        self.assertIn("KRW-ETH",{s.market for s in snapshots})
        self.assertNotIn("KRW-ETH",feed.execution_settings().limits.allowed_markets)

    def test_depth_slippage_excludes_market_without_relaxing_limit(self):
        # Given: a narrow best spread but expensive depth for the full order.
        self.http.slippage={"KRW-C00"}
        feed=PublicFeed(self.settings,self.http)
        # When: execution cost is calculated over the requested amount.
        with patch("ma_trader.feed.time.sleep"),patch("ma_trader.feed.time.time",return_value=self.http.now):
            feed.read(self.http.now)
        # Then: rank alone cannot authorize a costly entry.
        self.assertNotIn("KRW-C00",feed.analysis_markets)
        self.assertEqual(.002,feed.execution_settings().limits.max_slippage)

    def test_discovery_failure_does_not_stop_owned_quote_management(self):
        # Given: metadata is unavailable but owned-asset quotes are still readable.
        self.http.bad_discovery=True
        feed=PublicFeed(self.settings,self.http)
        # When: the scan runs after a restart.
        with patch("ma_trader.feed.time.sleep"),patch("ma_trader.feed.time.time",return_value=self.http.now):
            snapshots=feed.read(self.http.now,("KRW-XRP",))
        # Then: no entry candidates are authorized and management retains its quote.
        self.assertEqual(("KRW-XRP",),tuple(s.market for s in snapshots))
        self.assertEqual((),feed.analysis_markets)
        self.assertIn("universe",feed.errors)

    def test_missing_batch_ticker_cannot_silently_authorize_partial_ranking(self):
        # Given: a malformed discovery response omits one requested market.
        self.http.missing_ticker=True
        universe=TradingUniverse(self.http)
        # When/Then: ranking fails closed and no stale candidates survive.
        with self.assertRaises(BrokerUncertain):
            universe.current(self.settings,self.http.now)
        self.assertEqual((),universe.candidates)

    def test_failed_policy_refresh_recovers_after_retry_cooldown(self):
        # Given: a cached ranking and newly excluded market.
        universe=TradingUniverse(self.http)
        with patch("ma_trader.universe.time.sleep"):
            universe.current(self.settings,self.http.now)
            changed=self.settings.model_copy(update={"limits":self.settings.limits.model_copy(update={"excluded_markets":("KRW-C00",)})})
            self.http.bad_discovery=True
            with self.assertRaises(ValidationError):
                universe.current(changed,self.http.now+1)
            # When: valid discovery returns after the bounded retry delay.
            self.http.bad_discovery=False
            refreshed=universe.current(changed,self.http.now+31)
        # Then: a failed refresh cannot masquerade as a valid 30-minute empty cache.
        self.assertNotIn("KRW-C00",refreshed)
        self.assertTrue(refreshed)

    def test_dynamic_expansion_is_rejected_in_live_mode(self):
        # Given/When: an operator attempts to activate unqualified dynamic live markets.
        with self.assertRaises(ValidationError):
            Settings(mode="live",universe=UniverseSettings(enabled=True))
        # Then: existing static live ownership/locking contracts cannot be bypassed.

    def test_real_cli_can_trade_discovered_market_outside_old_allowlist(self):
        # Given: an empty static allowlist and an eligible discovered range signal.
        from ma_trader import __main__ as cli
        self.http.signal=True
        with TemporaryDirectory() as root:
            settings=paper_settings(root).model_copy(update={"limits":paper_settings(root).limits.model_copy(update={"allowed_markets":()}),"universe":UniverseSettings(enabled=True)})
            path=Path(root)/"settings.json"
            path.write_text(settings.model_dump_json())
            # When: the actual one-cycle CLI scans, chooses, approves and fills.
            with patch.object(cli,"UpbitHTTP",return_value=self.http), patch("time.time",return_value=self.http.now),patch("time.sleep"),redirect_stdout(StringIO()):
                cli.run(path,once=True)
            # Then: the deterministic runtime allowlist permits only a discovered asset.
            ledger=Ledger(Path(root)/"portfolio.sqlite3",settings)
            self.assertEqual(("KRW-C00",),tuple(p.market for p in ledger.positions()))


if __name__=="__main__":
    unittest.main()
