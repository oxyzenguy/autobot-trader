"""Offline public-read recovery; private mutations must never be replayed."""
import sys
import unittest

if sys.version_info < (3, 12):
    raise unittest.SkipTest("Separate multiagent runtime requires Python 3.12")

import requests
from requests.adapters import BaseAdapter
from unittest.mock import patch

from ma_trader.broker import BrokerUncertain
from ma_trader.feed import PublicFeed
from ma_trader.settings import Limits, Settings
from ma_trader.upbit_http import UpbitHTTP
from test_ma_feed import FakeHTTP


class ScriptedWire(BaseAdapter):
    """Requests transport fixture, with observable real prepared HTTP requests."""

    def __init__(self, outcomes):
        self.outcomes = iter(outcomes)
        self.calls = []

    def send(self, request, **kwargs):
        self.calls.append((request.method, request.url))
        outcome = next(self.outcomes)
        if isinstance(outcome, requests.RequestException):
            raise outcome
        response = requests.Response()
        response.status_code = outcome
        response._content = b'[]'
        response.headers['Retry-After'] = '1'
        response.request = request
        return response

    def close(self):
        self.outcomes = iter(())


class PublicRecoveryTests(unittest.TestCase):
    def transport(self, outcomes):
        http = UpbitHTTP()
        self.addCleanup(http.session.close)
        wire = ScriptedWire(outcomes)
        http.session.mount('https://', wire)
        http.access, http.secret = 'test-only', 'test-only'
        return http, wire

    def test_public_get_recovers_from_temporary_failure(self):
        # Given: one temporary HTTP or transport failure then a valid response.
        for failure in (429, 502, 503, 504, requests.ReadTimeout('offline')):
            with self.subTest(failure=failure):
                http, wire = self.transport([failure, 200])
                # When: the public endpoint is read once by its caller.
                with patch('time.sleep'):
                    result = http.call('GET', ('/v1/ticker', {'markets': 'KRW-XRP'}))
                # Then: the safe read recovers without involving the order API.
                self.assertEqual('[]', result)
                self.assertEqual(2, len(wire.calls))

    def test_retries_are_bounded_and_preserve_http_status(self):
        # Given: repeated gateway errors.
        http, wire = self.transport([502] * 4)
        # When: recovery is exhausted.
        with patch('time.sleep'), self.assertRaises(BrokerUncertain) as caught:
            http.call('GET', ('/v1/orderbook', {'markets': 'KRW-XRP'}))
        # Then: the caller gets a diagnosable failure, not stale success.
        self.assertEqual(3, len(wire.calls))
        self.assertEqual(502, caught.exception.status_code)
        self.assertEqual('/v1/orderbook', caught.exception.path)

    def test_private_requests_and_permanent_public_failures_are_not_retried(self):
        # Given: an order mutation, private read, ban or invalid public request.
        for method, path, status in (
            ('POST', '/v1/orders', 503), ('DELETE', '/v1/order', 503),
            ('GET', '/v1/order', 503), ('GET', '/v1/ticker', 400),
            ('GET', '/v1/ticker', 418),
        ):
            with self.subTest(method=method, path=path, status=status):
                http, wire = self.transport([status, 200])
                # When: a request fails.
                with self.assertRaises(BrokerUncertain):
                    http.call(method, (path, {}))
                # Then: no ambiguous execution is replayed and bans are respected.
                self.assertEqual(1, len(wire.calls))

    def test_malformed_feed_response_exposes_reason_without_payload(self):
        # Given: malformed public data for one market.
        feed = PublicFeed(Settings(limits=Limits(allowed_markets=('KRW-XRP',))),
                          FakeHTTP(1700002800, bad_markets=('KRW-XRP',)))
        # When: response parsing fails.
        feed.read(1700002800)
        # Then: diagnostics identify the failure without copying response data.
        self.assertEqual(('KRW-XRP',), feed.errors)
        detail = feed.error_details[0]
        self.assertEqual('KRW-XRP', detail.scope)
        self.assertEqual('ValidationError', detail.error_type)
        self.assertEqual('invalid_response', detail.reason)

    def test_cli_logs_diagnostic_fields_for_discovery_failure(self):
        # Given: malformed discovery metadata in a fresh isolated paper account.
        from contextlib import redirect_stdout
        from io import StringIO
        from pathlib import Path
        from tempfile import TemporaryDirectory
        import json
        from ma_trader import __main__ as cli
        from ma_trader.demo import paper_settings
        from ma_trader.settings import UniverseSettings
        from test_ma_universe import PublicFake
        http = PublicFake()
        http.bad_discovery = True
        with TemporaryDirectory() as root:
            settings = paper_settings(root).model_copy(update={'universe': UniverseSettings(enabled=True)})
            path = Path(root) / 'settings.json'
            path.write_text(settings.model_dump_json())
            output = StringIO()
            # When: the real CLI handles the failed public read.
            with patch.object(cli, 'UpbitHTTP', return_value=http), patch('time.time', return_value=http.now), redirect_stdout(output):
                cli.run(path, once=True)
            # Then: its event identifies the failed scope and parsing category.
            events = [json.loads(line) for line in output.getvalue().splitlines()]
            failure = next(e['payload'] for e in events if e['event'] == 'feed_errors')
            self.assertEqual(['universe'], failure['markets'])
            self.assertEqual('invalid_response', failure['details'][0]['reason'])

    def test_cooldown_keeps_entries_blocked_without_duplicate_error_event(self):
        # Given: a failed discovery followed by its 30-second retry cooldown.
        from contextlib import redirect_stdout
        from io import StringIO
        from pathlib import Path
        from tempfile import TemporaryDirectory
        import json
        from ma_trader import __main__ as cli
        from ma_trader.demo import paper_settings
        from ma_trader.settings import UniverseSettings
        from test_ma_universe import PublicFake
        http = PublicFake()
        http.bad_discovery = True
        with TemporaryDirectory() as root:
            settings = paper_settings(root).model_copy(update={'universe': UniverseSettings(enabled=True)})
            feed = PublicFeed(settings, http)
            feed.read(http.now)
            path = Path(root) / 'settings.json'
            path.write_text(settings.model_dump_json())
            output = StringIO()
            # When: the CLI ticks before another actual request is allowed.
            with patch.object(cli, 'PublicFeed', return_value=feed), patch.object(cli, 'UpbitHTTP', return_value=http), patch('time.time', return_value=http.now + 5), redirect_stdout(output):
                cli.run(path, once=True)
            # Then: waiting is not falsely counted as a new API failure.
            self.assertEqual(('universe',), feed.errors)
            self.assertEqual('retry_cooldown', feed.error_details[0].reason)
            self.assertFalse(any(json.loads(line)['event'] == 'feed_errors' for line in output.getvalue().splitlines()))

    def test_feed_preserves_endpoint_and_status_when_public_retries_exhausted(self):
        # Given: a public ticker endpoint persistently unavailable.
        http, wire = self.transport([503] * 3)
        feed = PublicFeed(Settings(limits=Limits(allowed_markets=('KRW-XRP',))), http)
        # When: the feed consumes the transport failure.
        with patch('time.sleep'):
            snapshots = feed.read(1700002800)
        # Then: diagnostics survive every layer while no stale quote is returned.
        self.assertEqual((), snapshots)
        self.assertEqual(3, len(wire.calls))
        self.assertEqual(503, feed.error_details[0].status_code)
        self.assertEqual('/v1/ticker', feed.error_details[0].path)


if __name__ == '__main__':
    unittest.main()
