"""
Synthetic but realistic sessions for the backtest tests (November 2024, EST:
09:30 ET = 14:30 UTC, 390 one-minute bars per session).

Opening hour (bars 0-59): flat at 577, bar 30 sets the box low, bar 59 the box high.
Defaults: box 574.5 to 580.0, delta 5.5.
Confirmation candle (5 bars starting at `confirm_at`, default 60 = 10:30 ET), then flat at the close:
  UP:   O 580.5 H 581.5 L 580.2 C 581.2  -> BUY  at 581.2, TP 586.7, SL 575.7
  DOWN: O 574.0 H 574.4 L 573.0 C 573.2  -> SELL at 573.2, TP 567.7, SL 578.7
  None: no confirmation (price stays inside the box)
The entry time (confirmation candle close) is bar index confirm_at + 5 (65 by default = 10:35 ET = 15:35 UTC).

A "quiet" follow-up day for carry-over tests: make_session(day, None, box_low=576.0, box_high=579.0)
stays between 576 and 579, i.e. above a BUY's stop (575.7) and below its target (586.7).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

ENTRY_BAR = 65


def make_session(
    day: str,
    direction: str | None = "UP",
    overrides: dict | None = None,
    n: int = 390,
    box_low: float = 574.5,
    box_high: float = 580.0,
    confirm_at: int = 60,
) -> pd.DataFrame:
    idx = pd.date_range(f"{day} 14:30", periods=n, freq="1min", tz="UTC")
    o = np.full(n, 577.0)
    h = np.full(n, 577.0)
    l = np.full(n, 577.0)
    c = np.full(n, 577.0)
    l[30] = box_low
    o[59], h[59], l[59], c[59] = 578.0, box_high, 577.0, 578.0
    if direction is not None:
        co, ch, cl, cc = (580.5, 581.5, 580.2, 581.2) if direction == "UP" else (574.0, 574.4, 573.0, 573.2)
        rows = [(co, co, co, co), (co, ch, co, ch), (ch, ch, cl, cl), (cl, cl, cl, cl), (cl, max(cl, cc), cl, cc)]
        for k, (a, b, d, e) in enumerate(rows):
            j = confirm_at + k
            o[j], h[j], l[j], c[j] = a, b, d, e
        j = confirm_at + 5
        o[j:], h[j:], l[j:], c[j:] = cc, cc, cc, cc
    for i, (a, b, d, e) in (overrides or {}).items():
        o[i], h[i], l[i], c[i] = a, b, d, e
    return pd.DataFrame({"open": o, "high": h, "low": l, "close": c, "volume": np.full(n, 1000.0)}, index=idx)


def stack(*sessions: pd.DataFrame) -> pd.DataFrame:
    return pd.concat(sessions).sort_index()


def quiet(day: str, overrides: dict | None = None, box_high: float = 579.0) -> pd.DataFrame:
    """A no-signal day. Use box_high=578.0 after a SELL (its stop is 578.7)."""
    return make_session(day, None, overrides=overrides, box_low=576.0, box_high=box_high)
