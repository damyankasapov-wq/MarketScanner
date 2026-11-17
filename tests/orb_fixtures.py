"""
Frozen 1-minute fixtures shared by the ORB tests.

Reference session 2024-11-06 (EST, UTC-5): 09:30 ET = 14:30 UTC, 10:30 ET = 15:30 UTC.
"""
from datetime import datetime, timezone

import pandas as pd

from marketscanner.strategies.orb_session import Box, Confirmation, Proposal


def bar(dt_str, o, h, l, c, v=1_000.0):
    ts = pd.Timestamp(dt_str)
    ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
    return pd.DataFrame(
        [{"open": float(o), "high": float(h), "low": float(l), "close": float(c), "volume": float(v)}],
        index=pd.DatetimeIndex([ts], tz="UTC"),
    )


def df_of(bars):
    return pd.concat(bars).sort_index()


def flat(start, n, price=579.0, step_min=1):
    """n consecutive 1-minute bars from start, all o=h=l=c=price."""
    t0 = pd.Timestamp(start, tz="UTC")
    return [bar(t0 + pd.Timedelta(minutes=i * step_min), price, price, price, price) for i in range(n)]


def candle_bars(start, o, h, l, c):
    """
    Five 1-minute bars whose 5-minute aggregate is exactly O/H/L/C = o/h/l/c
    (first open, max high, min low, last close). Requires l <= o, c <= h.
    """
    t0 = pd.Timestamp(start, tz="UTC")
    m = lambda i: t0 + pd.Timedelta(minutes=i)
    return [
        bar(m(0), o, o, o, o),
        bar(m(1), o, h, o, h),
        bar(m(2), h, h, l, l),
        bar(m(3), l, l, l, l),
        bar(m(4), l, max(l, c), l, c),
    ]


def utc(y, mo, d, h, mi, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc)


# Opening range for 2024-11-06: box top 580.00, bottom 574.50 (delta 5.50)
ORB_BARS = [
    bar("2024-11-06 14:30:00+00:00", 575.00, 578.00, 574.50, 577.00),  # 09:30 ET
    bar("2024-11-06 14:31:00+00:00", 577.00, 579.50, 576.00, 578.00),  # 09:31 ET
    bar("2024-11-06 15:29:00+00:00", 578.00, 580.00, 577.00, 579.00),  # 10:29 ET
]
BOX = Box(top=580.00, bottom=574.50)

# 10:30-10:35 ET candles (bucket start 15:30 UTC)
C_WICK_INSIDE   = candle_bars("2024-11-06 15:30", 580.2, 581.0, 579.8, 580.8)  # close out, low in
C_RED_ABOVE     = candle_bars("2024-11-06 15:30", 581.0, 581.5, 580.5, 580.6)  # fully above, red
C_GREEN_ABOVE   = candle_bars("2024-11-06 15:30", 580.5, 581.5, 580.2, 581.2)  # fully above, green
C_RED_BELOW     = candle_bars("2024-11-06 15:30", 574.0, 574.4, 573.0, 573.2)  # fully below, red
C_GREEN_ABOVE_2 = candle_bars("2024-11-06 15:35", 581.3, 582.5, 581.1, 582.0)
TRAIL   = [bar("2024-11-06 15:35:00+00:00", 581.2, 581.3, 581.1, 581.2)]      # proves 15:30 bucket complete
TRAIL_2 = [bar("2024-11-06 15:40:00+00:00", 582.0, 582.1, 581.9, 582.0)]

CONF_UP = Confirmation(
    candle_open=utc(2024, 11, 6, 15, 30),
    candle_close=utc(2024, 11, 6, 15, 35),
    direction="UP", open=580.5, high=581.5, low=580.2, close=581.2,
)
PROP_BUY = Proposal("BUY", 581.2, 586.7, 575.7, 5.5)
