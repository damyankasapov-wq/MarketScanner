"""
The one way to cut a UTC-indexed bar frame into New York sessions.

Used by the backfill, the backtester's idea generation, its exit walk and its
trade gallery, so they cannot disagree about which bar belongs to which day.

Fast on years of 1-minute bars: one tz_convert of the whole index, day codes as
datetime64[D] (no per-bar Python date objects), and session slices taken with
iloc between boundary positions instead of one boolean mask per date.

    index (UTC, sorted)   14:30 ... 20:59 | 14:30 ... 20:59 | 14:30 ...
    ET day code           d0 ... d0       | d1 ... d1       | d2 ...
    starts                0               | 390             | 780
    session_start_mask    T F F ... F     | T F ... F       | T ...

Rows with a missing timestamp (NaT) are dropped by `sort_bars`, as the old
groupby-based splitter did.
"""
from __future__ import annotations

from datetime import date
from typing import Optional

import numpy as np
import pandas as pd
import pytz

from marketscanner import config

_ET = pytz.timezone(config.TIMEZONE)


def sort_bars(df: pd.DataFrame) -> pd.DataFrame:
    """Drop rows without a timestamp, then sort by time (stable) unless already sorted."""
    if df.index.hasnans:
        df = df[df.index.notna()]
    return df if df.index.is_monotonic_increasing else df.sort_index(kind="stable")


def et_day_codes(index: pd.DatetimeIndex) -> np.ndarray:
    """New York calendar day of every bar, as datetime64[D] (vectorised)."""
    if len(index) == 0:
        return np.array([], dtype="datetime64[D]")
    local = index.tz_convert(_ET).tz_localize(None)
    return local.values.astype("datetime64[D]")


def session_starts(index: pd.DatetimeIndex) -> tuple[np.ndarray, list[date]]:
    """
    For a SORTED UTC index without NaT: integer positions of each session's
    first bar, and each session's New York date, in index order.
    """
    if len(index) == 0:
        return np.array([], dtype=np.int64), []
    days = et_day_codes(index)
    starts = np.concatenate(([0], np.flatnonzero(days[1:] != days[:-1]) + 1)).astype(np.int64)
    dates = [d.item() for d in days[starts]]
    return starts, dates


def session_start_mask(index: pd.DatetimeIndex) -> np.ndarray:
    """
    True on each session's first bar of a SORTED index, bar 0 included.

    Not walk_exit's gap mask as-is: walk_exit's `first_of_session` means "first
    bar of a session LATER than the entry session", so a slice taken from this
    mask must have its first entry set from the entry session first (see
    engine._walk_one). Passing it unchanged would turn an entry-bar stop into a
    gap fill.
    """
    mask = np.zeros(len(index), dtype=bool)
    mask[session_starts(index)[0]] = True
    return mask


def split_by_et_date(df: Optional[pd.DataFrame]) -> dict[date, pd.DataFrame]:
    """{New York date: that session's bars}, in date order. Unsorted input is sorted first."""
    if df is None or df.empty:
        return {}
    df = sort_bars(df)
    starts, dates = session_starts(df.index)
    ends = np.append(starts[1:], len(df))
    return {d: df.iloc[a:b] for d, a, b in zip(dates, starts, ends)}


def session_bars(df: pd.DataFrame, session_date: date) -> pd.DataFrame:
    """One session's bars (an empty frame if the date has none), without splitting every day."""
    if df.empty:
        return df.iloc[0:0]
    df = sort_bars(df)
    days = et_day_codes(df.index)
    d = np.datetime64(session_date, "D")
    return df.iloc[int(np.searchsorted(days, d, "left")):int(np.searchsorted(days, d, "right"))]
