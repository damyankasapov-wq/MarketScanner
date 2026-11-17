"""alpaca_feed: request shape, pagination, 429 retry, regular-session filter (no network)."""
from datetime import date

import pytest

from marketscanner.data import alpaca_feed, backfill


from tests.alpaca_fixtures import Resp, Session
from tests.alpaca_fixtures import bar as _bar


def test_requires_keys():
    with pytest.raises(alpaca_feed.AlpacaError):
        alpaca_feed.fetch_range("SPY", date(2025, 1, 6), date(2025, 1, 7), session=Session([]))


def test_request_shape_pagination_and_session_filter(alpaca_keys):
    page1 = {"bars": [_bar("2025-01-06T14:29:00Z"), _bar("2025-01-06T14:30:00Z")], "next_page_token": "p2"}
    page2 = {"bars": [_bar("2025-01-06T20:59:00Z", 501.0), _bar("2025-01-06T21:00:00Z")], "next_page_token": None}
    http = Session([Resp(200, page1), Resp(200, page2)])
    df = alpaca_feed.fetch_range("SPY", date(2025, 1, 6), date(2025, 1, 7), session=http)

    url, headers, params = http.calls[0]
    assert url.endswith("/v2/stocks/SPY/bars") and headers["APCA-API-KEY-ID"] == "kid"
    assert params["timeframe"] == "1Min" and params["adjustment"] == "raw" and params["feed"] == "sip"
    assert params["start"].startswith("2025-01-06T05:00:00") and params["end"].startswith("2025-01-07T05:00:00")
    assert "page_token" not in params and http.calls[1][2]["page_token"] == "p2"
    # 09:29 and 16:00 ET are outside the regular session
    assert [t.strftime("%H:%M") for t in df.index] == ["14:30", "20:59"]
    assert list(df.columns) == ["open", "high", "low", "close", "volume"] and str(df.index.tz) == "UTC"
    assert df["close"].iloc[-1] == 501.0


def test_retries_on_rate_limit(alpaca_keys):
    slept = []
    http = Session([Resp(429, headers={"Retry-After": "3"}), Resp(200, {"bars": [_bar("2025-01-06T15:00:00Z")]})])
    df = alpaca_feed.fetch_range("SPY", date(2025, 1, 6), date(2025, 1, 7), session=http, sleep=slept.append)
    assert slept == [3.0] and len(df) == 1


def test_http_error_raises(alpaca_keys):
    with pytest.raises(alpaca_feed.AlpacaError, match="403"):
        alpaca_feed.fetch_range("SPY", date(2025, 1, 6), date(2025, 1, 7), session=Session([Resp(403, {"message": "forbidden"})]))


def test_empty_response_is_empty_frame(alpaca_keys):
    df = alpaca_feed.fetch_range("SPY", date(2025, 1, 1), date(2025, 1, 2), session=Session([Resp(200, {"bars": None})]))
    assert df.empty


def test_keys_enable_deep_backfill(alpaca_keys):
    assert backfill.default_deep_fetch() is alpaca_feed.fetch_range
