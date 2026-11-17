"""Regression: ISSUE-005 - four separate holidays were printed as one range
"2026-05-25 to 2026-09-07 (4 sessions)", reading as if the whole summer were missing.
Found by /qa on 2026-11-17 (real run against Postgres + Alpaca)
Report: .gstack/qa-reports/qa-report-backtest-cli-2026-11-17.md
"""
from datetime import date

from marketscanner.backtest.metrics import Coverage, format_coverage
from marketscanner.data.backfill import BackfillReport

HOLIDAY = "no data returned (holiday?)"


def _lines(unavailable):
    return format_coverage(Coverage(sessions=[], backfill=BackfillReport(unavailable=unavailable))).splitlines()


def test_separate_holidays_stay_separate_lines():
    days = [date(2026, 5, 25), date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7)]
    lines = _lines([("SPY", d, HOLIDAY) for d in days])
    assert [l for l in lines if "unavailable" in l] == [f"    unavailable SPY {d}: {HOLIDAY}" for d in days]


def test_friday_to_monday_is_one_run_but_a_skipped_weekday_breaks_it():
    why = "older than yfinance's 30-day window and not archived"
    days = [date(2026, 5, 7), date(2026, 5, 8), date(2026, 5, 11), date(2026, 5, 13)]   # Thu Fri Mon, Wed
    lines = _lines([("SPY", d, why) for d in days])
    assert f"    unavailable SPY 2026-05-07 to 2026-05-11 (3 sessions): {why}" in lines
    assert f"    unavailable SPY 2026-05-13: {why}" in lines
