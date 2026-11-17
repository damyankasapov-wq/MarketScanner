"""
BarStore: where 1-minute bars live once they have been seen.

Why: yfinance only serves the last 30 days of 1-minute bars, so every trading day
that is not saved is lost to backtests after a month. The scanner archives each
day's bars (marketscanner.data.archive) and the backtester reads them back.

This module is pure (no database driver imported). Implementations:
  - InMemoryBarStore  (here; tests and dry runs)
  - PostgresBarStore  (marketscanner.data.pg_bar_store; the real archive)

Conventions: frames have a tz-aware UTC DatetimeIndex and float columns
open/high/low/close/volume. "Session date" is the New York calendar date.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Optional, Protocol

import pandas as pd

from marketscanner import config
from marketscanner.strategies.orb_session import et_time_utc

import pytz

_ET = pytz.timezone(config.TIMEZONE)
COLUMNS = ["open", "high", "low", "close", "volume"]
PRICE_DECIMALS = 4


class BarStore(Protocol):
    def upsert_bars(self, symbol: str, df: pd.DataFrame) -> int:
        """Insert or replace bars (the latest row per (symbol, ts) wins). Returns rows written."""

    def replace_session(self, symbol: str, session_date: date, df: pd.DataFrame) -> int:
        """Delete every bar of that New York session, then insert `df`, as one unit. Returns rows written."""

    def load(self, symbol: str, start: date, end: date) -> pd.DataFrame:
        """All bars whose New York date is in [start, end] inclusive, ascending."""

    def session_counts(self, symbol: str, start: date, end: date) -> dict[date, int]:
        """Number of bars per New York session date in [start, end]."""

    def last_session(self, symbol: str) -> Optional[date]:
        """Newest New York session date with any bars, or None."""


def empty_bars() -> pd.DataFrame:
    return pd.DataFrame({c: pd.Series(dtype="float64") for c in COLUMNS}, index=pd.DatetimeIndex([], tz="UTC"))


def clean_bars(df: pd.DataFrame) -> pd.DataFrame:
    """
    Normalise a frame for storage: UTC index, the five float columns, prices
    rounded to 4 decimals, rows with a missing price dropped, sorted, one row per
    timestamp (the last one wins).
    """
    if df is None or df.empty:
        return empty_bars()
    out = df[COLUMNS].astype(float).copy()
    idx = out.index
    out.index = idx.tz_localize("UTC") if idx.tz is None else idx.tz_convert("UTC")
    out = out.dropna(subset=["open", "high", "low", "close"])
    out["volume"] = out["volume"].fillna(0.0)
    out[["open", "high", "low", "close"]] = out[["open", "high", "low", "close"]].round(PRICE_DECIMALS)
    out = out[~out.index.duplicated(keep="last")].sort_index()
    return out


def et_date_bounds_utc(start: date, end: date) -> tuple[pd.Timestamp, pd.Timestamp]:
    """[midnight ET of `start`, midnight ET of the day after `end`) as UTC timestamps."""
    return et_time_utc(start, 0, 0), et_time_utc(end + timedelta(days=1), 0, 0)


def et_dates(index: pd.DatetimeIndex) -> list[date]:
    """Sorted distinct New York dates covered by a UTC index (never the UTC date)."""
    if len(index) == 0:
        return []
    return sorted(set(index.tz_convert(_ET).date))


class InMemoryBarStore:
    """Dict-backed BarStore with the same semantics as the Postgres one."""

    def __init__(self) -> None:
        self._frames: dict[str, pd.DataFrame] = {}

    def upsert_bars(self, symbol: str, df: pd.DataFrame) -> int:
        new = clean_bars(df)
        if new.empty:
            return 0
        old = self._frames.get(symbol, empty_bars())
        merged = pd.concat([old, new])
        merged = merged[~merged.index.duplicated(keep="last")].sort_index()
        self._frames[symbol] = merged
        return len(new)

    def replace_session(self, symbol: str, session_date: date, df: pd.DataFrame) -> int:
        old = self._frames.get(symbol)
        if old is not None and not old.empty:
            lo, hi = et_date_bounds_utc(session_date, session_date)
            self._frames[symbol] = old[(old.index < lo) | (old.index >= hi)]
        return self.upsert_bars(symbol, df)

    def load(self, symbol: str, start: date, end: date) -> pd.DataFrame:
        df = self._frames.get(symbol)
        if df is None or df.empty:
            return empty_bars()
        lo, hi = et_date_bounds_utc(start, end)
        return df[(df.index >= lo) & (df.index < hi)].copy()

    def session_counts(self, symbol: str, start: date, end: date) -> dict[date, int]:
        df = self.load(symbol, start, end)
        if df.empty:
            return {}
        counts = pd.Series(df.index.tz_convert(_ET).date).value_counts()
        return {d: int(n) for d, n in counts.items()}

    def last_session(self, symbol: str) -> Optional[date]:
        df = self._frames.get(symbol)
        if df is None or df.empty:
            return None
        return et_dates(df.index)[-1]
