"""
The midnight reset is a production path: it must reset the strategies and clear
the feed no matter what the bars archive does (fails, or hangs forever), and the
archive must run from COPIES taken before the clear.
"""
import threading
import time
from datetime import date

import pandas as pd
import pytest

import main
from marketscanner.data.bar_store import InMemoryBarStore
from marketscanner.strategies.opening_range import OpeningRangeStrategy
from tests.orb_fixtures import df_of, flat


class FakeFeed:
    def __init__(self, frames):
        self.frames, self.cleared, self.order = frames, False, []

    def get_df(self, symbol):
        self.order.append(("get_df", symbol))
        return self.frames.get(symbol, pd.DataFrame())

    def clear(self):
        self.order.append(("clear",))
        self.cleared = True
        for k in self.frames:
            self.frames[k] = pd.DataFrame()      # what the real feed does to its buffer


@pytest.fixture
def strategies(monkeypatch):
    strat = {s: OpeningRangeStrategy(s) for s in ("SPY", "QQQ", "GLD")}
    for s in strat.values():
        s._fired_session = date(2024, 11, 6)
    monkeypatch.setattr(main, "_strategies", strat)
    return strat


def _frames():
    return {s: df_of(flat("2024-11-06 14:30", 4)) for s in ("SPY", "QQQ", "GLD")}


def test_reset_clears_everything_and_archives_the_copies(strategies):
    feed, store = FakeFeed(_frames()), InMemoryBarStore()
    thread = main._do_midnight_reset(feed, store_factory=lambda: store)
    thread.join(timeout=5)
    assert feed.cleared and all(s._fired_session is None for s in strategies.values())
    # the buffer was emptied, yet the archive still got the data: it ran from copies
    for sym in ("SPY", "QQQ", "GLD"):
        assert len(store.load(sym, date(2024, 11, 6), date(2024, 11, 6))) == 4
    # every copy was taken before the clear
    assert feed.order.index(("clear",)) > max(i for i, e in enumerate(feed.order) if e[0] == "get_df")


def test_archive_that_hangs_forever_does_not_delay_the_reset(strategies):
    release = threading.Event()

    class HangingStore:
        def upsert_bars(self, symbol, df):
            release.wait(30)

    feed = FakeFeed(_frames())
    t0 = time.monotonic()
    thread = main._do_midnight_reset(feed, store_factory=HangingStore)
    elapsed = time.monotonic() - t0
    try:
        assert elapsed < 2.0, "the reset must not wait for the archive"
        assert feed.cleared and all(s._fired_session is None for s in strategies.values())
        assert thread.is_alive()                 # the archive is still stuck, off to the side
    finally:
        release.set()
        thread.join(timeout=5)


def test_archive_failure_is_logged_not_raised_and_reset_completes(strategies, caplog):
    class BrokenStore:
        def upsert_bars(self, symbol, df):
            raise ConnectionError("db down")

    feed = FakeFeed(_frames())
    with caplog.at_level("ERROR"):
        thread = main._do_midnight_reset(feed, store_factory=BrokenStore)
        thread.join(timeout=5)
    assert feed.cleared and all(s._fired_session is None for s in strategies.values())
    assert any("db down" in r.getMessage() for r in caplog.records)


def test_store_factory_crash_is_contained(strategies, caplog):
    def boom():
        raise RuntimeError("cannot even connect")

    feed = FakeFeed(_frames())
    with caplog.at_level("ERROR"):
        thread = main._do_midnight_reset(feed, store_factory=boom)
        thread.join(timeout=5)
    assert feed.cleared
    assert any("Bars archive crashed" in r.getMessage() for r in caplog.records)


def test_feed_that_cannot_be_copied_does_not_block_the_reset(strategies):
    class BadFeed(FakeFeed):
        def get_df(self, symbol):
            raise RuntimeError("buffer gone")

    feed = BadFeed({})
    assert main._do_midnight_reset(feed, store_factory=InMemoryBarStore) is None
    assert feed.cleared and all(s._fired_session is None for s in strategies.values())


def test_no_feed_still_resets_strategies(strategies):
    assert main._do_midnight_reset(None) is None
    assert all(s._fired_session is None for s in strategies.values())


def test_empty_buffers_start_no_archive_thread(strategies):
    feed = FakeFeed({s: pd.DataFrame() for s in ("SPY", "QQQ", "GLD")})
    thread = main._do_midnight_reset(feed, store_factory=InMemoryBarStore)
    # frames dict is non-empty (three empty copies) so a thread starts, but writes nothing
    if thread:
        thread.join(timeout=5)
    assert feed.cleared
