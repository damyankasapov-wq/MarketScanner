"""
Regression: the opening range is still forming between 09:30 and 10:30 ET.

Found by /qa on the live dashboard, 2026-11-18. At 09:58 ET the cards read
"waiting for confirmation · box ..." while the box was still moving, and at
09:30:22 ET the live strategy logged "flat opening range, no proposal today"
for all three symbols from a single just-opened bar (high == low).
Report: .gstack/qa-reports/qa-report-openmediavault-local-2026-11-18.md
"""
import logging

from marketscanner.strategies import orb_session
from marketscanner.strategies.opening_range import OpeningRangeStrategy
from marketscanner.web import server
from tests.orb_fixtures import C_GREEN_ABOVE, ORB_BARS, TRAIL, bar, df_of, utc

MID = utc(2024, 11, 6, 14, 58)                          # 09:58 ET
PARTIAL = df_of(ORB_BARS[:2])                           # bars 09:30 and 09:31 only
OPENING_BAR = df_of([bar("2024-11-06 14:30:00+00:00", 579.0, 579.0, 579.0, 579.0)])


def test_mid_range_card_says_the_range_is_still_forming():
    snap = orb_session.snapshot(PARTIAL, 5, now=MID)
    assert snap.box is not None and snap.range_final is False
    assert server.status_line(snap) == "building range until 10:30 ET · so far 574.50–579.50"
    assert server.status_payload(snap)["range_final"] is False


def test_a_lone_opening_bar_is_not_called_flat():
    snap = orb_session.snapshot(OPENING_BAR, 5, now=utc(2024, 11, 6, 14, 30, 22))
    assert snap.box.delta == 0 and snap.range_final is False
    assert "flat" not in server.status_line(snap)


def test_live_strategy_does_not_warn_flat_before_1030(caplog):
    strat = OpeningRangeStrategy("SPY")
    with caplog.at_level(logging.WARNING):
        assert strat.check(OPENING_BAR, {}, now=utc(2024, 11, 6, 14, 30, 22)) is None
    assert not [r for r in caplog.records if "flat opening range" in r.getMessage()]
    assert strat._warned_flat_session is None


def test_range_is_final_from_1030_by_bar_or_by_clock():
    assert orb_session.snapshot(df_of(ORB_BARS + C_GREEN_ABOVE + TRAIL), 5, now=None).range_final
    # no bar at 10:30 yet (feed gap), but the clock is past it
    assert orb_session.snapshot(df_of(ORB_BARS), 5, now=utc(2024, 11, 6, 15, 30, 5)).range_final
    assert not orb_session.snapshot(df_of(ORB_BARS), 5, now=utc(2024, 11, 6, 15, 29, 50)).range_final


def test_flat_range_after_1030_still_says_flat():
    flat_day = df_of([bar(f"2024-11-06 {h}:00+00:00", 579.0, 579.0, 579.0, 579.0)
                      for h in ("14:30", "14:31", "15:29", "15:30")])
    snap = orb_session.snapshot(flat_day, 5, now=utc(2024, 11, 6, 15, 31))
    assert snap.range_final and server.status_line(snap) == "flat opening range — no proposal"
