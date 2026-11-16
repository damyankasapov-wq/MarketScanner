"""ensure_bars: fetch only what is missing, one weekday per request, report what cannot be had."""
from datetime import date, timedelta

import pandas as pd

from marketscanner.data import backfill
from marketscanner.data.backfill import MIN_COMPLETE_BARS, ensure_bars, weekdays
from marketscanner.data.bar_store import InMemoryBarStore
from tests.orb_fixtures import df_of, flat

TODAY = date(2024, 11, 15)          # a Friday
MON, TUE, WED = date(2024, 11, 11), date(2024, 11, 12), date(2024, 11, 13)


def _session_frame(d, n=390):
    # n one-minute bars starting 09:30 ET (14:30 UTC in November)
    return df_of(flat(f"{d.isoformat()} 14:30", n))


class Fetcher:
    def __init__(self, frames=None, fail_on=None):
        self.calls, self.frames, self.fail_on = [], frames or {}, fail_on or set()

    def __call__(self, symbol, start, end):
        self.calls.append((symbol, start, end))
        if (symbol, start) in self.fail_on:
            raise ConnectionError("yahoo down")
        return self.frames.get((symbol, start), _session_frame(start))


def test_weekdays_skips_weekends():
    assert weekdays(date(2024, 11, 8), date(2024, 11, 12)) == [date(2024, 11, 8), MON, TUE]
    assert weekdays(date(2024, 11, 9), date(2024, 11, 10)) == []


def test_fetches_each_missing_weekday_one_request_per_day():
    store, fetch = InMemoryBarStore(), Fetcher()
    rep = ensure_bars(store, ["SPY"], MON, WED, fetch=fetch, today=TODAY)
    assert [(c[1], c[2]) for c in fetch.calls] == [(MON, MON + timedelta(1)), (TUE, TUE + timedelta(1)), (WED, WED + timedelta(1))]
    assert [d for _, d, _ in rep.fetched] == [MON, TUE, WED] and rep.unavailable == [] and rep.incomplete == []
    assert store.session_counts("SPY", MON, WED) == {MON: 390, TUE: 390, WED: 390}


def test_complete_days_are_not_refetched():
    store, fetch = InMemoryBarStore(), Fetcher()
    store.upsert_bars("SPY", _session_frame(MON, MIN_COMPLETE_BARS))
    ensure_bars(store, ["SPY"], MON, TUE, fetch=fetch, today=TODAY)
    assert [c[1] for c in fetch.calls] == [TUE]


def test_partial_day_is_refetched_and_upgraded():
    store, fetch = InMemoryBarStore(), Fetcher()
    store.upsert_bars("SPY", _session_frame(MON, 100))
    ensure_bars(store, ["SPY"], MON, MON, fetch=fetch, today=TODAY)
    assert store.session_counts("SPY", MON, MON) == {MON: 390}


def test_today_and_future_are_excluded():
    store, fetch = InMemoryBarStore(), Fetcher()
    ensure_bars(store, ["SPY"], date(2024, 11, 14), date(2024, 11, 22), fetch=fetch, today=TODAY)
    assert [c[1] for c in fetch.calls] == [date(2024, 11, 14)]


def test_beyond_the_vendor_window_is_reported_not_fetched():
    store, fetch = InMemoryBarStore(), Fetcher()
    old = TODAY - timedelta(days=40)
    while old.weekday() >= 5:
        old -= timedelta(days=1)
    rep = ensure_bars(store, ["SPY"], old, old, fetch=fetch, today=TODAY)
    assert fetch.calls == [] and rep.unavailable[0][1] == old and "30-day" in rep.unavailable[0][2]


def test_archived_old_day_is_neither_fetched_nor_reported():
    store, fetch = InMemoryBarStore(), Fetcher()
    old = TODAY - timedelta(days=40)
    while old.weekday() >= 5:
        old -= timedelta(days=1)
    store.upsert_bars("SPY", _session_frame(old, 50))       # partial but archived: nothing more to do
    rep = ensure_bars(store, ["SPY"], old, old, fetch=fetch, today=TODAY)
    assert fetch.calls == [] and rep.unavailable == []


def test_holiday_empty_response_is_reported():
    fetch = Fetcher(frames={("SPY", TUE): pd.DataFrame()})
    rep = ensure_bars(InMemoryBarStore(), ["SPY"], TUE, TUE, fetch=fetch, today=TODAY)
    assert rep.unavailable == [("SPY", TUE, "no data returned (holiday?)")] and rep.fetched == []


def test_fetch_error_on_one_day_does_not_stop_the_run():
    fetch = Fetcher(fail_on={("SPY", TUE)})
    store = InMemoryBarStore()
    rep = ensure_bars(store, ["SPY"], MON, WED, fetch=fetch, today=TODAY)
    assert [d for _, d, _ in rep.fetched] == [MON, WED]
    assert rep.unavailable[0][1] == TUE and "yahoo down" in rep.unavailable[0][2]


def test_short_session_is_stored_and_flagged_incomplete():
    fetch = Fetcher(frames={("SPY", TUE): _session_frame(TUE, 289)})
    store = InMemoryBarStore()
    rep = ensure_bars(store, ["SPY"], TUE, TUE, fetch=fetch, today=TODAY)
    assert rep.incomplete == [("SPY", TUE, 289)] and store.session_counts("SPY", TUE, TUE) == {TUE: 289}


def test_every_symbol_is_handled_separately():
    store, fetch = InMemoryBarStore(), Fetcher()
    ensure_bars(store, ["SPY", "GLD"], MON, MON, fetch=fetch, today=TODAY)
    assert sorted(c[0] for c in fetch.calls) == ["GLD", "SPY"]


def test_default_fetch_asks_for_raw_prices(monkeypatch):
    seen = {}

    def fake_fetch_range(symbol, start, end, auto_adjust=True):
        seen["auto_adjust"] = auto_adjust
        return _session_frame(start)

    monkeypatch.setattr(backfill, "fetch_range", fake_fetch_range)
    ensure_bars(InMemoryBarStore(), ["SPY"], MON, MON, today=TODAY)
    assert seen["auto_adjust"] is False
