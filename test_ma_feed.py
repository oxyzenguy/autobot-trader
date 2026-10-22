"""Public-feed response boundary tests; no exchange requests or credentials."""
import sys
import unittest
if sys.version_info < (3, 12):
    raise unittest.SkipTest("Separate multiagent runtime requires Python 3.12")
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import unittest
from unittest.mock import patch

from ma_trader.feed import PublicFeed
from ma_trader.settings import Limits, Settings


@dataclass(frozen=True, slots=True)
class FakeHTTP:
    now: float
    bad_markets: tuple[str, ...] = ()
    mismatch_markets: tuple[str, ...] = ()

    def call(self, method: str, request: tuple[str, dict[str, str]]) -> str:
        # Typed dictionary matches the existing transport contract exactly.
        path, params = request
        market = params.get("markets", params.get("market", ""))
        if market in self.bad_markets:
            return "not-json"
        response_market = "KRW-WRONG" if market in self.mismatch_markets else market
        responses = {
            "/v1/ticker": [{"market": market, "acc_trade_price_24h": 2000000000}],
            "/v1/orderbook": [{"market": response_market, "timestamp": self.now * 1000,
                                  "orderbook_units": [{"ask_price": 1001, "bid_price": 1000, "ask_size": 10000}]}],
            "/v1/candles/minutes/60": [{"candle_date_time_utc": datetime.fromtimestamp(self.now - i * 3600, timezone.utc).isoformat(),
                                         "trade_price": 1000 + i % 2} for i in range(1, 31)]}
        return json.dumps(responses[path])


class FeedTests(unittest.TestCase):
    def test_excluded_owned_market_remains_observable_for_exit_management(self):
        # Given
        settings = Settings(limits=Limits(allowed_markets=("KRW-XRP",), excluded_markets=("KRW-ETH",)))
        feed = PublicFeed(settings, FakeHTTP(1700002800))
        # When
        with patch("ma_trader.feed.time.sleep"):
            snapshots = feed.read(1700002800, managed_markets=("KRW-ETH",))
        # Then
        self.assertEqual({s.market for s in snapshots}, {"KRW-XRP", "KRW-ETH"})

    def test_bad_market_response_does_not_discard_healthy_market(self):
        # Given
        settings = Settings(limits=Limits(allowed_markets=("KRW-XRP", "KRW-DOGE"), excluded_markets=()))
        feed = PublicFeed(settings, FakeHTTP(1700002800, bad_markets=("KRW-XRP",)))
        # When
        with patch("ma_trader.feed.time.sleep"):
            snapshots = feed.read(1700002800)
        # Then
        self.assertEqual(tuple(s.market for s in snapshots), ("KRW-DOGE",))
        self.assertEqual(feed.errors, ("KRW-XRP",))

    def test_cross_market_book_is_rejected(self):
        # Given
        settings = Settings(limits=Limits(allowed_markets=("KRW-XRP",), excluded_markets=()))
        feed = PublicFeed(settings, FakeHTTP(1700002800, mismatch_markets=("KRW-XRP",)))
        # When
        snapshots = feed.read(1700002800)
        # Then
        self.assertEqual(snapshots, ())
        self.assertEqual(feed.errors, ("KRW-XRP",))
