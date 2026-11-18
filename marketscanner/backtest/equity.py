"""
The account curve, sampled at every 5-minute boundary and at every trade event.

For each sample time t:
    invested(t)   = sum of the amounts of trades open at t        (opened <= t < closed)
    unrealised(t) = sum of side x shares x (mark(t) - entry)      for those trades
    realised(t)   = sum of pnl of trades closed at or before t
    equity(t)     = initial_cash + realised(t) + unrealised(t)    <- the one identity
    cash(t)       = initial_cash + realised(t) - invested(t)
so cash + invested + unrealised == equity at every sample, by construction.
Marks are each symbol's latest close, carried forward over the night and over
data holes.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from marketscanner.backtest.engine import AccountConfig, SKIPPED, Trade

COLUMNS = ["cash", "invested", "unrealised", "equity"]


def sample_grid(trades: list[Trade], frames: dict[str, pd.DataFrame]) -> pd.DatetimeIndex:
    """
    Every 5-minute bar of every frame, each frame's last bar (period end: marks
    equal each trade's mark_price) and every trade open/close, sorted and unique.
    Built in one step from int64 nanoseconds: unioning one timestamp at a time
    re-sorted the whole grid per trade event (~30 s on a 10-year grid).

    Same timestamps as the old union for the UTC, duplicate-free frames the
    store returns; the result is always UTC at nanosecond resolution, and
    duplicate bar timestamps collapse to one point. Trade times must be
    timezone-aware (a naive one raises, as before).
    """
    parts: list[np.ndarray] = []
    for f in frames.values():
        if len(f):
            idx = f.index.tz_convert("UTC").as_unit("ns")
            parts.append(idx[idx.minute % 5 == 0].asi8)
            parts.append(idx[-1:].asi8)
    events = [pd.Timestamp(ts).tz_convert("UTC").value
              for t in trades for ts in (t.opened_at, t.closed_at) if ts is not None]
    if events:
        parts.append(np.asarray(events, dtype=np.int64))
    if not parts:
        return pd.DatetimeIndex([], tz="UTC")
    values = np.unique(np.concatenate(parts))
    return pd.DatetimeIndex(values.view("datetime64[ns]")).tz_localize("UTC")


def build_equity(trades: list[Trade], frames: dict[str, pd.DataFrame], cfg: AccountConfig) -> pd.DataFrame:
    grid = sample_grid(trades, frames)
    n = len(grid)
    if n == 0:
        return pd.DataFrame(columns=COLUMNS, index=pd.DatetimeIndex([], tz="UTC"), dtype="float64")

    marks = {s: f["close"].reindex(grid, method="ffill").to_numpy() for s, f in frames.items() if len(f)}
    realised_delta = np.zeros(n + 1)    # realised P&L steps up at each close; cumsum once at the end
    invested = np.zeros(n)
    unreal = np.zeros(n)
    for t in trades:
        if t.status == SKIPPED:
            continue
        i0 = int(grid.searchsorted(pd.Timestamp(t.opened_at), side="left"))
        i1 = int(grid.searchsorted(pd.Timestamp(t.closed_at), side="left")) if t.closed_at is not None else n
        if i1 > i0:
            invested[i0:i1] += t.amount
            m = marks[t.symbol][i0:i1]
            m = np.where(np.isnan(m), t.entry, m)
            unreal[i0:i1] += t.sign * t.shares * (m - t.entry)
        if t.closed_at is not None:
            realised_delta[i1] += t.pnl
    realised = np.cumsum(realised_delta[:n])
    out = pd.DataFrame(
        {
            "cash": cfg.initial_cash + realised - invested,
            "invested": invested,
            "unrealised": unreal,
            "equity": cfg.initial_cash + realised + unreal,
        },
        index=grid,
    )
    return out


def max_drawdown(equity: pd.Series) -> tuple[float, float]:
    """(largest peak-to-trough fall in dollars (<= 0), the same as % of the peak (<= 0))."""
    if equity.empty:
        return 0.0, 0.0
    peak = equity.cummax()
    dd = equity - peak
    pct = (dd / peak.where(peak > 0)).min()
    return float(dd.min()), float(0.0 if pd.isna(pct) else pct * 100.0)
