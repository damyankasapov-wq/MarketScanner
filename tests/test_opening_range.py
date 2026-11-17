"""
Unit tests for OpeningRangeStrategy on the snapshot-based trigger.

The strategy no longer owns a state machine: it reads orb_session.snapshot()
and decides whether to alert (once per session, only while the confirmation
is fresh). Reference session 2024-11-06 (EST): 09:30 ET = 14:30 UTC.
"""
import logging
from datetime import date, timedelta

import pandas as pd
import pytest

from marketscanner import config
from marketscanner.strategies import orb_session
from marketscanner.strategies.opening_range import OpeningRangeStrategy
from tests.orb_fixtures import (
    BOX, C_GREEN_ABOVE, C_GREEN_ABOVE_2, C_RED_ABOVE, C_RED_BELOW, C_WICK_INSIDE,
    ORB_BARS, TRAIL, TRAIL_2, bar, candle_bars, df_of, flat, utc,
)

SESSION = date(2024, 11, 6)
NOW = utc(2024, 11, 6, 15, 36, 10)   # 10:36:10 ET, just after the 15:35 bar stamps


def _run(strategy, df, now=NOW):
    """Feed the frame bar by bar (as the live loop does) and collect signals."""
    signals = []
    for i in range(1, len(df) + 1):
        sig = strategy.check(df.iloc[:i], {}, now=now)
        if sig:
            signals.append(sig)
    return signals


class TestTrigger:
    def test_empty_df_returns_none(self):
        assert OpeningRangeStrategy("SPY").check(pd.DataFrame(), {}, now=NOW) is None

    def test_no_signal_before_orb_end(self):
        s = OpeningRangeStrategy("SPY")
        assert _run(s, df_of(ORB_BARS)) == []
        assert s._fired_session is None

    def test_close_outside_wick_inside_does_not_fire(self):
        assert _run(OpeningRangeStrategy("SPY"), df_of(ORB_BARS + C_WICK_INSIDE + TRAIL)) == []

    def test_red_candle_above_does_not_fire(self):
        assert _run(OpeningRangeStrategy("SPY"), df_of(ORB_BARS + C_RED_ABOVE + TRAIL)) == []

    def test_green_candle_fully_above_fires_buy(self):
        s = OpeningRangeStrategy("SPY")
        sigs = _run(s, df_of(ORB_BARS + C_GREEN_ABOVE + TRAIL))
        assert len(sigs) == 1
        sig = sigs[0]
        assert sig.direction == "UP" and sig.side == "BUY"
        assert sig.entry == pytest.approx(581.2) and sig.price == sig.entry
        assert sig.take_profit == pytest.approx(586.7)
        assert sig.stop_loss == pytest.approx(575.7)
        assert sig.delta == pytest.approx(5.5)
        assert sig.box_top == pytest.approx(580.0) and sig.box_bottom == pytest.approx(574.5)
        assert sig.confirm_open == utc(2024, 11, 6, 15, 30)
        assert sig.confirm_close == utc(2024, 11, 6, 15, 35)
        assert sig.candle_minutes == config.CONFIRM_CANDLE_MINUTES
        assert sig.session_date == SESSION
        assert s._fired_session == SESSION

    def test_red_candle_fully_below_fires_sell(self):
        sigs = _run(OpeningRangeStrategy("SPY"), df_of(ORB_BARS + C_RED_BELOW + TRAIL))
        assert len(sigs) == 1
        sig = sigs[0]
        assert sig.direction == "DOWN" and sig.side == "SELL"
        assert sig.entry == pytest.approx(573.2)
        assert sig.take_profit == pytest.approx(567.7)
        assert sig.stop_loss == pytest.approx(578.7)

    def test_fires_only_once_the_candle_is_complete(self):
        # The confirmation candle's bars alone (newest bucket, no later bar, no grace) → silent
        s = OpeningRangeStrategy("SPY")
        assert _run(s, df_of(ORB_BARS + C_GREEN_ABOVE), now=utc(2024, 11, 6, 15, 34, 30)) == []
        assert s._fired_session is None

    def test_fired_at_is_injected_now_not_candle_time(self):
        sig = _run(OpeningRangeStrategy("SPY"), df_of(ORB_BARS + C_GREEN_ABOVE + TRAIL))[0]
        assert sig.fired_at == NOW
        assert sig.fired_at != sig.confirm_close


class TestLatches:
    def test_one_signal_per_day(self):
        s = OpeningRangeStrategy("SPY")
        df = df_of(ORB_BARS + C_GREEN_ABOVE + C_GREEN_ABOVE_2 + TRAIL_2)
        assert len(_run(s, df, now=utc(2024, 11, 6, 15, 41))) == 1
        # and again on a later poll with the same frame
        assert s.check(df, {}, now=utc(2024, 11, 6, 15, 42)) is None

    def test_reset_clears_latches(self):
        s = OpeningRangeStrategy("SPY")
        _run(s, df_of(ORB_BARS + C_GREEN_ABOVE + TRAIL))
        assert s._fired_session == SESSION
        s.reset()
        assert s._fired_session is None and s._warned_flat_session is None

    def test_flat_box_warning_logged_once(self, caplog):
        s = OpeningRangeStrategy("SPY")
        flat_orb = [bar("2024-11-06 14:30:00+00:00", 580, 580, 580, 580)]
        above = candle_bars("2024-11-06 15:30", 580.5, 581.5, 580.2, 581.2) + TRAIL + TRAIL_2
        df = df_of(flat_orb + above)
        with caplog.at_level(logging.WARNING, logger="marketscanner.strategies.opening_range"):
            assert _run(s, df, now=utc(2024, 11, 6, 15, 41)) == []
            assert s.check(df, {}, now=utc(2024, 11, 6, 15, 42)) is None
        flat_warnings = [r for r in caplog.records if "flat opening range" in r.getMessage()]
        assert len(flat_warnings) == 1
        assert s._warned_flat_session == SESSION
        assert s._fired_session is None


class TestLateness:
    # Confirmation candle 10:40-10:45 ET (15:40-15:45 UTC)
    CONF_1040 = candle_bars("2024-11-06 15:40", 580.5, 581.5, 580.2, 581.2)

    def test_missed_poll_still_fires(self):
        # frame jumps from 10:44 to 10:52: the 10:45 confirmation is 7 min old → fires
        s = OpeningRangeStrategy("SPY")
        df = df_of(ORB_BARS + self.CONF_1040 + flat("2024-11-06 15:52", 1, 582.0))
        sig = s.check(df, {}, now=utc(2024, 11, 6, 15, 53))
        assert sig is not None
        assert sig.confirm_close == utc(2024, 11, 6, 15, 45)

    def test_restart_at_1100_refuses_but_snapshot_keeps_proposal(self):
        s = OpeningRangeStrategy("SPY")
        df = df_of(ORB_BARS + self.CONF_1040 + flat("2024-11-06 15:45", 16, 582.0))   # through 11:00 ET
        assert s.check(df, {}, now=utc(2024, 11, 6, 16, 0, 30)) is None
        assert s._fired_session == SESSION                       # refusal latches the session
        snap = orb_session.snapshot(df, now=utc(2024, 11, 6, 16, 0, 30))
        assert snap.proposal is not None and snap.proposal.side == "BUY"

    def test_two_hour_old_confirmation_refused_and_stays_silent(self, caplog):
        s = OpeningRangeStrategy("SPY")
        df = df_of(ORB_BARS + self.CONF_1040 + flat("2024-11-06 15:45", 120, 582.0))  # through 12:44 ET
        with caplog.at_level(logging.INFO, logger="marketscanner.strategies.opening_range"):
            assert s.check(df, {}, now=utc(2024, 11, 6, 17, 45)) is None
        assert any("refused as stale" in r.getMessage() for r in caplog.records)
        # later polls, even with fresh bars, stay silent for the session
        later = df_of(list(df.iloc[[i]] for i in range(len(df))) + flat("2024-11-06 17:45", 3, 582.0))
        assert s.check(later, {}, now=utc(2024, 11, 6, 17, 48)) is None

    def test_boundary_is_inclusive(self):
        s = OpeningRangeStrategy("SPY")
        limit = timedelta(minutes=config.CONFIRM_MAX_LATENESS_MINUTES)
        newest = utc(2024, 11, 6, 15, 45) + limit
        df = df_of(ORB_BARS + self.CONF_1040 + [bar(newest, 582, 582, 582, 582)])
        assert s.check(df, {}, now=newest + timedelta(seconds=30)) is not None


class TestSessionBoxAlias:
    def test_tuple_alias(self):
        assert OpeningRangeStrategy.session_box(df_of(ORB_BARS)) == (BOX.top, BOX.bottom)
        assert OpeningRangeStrategy.session_box(pd.DataFrame()) == (None, None)
