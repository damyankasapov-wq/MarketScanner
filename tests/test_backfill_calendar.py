"""ensure_bars with the NYSE calendar, fail-fast and whole-day replace (found by /ship review, 2026-11-17)."""
from datetime import date, timedelta

import pandas as pd

from marketscanner.data import alpaca_feed
from marketscanner.data.backfill import (
    DEEP_MAX_FAILURES, REASON_DEEP_NO_DATA, REASON_DEEP_STOPPED, ensure_bars, required_bars,
)
from marketscanner.data.bar_store import InMemoryBarStore
from marketscanner.data.market_calendar import sessions
from tests.orb_fixtures import df_of, flat

TODAY = date(2026, 11, 17)
BLACK_FRIDAY = date(2025, 11, 28)          # NYSE closes 13:00 ET
THANKSGIVING = date(2025, 11, 27)          # closed


def _bars(d, n, start="09:30", price=579.0):
    t0 = pd.Timestamp(f"{d.isoformat()} {start}", tz="America/New_York").tz_convert("UTC")
    return df_of(flat(t0.strftime("%Y-%m-%d %H:%M"), n, price))


class Deep:
    """Deep source returning `per_day(d)` for every weekday in the requested range."""

    def __init__(self, per_day=lambda d: _bars(d, 390), errors=()):
        self.calls, self.per_day, self.errors = [], per_day, list(errors)

    def __call__(self, symbol, start, end):
        self.calls.append((symbol, start, end))
        if self.errors:
            raise self.errors.pop(0)
        d, frames = start, []
        while d < end:
            if d.weekday() < 5:
                frames.append(self.per_day(d))
            d += timedelta(days=1)
        frames = [f for f in frames if f is not None and not f.empty]
        return pd.concat(frames) if frames else pd.DataFrame()


def _never(*a):
    raise AssertionError("yfinance must not be called for old sessions")


def test_holidays_are_never_requested_or_reported():
    deep = Deep()
    rep = ensure_bars(InMemoryBarStore(), ["SPY"], date(2025, 11, 26), date(2025, 11, 28),
                      fetch=_never, today=TODAY, deep_fetch=deep)
    assert deep.calls == [("SPY", date(2025, 11, 26), BLACK_FRIDAY + timedelta(1))]
    assert THANKSGIVING not in [d for _, d, _ in rep.fetched + rep.unavailable]


def test_half_day_after_hours_bars_are_dropped_and_the_day_counts_as_complete():
    store, deep = InMemoryBarStore(), Deep()          # vendor sends 390 bars, 09:30-16:00
    rep = ensure_bars(store, ["SPY"], BLACK_FRIDAY, BLACK_FRIDAY, fetch=_never, today=TODAY, deep_fetch=deep)
    assert store.session_counts("SPY", BLACK_FRIDAY, BLACK_FRIDAY) == {BLACK_FRIDAY: 210}
    last = store.load("SPY", BLACK_FRIDAY, BLACK_FRIDAY).index.max().tz_convert("America/New_York")
    assert last.strftime("%H:%M") == "12:59" and rep.incomplete == []
    ensure_bars(store, ["SPY"], BLACK_FRIDAY, BLACK_FRIDAY, fetch=_never, today=TODAY, deep_fetch=deep)
    assert len(deep.calls) == 1                         # complete at 210: not fetched again


def test_required_bars_follow_the_session_length():
    s = sessions(date(2025, 11, 26), BLACK_FRIDAY)
    assert required_bars(s[date(2025, 11, 26)]) == 385 and required_bars(s[BLACK_FRIDAY]) == 205


def test_one_month_request_fills_several_sessions_and_skips_complete_ones():
    store = InMemoryBarStore()
    store.upsert_bars("SPY", _bars(date(2025, 6, 4), 390))                      # complete, keep
    deep = Deep()
    rep = ensure_bars(store, ["SPY"], date(2025, 6, 2), date(2025, 6, 6), fetch=_never, today=TODAY, deep_fetch=deep)
    assert deep.calls == [("SPY", date(2025, 6, 2), date(2025, 6, 7))]
    assert sorted(d for _, d, _ in rep.fetched) == [date(2025, 6, d) for d in (2, 3, 5, 6)]
    assert set(store.session_counts("SPY", date(2025, 6, 2), date(2025, 6, 6)).values()) == {390}


def test_partial_old_day_is_replaced_not_merged():
    d = date(2025, 6, 3)
    store = InMemoryBarStore()
    store.upsert_bars("SPY", _bars(d, 60, start="11:00", price=1.0))           # partial yfinance archive
    ensure_bars(store, ["SPY"], d, d, fetch=_never, today=TODAY, deep_fetch=Deep(lambda x: _bars(x, 390, price=2.0)))
    got = store.load("SPY", d, d)
    assert len(got) == 390 and set(got["close"]) == {2.0}                       # one vendor owns the day


def test_partial_old_day_with_no_alpaca_data_is_kept_silently():
    d = date(2025, 6, 3)
    store = InMemoryBarStore()
    store.upsert_bars("SPY", _bars(d, 60))
    rep = ensure_bars(store, ["SPY"], d, d, fetch=_never, today=TODAY, deep_fetch=Deep(lambda x: None))
    assert store.session_counts("SPY", d, d) == {d: 60} and rep.unavailable == [] and rep.fetched == []


def test_empty_alpaca_answer_says_alpaca_not_holiday():
    d = date(2015, 6, 3)
    rep = ensure_bars(InMemoryBarStore(), ["SPY"], d, d, fetch=_never, today=TODAY, deep_fetch=Deep(lambda x: None))
    assert rep.unavailable == [("SPY", d, REASON_DEEP_NO_DATA)]


def test_fatal_error_stops_the_deep_source_for_every_symbol():
    deep = Deep(errors=[alpaca_feed.AlpacaFatalError("HTTP 401 (check the Alpaca keys)")])
    rep = ensure_bars(InMemoryBarStore(), ["SPY", "QQQ"], date(2025, 5, 1), date(2025, 7, 31),
                      fetch=_never, today=TODAY, deep_fetch=deep)
    assert len(deep.calls) == 1 and "HTTP 401" in rep.deep_stopped
    reasons = {(s, why) for s, _, why in rep.unavailable}
    assert ("QQQ", REASON_DEEP_STOPPED) in reasons and ("SPY", REASON_DEEP_STOPPED) in reasons


def test_two_failed_months_in_a_row_stop_the_deep_source():
    deep = Deep(errors=[alpaca_feed.AlpacaOutageError("gave up, last: HTTP 500")] * DEEP_MAX_FAILURES)
    rep = ensure_bars(InMemoryBarStore(), ["SPY"], date(2025, 5, 1), date(2025, 8, 29),
                      fetch=_never, today=TODAY, deep_fetch=deep)
    assert len(deep.calls) == DEEP_MAX_FAILURES and "2 months in a row" in rep.deep_stopped


def test_a_success_resets_the_failure_count():
    deep = Deep(errors=[alpaca_feed.AlpacaOutageError("gave up, last: HTTP 500")])
    rep = ensure_bars(InMemoryBarStore(), ["SPY"], date(2025, 5, 1), date(2025, 8, 29),
                      fetch=_never, today=TODAY, deep_fetch=deep)
    assert len(deep.calls) == 4 and rep.deep_stopped is None and rep.deep_fetched > 0


def test_an_error_about_one_symbol_does_not_stop_the_others():
    # an unknown ticker gets a 4xx for every month; SPY must still be fetched
    class PerSymbol(Deep):
        def __call__(self, symbol, start, end):
            if symbol == "^VIX":
                self.calls.append((symbol, start, end))
                raise alpaca_feed.AlpacaError("HTTP 422: invalid symbol")
            return super().__call__(symbol, start, end)

    deep = PerSymbol()
    rep = ensure_bars(InMemoryBarStore(), ["^VIX", "SPY"], date(2025, 5, 1), date(2025, 7, 31),
                      fetch=_never, today=TODAY, deep_fetch=deep)
    assert rep.deep_stopped is None and len([c for c in deep.calls if c[0] == "^VIX"]) == 3
    assert {s for s, _, _ in rep.fetched} == {"SPY"}


def test_stored_bars_after_the_close_force_a_clean_replace():
    store = InMemoryBarStore()
    store.upsert_bars("SPY", _bars(BLACK_FRIDAY, 379))                  # written before the calendar fix
    deep = Deep()
    ensure_bars(store, ["SPY"], BLACK_FRIDAY, BLACK_FRIDAY, fetch=_never, today=TODAY, deep_fetch=deep)
    assert len(deep.calls) == 1 and store.session_counts("SPY", BLACK_FRIDAY, BLACK_FRIDAY) == {BLACK_FRIDAY: 210}


def test_recent_day_with_bars_after_the_close_is_replaced_too():
    d = TODAY - timedelta(days=5)                                         # within yfinance's window
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    store = InMemoryBarStore()
    store.upsert_bars("SPY", _bars(d, 420))                               # 30 bars past 16:00
    rep = ensure_bars(store, ["SPY"], d, d, fetch=lambda s, a, b: _bars(a, 390), today=TODAY, deep_fetch=None)
    assert store.session_counts("SPY", d, d) == {d: 390} and len(rep.fetched) == 1
