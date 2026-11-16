"""
ensure_bars: make sure the store has the sessions a backtest needs.

For every weekday in the period (today excluded: it is still incomplete) whose
stored bar count is below MIN_COMPLETE_BARS, fetch that one session from yfinance
(raw prices, one request per day: the vendor allows at most 8 calendar days per
request) and upsert it. Sessions older than the vendor's 30-day window cannot be
fetched; they are reported, never silently skipped. A weekday that returns no
data (holiday) is reported and not retried within the run.
"""
from __future__ import annotations

import functools
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Callable, Optional

import pytz

from marketscanner import config
from marketscanner.data.bar_store import BarStore
from marketscanner.data.yfinance_feed import fetch_range

log = logging.getLogger(__name__)
_ET = pytz.timezone(config.TIMEZONE)

MIN_COMPLETE_BARS = 385   # a regular session has 390 one-minute bars
YF_DEPTH_DAYS = 29        # yfinance: "must be within the last 30 days"; stay one day inside


@dataclass
class BackfillReport:
    fetched: list[tuple[str, date, int]] = field(default_factory=list)       # symbol, session, rows
    incomplete: list[tuple[str, date, int]] = field(default_factory=list)    # fetched but still short
    unavailable: list[tuple[str, date, str]] = field(default_factory=list)   # symbol, session, reason


def weekdays(start: date, end: date) -> list[date]:
    out, d = [], start
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def ensure_bars(
    store: BarStore,
    symbols: list[str],
    start: date,
    end: date,
    fetch: Optional[Callable] = None,
    today: Optional[date] = None,
    min_bars: int = MIN_COMPLETE_BARS,
) -> BackfillReport:
    fetch = fetch or functools.partial(fetch_range, auto_adjust=False)
    today = today or datetime.now(_ET).date()
    report = BackfillReport()
    days = weekdays(start, min(end, today - timedelta(days=1)))
    for symbol in symbols:
        counts = store.session_counts(symbol, start, end)
        for d in days:
            have = counts.get(d, 0)
            if have >= min_bars:
                continue
            if (today - d).days > YF_DEPTH_DAYS:
                if have == 0:
                    report.unavailable.append((symbol, d, "older than yfinance's 30-day window and not archived"))
                continue
            try:
                df = fetch(symbol, d, d + timedelta(days=1))
            except Exception as exc:  # noqa: BLE001 - one bad day must not stop the run
                report.unavailable.append((symbol, d, f"fetch failed: {exc!r}"))
                log.warning("backfill %s %s failed: %r", symbol, d, exc)
                continue
            if df is None or df.empty:
                if have == 0:
                    report.unavailable.append((symbol, d, "no data returned (holiday?)"))
                continue
            store.upsert_bars(symbol, df)
            report.fetched.append((symbol, d, len(df)))
            if len(df) < min_bars:
                report.incomplete.append((symbol, d, len(df)))
    return report
