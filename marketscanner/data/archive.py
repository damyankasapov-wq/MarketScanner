"""
archive_frames: save the bars the live scanner holds into a BarStore.

Called from the midnight reset on its own background thread, from COPIED frames,
after the strategies have been reset and the feed cleared, so the archive can
never delay or break the reset. It therefore never raises: failures come back in
ArchiveResult.errors and are logged by the caller.

Session dates are New York dates (a 19:30 ET bar is 00:30 UTC the next day and
still belongs to the earlier session).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date

import pandas as pd

from marketscanner.data.bar_store import BarStore, et_dates

log = logging.getLogger(__name__)


@dataclass
class ArchiveResult:
    written: dict[str, list[date]] = field(default_factory=dict)   # symbol -> ET session dates saved
    errors: dict[str, str] = field(default_factory=dict)           # symbol -> error text


def archive_frames(store: BarStore, frames: dict[str, pd.DataFrame]) -> ArchiveResult:
    result = ArchiveResult()
    for symbol, df in frames.items():
        try:
            if df is None or df.empty:
                continue
            store.upsert_bars(symbol, df)
            result.written[symbol] = et_dates(df.index if df.index.tz is not None else df.index.tz_localize("UTC"))
        except Exception as exc:  # noqa: BLE001 - the contract is "never raise"
            result.errors[symbol] = repr(exc)
            log.error("bars archive failed for %s: %r", symbol, exc)
    return result
