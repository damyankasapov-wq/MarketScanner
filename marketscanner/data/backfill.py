"""
ensure_bars: make sure the store has the sessions a backtest needs.

Only NYSE sessions are considered (market_calendar): holidays are never requested,
and a session counts as complete at its real length (210 minutes on a half-day,
390 otherwise), less a small tolerance. Bars outside a session's open..close are
dropped before storing, so after-hours prints on a 13:00 half-day never land in
the archive. For every past session (today excluded: it is still incomplete) with
fewer bars than that:

- within yfinance's 30-day window: fetch that one session from yfinance (raw
  prices, one request per day: the vendor allows at most 8 calendar days per
  request) and upsert it;
- older: fetch from the deep-history source (Alpaca, when its keys are set), one
  request per calendar month of missing sessions. Each returned session REPLACES
  whatever was stored for that day, so one vendor owns the whole session. On an
  AlpacaFatalError (keys, permission, settings) or after DEEP_MAX_FAILURES failed
  months in a row the deep source is not asked again this run; the reason is kept
  once in BackfillReport.deep_stopped. Without a deep source old sessions are
  reported, never silently skipped, and an archived (even partial) old session is
  left as it is.
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
from marketscanner.data import alpaca_feed, market_calendar
from marketscanner.data.bar_store import BarStore
from marketscanner.data.yfinance_feed import fetch_range

log = logging.getLogger(__name__)
_ET = pytz.timezone(config.TIMEZONE)

MIN_COMPLETE_BARS = 385       # a regular session has 390 one-minute bars
MISSING_BAR_TOLERANCE = 5     # same tolerance on a half-day: 210 minutes -> 205 bars
YF_DEPTH_DAYS = 29            # yfinance: "must be within the last 30 days"; stay one day inside
DEEP_MAX_FAILURES = 2         # failed deep months in a row before the deep source is dropped for the run

REASON_NO_DATA = "no data returned"
REASON_DEEP_NO_DATA = "no data from Alpaca (before its 2016 history?)"
REASON_NOT_ARCHIVED = (
    "older than yfinance's 30-day window and not archived "
    "(set ALPACA_API_KEY_ID / ALPACA_API_SECRET_KEY to fetch it)"
)
REASON_DEEP_STOPPED = "Alpaca not asked: stopped earlier this run"

_DEFAULT = object()           # deep_fetch not given: use Alpaca if configured


@dataclass
class BackfillReport:
    fetched: list[tuple[str, date, int]] = field(default_factory=list)       # symbol, session, rows
    incomplete: list[tuple[str, date, int]] = field(default_factory=list)    # fetched but still short
    unavailable: list[tuple[str, date, str]] = field(default_factory=list)   # symbol, session, reason
    deep_fetched: int = 0                    # how many of `fetched` came from the deep-history source
    deep_stopped: Optional[str] = None       # why the deep source was dropped for the rest of the run


@dataclass
class _DeepState:
    fetch: Optional[Callable]
    failures: int = 0


def weekdays(start: date, end: date) -> list[date]:
    out, d = [], start
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def default_deep_fetch() -> Optional[Callable]:
    return alpaca_feed.fetch_range if alpaca_feed.is_configured() else None


def required_bars(session: market_calendar.Session, min_bars: int = MIN_COMPLETE_BARS) -> int:
    """Bars a stored session needs to count as complete: 385 normally, 205 on a half-day."""
    return min(min_bars, session.minutes - MISSING_BAR_TOLERANCE)


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
    deep = _DeepState(default_deep_fetch() if deep_fetch is _DEFAULT else deep_fetch)
    today = today or datetime.now(_ET).date()
    report = BackfillReport()
    cal = market_calendar.sessions(start, min(end, today - timedelta(days=1)))
    for symbol in symbols:
        counts = store.session_counts(symbol, start, end)
        old_missing: list[date] = []
        for d, sess in cal.items():
            have = counts.get(d, 0)
            if have >= required_bars(sess, min_bars):
                continue
            if (today - d).days > YF_DEPTH_DAYS:
                if deep.fetch is not None:
                    old_missing.append(d)
                elif have == 0:
                    report.unavailable.append(
                        (symbol, d, REASON_DEEP_STOPPED if report.deep_stopped else REASON_NOT_ARCHIVED))
                continue
            try:
                df = fetch(symbol, d, d + timedelta(days=1))
            except Exception as exc:  # noqa: BLE001 - one bad day must not stop the run
                report.unavailable.append((symbol, d, f"fetch failed: {exc!r}"))
                log.warning("backfill %s %s failed: %r", symbol, d, exc)
                continue
            _store_session(store, report, symbol, d, sess, df, have, min_bars, replace=False)
        if old_missing:
            _deep_backfill(store, report, symbol, old_missing, counts, cal, deep, min_bars)
    return report


def _deep_backfill(store, report, symbol, missing, counts, cal, deep: _DeepState, min_bars) -> None:
    for _, month in groupby(missing, key=lambda d: (d.year, d.month)):
        month = list(month)
        if deep.fetch is None:
            report.unavailable.extend((symbol, d, REASON_DEEP_STOPPED) for d in month if counts.get(d, 0) == 0)
            continue
        try:
            df = deep.fetch(symbol, month[0], month[-1] + timedelta(days=1))
        except Exception as exc:  # noqa: BLE001 - one bad month must not stop the run
            log.warning("deep backfill %s %s..%s failed: %r", symbol, month[0], month[-1], exc)
            report.unavailable.extend((symbol, d, f"Alpaca fetch failed: {exc!r}") for d in month)
            deep.failures += 1
            if isinstance(exc, alpaca_feed.AlpacaFatalError):
                _stop_deep(report, deep, str(exc))
            elif deep.failures >= DEEP_MAX_FAILURES:
                _stop_deep(report, deep, f"{deep.failures} months in a row failed, last: {exc!r}")
            continue
        deep.failures = 0
        by_day = _split_by_et_date(df)
        for d in month:
            before = len(report.fetched)
            _store_session(store, report, symbol, d, cal[d], by_day.get(d), counts.get(d, 0), min_bars,
                           replace=True, empty_reason=REASON_DEEP_NO_DATA)
            report.deep_fetched += len(report.fetched) - before


def _stop_deep(report: BackfillReport, deep: _DeepState, reason: str) -> None:
    log.warning("deep backfill stopped for this run: %s", reason)
    report.deep_stopped = reason
    deep.fetch = None


def _split_by_et_date(df: Optional[pd.DataFrame]) -> dict[date, pd.DataFrame]:
    if df is None or df.empty:
        return {}
    keys = df.index.tz_convert(_ET).date
    return {d: part for d, part in df.groupby(keys)}


def _store_session(store, report, symbol, d, sess, df, have, min_bars, replace: bool,
                   empty_reason: str = REASON_NO_DATA) -> None:
    df = market_calendar.within_session(df, sess)
    if df is None or df.empty:
        if have == 0:
            report.unavailable.append((symbol, d, empty_reason))
        return
    if replace:
        store.replace_session(symbol, d, df)
    else:
        store.upsert_bars(symbol, df)
    report.fetched.append((symbol, d, len(df)))
    if len(df) < required_bars(sess, min_bars):
        report.incomplete.append((symbol, d, len(df)))
