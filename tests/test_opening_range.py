"""
Unit tests for OpeningRangeStrategy using frozen historical data.
Reference date: 2024-11-06 (US election day — high SPY volatility, confirmed ORB breakout).
"""
from datetime import datetime, timezone

import pandas as pd
import pytest

from marketscanner.strategies.opening_range import OpeningRangeStrategy, _State


def _make_bar(dt_str: str, open_, high, low, close, volume=1_000_000) -> pd.DataFrame:
    ts = pd.Timestamp(dt_str, tz="UTC")
    return pd.DataFrame(
        [{"open": open_, "high": high, "low": low, "close": close, "volume": volume}],
        index=pd.DatetimeIndex([ts], tz="UTC"),
    )


def _build_df(bars: list[pd.DataFrame]) -> pd.DataFrame:
    return pd.concat(bars).sort_index()


# 2024-11-06 ET times → UTC: ET is UTC-5 on that date (EST)
# 09:30 ET = 14:30 UTC
# 10:30 ET = 15:30 UTC
# 10:31 ET = 15:31 UTC

ORB_BARS = [
    _make_bar("2024-11-06 14:30:00+00:00", 575.00, 578.00, 574.50, 577.00),  # 09:30 ET
    _make_bar("2024-11-06 14:31:00+00:00", 577.00, 579.50, 576.00, 578.00),  # 09:31 ET
    _make_bar("2024-11-06 15:29:00+00:00", 578.00, 580.00, 577.00, 579.00),  # 10:29 ET — last in window
    # box_high = 580.00, box_low = 574.50
]

POST_ORB_NO_BREAK = [
    _make_bar("2024-11-06 15:30:00+00:00", 579.00, 579.50, 578.00, 579.00),  # 10:30 ET — watching, no break
]

POST_ORB_BREAK_UP = [
    _make_bar("2024-11-06 15:31:00+00:00", 580.50, 581.00, 580.00, 580.50),  # close > 580.00 → UP
]

POST_ORB_BREAK_DOWN = [
    _make_bar("2024-11-06 15:31:00+00:00", 574.00, 574.50, 573.00, 573.80),  # close < 574.50 → DOWN
]


class TestOpeningRangeStrategy:
    def _strategy(self):
        return OpeningRangeStrategy("SPY")

    def test_idle_before_orb_start(self):
        s = self._strategy()
        pre_bar = _make_bar("2024-11-06 14:00:00+00:00", 574.00, 575.00, 573.50, 574.50)
        signal = s.check(pre_bar, {})
        assert signal is None
        assert s._state == _State.IDLE

    def test_builds_range_during_orb(self):
        s = self._strategy()
        df = _build_df(ORB_BARS)
        for i in range(1, len(df) + 1):
            s.check(df.iloc[:i], {})
        assert s._state == _State.BUILDING
        assert s._range_high == pytest.approx(580.00)
        assert s._range_low == pytest.approx(574.50)

    def test_no_signal_on_touch(self):
        s = self._strategy()
        df = _build_df(ORB_BARS + POST_ORB_NO_BREAK)
        signal = None
        for i in range(1, len(df) + 1):
            signal = s.check(df.iloc[:i], {})
        assert signal is None

    def test_fires_up_on_close_above_box(self):
        s = self._strategy()
        df = _build_df(ORB_BARS + POST_ORB_NO_BREAK + POST_ORB_BREAK_UP)
        signal = None
        for i in range(1, len(df) + 1):
            result = s.check(df.iloc[:i], {})
            if result:
                signal = result
        assert signal is not None
        assert signal.direction == "UP"
        assert signal.price == pytest.approx(580.50)
        assert signal.box_top == pytest.approx(580.00)
        assert signal.box_bottom == pytest.approx(574.50)

    def test_fires_down_on_close_below_box(self):
        s = self._strategy()
        df = _build_df(ORB_BARS + POST_ORB_NO_BREAK + POST_ORB_BREAK_DOWN)
        signal = None
        for i in range(1, len(df) + 1):
            result = s.check(df.iloc[:i], {})
            if result:
                signal = result
        assert signal is not None
        assert signal.direction == "DOWN"
        assert signal.price == pytest.approx(573.80)

    def test_no_second_signal_after_fire(self):
        s = self._strategy()
        another_break = _make_bar("2024-11-06 15:32:00+00:00", 581.00, 582.00, 580.50, 581.50)
        df = _build_df(ORB_BARS + POST_ORB_NO_BREAK + POST_ORB_BREAK_UP + [another_break])
        signals = []
        for i in range(1, len(df) + 1):
            result = s.check(df.iloc[:i], {})
            if result:
                signals.append(result)
        assert len(signals) == 1

    def test_exact_touch_does_not_fire(self):
        # close == box_high must NOT fire (strategy uses strict >, not >=)
        s = self._strategy()
        touch_bar = _make_bar("2024-11-06 15:31:00+00:00", 579.5, 581.0, 579.0, 580.00)
        df = _build_df(ORB_BARS + POST_ORB_NO_BREAK + [touch_bar])
        signal = None
        for i in range(1, len(df) + 1):
            result = s.check(df.iloc[:i], {})
            if result:
                signal = result
        assert signal is None

    def test_exact_touch_bottom_does_not_fire(self):
        # close == box_low must NOT fire
        s = self._strategy()
        touch_bar = _make_bar("2024-11-06 15:31:00+00:00", 575.0, 575.5, 573.0, 574.50)
        df = _build_df(ORB_BARS + POST_ORB_NO_BREAK + [touch_bar])
        signal = None
        for i in range(1, len(df) + 1):
            result = s.check(df.iloc[:i], {})
            if result:
                signal = result
        assert signal is None

    def test_empty_df_returns_none(self):
        s = self._strategy()
        assert s.check(pd.DataFrame(), {}) is None

    def test_building_to_watching_transition_at_orb_end(self):
        # first bar AT 10:30 ET (15:30 UTC) triggers BUILDING→WATCHING
        s = self._strategy()
        df = _build_df(ORB_BARS)
        for i in range(1, len(df) + 1):
            s.check(df.iloc[:i], {})
        assert s._state == _State.BUILDING
        first_watch = _make_bar("2024-11-06 15:30:00+00:00", 579.0, 579.5, 578.0, 579.0)
        s.check(_build_df(ORB_BARS + [first_watch]), {})
        assert s._state == _State.WATCHING

    def test_reset_clears_state(self):
        s = self._strategy()
        df = _build_df(ORB_BARS + POST_ORB_NO_BREAK + POST_ORB_BREAK_UP)
        for i in range(1, len(df) + 1):
            s.check(df.iloc[:i], {})
        s.reset()
        assert s._state == _State.IDLE
        assert s._range_high is None
        assert s._range_low is None
