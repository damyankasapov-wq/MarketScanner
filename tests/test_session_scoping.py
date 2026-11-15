"""
Regression tests for cross-session contamination.

Found by /qa on 2026-07-02.
Report: .gstack/qa-reports/ (live QQQ box top sat above every candle; the
716.06 email's box bottom 702.81 was ~11 pts below the visible low).

The live feed keeps a rolling multi-day bar buffer. Before the fix, the ORB
window matched 09:30-10:30 on every date present in the buffer, so the
opening-range box was computed across several sessions and drawn as levels
that didn't bracket the current day's candles. Everything now goes through
orb_session.session_slice (the ET date of the newest bar): box, candles,
confirmation, outcome and the chart window.
"""
import pandas as pd
import pytest
import pytz

from marketscanner.strategies import orb_session
from marketscanner.strategies.opening_range import OpeningRangeStrategy
from marketscanner.ui.chart import _trim
from tests.orb_fixtures import C_GREEN_ABOVE, TRAIL, bar, df_of, utc

_ET = pytz.timezone("America/New_York")


# 2024-11-05 and -06 are both EST (UTC-5): 09:30 ET = 14:30 UTC, 10:29 = 15:29.
# The prior session carries extreme 480 low / 620 high that must NOT leak into
# today's box, and a 700/100 bar that would count as a TP/SL touch if it did.
PRIOR_SESSION = [
    bar("2024-11-05 14:30:00+00:00", 500.0, 505.0, 480.0, 502.0),  # 09:30 ET, low 480
    bar("2024-11-05 15:29:00+00:00", 502.0, 620.0, 500.0, 610.0),  # 10:29 ET, high 620
    bar("2024-11-05 16:00:00+00:00", 700.0, 700.0, 100.0, 700.0),  # 11:00 ET, wild range
]
TODAY_ORB = [
    bar("2024-11-06 14:30:00+00:00", 575.0, 578.0, 574.5, 577.0),  # 09:30 ET
    bar("2024-11-06 14:31:00+00:00", 577.0, 579.5, 576.0, 578.0),  # 09:31 ET
    bar("2024-11-06 15:29:00+00:00", 578.0, 580.0, 577.0, 579.0),  # 10:29 ET
    # today's box: high 580.0, low 574.5
]
NOW = utc(2024, 11, 6, 15, 36)


def test_orb_box_ignores_prior_session_bars():
    box = orb_session.session_box(df_of(PRIOR_SESSION + TODAY_ORB))
    # Box must reflect ONLY today's opening range, not the prior session's
    # extreme low (480) / high (620) still sitting in the buffer.
    assert box.top == pytest.approx(580.0)
    assert box.bottom == pytest.approx(574.5)


def test_orb_fires_with_todays_box_despite_prior_session():
    s = OpeningRangeStrategy("SPY")
    df = df_of(PRIOR_SESSION + TODAY_ORB + C_GREEN_ABOVE + TRAIL)
    sig = None
    for i in range(1, len(df) + 1):
        result = s.check(df.iloc[:i], {}, now=NOW)
        if result:
            sig = result
    assert sig is not None
    assert sig.direction == "UP"
    assert sig.box_top == pytest.approx(580.0)     # today's high, not 620
    assert sig.box_bottom == pytest.approx(574.5)  # today's low, not 480


def test_snapshot_candles_and_outcome_scoped_to_session():
    snap = orb_session.snapshot(df_of(PRIOR_SESSION + TODAY_ORB + C_GREEN_ABOVE + TRAIL), now=NOW)
    assert {ts.tz_convert(_ET).date().isoformat() for ts in snap.candles.index} == {"2024-11-06"}
    assert snap.proposal is not None
    assert snap.outcome.status == "OPEN"           # the prior 700/100 bar never counts as a touch


def test_session_box_computes_from_data_not_strategy_state():
    # The dashboard derives the box from the charted data, so it renders even
    # when the live strategy hasn't processed the bars (restart / after-hours
    # freshness-guard skip). Must reflect only today's ORB.
    df = df_of(PRIOR_SESSION + TODAY_ORB)
    top, bottom = OpeningRangeStrategy.session_box(df)
    assert top == pytest.approx(580.0)     # today's high, not the prior 620
    assert bottom == pytest.approx(574.5)  # today's low, not the prior 480


def test_session_box_none_when_no_orb_bars():
    # empty frame → no box
    assert OpeningRangeStrategy.session_box(pd.DataFrame()) == (None, None)
    # only a pre-market bar (08:00 ET = 13:00 UTC) → no ORB window bars
    premarket = bar("2024-11-06 13:00:00+00:00", 570.0, 571.0, 569.0, 570.5)
    assert OpeningRangeStrategy.session_box(premarket) == (None, None)


def test_chart_trim_keeps_only_current_session():
    df = df_of(PRIOR_SESSION + TODAY_ORB)
    df_et = df.copy()
    df_et.index = df_et.index.tz_convert(_ET)
    trimmed = _trim(df_et)
    sessions = {ts.date() for ts in trimmed.index}
    assert sessions == {trimmed.index[-1].date()}          # a single session
    assert trimmed.index[-1].date().isoformat() == "2024-11-06"  # today, not prior
