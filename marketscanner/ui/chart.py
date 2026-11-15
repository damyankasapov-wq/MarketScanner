"""
render_chart() is a pure function: takes an OrbSnapshot, returns a matplotlib
Figure. Never shows or writes anything — callers decide what to do with it.

Thread-safety: only the object-oriented matplotlib API is used (Figure +
FigureCanvasAgg, mplfinance in external-axes mode). No pyplot, so figures are
never registered with a global manager and nothing needs closing.

Drawing order (all on mplfinance integer x positions):
  1. candles (CONFIRM_CANDLE_MINUTES-wide, complete buckets only) + volume
  2. pre-market shading and the 09:30 marker (no-op under regular-hours feeds)
  3. orange opening-range box over the first hour
  4. dashed levels: box top, box bottom, take profit, stop loss (right-edge labels)
  5. position zones (green entry→TP, red entry→SL) from the confirmation candle
  6. outcome marker (TP hit / SL hit) on the exit candle
  7. title, local-time axis
"""
import time as _time
from datetime import datetime
from typing import List, Optional

from dateutil import tz as _dateutil_tz

_LOCAL_TZ = _dateutil_tz.tzlocal()   # server's OS local timezone

import matplotlib.backends.backend_agg as _mpl_agg
import matplotlib.figure as _mpl_figure
from matplotlib.figure import Figure
from matplotlib.patches import Rectangle
import mplfinance as mpf
import pandas as pd
import pytz

from marketscanner import config
from marketscanner.strategies.orb_session import OrbSnapshot

_ET = pytz.timezone(config.TIMEZONE)

# palette
_UP, _DOWN = "#16a34a", "#dc2626"
_BOX = "#f59e0b"
_BOX_TEXT = "#b45309"
_LEVEL_BOX = "#7c3aed"
_TP = "#15803d"
_SL = "#b91c1c"
_ZONE_WIN, _ZONE_LOSS = "#22c55e", "#ef4444"
_INK = "#111827"
_PILL = dict(boxstyle="round,pad=0.15", fc="white", ec="none", alpha=0.8)

_MARKET_COLORS = mpf.make_marketcolors(
    up=_UP, down=_DOWN, edge="inherit", wick="inherit", volume="in",
)
_STYLE = mpf.make_mpf_style(base_mpf_style="charles", marketcolors=_MARKET_COLORS, gridstyle=":")


def render_chart(snapshot: OrbSnapshot, market: str) -> Figure:
    """
    snapshot: orb_session.snapshot(...) for the session to draw
    market:   symbol for the title
    """
    candles = snapshot.candles
    if candles is None or candles.empty:
        return _message_figure("No data")

    df_et = candles.copy()
    df_et.index = df_et.index.tz_convert(_ET)
    df_et = _trim(df_et)
    if df_et.empty:
        return _message_figure("No data in window")

    n = len(df_et)
    idx = df_et.index
    right_edge = n + 0.5                      # xlim padded 1.5 candles past the last centre

    fig = _mpl_figure.Figure(figsize=(14, 7))
    _mpl_agg.FigureCanvasAgg(fig)
    gs = fig.add_gridspec(2, 1, height_ratios=[3, 1], hspace=0.06)
    ax = fig.add_subplot(gs[0])
    ax_vol = fig.add_subplot(gs[1], sharex=ax)

    # 1. candles + volume
    mpf.plot(df_et, type="candle", style=_STYLE, ax=ax, volume=ax_vol,
             warn_too_much_data=100_000)
    ax.set_ylabel("Price")
    ax_vol.set_ylabel("Volume")
    ax.tick_params(axis="x", labelbottom=False)
    ax.set_xlabel("")
    ax.grid(True, linestyle=":", linewidth=0.6, alpha=0.6)
    ax_vol.grid(True, linestyle=":", linewidth=0.6, alpha=0.6)

    y_lo = float(df_et["low"].min())
    y_hi = float(df_et["high"].max())

    # 2. pre-market shading and the 09:30 marker (candles whose bucket start is
    #    before 09:30 ET — zero under the current regular-hours feed)
    session_open = _ET.localize(datetime.combine(
        idx[0].date(), datetime.min.time()).replace(
        hour=config.ORB_START_HOUR, minute=config.ORB_START_MINUTE))
    open_pos = int((idx < session_open).sum())
    if open_pos > 0:
        ax.axvspan(-0.5, open_pos - 0.5, alpha=0.06, color="gray", zorder=0)
        ax.axvline(x=open_pos - 0.5, color="#64748b", linestyle="--", linewidth=0.9)
        if open_pos >= 2:
            ax.text((open_pos - 1) / 2, 0.97, "pre-market",
                    transform=ax.get_xaxis_transform(), ha="center", va="top",
                    fontsize=7, color="#94a3b8", fontstyle="italic")
        ax.text(open_pos - 0.5, 0.97, "  9:30", transform=ax.get_xaxis_transform(),
                ha="left", va="top", fontsize=7, color="#64748b")

    box = snapshot.box
    prop = snapshot.proposal
    conf = snapshot.confirmation
    outcome = snapshot.outcome

    # 3. opening-range box
    if box is not None:
        orb_end = session_open.replace(hour=config.ORB_END_HOUR, minute=config.ORB_END_MINUTE)
        orb_mask = (idx >= session_open) & (idx < orb_end)
        n_orb = int(orb_mask.sum())
        if n_orb:
            first = int(orb_mask.argmax())
            ax.add_patch(Rectangle((first - 0.5, box.bottom), n_orb, box.delta,
                                   facecolor=_BOX, alpha=0.18, edgecolor=_BOX,
                                   linewidth=1.0, zorder=0))
            ax.text(first - 0.5 + n_orb / 2, box.top,
                    f" opening range {config.ORB_START_HOUR:02d}:{config.ORB_START_MINUTE:02d}"
                    f"–{config.ORB_END_HOUR:02d}:{config.ORB_END_MINUTE:02d} ",
                    ha="center", va="bottom", fontsize=7.5, color=_BOX_TEXT)
        y_lo, y_hi = min(y_lo, box.bottom), max(y_hi, box.top)

        # 4. levels
        levels = [(box.top, f"box top {box.top:.2f}", _LEVEL_BOX),
                  (box.bottom, f"box bottom {box.bottom:.2f}", _LEVEL_BOX)]
        if prop is not None:
            levels += [(prop.take_profit, f"take profit {prop.take_profit:.2f}", _TP),
                       (prop.stop_loss, f"stop loss {prop.stop_loss:.2f}", _SL)]
            y_lo = min(y_lo, prop.stop_loss, prop.take_profit)
            y_hi = max(y_hi, prop.stop_loss, prop.take_profit)
        for y, label, colour in levels:
            ax.axhline(y, color=colour, linestyle="--", linewidth=1.1, zorder=1)
            ax.text(right_edge - 0.2, y, label, ha="right", va="bottom", fontsize=7.5,
                    color=colour,
                    bbox=_PILL)

    # 5. position zones + 6. outcome marker
    if prop is not None and conf is not None:
        conf_et = pd.Timestamp(conf.candle_open).tz_convert(_ET)
        conf_hits = idx.get_indexer([conf_et])
        conf_pos = int(conf_hits[0]) if conf_hits[0] >= 0 else None
        if conf_pos is not None:
            zone_start = conf_pos - 0.5
            exit_pos: Optional[int] = None
            zone_end = right_edge
            resolved = outcome is not None and outcome.status in ("TP_HIT", "SL_HIT")
            if resolved:
                exit_et = pd.Timestamp(outcome.exit_candle).tz_convert(_ET)
                hit = idx.get_indexer([exit_et])
                if hit[0] >= 0:
                    exit_pos = int(hit[0])
                    zone_end = exit_pos + 0.5
                else:
                    # touch happened inside the still-incomplete final bucket:
                    # mark it just past the last plotted candle, zones to the edge
                    exit_pos = n
                    zone_end = right_edge
            width = zone_end - zone_start
            tp, sl, entry = prop.take_profit, prop.stop_loss, prop.entry
            ax.add_patch(Rectangle((zone_start, min(entry, tp)), width, abs(tp - entry),
                                   facecolor=_ZONE_WIN, alpha=0.15, edgecolor="none", zorder=0))
            ax.add_patch(Rectangle((zone_start, min(entry, sl)), width, abs(entry - sl),
                                   facecolor=_ZONE_LOSS, alpha=0.15, edgecolor="none", zorder=0))
            ax.plot([zone_start, zone_end], [entry, entry], color=_INK, linewidth=1.0, zorder=2)
            text_y = entry + prop.delta * (0.55 if prop.side == "BUY" else -0.55)
            ax.annotate(f"{prop.side} @ {entry:.2f}", xy=(conf_pos, entry),
                        xytext=(max(conf_pos - 4.5, 0.5), text_y),
                        fontsize=8, fontweight="bold", color=_INK,
                        arrowprops=dict(arrowstyle="->", color=_INK, lw=0.9), zorder=6)

            if resolved and exit_pos is not None:
                hit_et = pd.Timestamp(outcome.exit_ts).tz_convert(_ET).strftime("%H:%M")
                if outcome.status == "TP_HIT":
                    ax.plot(exit_pos, tp, marker="v", markersize=11, color=_TP, zorder=5)
                    ax.text(exit_pos, tp + prop.delta * 0.08, f"TP hit {hit_et}  +1R",
                            ha="center", va="bottom", fontsize=8, fontweight="bold", color=_TP,
                            bbox=_PILL, zorder=6)
                else:
                    ax.plot(exit_pos, sl, marker="^", markersize=11, color=_SL, zorder=5)
                    ax.text(exit_pos, sl - prop.delta * 0.08, f"SL hit {hit_et}  −1R",
                            ha="center", va="top", fontsize=8, fontweight="bold", color=_SL,
                            bbox=_PILL, zorder=6)

    # limits: labels never overlap the right-most candles; levels above/below
    # the candle range stay visible
    pad = (y_hi - y_lo) * 0.08 or 1.0
    ax.set_xlim(-1, right_edge)
    ax.set_ylim(y_lo - pad, y_hi + pad)

    # 7. title + local-time axis
    date_str = idx[0].strftime("%Y-%m-%d")
    fig.suptitle(
        f"{market} — Opening Range Breakout  {date_str} (ET, {snapshot.candle_minutes}-min)",
        fontsize=13, fontweight="bold", y=0.96,
    )
    _add_local_tz_axis(fig, [ax, ax_vol], df_et)
    return fig


def _message_figure(text: str) -> Figure:
    fig = _mpl_figure.Figure()
    _mpl_agg.FigureCanvasAgg(fig)
    ax = fig.add_subplot(111)
    ax.text(0.5, 0.5, text, ha="center", va="center")
    return fig


def _local_tz_label() -> str:
    """
    Return a short label for the local timezone, e.g. 'CET', 'UTC+2'.
    Falls back to offset notation if the OS returns a long name (common on Windows).
    """
    abbr = _time.strftime("%Z")   # OS-level tz name; avoids pandas Timestamp quirks
    if len(abbr) > 6:
        # Windows returns full names like "Central European Summer Time"
        offset_s = _LOCAL_TZ.utcoffset(datetime.now()).total_seconds()
        hours = int(offset_s / 3600)
        abbr = f"UTC{hours:+d}"
    return abbr


def _add_local_tz_axis(fig: Figure, axes: list, df_et: pd.DataFrame) -> None:
    """
    Adds a secondary x-axis at the top of the price chart showing times in the
    server's local timezone. No-op if local UTC offset equals ET offset at the
    session midpoint (i.e., both timezones currently agree).
    """
    midpoint = df_et.index[len(df_et) // 2]
    et_offset = midpoint.utcoffset()
    local_offset = midpoint.tz_convert(_LOCAL_TZ).utcoffset()
    if et_offset == local_offset:
        return  # same offset — single axis is sufficient

    local_label = _local_tz_label()
    ax_price = axes[0]  # top subplot (price)

    tick_locs = [t for t in ax_price.get_xticks() if 0 <= round(t) < len(df_et)]
    if not tick_locs:
        return

    local_labels: List[str] = []
    for pos in tick_locs:
        i = max(0, min(int(round(pos)), len(df_et) - 1))
        local_labels.append(df_et.index[i].tz_convert(_LOCAL_TZ).strftime("%H:%M"))

    ax2 = ax_price.twiny()
    ax2.set_xlim(ax_price.get_xlim())
    ax2.set_xticks(tick_locs)
    ax2.set_xticklabels(local_labels, fontsize=8)
    ax2.set_xlabel(f"Local time ({local_label})", fontsize=8, labelpad=3)

    fig.text(0.5, 0.01, "Market time (ET)", ha="center", va="bottom", fontsize=8, color="black")

    # Pull the top of the figure down slightly so the local-time axis label
    # doesn't overlap the chart title.
    top = fig.subplotpars.top
    fig.subplots_adjust(top=max(top - 0.06, 0.80))


def _trim(df_et: pd.DataFrame) -> pd.DataFrame:
    """
    Keep the CURRENT session (the ET date of the newest candle) from market
    open (09:30 ET) through the newest complete candle. Scoping to a single
    date stops a rolling multi-day buffer from rendering several sessions in
    one frame. Falls back to the full frame if the result would be empty.
    """
    idx = df_et.index
    session_date = idx[-1].date()
    day_idx = idx[idx.date == session_date]
    if day_idx.empty:
        return df_et
    open_bars = day_idx[
        (day_idx.hour == config.ORB_START_HOUR) & (day_idx.minute == config.ORB_START_MINUTE)
    ]
    start = open_bars[0] if len(open_bars) else day_idx[0]
    trimmed = df_et[(df_et.index >= start) & (df_et.index <= day_idx[-1])]
    return trimmed if not trimmed.empty else df_et
