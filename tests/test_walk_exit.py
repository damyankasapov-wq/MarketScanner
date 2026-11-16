"""
walk_exit: the single take-profit / stop-loss rule shared by the dashboard
outcome (session_outcome, gap fills off) and the backtester (gap fills on).

Reference sessions: 2024-11-06 (entry, EST), 2024-11-07, 2024-11-08.
09:30 ET = 14:30 UTC in November.
"""
from datetime import date, datetime, timezone

import pandas as pd
import pytest

from marketscanner import config
from marketscanner.strategies.orb_session import ExitHit, replay_now, walk_exit
from tests.orb_fixtures import bar, df_of, utc

ENTRY = date(2024, 11, 6)
# BUY at 100 with TP 105 and SL 95
TP, SL = 105.0, 95.0


def _b(ts, o, h, l, c):
    return bar(ts, o, h, l, c)


def _buy(frame, **kw):
    return walk_exit(frame, "BUY", TP, SL, **kw)


def _sell(frame, **kw):
    # SELL at 100: TP 95 (below), SL 105 (above)
    return walk_exit(frame, "SELL", 95.0, 105.0, **kw)


class TestLevels:
    def test_empty_frame(self):
        assert _buy(pd.DataFrame(columns=["open", "high", "low", "close", "volume"])) is None

    def test_untouched(self):
        assert _buy(df_of([_b("2024-11-06 15:40", 100, 101, 99, 100)])) is None

    def test_buy_tp_fills_at_level(self):
        hit = _buy(df_of([_b("2024-11-06 15:40", 100, 101, 99, 100), _b("2024-11-06 15:41", 101, 106, 100, 105)]))
        assert hit == ExitHit(utc(2024, 11, 6, 15, 41), 105.0, "TP")

    def test_buy_sl_fills_at_level(self):
        hit = _buy(df_of([_b("2024-11-06 15:40", 100, 101, 94, 96)]))
        assert hit == ExitHit(utc(2024, 11, 6, 15, 40), 95.0, "SL")

    def test_both_in_one_bar_is_stop(self):
        hit = _buy(df_of([_b("2024-11-06 15:40", 100, 106, 94, 100)]))
        assert hit.kind == "SL" and hit.price == 95.0

    def test_first_touching_bar_wins(self):
        hit = _buy(df_of([_b("2024-11-06 15:40", 100, 106, 99, 105), _b("2024-11-06 15:41", 100, 101, 90, 91)]))
        assert hit.kind == "TP" and hit.ts == utc(2024, 11, 6, 15, 40)

    def test_sell_mirrored(self):
        tp = _sell(df_of([_b("2024-11-06 15:40", 100, 101, 94, 95)]))
        assert tp == ExitHit(utc(2024, 11, 6, 15, 40), 95.0, "TP")
        sl = _sell(df_of([_b("2024-11-06 15:40", 100, 106, 99, 104)]))
        assert sl == ExitHit(utc(2024, 11, 6, 15, 40), 105.0, "SL")
        both = _sell(df_of([_b("2024-11-06 15:40", 100, 106, 94, 100)]))
        assert both.kind == "SL"


class TestGapFills:
    def test_gap_fills_needs_entry_session(self):
        with pytest.raises(ValueError, match="entry_session"):
            _buy(df_of([_b("2024-11-07 14:30", 100, 101, 99, 100)]), gap_fills=True)

    def test_off_by_default_later_session_fills_at_level(self):
        # next-day open 90 is far below the stop, but with gap fills off the rule fills at the level
        hit = _buy(df_of([_b("2024-11-07 14:30", 90, 92, 89, 91)]))
        assert hit == ExitHit(utc(2024, 11, 7, 14, 30), 95.0, "SL")

    def test_gap_stop_buy_fills_at_open(self):
        hit = _buy(df_of([_b("2024-11-06 15:40", 100, 101, 99, 100), _b("2024-11-07 14:30", 90, 92, 89, 91)]),
                   gap_fills=True, entry_session=ENTRY)
        assert hit == ExitHit(utc(2024, 11, 7, 14, 30), 90.0, "GAP_SL")

    def test_gap_target_buy_fills_at_open(self):
        hit = _buy(df_of([_b("2024-11-06 15:40", 100, 101, 99, 100), _b("2024-11-07 14:30", 108, 109, 107, 108)]),
                   gap_fills=True, entry_session=ENTRY)
        assert hit == ExitHit(utc(2024, 11, 7, 14, 30), 108.0, "GAP_TP")

    def test_gap_sell_mirrored(self):
        stop = _sell(df_of([_b("2024-11-07 14:30", 110, 111, 109, 110)]), gap_fills=True, entry_session=ENTRY)
        assert stop == ExitHit(utc(2024, 11, 7, 14, 30), 110.0, "GAP_SL")
        target = _sell(df_of([_b("2024-11-07 14:30", 90, 91, 89, 90)]), gap_fills=True, entry_session=ENTRY)
        assert target == ExitHit(utc(2024, 11, 7, 14, 30), 90.0, "GAP_TP")

    def test_gap_open_beats_same_bar_stop_convention(self):
        # opens above the target (gap target) and its low would also touch the stop: the open comes first
        hit = _buy(df_of([_b("2024-11-07 14:30", 108, 109, 90, 100)]), gap_fills=True, entry_session=ENTRY)
        assert hit.kind == "GAP_TP" and hit.price == 108.0

    def test_entry_session_never_gaps(self):
        # a wild bar later the same session is not a gap: fills at the level
        hit = _buy(df_of([_b("2024-11-06 15:40", 100, 101, 99, 100), _b("2024-11-06 15:41", 90, 92, 89, 91)]),
                   gap_fills=True, entry_session=ENTRY)
        assert hit == ExitHit(utc(2024, 11, 6, 15, 41), 95.0, "SL")

    def test_only_first_bar_of_a_later_session_gaps(self):
        frame = df_of([_b("2024-11-07 14:30", 100, 101, 99, 100), _b("2024-11-07 14:31", 90, 92, 89, 91)])
        hit = _buy(frame, gap_fills=True, entry_session=ENTRY)
        assert hit == ExitHit(utc(2024, 11, 7, 14, 31), 95.0, "SL")

    def test_data_hole_inside_session_fills_at_level(self):
        # known limitation, documented: bars jump 15:40 -> 15:55 and the open is far beyond the stop
        frame = df_of([_b("2024-11-06 15:40", 100, 101, 99, 100), _b("2024-11-06 15:55", 90, 92, 89, 91)])
        hit = _buy(frame, gap_fills=True, entry_session=ENTRY)
        assert hit == ExitHit(utc(2024, 11, 6, 15, 55), 95.0, "SL")

    def test_carry_two_nights_then_gap_on_day_three(self):
        frame = df_of([
            _b("2024-11-06 15:40", 100, 101, 99, 100),
            _b("2024-11-07 14:30", 100, 102, 98, 101),
            _b("2024-11-07 15:00", 101, 103, 99, 102),
            _b("2024-11-08 14:30", 110, 111, 109, 110),
        ])
        hit = _buy(frame, gap_fills=True, entry_session=ENTRY)
        assert hit == ExitHit(utc(2024, 11, 8, 14, 30), 110.0, "GAP_TP")


class TestReplayNow:
    def test_winter_and_summer(self):
        grace = config.CANDLE_GRACE_SECONDS
        assert replay_now(date(2024, 11, 6)) == datetime(2024, 11, 6, 21, 0, tzinfo=timezone.utc) + pd.Timedelta(seconds=grace)
        assert replay_now(date(2024, 7, 10)) == datetime(2024, 7, 10, 20, 0, tzinfo=timezone.utc) + pd.Timedelta(seconds=grace)
