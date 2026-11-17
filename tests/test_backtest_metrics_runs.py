"""Regression: ISSUE-005 - four separate holidays were printed as one range
"2026-05-25 to 2026-09-07 (4 sessions)", reading as if the whole summer were missing.
Found by /qa on 2026-11-17 (real run against Postgres + Alpaca)
Report: .gstack/qa-reports/qa-report-backtest-cli-2026-11-17.md
"""
from datetime import date

from marketscanner.backtest.metrics import Coverage, format_coverage
from marketscanner.data.backfill import BackfillReport

from marketscanner.data.backfill import REASON_NO_DATA


def _lines(unavailable):
    return format_coverage(Coverage(sessions=[], backfill=BackfillReport(unavailable=unavailable))).splitlines()


def test_sessions_far_apart_stay_separate_lines():
    days = [date(2026, 5, 26), date(2026, 6, 18), date(2026, 7, 6), date(2026, 9, 8)]
    lines = _lines([("SPY", d, REASON_NO_DATA) for d in days])
    assert [l for l in lines if "unavailable" in l] == [f"    unavailable SPY {d}: {REASON_NO_DATA}" for d in days]


def test_friday_to_monday_is_one_run_but_a_skipped_weekday_breaks_it():
    why = "older than yfinance's 30-day window and not archived"
    days = [date(2026, 5, 7), date(2026, 5, 8), date(2026, 5, 11), date(2026, 5, 13)]   # Thu Fri Mon, Wed
    lines = _lines([("SPY", d, why) for d in days])
    assert f"    unavailable SPY 2026-05-07 to 2026-05-11 (3 sessions): {why}" in lines
    assert f"    unavailable SPY 2026-05-13: {why}" in lines


def test_runs_continue_across_a_market_holiday():
    why = "older than yfinance's 30-day window and not archived"
    days = [date(2026, 5, 21), date(2026, 5, 22), date(2026, 5, 26)]         # Memorial Day 05-25 in between
    assert f"    unavailable SPY 2026-05-21 to 2026-05-26 (3 sessions): {why}" in _lines([("SPY", d, why) for d in days])


def test_unavailable_lines_come_out_in_date_order():
    why = "fetch failed: boom"
    lines = _lines([("SPY", date(2026, 11, 5), why), ("SPY", date(2026, 5, 6), why)])
    got = [l for l in lines if "unavailable" in l]
    assert got == [f"    unavailable SPY 2026-05-06: {why}", f"    unavailable SPY 2026-11-05: {why}"]


def test_deep_stop_is_reported_once():
    rep = BackfillReport(deep_stopped="HTTP 401 (check the Alpaca keys)")
    text = format_coverage(Coverage(sessions=[], backfill=rep))
    assert text.count("Alpaca stopped for the rest of this run: HTTP 401") == 1


def test_mixed_reasons_are_printed_in_date_order():
    lines = _lines([("SPY", date(2025, 6, 3), "Alpaca fetch failed: boom"),
                    ("SPY", date(2015, 6, 3), "no data from Alpaca (before its 2016 history?)")])
    got = [l for l in lines if "unavailable" in l]
    assert got[0].startswith("    unavailable SPY 2015-06-03") and got[1].startswith("    unavailable SPY 2025-06-03")


def test_incomplete_lines_are_sorted():
    rep = BackfillReport(incomplete=[("SPY", date(2026, 11, 5), 289), ("GLD", date(2026, 11, 5), 289),
                                     ("SPY", date(2026, 10, 1), 300)])
    got = [l for l in format_coverage(Coverage(sessions=[], backfill=rep)).splitlines() if "incomplete" in l]
    assert [l.split()[1:3] for l in got] == [["GLD", "2026-11-05:"], ["SPY", "2026-10-01:"], ["SPY", "2026-11-05:"]]
