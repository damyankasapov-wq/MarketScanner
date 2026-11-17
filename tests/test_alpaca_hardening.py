"""alpaca_feed hardening found by /ship review (2026-11-17): fatal errors, network
retries, pagination guard, Retry-After parsing, non-JSON bodies, URL quoting,
session lifecycle, summer time, duplicate bars."""
from datetime import date

import pandas as pd
import pytest
import requests

from marketscanner import config
from marketscanner.data import alpaca_feed
from tests.alpaca_fixtures import Resp, Session, bar

pytestmark = pytest.mark.usefixtures("alpaca_keys")
JAN6, JAN7 = date(2025, 1, 6), date(2025, 1, 7)


def _fetch(http, sleep=None, start=JAN6, end=JAN7, symbol="SPY"):
    return alpaca_feed.fetch_range(symbol, start, end, session=http, sleep=sleep or (lambda s: None))


@pytest.mark.parametrize("status", [401, 403])
def test_auth_errors_are_fatal_and_not_retried(status):
    slept = []
    with pytest.raises(alpaca_feed.AlpacaFatalError, match=str(status)):
        _fetch(Session([Resp(status, {"message": "forbidden"})]), sleep=slept.append)
    assert slept == []


def test_only_sip_feed_is_accepted(monkeypatch):
    monkeypatch.setattr(config, "ALPACA_DATA_FEED", "iex")
    http = Session([])
    with pytest.raises(alpaca_feed.AlpacaFatalError, match="only 'sip'"):
        _fetch(http)
    assert http.calls == []


def test_missing_keys_is_fatal(monkeypatch):
    monkeypatch.setattr(config, "ALPACA_API_KEY_ID", "")
    with pytest.raises(alpaca_feed.AlpacaFatalError):
        _fetch(Session([]))


def test_network_errors_are_retried_like_server_errors():
    slept = []
    http = Session([requests.ConnectionError("reset"), requests.Timeout("slow"),
                    Resp(200, {"bars": [bar("2025-01-06T15:00:00Z")]})])
    assert len(_fetch(http, sleep=slept.append)) == 1 and slept == [1.0, 2.0]


def test_persistent_network_error_gives_up_with_the_cause():
    http = Session([requests.ConnectionError("reset")] * (alpaca_feed.MAX_RETRIES + 1))
    with pytest.raises(alpaca_feed.AlpacaError, match="gave up after 6 attempts.*network error"):
        _fetch(http)


def test_persistent_rate_limit_names_http_429():
    with pytest.raises(alpaca_feed.AlpacaError, match="HTTP 429"):
        _fetch(Session([Resp(429)] * (alpaca_feed.MAX_RETRIES + 1)))


@pytest.mark.parametrize("header,expected", [("Wed, 21 Oct 2026 07:28:00 GMT", 1.0), ("86400", 65.0), ("-5", 1.0), ("7", 7.0)])
def test_retry_after_is_parsed_safely_and_clamped(header, expected):
    slept = []
    _fetch(Session([Resp(429, headers={"Retry-After": header}), Resp(200, {"bars": []})]), sleep=slept.append)
    assert slept == [expected]


def test_repeated_page_token_raises_instead_of_looping():
    page = {"bars": [bar("2025-01-06T15:00:00Z")], "next_page_token": "same"}
    with pytest.raises(alpaca_feed.AlpacaError, match="pagination loop"):
        _fetch(Session([Resp(200, page), Resp(200, page)]))


def test_page_count_is_capped(monkeypatch):
    monkeypatch.setattr(alpaca_feed, "MAX_PAGES", 3)
    pages = [Resp(200, {"bars": [], "next_page_token": f"t{i}"}) for i in range(3)]
    with pytest.raises(alpaca_feed.AlpacaError, match="more than 3 pages"):
        _fetch(Session(pages))


def test_non_json_200_is_a_clear_error():
    with pytest.raises(alpaca_feed.AlpacaError, match="not JSON"):
        _fetch(Session([Resp(200, "<html>portal</html>", headers={"Content-Type": "text/html"}, json_error=True)]))


def test_symbol_is_url_quoted():
    http = Session([Resp(200, {"bars": []})])
    _fetch(http, symbol="BRK/B?x=1")
    assert http.calls[0][0].endswith("/v2/stocks/BRK%2FB%3Fx%3D1/bars")


def test_own_session_is_closed(monkeypatch):
    made = []

    class Tracked(Session):
        closed = False
        def __enter__(self): return self
        def __exit__(self, *a): self.closed = True

    def factory():
        made.append(Tracked([Resp(200, {"bars": []})]))
        return made[-1]

    monkeypatch.setattr(alpaca_feed.requests, "Session", factory)
    alpaca_feed.fetch_range("SPY", JAN6, JAN7)
    assert len(made) == 1 and made[0].closed and len(made[0].calls) == 1


def test_summer_dates_use_edt_midnight():
    http = Session([Resp(200, {"bars": []})])
    _fetch(http, start=date(2025, 7, 7), end=date(2025, 7, 8))
    params = http.calls[0][2]
    assert params["start"].startswith("2025-07-07T04:00:00") and params["end"].startswith("2025-07-08T04:00:00")


def test_duplicate_and_unsorted_bars_are_cleaned():
    df = alpaca_feed.bars_to_frame([bar("2025-01-06T15:01:00Z", 2.0), bar("2025-01-06T15:00:00Z", 1.0),
                                    bar("2025-01-06T15:01:00Z", 3.0)])
    assert list(df.index) == [pd.Timestamp("2025-01-06 15:00", tz="UTC"), pd.Timestamp("2025-01-06 15:01", tz="UTC")]
    assert df["close"].tolist() == [1.0, 3.0]
