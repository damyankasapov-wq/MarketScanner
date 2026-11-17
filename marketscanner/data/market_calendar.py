"""
NYSE trading calendar for the backfill: which weekdays are sessions, and when
each one opens and closes (half-days close at 13:00 ET).

Offline: pandas-market-calendars ships the holiday rules, no network needed.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pandas as pd
import pandas_market_calendars as mcal

_NYSE = mcal.get_calendar("NYSE")


@dataclass(frozen=True)
class Session:
    open: pd.Timestamp     # UTC
    close: pd.Timestamp    # UTC; 13:00 ET on half-days

    @property
    def minutes(self) -> int:
        return int((self.close - self.open).total_seconds() // 60)


def sessions(start: date, end: date) -> dict[date, Session]:
    """Every NYSE session with a New York date in [start, end], keyed by that date."""
    if end < start:
        return {}
    sched = _NYSE.schedule(start_date=start, end_date=end)
    return {
        ts.date(): Session(open=row.market_open.tz_convert("UTC"), close=row.market_close.tz_convert("UTC"))
        for ts, row in sched.iterrows()
    }


def within_session(df: pd.DataFrame, session: Session) -> pd.DataFrame:
    """Only the 1-minute bars that start inside [open, close) of the session."""
    if df is None or df.empty:
        return df
    return df[(df.index >= session.open) & (df.index < session.close)]
