"""
Dashboard endpoints render from the snapshot of the feed's frame — no DB,
no strategy memory.
"""
import pandas as pd
import pytest

from marketscanner.strategies import orb_session
from marketscanner.web import server
from tests.orb_fixtures import C_GREEN_ABOVE, ORB_BARS, TRAIL, bar, df_of, flat, utc


class FakeFeed:
    def __init__(self, frames):
        self.frames = frames

    def get_df(self, symbol):
        return self.frames.get(symbol, pd.DataFrame())

    def newest_bar_time(self, symbol):
        df = self.get_df(symbol)
        return None if df.empty else df.index[-1].to_pydatetime()


@pytest.fixture
def client(monkeypatch):
    frames = {
        "SPY": df_of(ORB_BARS + C_GREEN_ABOVE + TRAIL + [bar("2024-11-06 15:40:00+00:00", 586.0, 587.0, 585.9, 586.5)]),
        "QQQ": df_of(ORB_BARS + flat("2024-11-06 15:30", 1)),
        "GLD": pd.DataFrame(),
    }
    monkeypatch.setattr(server, "_state", {"symbols": ["SPY", "QQQ", "GLD"], "feed": FakeFeed(frames)})
    server.app.config["TESTING"] = True
    return server.app.test_client()


def test_index_lists_symbols_and_status_slots(client):
    html = client.get("/").get_data(as_text=True)
    for sym in ("SPY", "QQQ", "GLD"):
        assert f'id="status-{sym}"' in html and f"/chart/{sym}" in html
    assert "/status/" in html


def test_chart_png_from_snapshot(client):
    r = client.get("/chart/spy")
    assert r.status_code == 200 and r.mimetype == "image/png"
    assert r.data[:8] == b"\x89PNG\r\n\x1a\n" and len(r.data) > 5_000
    assert r.headers["Cache-Control"].startswith("no-cache")


def test_chart_placeholder_when_no_data(client):
    r = client.get("/chart/GLD")
    assert r.status_code == 200 and r.data[:8] == b"\x89PNG\r\n\x1a\n"


def test_unknown_symbol(client):
    assert client.get("/chart/TSLA").status_code == 404
    assert client.get("/status/TSLA").status_code == 404


def test_status_with_proposal_and_outcome(client):
    s = client.get("/status/SPY").get_json()
    assert s["session_date"] == "2024-11-06" and s["candle_minutes"] == 5
    assert s["box"] == {"top": 580.0, "bottom": 574.5, "delta": pytest.approx(5.5)}
    assert s["proposal"]["side"] == "BUY"
    assert s["proposal"]["take_profit"] == pytest.approx(586.7)
    assert s["proposal"]["confirm_close"] == "2024-11-06T15:35:00+00:00"
    assert s["outcome"]["status"] == "TP_HIT" and s["outcome"]["r_multiple"] == 1.0
    assert s["status_line"] == "BUY 581.20 · TP 586.70 · SL 575.70 · TP hit 10:40 (+1R)"
    # the dashboard uses the real clock, so the 15:40 bucket is long past its
    # grace and counts as complete (the replayed day is in the past)
    assert s["last_candle"] == "2024-11-06T15:40:00+00:00"


def test_status_waiting_for_confirmation(client):
    s = client.get("/status/QQQ").get_json()
    assert s["proposal"] is None and s["outcome"] is None
    assert s["status_line"].startswith("waiting for confirmation")


def test_status_no_data(client):
    s = client.get("/status/GLD").get_json()
    assert s["session_date"] is None and s["box"] is None
    assert s["status_line"] == "waiting for market open"


def test_status_line_variants():
    open_snap = orb_session.snapshot(df_of(ORB_BARS + C_GREEN_ABOVE + TRAIL), 5, now=utc(2024, 11, 6, 15, 36))
    assert server.status_line(open_snap) == "BUY 581.20 · TP 586.70 · SL 575.70 · open"
    flat_snap = orb_session.snapshot(df_of([bar("2024-11-06 14:30:00+00:00", 580, 580, 580, 580)] + flat("2024-11-06 15:30", 1)), 5)
    assert server.status_line(flat_snap) == "flat opening range — no proposal"
    pre = orb_session.snapshot(bar("2024-11-06 13:00:00+00:00", 570, 571, 569, 570.5), 5)
    assert server.status_line(pre) == "building range"


def test_health_closed_outside_hours(client):
    r = client.get("/health").get_json()
    assert set(r["data"]) == {"SPY", "QQQ", "GLD"}
    assert r["data"]["GLD"]["status"] == "no_data" or r["data"]["GLD"]["last_bar"] is None


class FakeBarStore:
    def __init__(self, last=None, fail=False):
        self.last, self.fail, self.calls = last, fail, 0

    def last_session(self, symbol):
        self.calls += 1
        if self.fail:
            raise ConnectionError("db down")
        return self.last


def _client_with_store(monkeypatch, store):
    from datetime import date
    state = {"symbols": ["SPY", "QQQ"], "feed": FakeFeed({}), "bar_store": store}
    monkeypatch.setattr(server, "_state", state)
    monkeypatch.setitem(server._bars_cache, "at", None)
    monkeypatch.setitem(server._bars_cache, "value", None)
    server.app.config["TESTING"] = True
    return server.app.test_client()


def test_health_reports_bars_last_archived(monkeypatch):
    from datetime import date
    client = _client_with_store(monkeypatch, FakeBarStore(last=date(2026, 11, 13)))
    body = client.get("/health").get_json()
    assert body["bars_last_archived"] == {"SPY": "2026-11-13", "QQQ": "2026-11-13"}


def test_health_without_a_store_has_no_bars_field(client):
    assert "bars_last_archived" not in client.get("/health").get_json()


def test_health_survives_a_dead_database(monkeypatch):
    client = _client_with_store(monkeypatch, FakeBarStore(fail=True))
    r = client.get("/health")
    assert r.status_code == 200 and r.get_json()["bars_last_archived"] == {"SPY": "error", "QQQ": "error"}


def test_health_asks_the_database_at_most_once_a_minute(monkeypatch):
    from datetime import date
    store = FakeBarStore(last=date(2026, 11, 13))
    client = _client_with_store(monkeypatch, store)
    clock = {"t": 1000.0}
    monkeypatch.setattr(server.time, "monotonic", lambda: clock["t"])
    client.get("/health"); client.get("/health")
    assert store.calls == 2                      # one call per symbol, only for the first request
    clock["t"] += 61
    client.get("/health")
    assert store.calls == 4
