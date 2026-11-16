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

from functools import reduce

import numpy as np
import pandas as pd

from marketscanner.backtest.engine import AccountConfig, SKIPPED, Trade

COLUMNS = ["cash", "invested", "unrealised", "equity"]


def sample_grid(trades: list[Trade], frames: dict[str, pd.DataFrame]) -> pd.DatetimeIndex:
    parts = []
    for f in frames.values():
        if len(f):
            parts.append(f.index[f.index.minute % 5 == 0])
            parts.append(f.index[-1:])          # period end: marks equal each trade's mark_price
    for t in trades:
        for ts in (t.opened_at, t.closed_at):
            if ts is not None:
                parts.append(pd.DatetimeIndex([pd.Timestamp(ts)]).tz_convert("UTC"))
    if not parts:
        return pd.DatetimeIndex([], tz="UTC")
    return reduce(lambda a, b: a.union(b), parts).sort_values()


def build_equity(trades: list[Trade], frames: dict[str, pd.DataFrame], cfg: AccountConfig) -> pd.DataFrame:
    grid = sample_grid(trades, frames)
    n = len(grid)
    if n == 0:
        return pd.DataFrame(columns=COLUMNS, index=pd.DatetimeIndex([], tz="UTC"), dtype="float64")

    marks = {s: f["close"].reindex(grid, method="ffill").to_numpy() for s, f in frames.items() if len(f)}
    realised = np.zeros(n)
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
            realised[i1:] += t.pnl
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
