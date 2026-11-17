"""
ensure_bars: make sure the store has the sessions a backtest needs.

For every weekday in the period (today excluded: it is still incomplete) whose
stored bar count is below MIN_COMPLETE_BARS:

- within yfinance's 30-day window: fetch that one session from yfinance (raw
  prices, one request per day: the vendor allows at most 8 calendar days per
  request) and upsert it;
- older: fetch from the deep-history source (Alpaca, when its keys are set), one
  request per calendar month of missing sessions, split into sessions and
  upserted. Without a deep source those sessions are reported, never silently
  skipped, and an archived (even partial) old session is left as it is.

A weekday that returns no data (holiday) is reported and not retried within the run.
"""
from __future__ import annotations

import functools
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from itertools import groupby
from typing import Callable, Optional

import pandas as pd
import pytz

from marketscanner import config
from marketscanner.data import alpaca_feed
from marketscanner.data.bar_store import BarStore
from marketscanner.data.yfinance_feed import fetch_range

log = logging.getLogger(__name__)
_ET = pytz.timezone(config.TIMEZONE)

MIN_COMPLETE_BARS = 385   # a regular session has 390 one-minute bars
YF_DEPTH_DAYS = 29        # yfinance: "must be within the last 30 days"; stay one day inside

_DEFAULT = object()       # deep_fetch not given: use Alpaca if configured


@dataclass
class BackfillReport:
    fetched: list[tuple[str, date, int]] = field(default_factory=list)       # symbol, session, rows
    incomplete: list[tuple[str, date, int]] = field(default_factory=list)    # fetched but still short
    unavailable: list[tuple[str, date, str]] = field(default_factory=list)   # symbol, session, reason
    deep_fetched: int = 0     # how many of `fetched` came from the deep-history source


def weekdays(start: date, end: date) -> list[date]:
    out, d = [], start
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def default_deep_fetch() -> Optional[Callable]:
    return alpaca_feed.fetch_range if alpaca_feed.is_configured() else None


def ensure_bars(
    store: BarStore,
    symbols: list[str],
    start: date,
    end: date,
    fetch: Optional[Callable] = None,
    today: Optional[date] = None,
    min_bars: int = MIN_COMPLETE_BARS,
    deep_fetch=_DEFAULT,
) -> BackfillReport:
    fetch = fetch or functools.partial(fetch_range, auto_adjust=False)
    if deep_fetch is _DEFAULT:
        deep_fetch = default_deep_fetch()
    today = today or datetime.now(_ET).date()
    report = BackfillReport()
    days = weekdays(start, min(end, today - timedelta(days=1)))
    for symbol in symbols:
        counts = store.session_counts(symbol, start, end)
        old_missing: list[date] = []
        for d in days:
            have = counts.get(d, 0)
            if have >= min_bars:
                continue
            if (today - d).days > YF_DEPTH_DAYS:
                if deep_fetch is not None:
                    old_missing.append(d)
                elif have == 0:
                    report.unavailable.append((symbol, d, "older than yfinance's 30-day window and not archived "
                                                          "(set ALPACA_API_KEY_ID / ALPACA_API_SECRET_KEY to fetch it)"))
                continue
            try:
                df = fetch(symbol, d, d + timedelta(days=1))
            except Exception as exc:  # noqa: BLE001 - one bad day must not stop the run
                report.unavailable.append((symbol, d, f"fetch failed: {exc!r}"))
                log.warning("backfill %s %s failed: %r", symbol, d, exc)
                continue
            _store_session(store, report, symbol, d, df, have, min_bars)
        if old_missing:
            _deep_backfill(store, report, symbol, old_missing, counts, deep_fetch, min_bars)
    return report


def _deep_backfill(store, report, symbol, missing, counts, deep_fetch, min_bars) -> None:
    for _, month in groupby(missing, key=lambda d: (d.year, d.month)):
        month = list(month)
        try:
            df = deep_fetch(symbol, month[0], month[-1] + timedelta(days=1))
        except Exception as exc:  # noqa: BLE001 - one bad month must not stop the run
            log.warning("deep backfill %s %s..%s failed: %r", symbol, month[0], month[-1], exc)
            for d in month:
                report.unavailable.append((symbol, d, f"Alpaca fetch failed: {exc!r}"))
            continue
        by_day = _split_by_et_date(df)
        for d in month:
            before = len(report.fetched)
            _store_session(store, report, symbol, d, by_day.get(d), counts.get(d, 0), min_bars)
            report.deep_fetched += len(report.fetched) - before


def _split_by_et_date(df: Optional[pd.DataFrame]) -> dict[date, pd.DataFrame]:
    if df is None or df.empty:
        return {}
    keys = df.index.tz_convert(_ET).date
    return {d: part for d, part in df.groupby(keys)}


def _store_session(store, report, symbol, d, df, have, min_bars) -> None:
    if df is None or df.empty:
        if have == 0:
            report.unavailable.append((symbol, d, "no data returned (holiday?)"))
        return
    store.upsert_bars(symbol, df)
    report.fetched.append((symbol, d, len(df)))
    if len(df) < min_bars:
        report.incomplete.append((symbol, d, len(df)))
