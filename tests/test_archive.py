"""archive_frames: saves what the scanner holds, by New York session date, and never raises."""
from datetime import date

import pandas as pd

from marketscanner.data.archive import archive_frames
from marketscanner.data.bar_store import InMemoryBarStore
from tests.orb_fixtures import bar, df_of, flat

D1, D2 = date(2024, 11, 6), date(2024, 11, 7)


class BrokenStore:
    def upsert_bars(self, symbol, df):
        raise ConnectionError("db down")


class SelectiveStore(InMemoryBarStore):
    def upsert_bars(self, symbol, df):
        if symbol == "QQQ":
            raise ConnectionError("db down")
        return super().upsert_bars(symbol, df)


def test_writes_every_symbol_and_reports_et_dates():
    store = InMemoryBarStore()
    res = archive_frames(store, {"SPY": df_of(flat("2024-11-06 14:30", 5)), "GLD": df_of(flat("2024-11-06 14:30", 3))})
    assert res.written == {"SPY": [D1], "GLD": [D1]} and res.errors == {}
    assert len(store.load("SPY", D1, D1)) == 5 and len(store.load("GLD", D1, D1)) == 3


def test_frame_spanning_two_et_dates_and_the_utc_midnight_trap():
    # 19:30 ET on 11-06 is 00:30 UTC on 11-07: still the 11-06 session
    frame = df_of([bar("2024-11-07 00:30", 1, 1, 1, 1), bar("2024-11-07 14:30", 1, 1, 1, 1)])
    store = InMemoryBarStore()
    res = archive_frames(store, {"SPY": frame})
    assert res.written["SPY"] == [D1, D2]
    assert store.session_counts("SPY", D1, D2) == {D1: 1, D2: 1}


def test_rearchiving_the_same_session_is_idempotent():
    store = InMemoryBarStore()
    frame = df_of(flat("2024-11-06 14:30", 10))
    archive_frames(store, {"SPY": frame})
    archive_frames(store, {"SPY": frame})
    assert len(store.load("SPY", D1, D1)) == 10


def test_empty_and_none_frames_are_skipped():
    store = InMemoryBarStore()
    res = archive_frames(store, {"SPY": pd.DataFrame(), "QQQ": None})
    assert res.written == {} and res.errors == {}


def test_store_error_is_returned_not_raised():
    res = archive_frames(BrokenStore(), {"SPY": df_of(flat("2024-11-06 14:30", 2))})
    assert "SPY" in res.errors and "db down" in res.errors["SPY"] and res.written == {}


def test_one_symbol_failing_does_not_stop_the_others():
    store = SelectiveStore()
    res = archive_frames(store, {
        "SPY": df_of(flat("2024-11-06 14:30", 2)),
        "QQQ": df_of(flat("2024-11-06 14:30", 2)),
        "GLD": df_of(flat("2024-11-06 14:30", 2)),
    })
    assert set(res.written) == {"SPY", "GLD"} and set(res.errors) == {"QQQ"}
