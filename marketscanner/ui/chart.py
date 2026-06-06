"""
render_chart() is a pure function: takes data, returns a matplotlib Figure.
Never calls plt.show() or writes to disk — callers decide what to do with the Figure.
"""
import time as _time
from datetime import datetime, timedelta
from typing import Optional, List

from dateutil import tz as _dateutil_tz

_LOCAL_TZ = _dateutil_tz.tzlocal()   # server's OS local timezone

import matplotlib.pyplot as plt
import mplfinance as mpf
import pandas as pd
import pytz

from marketscanner import config

_ET = pytz.timezone(config.TIMEZONE)

# How many bars to show after the last signal (or after the last bar if no signal)
_TRAIL_BARS = 10


def render_chart(
    df: pd.DataFrame,
    market: str,
    box_top: Optional[float] = None,
    box_bottom: Optional[float] = None,
    signal_times: Optional[list] = None,
) -> plt.Figure:
    """
    df: OHLCV DataFrame with UTC DatetimeIndex
    box_top / box_bottom: ORB levels drawn as dotted horizontal lines
    signal_times: list of UTC datetimes where signals fired (drawn as vertical markers)
    """
    if df.empty:
        fig, ax = plt.subplots()
        ax.text(0.5, 0.5, "No data", ha="center", va="center")
        return fig

    df_et = df.copy()
    df_et.index = df_et.index.tz_convert(_ET)

    # Trim to the visible window: market open through last signal + trail, or
    # all available bars if no signal yet. Keeps candles filling the frame.
    df_et = _trim(df_et, signal_times)

    if df_et.empty:
        fig, ax = plt.subplots()
        ax.text(0.5, 0.5, "No data in window", ha="center", va="center")
        return fig

    add_plots = []
    if box_top is not None:
        add_plots.append(
            mpf.make_addplot(
                pd.Series(box_top, index=df_et.index),
                color="blue", linestyle="dotted", width=1.2,
            )
        )
    if box_bottom is not None:
        add_plots.append(
            mpf.make_addplot(
                pd.Series(box_bottom, index=df_et.index),
                color="red", linestyle="dotted", width=1.2,
            )
        )

    orb_mask = _orb_mask(df_et)

    date_str = df_et.index[0].strftime("%Y-%m-%d")
    fig, axes = mpf.plot(
        df_et,
        type="candle",
        style="charles",
        title=f"{market} — Opening Range Breakout  {date_str} (ET)",
        ylabel="Price",
        volume=True,
        addplot=add_plots if add_plots else None,
        returnfig=True,
        figsize=(14, 7),
    )

    ax = axes[0]

    # mplfinance uses integer bar positions (0, 1, 2, ...) not datetime on the x-axis.
    # axvline/axvspan must use these integer positions, not timestamps.

    # shade ORB window
    if orb_mask.any():
        starts = df_et.index[orb_mask]
        start_pos = int(df_et.index.searchsorted(starts[0]))
        end_pos   = int(df_et.index.searchsorted(starts[-1]))
        ax.axvspan(start_pos, end_pos, alpha=0.08, color="yellow", label="ORB window")

    # signal vertical lines
    if signal_times:
        for ts in signal_times:
            ts_et = ts.astimezone(_ET)
            pos = int(df_et.index.searchsorted(ts_et))
            if 0 <= pos < len(df_et):
                ax.axvline(x=pos, color="purple", linestyle="--", linewidth=1.2)

    _add_local_tz_axis(fig, axes, df_et)

    plt.close(fig)
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


def _add_local_tz_axis(
    fig: plt.Figure,
    axes: list,
    df_et: pd.DataFrame,
) -> None:
    """
    Adds a secondary x-axis at the top of the price chart showing times in the
    server's local timezone. No-op if local UTC offset equals ET offset at the
    session midpoint (i.e., both timezones currently agree).

    Layout:
      [CET / local tz label]   ← top of price subplot (new)
      price candles
      volume bars
      [ET label]               ← bottom of volume subplot (existing mplfinance ticks)
    """
    midpoint = df_et.index[len(df_et) // 2]
    et_offset = midpoint.utcoffset()
    local_offset = midpoint.tz_convert(_LOCAL_TZ).utcoffset()
    if et_offset == local_offset:
        return  # same offset — single axis is sufficient

    local_label = _local_tz_label()
    ax_price = axes[0]  # top subplot (price)

    # All subplots share x (sharex), so tick positions are consistent.
    # Filter to positions that map to real bars.
    tick_locs = [t for t in ax_price.get_xticks()
                 if 0 <= round(t) < len(df_et)]
    if not tick_locs:
        return

    local_labels: List[str] = []
    for pos in tick_locs:
        idx = max(0, min(int(round(pos)), len(df_et) - 1))
        ts_local = df_et.index[idx].tz_convert(_LOCAL_TZ)
        local_labels.append(ts_local.strftime("%H:%M"))

    # Secondary axis at the top of the price chart
    ax2 = ax_price.twiny()
    ax2.set_xlim(ax_price.get_xlim())
    ax2.set_xticks(tick_locs)
    ax2.set_xticklabels(local_labels, fontsize=8)
    ax2.set_xlabel(local_label, fontsize=8, labelpad=3)

    # Label the bottom (ET) axis for symmetry
    ax_bottom = axes[-1]
    ax_bottom.set_xlabel("ET", fontsize=8, labelpad=3)

    # Pull the top of the figure down slightly so the new axis label doesn't
    # overlap the chart title that mplfinance places at the top.
    top = fig.subplotpars.top
    fig.subplots_adjust(top=max(top - 0.04, 0.80))


def _trim(df_et: pd.DataFrame, signal_times: Optional[list]) -> pd.DataFrame:
    """
    Keep bars from market open (09:30 ET) through:
      - last signal time + _TRAIL_BARS, if signals exist
      - last available bar, if no signals
    Falls back to the full df if no bars fall in the window.
    """
    # find today's session open in the index
    idx = df_et.index
    session_open = idx[
        (idx.hour == config.ORB_START_HOUR) & (idx.minute == config.ORB_START_MINUTE)
    ]
    if session_open.empty:
        # no 09:30 bar — use first bar in df
        start = idx[0]
    else:
        start = session_open[0]

    if signal_times:
        last_signal_et = max(ts.astimezone(_ET) for ts in signal_times)
        # find the bar position of the signal and add trail
        positions = (idx >= last_signal_et).argmax()
        end_pos = min(int(positions) + _TRAIL_BARS, len(idx) - 1)
        end = idx[end_pos]
    else:
        end = idx[-1]

    trimmed = df_et[(df_et.index >= start) & (df_et.index <= end)]
    return trimmed if not trimmed.empty else df_et


def _orb_mask(df_et: pd.DataFrame) -> pd.Series:
    idx = df_et.index
    if config.ORB_START_HOUR == config.ORB_END_HOUR:
        return (
            (idx.hour == config.ORB_START_HOUR)
            & (idx.minute >= config.ORB_START_MINUTE)
            & (idx.minute < config.ORB_END_MINUTE)
        )
    return (
        (idx.hour == config.ORB_START_HOUR) & (idx.minute >= config.ORB_START_MINUTE)
    ) | (
        (idx.hour == config.ORB_END_HOUR) & (idx.minute < config.ORB_END_MINUTE)
    )
