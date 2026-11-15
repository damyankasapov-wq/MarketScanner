"""
Unit tests for the pure ORB session module (marketscanner/strategies/orb_session.py).

Frozen fixtures in the style of test_opening_range.py. Reference session
2024-11-06 (EST, UTC-5): 09:30 ET = 14:30 UTC, 10:30 ET = 15:30 UTC.
EDT reference 2024-07-10 (UTC-4): 09:30 ET = 13:30 UTC.
"""
import importlib
from datetime import datetime, timezone

import pandas as pd
import pytest

from marketscanner import config
from marketscanner.strategies import orb_session as orb
from marketscanner.strategies.orb_session import (
    Box, Confirmation, Proposal, build_proposal, find_confirmation,
    session_box, session_outcome, session_slice, snapshot, to_candles,
)


from tests.orb_fixtures import (
    BOX, C_GREEN_ABOVE, C_GREEN_ABOVE_2, C_RED_ABOVE, C_RED_BELOW, C_WICK_INSIDE,
    CONF_UP, ORB_BARS, PROP_BUY, TRAIL, TRAIL_2,
    bar as _bar, candle_bars as _candle_bars, df_of as _df, flat as _flat,
)


# ── session_slice ────────────────────────────────────────────────────────────

class TestSessionSlice:
    def test_keeps_only_newest_et_date(self):
        prior = _bar("2024-11-05 15:00:00+00:00", 500, 620, 480, 600)
        df = _df([prior] + ORB_BARS)
        out = session_slice(df)
        assert len(out) == 3
        assert {ts.date().isoformat() for ts in out.index} == {"2024-11-06"}

    def test_empty(self):
        assert session_slice(pd.DataFrame()).empty

    def test_et_date_not_utc_date(self):
        # 19:30 ET on 11-05 is 00:30 UTC on 11-06; a 09:30 ET 11-06 bar is a different session
        late = _bar("2024-11-06 00:30:00+00:00", 1, 1, 1, 1)
        today = _bar("2024-11-06 14:30:00+00:00", 2, 2, 2, 2)
        out = session_slice(_df([late, today]))
        assert len(out) == 1 and out.iloc[0]["open"] == 2


# ── to_candles ───────────────────────────────────────────────────────────────

class TestToCandles:
    def test_twelve_orb_candles_from_sixty_bars(self):
        bars = _flat("2024-11-06 14:30", 60) + _flat("2024-11-06 15:30", 1)
        candles = to_candles(_df(bars), 5)
        assert len(candles) == 12
        assert candles.index[0] == pd.Timestamp("2024-11-06 14:30", tz="UTC")
        assert candles.index[-1] == pd.Timestamp("2024-11-06 15:25", tz="UTC")

    def test_ohlcv_aggregation(self):
        bars = _candle_bars("2024-11-06 14:30", 575.0, 578.0, 574.0, 576.0) + _flat("2024-11-06 14:35", 1)
        c = to_candles(_df(bars), 5).iloc[0]
        assert (c["open"], c["high"], c["low"], c["close"]) == (575.0, 578.0, 574.0, 576.0)
        assert c["volume"] == 5_000.0

    def test_newest_bucket_dropped_without_now(self):
        bars = _flat("2024-11-06 14:30", 8)        # 14:30..14:37 → 14:35 bucket holds newest
        candles = to_candles(_df(bars), 5)
        assert list(candles.index) == [pd.Timestamp("2024-11-06 14:30", tz="UTC")]

    def test_newest_bucket_kept_under_now_grace(self):
        bars = _flat("2024-11-06 14:30", 8)
        end_plus_grace = datetime(2024, 11, 6, 14, 40, tzinfo=timezone.utc) + pd.Timedelta(
            seconds=config.CANDLE_GRACE_SECONDS)
        kept = to_candles(_df(bars), 5, now=end_plus_grace)
        assert len(kept) == 2
        too_early = to_candles(_df(bars), 5, now=end_plus_grace - pd.Timedelta(seconds=1))
        assert len(too_early) == 1

    def test_empty_buckets_skipped(self):
        bars = _flat("2024-11-06 14:30", 5) + _flat("2024-11-06 14:45", 5) + _flat("2024-11-06 14:50", 1)
        candles = to_candles(_df(bars), 5)
        assert [ts.strftime("%H:%M") for ts in candles.index] == ["14:30", "14:45"]

    def test_gap_bucket_complete_when_later_bar_exists(self):
        # missed poll: frame jumps from 10:44 to 10:52 — the 10:40 candle (from 10:40-10:44)
        # is complete because a later bar exists; the 10:50 bucket holds the newest bar.
        bars = _flat("2024-11-06 15:40", 5) + _flat("2024-11-06 15:52", 1)
        candles = to_candles(_df(bars), 5)
        assert [ts.strftime("%H:%M") for ts in candles.index] == ["15:40"]

    @pytest.mark.parametrize("minutes", [5, 15, 20])
    @pytest.mark.parametrize("open_utc", ["2024-11-06 14:30", "2024-07-10 13:30"])  # EST, EDT
    def test_buckets_start_at_0930_et(self, minutes, open_utc):
        bars = _flat(open_utc, 70)
        candles = to_candles(_df(bars), minutes)
        origin = pd.Timestamp(open_utc, tz="UTC")
        assert not candles.empty
        for ts in candles.index:
            assert (ts - origin) % pd.Timedelta(minutes=minutes) == pd.Timedelta(0)
        assert candles.index[0] == origin

    def test_empty_frame(self):
        c = to_candles(pd.DataFrame(), 5)
        assert c.empty and list(c.columns) == ["open", "high", "low", "close", "volume"]

    def test_index_is_utc(self):
        bars = _flat("2024-11-06 14:30", 6)
        assert str(to_candles(_df(bars), 5).index.tz) == "UTC"


class TestConfigValidation:
    def test_validator_rejects_non_divisor(self):
        with pytest.raises(ValueError):
            config._validate_candle_minutes(7)
        with pytest.raises(ValueError):
            config._validate_candle_minutes(0)
        assert config._validate_candle_minutes(20) == 20

    def test_invalid_env_fails_import(self, monkeypatch):
        monkeypatch.setenv("CONFIRM_CANDLE_MINUTES", "7")
        try:
            with pytest.raises(ValueError, match="divide 60"):
                importlib.reload(config)
        finally:
            monkeypatch.setenv("CONFIRM_CANDLE_MINUTES", "5")
            importlib.reload(config)
        assert config.CONFIRM_CANDLE_MINUTES == 5


# ── session_box ──────────────────────────────────────────────────────────────

class TestSessionBox:
    def test_box_from_window(self):
        box = session_box(_df(ORB_BARS + C_GREEN_ABOVE))
        assert box == Box(top=580.0, bottom=574.5)
        assert box.delta == pytest.approx(5.5)

    def test_none_when_no_window_bars(self):
        assert session_box(pd.DataFrame()) is None
        premarket = _bar("2024-11-06 13:00:00+00:00", 570.0, 571.0, 569.0, 570.5)
        assert session_box(premarket) is None

    def test_prior_session_does_not_leak(self):
        prior = [_bar("2024-11-05 14:30:00+00:00", 500, 620, 480, 600)]
        assert session_box(_df(prior + ORB_BARS)) == Box(580.0, 574.5)


# ── find_confirmation ────────────────────────────────────────────────────────

def _candles(*groups):
    return to_candles(_df(ORB_BARS + [b for g in groups for b in g]), 5)


class TestFindConfirmation:
    def test_close_outside_wick_inside_does_not_fire(self):
        assert find_confirmation(_candles(C_WICK_INSIDE, TRAIL), BOX, 5) is None

    def test_red_candle_fully_above_does_not_fire(self):
        assert find_confirmation(_candles(C_RED_ABOVE, TRAIL), BOX, 5) is None

    def test_green_candle_fully_above_fires_up(self):
        conf = find_confirmation(_candles(C_GREEN_ABOVE, TRAIL), BOX, 5)
        assert conf == CONF_UP

    def test_red_candle_fully_below_fires_down(self):
        conf = find_confirmation(_candles(C_RED_BELOW, TRAIL), BOX, 5)
        assert conf is not None
        assert conf.direction == "DOWN"
        assert (conf.open, conf.high, conf.low, conf.close) == (574.0, 574.4, 573.0, 573.2)

    def test_first_qualifying_candle_wins(self):
        conf = find_confirmation(_candles(C_GREEN_ABOVE, C_GREEN_ABOVE_2, TRAIL_2), BOX, 5)
        assert conf.candle_open == CONF_UP.candle_open

    def test_candle_before_1030_never_qualifies(self):
        # a 10:25 ET candle fully above a (partial) box must not confirm
        early = _candle_bars("2024-11-06 15:25", 580.5, 581.5, 580.2, 581.2) + TRAIL
        candles = to_candles(_df(ORB_BARS[:2] + early), 5)
        assert any(ts == pd.Timestamp("2024-11-06 15:25", tz="UTC") for ts in candles.index)
        assert find_confirmation(candles, BOX, 5) is None

    def test_empty_candles(self):
        assert find_confirmation(to_candles(pd.DataFrame(), 5), BOX, 5) is None

    def test_last_bucket_only_under_grace(self):
        df = _df(ORB_BARS + C_GREEN_ABOVE)        # confirmation candle is the newest bucket
        assert snapshot(df, 5).confirmation is None
        now = datetime(2024, 11, 6, 15, 35, tzinfo=timezone.utc) + pd.Timedelta(
            seconds=config.CANDLE_GRACE_SECONDS)
        assert snapshot(df, 5, now=now).confirmation == CONF_UP


# ── build_proposal ───────────────────────────────────────────────────────────

class TestBuildProposal:
    def test_buy_maths(self):
        p = build_proposal(CONF_UP, BOX)
        assert p.side == "BUY"
        assert p.entry == pytest.approx(581.2)
        assert p.take_profit == pytest.approx(586.7)
        assert p.stop_loss == pytest.approx(575.7)
        assert p.delta == pytest.approx(5.5)

    def test_sell_maths(self):
        conf = Confirmation(CONF_UP.candle_open, CONF_UP.candle_close, "DOWN", 574.0, 574.4, 573.0, 573.2)
        p = build_proposal(conf, BOX)
        assert p.side == "SELL"
        assert p.entry == pytest.approx(573.2)
        assert p.take_profit == pytest.approx(567.7)
        assert p.stop_loss == pytest.approx(578.7)

    def test_flat_box_no_proposal(self):
        flat = [_bar("2024-11-06 14:30:00+00:00", 580, 580, 580, 580)]
        above = _candle_bars("2024-11-06 15:30", 580.5, 581.5, 580.2, 581.2) + TRAIL
        snap = snapshot(_df(flat + above), 5)
        assert snap.box == Box(580.0, 580.0)
        assert snap.box.delta == 0
        assert snap.confirmation is None
        assert snap.proposal is None
        assert snap.outcome is None


# ── session_outcome ──────────────────────────────────────────────────────────

def _after(*bars):
    return _df(ORB_BARS + C_GREEN_ABOVE + list(bars))


class TestSessionOutcome:
    def test_tp_touched(self):
        df = _after(_bar("2024-11-06 15:40:00+00:00", 586.0, 587.0, 585.9, 586.5))
        o = session_outcome(df, CONF_UP, PROP_BUY, 5)
        assert o.status == "TP_HIT"
        assert o.exit_ts == datetime(2024, 11, 6, 15, 40, tzinfo=timezone.utc)
        assert o.exit_price == pytest.approx(586.7)
        assert o.r_multiple == 1.0
        assert o.exit_candle == datetime(2024, 11, 6, 15, 40, tzinfo=timezone.utc)

    def test_sl_touched(self):
        df = _after(_bar("2024-11-06 15:41:00+00:00", 576.0, 576.5, 575.0, 575.5))
        o = session_outcome(df, CONF_UP, PROP_BUY, 5)
        assert o.status == "SL_HIT"
        assert o.exit_price == pytest.approx(575.7)
        assert o.r_multiple == -1.0

    def test_both_in_one_bar_is_sl(self):
        df = _after(_bar("2024-11-06 15:40:00+00:00", 580.0, 587.0, 575.0, 580.0))
        assert session_outcome(df, CONF_UP, PROP_BUY, 5).status == "SL_HIT"

    def test_untouched_is_open(self):
        df = _after(_bar("2024-11-06 15:40:00+00:00", 582.0, 583.0, 581.0, 582.5))
        o = session_outcome(df, CONF_UP, PROP_BUY, 5)
        assert o.status == "OPEN" and o.exit_ts is None and o.r_multiple is None

    def test_touch_attributed_to_right_minute(self):
        bars = _flat("2024-11-06 15:40", 5, 582.0)
        bars[3] = _bar("2024-11-06 15:43:00+00:00", 586.0, 587.0, 585.9, 586.5)
        o = session_outcome(_after(*bars), CONF_UP, PROP_BUY, 5)
        assert o.exit_ts == datetime(2024, 11, 6, 15, 43, tzinfo=timezone.utc)
        assert o.exit_candle == datetime(2024, 11, 6, 15, 40, tzinfo=timezone.utc)

    def test_exit_candle_is_bucket_floor_for_other_sizes(self):
        df = _after(_bar("2024-11-06 15:43:00+00:00", 586.0, 587.0, 585.9, 586.5))
        o = session_outcome(df, CONF_UP, PROP_BUY, 15)
        assert o.exit_candle == datetime(2024, 11, 6, 15, 30, tzinfo=timezone.utc)

    def test_touch_in_incomplete_final_bucket_resolves(self):
        bars = _flat("2024-11-06 15:35", 5, 582.0) + [
            _bar("2024-11-06 15:40:00+00:00", 582.0, 582.5, 581.5, 582.0),
            _bar("2024-11-06 15:41:00+00:00", 582.0, 582.5, 581.5, 582.0),
            _bar("2024-11-06 15:42:00+00:00", 586.0, 587.0, 585.9, 586.5),   # newest bar, bucket 15:40
        ]
        snap = snapshot(_df(ORB_BARS + C_GREEN_ABOVE + bars), 5)
        assert pd.Timestamp("2024-11-06 15:40", tz="UTC") not in snap.candles.index
        assert snap.outcome.status == "TP_HIT"
        assert snap.outcome.exit_candle == datetime(2024, 11, 6, 15, 40, tzinfo=timezone.utc)

    def test_sell_mirrored(self):
        conf = Confirmation(CONF_UP.candle_open, CONF_UP.candle_close, "DOWN", 574.0, 574.4, 573.0, 573.2)
        prop = build_proposal(conf, BOX)                     # entry 573.2, tp 567.7, sl 578.7
        df = _df(ORB_BARS + C_RED_BELOW + [_bar("2024-11-06 15:40:00+00:00", 568.0, 568.2, 567.0, 567.5)])
        o = session_outcome(df, conf, prop, 5)
        assert o.status == "TP_HIT" and o.exit_price == pytest.approx(567.7)
        df = _df(ORB_BARS + C_RED_BELOW + [_bar("2024-11-06 15:40:00+00:00", 578.0, 579.0, 577.5, 578.5)])
        assert session_outcome(df, conf, prop, 5).status == "SL_HIT"

    def test_bars_before_candle_close_ignored(self):
        # the confirmation candle's own bars never count as an exit
        df = _df(ORB_BARS + _candle_bars("2024-11-06 15:30", 580.5, 590.0, 580.2, 581.2) + TRAIL)
        conf = Confirmation(CONF_UP.candle_open, CONF_UP.candle_close, "UP", 580.5, 590.0, 580.2, 581.2)
        assert session_outcome(df, conf, PROP_BUY, 5).status == "OPEN"


# ── snapshot composition ─────────────────────────────────────────────────────

class TestSnapshot:
    def test_empty(self):
        snap = snapshot(pd.DataFrame(), 5)
        assert snap.session_date is None and snap.box is None
        assert snap.candles.empty and snap.proposal is None and snap.outcome is None
        assert snap.candle_minutes == 5

    def test_no_orb_bars(self):
        snap = snapshot(_bar("2024-11-06 13:00:00+00:00", 570, 571, 569, 570.5), 5)
        assert snap.box is None and snap.confirmation is None

    def test_full_chain(self):
        df = _after(_bar("2024-11-06 15:40:00+00:00", 586.0, 587.0, 585.9, 586.5))
        snap = snapshot(df, 5)
        assert snap.session_date.isoformat() == "2024-11-06"
        assert snap.box == BOX
        assert snap.confirmation == CONF_UP
        assert snap.proposal.side == "BUY"
        assert snap.proposal.take_profit == pytest.approx(586.7)
        assert snap.outcome.status == "TP_HIT"

    def test_default_minutes_from_config(self):
        assert snapshot(pd.DataFrame()).candle_minutes == config.CONFIRM_CANDLE_MINUTES

    def test_prior_session_never_leaks(self):
        prior = [
            _bar("2024-11-05 14:30:00+00:00", 500, 620, 480, 600),
            _bar("2024-11-05 16:00:00+00:00", 700, 700, 100, 700),   # would be TP/SL touch if counted
        ]
        snap = snapshot(_df(prior + list(_after(_bar("2024-11-06 15:40:00+00:00", 582, 583, 581, 582.5)).pipe(
            lambda d: [d.iloc[[i]] for i in range(len(d))]))), 5)
        assert snap.box == BOX
        assert {ts.date().isoformat() for ts in snap.candles.index} == {"2024-11-06"}
        assert snap.outcome.status == "OPEN"
