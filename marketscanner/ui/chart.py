"""
render_chart() is a pure function: takes data, returns a matplotlib Figure.
Never calls plt.show() or writes to disk — callers decide what to do with the Figure.
"""
from typing import Optional

import matplotlib.pyplot as plt
import mplfinance as mpf
import pandas as pd
import pytz

from marketscanner import config

_ET = pytz.timezone(config.TIMEZONE)


def render_chart(
    df: pd.DataFrame,
    market: str,
    box_top: Optional[float] = None,
    box_bottom: Optional[float] = None,
    signal_times: Optional[list] = None,
) -> plt.Figure:
    """
    df: OHLCV DataFrame with UTC DatetimeIndex
    box_top / box_bottom: ORB levels to draw as dotted horizontal lines
    signal_times: list of UTC datetimes where signals fired (drawn as vertical markers)
    """
    if df.empty:
        fig, ax = plt.subplots()
        ax.text(0.5, 0.5, "No data", ha="center", va="center")
        return fig

    df_et = df.copy()
    df_et.index = df_et.index.tz_convert(_ET)

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

    # shade the ORB window
    orb_mask = _orb_mask(df_et)

    fig, axes = mpf.plot(
        df_et,
        type="candle",
        style="charles",
        title=f"{market} — Opening Range Breakout",
        ylabel="Price",
        volume=True,
        addplot=add_plots if add_plots else None,
        returnfig=True,
        figsize=(14, 7),
    )

    ax = axes[0]
    # shade ORB window
    if orb_mask.any():
        starts = df_et.index[orb_mask]
        ax.axvspan(starts[0], starts[-1], alpha=0.05, color="yellow", label="ORB window")

    # vertical lines at signal times
    if signal_times:
        for ts in signal_times:
            ts_et = ts.astimezone(_ET)
            ax.axvline(x=ts_et, color="purple", linestyle="--", linewidth=1)

    plt.close(fig)  # prevent implicit display; caller owns the figure
    return fig


def _orb_mask(df_et: pd.DataFrame) -> pd.Series:
    idx = df_et.index
    in_window = (
        (idx.hour == config.ORB_START_HOUR) & (idx.minute >= config.ORB_START_MINUTE)
    ) | (
        (idx.hour == config.ORB_END_HOUR) & (idx.minute < config.ORB_END_MINUTE)
    )
    # handle case where start and end are in the same hour
    if config.ORB_START_HOUR == config.ORB_END_HOUR:
        in_window = (
            (idx.hour == config.ORB_START_HOUR)
            & (idx.minute >= config.ORB_START_MINUTE)
            & (idx.minute < config.ORB_END_MINUTE)
        )
    return in_window
