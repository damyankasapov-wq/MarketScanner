"""The shared ET session splitter must agree with the two splitters it replaced."""
from datetime import date

import numpy as np
import pandas as pd
import pytz

from marketscanner import config
from marketscanner.data.sessions import (
    et_day_codes, session_bars, session_start_mask, session_starts, sort_bars, split_by_et_date,
)

_ET = pytz.timezone(config.TIMEZONE)


def session(day: str, n: int = 390, start=(9, 30)) -> pd.DataFrame:
    """n one-minute bars from the given ET time on `day` (DST handled by pytz)."""
    t0 = _ET.localize(pd.Timestamp(f"{day} {start[0]:02d}:{start[1]:02d}").to_pydatetime())
    idx = pd.date_range(pd.Timestamp(t0).tz_convert("UTC"), periods=n, freq="1min")
    px = np.linspace(100, 101, n)
    return pd.DataFrame({"open": px, "high": px + 0.1, "low": px - 0.1, "close": px, "volume": 1.0}, index=idx)


# The two splitters this module replaced, kept here as the reference.
def old_groupby_split(df):
    if df is None or df.empty:
        return {}
    keys = df.index.tz_convert(_ET).date
    return {d: part for d, part in df.groupby(keys)}


def old_mask_split(df):
    if df.empty:
        return {}
    dates = pd.Index(df.index.tz_convert(_ET).date)
    return {d: df[dates == d] for d in sorted(set(dates))}


def assert_same_split(a: dict, b: dict) -> None:
    assert list(a) == list(b)
    for d in a:
        pd.testing.assert_frame_equal(a[d], b[d])


# Fri 2024-11-01 is EDT (09:30 ET = 13:30 UTC), Mon 2024-11-04 is EST (14:30 UTC).
ACROSS_DST = pd.concat([session("2024-10-31"), session("2024-11-01"), session("2024-11-04"), session("2024-11-05")])


def test_matches_both_old_splitters_across_the_dst_change():
    new = split_by_et_date(ACROSS_DST)
    assert_same_split(new, old_groupby_split(ACROSS_DST))
    assert_same_split(new, old_mask_split(ACROSS_DST))
    assert list(new) == [date(2024, 10, 31), date(2024, 11, 1), date(2024, 11, 4), date(2024, 11, 5)]
    assert new[date(2024, 11, 1)].index[0] == pd.Timestamp("2024-11-01 13:30", tz="UTC")
    assert new[date(2024, 11, 4)].index[0] == pd.Timestamp("2024-11-04 14:30", tz="UTC")


def test_groups_by_new_york_date_not_utc_date():
    # 19:30-20:29 ET on 2024-11-05 is 00:30-01:29 UTC on 11-06: still the 11-05 session.
    late = session("2024-11-05", n=60, start=(19, 30))
    df = pd.concat([session("2024-11-05"), late]).sort_index()
    parts = split_by_et_date(df)
    assert list(parts) == [date(2024, 11, 5)]
    assert len(parts[date(2024, 11, 5)]) == 450
    assert_same_split(parts, old_groupby_split(df))


def test_unsorted_input_is_sorted_first():
    shuffled = ACROSS_DST.sample(frac=1.0, random_state=7)
    assert_same_split(split_by_et_date(shuffled), old_groupby_split(ACROSS_DST))


def test_empty_and_none():
    assert split_by_et_date(None) == {}
    assert split_by_et_date(pd.DataFrame()) == {}
    starts, dates = session_starts(pd.DatetimeIndex([], tz="UTC"))
    assert len(starts) == 0 and dates == []
    assert len(session_start_mask(pd.DatetimeIndex([], tz="UTC"))) == 0


def test_starts_dates_and_first_bar_mask():
    starts, dates = session_starts(ACROSS_DST.index)
    assert starts.tolist() == [0, 390, 780, 1170]
    assert dates == [date(2024, 10, 31), date(2024, 11, 1), date(2024, 11, 4), date(2024, 11, 5)]
    mask = session_start_mask(ACROSS_DST.index)
    assert np.flatnonzero(mask).tolist() == [0, 390, 780, 1170]
    # A bar is a session start if its ET date differs from the previous bar's (bar 0 always is).
    # This is NOT walk_exit's gap mask as-is: that one's first entry depends on the entry session.
    d = pd.Index(ACROSS_DST.index.tz_convert(_ET).date)
    assert (mask[1:] == np.asarray(d[1:] != d[:-1])).all()


def test_day_codes_match_python_dates():
    codes = et_day_codes(ACROSS_DST.index)
    assert [c.item() for c in codes] == list(ACROSS_DST.index.tz_convert(_ET).date)
    assert et_day_codes(pd.DatetimeIndex([], tz="UTC")).dtype == np.dtype("datetime64[D]")


def test_rows_without_a_timestamp_are_dropped_like_the_old_groupby():
    df = ACROSS_DST.copy()
    idx = df.index.tolist()
    idx[5] = pd.NaT
    idx[400] = pd.NaT
    df.index = pd.DatetimeIndex(idx)
    new = split_by_et_date(df)
    assert None not in new
    assert_same_split(new, old_groupby_split(df))
    assert sum(len(p) for p in new.values()) == len(df) - 2
    assert not sort_bars(df).index.hasnans


def test_session_bars_equals_the_split_and_handles_missing_days():
    split = split_by_et_date(ACROSS_DST)
    for d, part in split.items():
        pd.testing.assert_frame_equal(session_bars(ACROSS_DST, d), part)
    assert session_bars(ACROSS_DST, date(2024, 11, 2)).empty          # Saturday
    assert session_bars(ACROSS_DST.iloc[0:0], date(2024, 11, 1)).empty
    shuffled = ACROSS_DST.sample(frac=1.0, random_state=1)
    pd.testing.assert_frame_equal(session_bars(shuffled, date(2024, 11, 4)), split[date(2024, 11, 4)])
