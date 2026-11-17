"""Regression: ISSUE-002 - one transient Alpaca 5xx dropped a whole month of sessions.
Found by /qa on 2026-11-17
Report: .gstack/qa-reports/qa-report-backtest-cli-2026-11-17.md
"""
from datetime import date

import pytest

from marketscanner.data import alpaca_feed
from tests.alpaca_fixtures import Resp, Session
from tests.alpaca_fixtures import bar as _bar


pytestmark = pytest.mark.usefixtures("alpaca_keys")


def test_transient_server_error_is_retried_with_backoff():
    slept = []
    http = Session([Resp(502), Resp(503), Resp(200, {"bars": [_bar("2025-01-06T15:00:00Z")]})])
    df = alpaca_feed.fetch_range("SPY", date(2025, 1, 6), date(2025, 1, 7), session=http, sleep=slept.append)
    assert len(df) == 1 and slept == [1.0, 2.0]


def test_persistent_server_error_gives_up_after_max_retries():
    slept = []
    http = Session([Resp(500, {"message": "down"})] * (alpaca_feed.MAX_RETRIES + 1))
    with pytest.raises(alpaca_feed.AlpacaError, match="HTTP 500"):
        alpaca_feed.fetch_range("SPY", date(2025, 1, 6), date(2025, 1, 7), session=http, sleep=slept.append)
    assert len(slept) == alpaca_feed.MAX_RETRIES


def test_client_error_is_not_retried():
    slept = []
    with pytest.raises(alpaca_feed.AlpacaError, match="401"):
        alpaca_feed.fetch_range("SPY", date(2025, 1, 6), date(2025, 1, 7), session=Session([Resp(401)]), sleep=slept.append)
    assert slept == []
