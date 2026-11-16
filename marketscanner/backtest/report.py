"""
Report files for a run: ledger.csv, equity.png, summary.txt.

OO matplotlib only (Figure + FigureCanvasAgg), no pyplot, like the alert chart.
Everything is deterministic so a rerun on the same stored bars is byte-identical.
"""
from __future__ import annotations

import csv
from pathlib import Path
from typing import Optional

import matplotlib.backends.backend_agg as _agg
import matplotlib.figure as _fig
import numpy as np
import pandas as pd
import pytz

from marketscanner import config
from marketscanner.backtest.engine import LOSS, SKIPPED, Trade, WIN
from marketscanner.backtest.metrics import Summary

_ET = pytz.timezone(config.TIMEZONE)

LEDGER_COLUMNS = [
    "id", "symbol", "side", "signal_time_et", "status", "amount", "shares", "entry",
    "take_profit", "stop_loss", "closed_time_et", "exit_price", "exit_kind", "pnl",
    "pnl_pct", "r_multiple", "trading_minutes", "sessions_held", "mark_price",
    "unrealised", "skip_reason",
]


def fmt_et(ts) -> str:
    if ts is None:
        return ""
    return pd.Timestamp(ts).tz_convert(_ET).strftime("%Y-%m-%d %H:%M ET")


def _r(x: Optional[float], nd: int) -> str:
    return "" if x is None else f"{round(float(x), nd):.{nd}f}"


def ledger_rows(trades: list[Trade]) -> list[dict]:
    rows = []
    for t in trades:
        skipped = t.status == SKIPPED
        rows.append({
            "id": t.id, "symbol": t.symbol, "side": t.side,
            "signal_time_et": fmt_et(t.signal_ts), "status": t.status,
            "amount": "" if skipped else _r(t.amount, 2),
            "shares": "" if skipped else _r(t.shares, 6),
            "entry": _r(t.entry, 4), "take_profit": _r(t.take_profit, 4), "stop_loss": _r(t.stop_loss, 4),
            "closed_time_et": fmt_et(t.closed_at), "exit_price": _r(t.exit_price, 4),
            "exit_kind": t.exit_kind or "", "pnl": _r(t.pnl, 2), "pnl_pct": _r(None if t.pnl_pct is None else t.pnl_pct * 100, 3),
            "r_multiple": _r(t.r_multiple, 2),
            "trading_minutes": "" if t.trading_minutes is None else t.trading_minutes,
            "sessions_held": "" if t.sessions_held is None else t.sessions_held,
            "mark_price": _r(t.mark_price, 4), "unrealised": _r(t.unrealised, 2),
            "skip_reason": t.skip_reason or "",
        })
    return rows


def write_ledger_csv(trades: list[Trade], path: Path) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=LEDGER_COLUMNS, lineterminator="\n")
        w.writeheader()
        w.writerows(ledger_rows(trades))


def write_text(path: Path, text: str) -> None:
    Path(path).write_text(text + "\n", encoding="utf-8")


def render_equity_figure(equity: pd.DataFrame, trades: list[Trade], summary: Summary):
    fig = _fig.Figure(figsize=(13, 6.5))
    _agg.FigureCanvasAgg(fig)
    ax = fig.add_subplot(111)
    title = f"{summary.strategy} account  {', '.join(summary.symbols)}  {summary.start} to {summary.end}"
    if equity.empty:
        ax.text(0.5, 0.5, "No data in the period", ha="center", va="center")
        ax.set_title(title)
        return fig

    x = np.arange(len(equity))
    ax.stackplot(x, equity["cash"].to_numpy(), equity["invested"].to_numpy(),
                 labels=["cash available", "invested"], colors=["#bfdbfe", "#fed7aa"], alpha=0.9)
    ax.plot(x, equity["equity"].to_numpy(), color="#111827", linewidth=1.4, label="total equity")
    ax.axhline(summary.initial_cash, color="#6b7280", linestyle=":", linewidth=0.9)
    ax.axhline(summary.min_cash, color="#b91c1c", linestyle="--", linewidth=0.9, label="minimum cash")

    grid = equity.index
    eq = equity["equity"].to_numpy()
    for t in trades:
        if t.status == SKIPPED:
            continue
        i0 = int(grid.searchsorted(pd.Timestamp(t.opened_at), side="left"))
        if i0 < len(x):
            ax.plot(x[i0], eq[i0], marker="^" if t.side == "BUY" else "v", markersize=6,
                    color="#16a34a" if t.side == "BUY" else "#dc2626", linestyle="none", zorder=5)
        if t.closed_at is not None:
            i1 = int(grid.searchsorted(pd.Timestamp(t.closed_at), side="left"))
            if i1 < len(x):
                ax.plot(x[i1], eq[i1], marker="o", markersize=5, markerfacecolor="none",
                        markeredgecolor="#15803d" if t.status == WIN else "#b91c1c", linestyle="none", zorder=5)

    dates = pd.Index(grid.tz_convert(_ET).date)
    starts = [i for i in range(len(dates)) if i == 0 or dates[i] != dates[i - 1]]
    for i in starts[1:]:
        ax.axvline(i, color="#e5e7eb", linewidth=0.6, zorder=0)
    step = max(1, len(starts) // 12)
    shown = starts[::step]
    ax.set_xticks(shown)
    ax.set_xticklabels([dates[i].strftime("%m-%d") for i in shown], rotation=45, fontsize=8)
    ax.set_xlim(0, len(x) - 1)
    ax.set_ylabel("USD")
    ax.set_title(title, fontsize=12, fontweight="bold")
    ax.legend(loc="upper left", fontsize=8, ncol=4)
    ax.grid(True, axis="y", linestyle=":", alpha=0.5)
    fig.text(0.995, 0.005, "triangle = open (up BUY, down SELL), circle = close; overnight gaps compressed",
             ha="right", va="bottom", fontsize=7, color="#6b7280")
    return fig


def save_figure(fig, path: Path, dpi: int = 100) -> None:
    fig.savefig(path, format="png", dpi=dpi, bbox_inches="tight")
