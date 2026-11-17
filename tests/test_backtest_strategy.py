"""Strategy registry and the ORB strategy on whole synthetic sessions."""
from datetime import date

import pandas as pd
import pytest

from marketscanner import config
from marketscanner.backtest.strategy import OrbStrategy, STRATEGIES, TradeIdea, get_strategy
from tests.backtest_fixtures import ENTRY_BAR, make_session
from tests.orb_fixtures import utc


def test_registry_is_case_insensitive_and_lists_options_on_error():
    assert isinstance(get_strategy("orb"), OrbStrategy)
    assert get_strategy(" ORB ").name == "ORB"
    with pytest.raises(ValueError, match=r"unknown strategy 'nope'; available: ORB"):
        get_strategy("nope")
    assert set(STRATEGIES) == {"ORB"}


def test_buy_idea_from_an_up_breakout():
    idea = OrbStrategy().propose("SPY", make_session("2024-11-06", "UP"))
    assert isinstance(idea, TradeIdea)
    assert (idea.symbol, idea.side, idea.session_date) == ("SPY", "BUY", date(2024, 11, 6))
    assert idea.signal_ts == utc(2024, 11, 6, 15, 35)
    assert (idea.entry, idea.take_profit, idea.stop_loss) == pytest.approx((581.2, 586.7, 575.7))
    assert idea.delta == pytest.approx(5.5)
    assert idea.meta["box"] == (574.5, 580.0) and idea.meta["candle_minutes"] == config.CONFIRM_CANDLE_MINUTES


def test_sell_idea_from_a_down_breakout():
    idea = OrbStrategy().propose("GLD", make_session("2024-11-06", "DOWN"))
    assert idea.side == "SELL"
    assert (idea.entry, idea.take_profit, idea.stop_loss) == pytest.approx((573.2, 567.7, 578.7))


def test_no_idea_without_a_confirmation_candle():
    assert OrbStrategy().propose("SPY", make_session("2024-11-06", None)) is None


def test_no_idea_for_a_flat_opening_range():
    flat = make_session("2024-11-06", "UP")
    flat.iloc[:60] = 580.0                      # box top == bottom
    assert OrbStrategy().propose("SPY", flat) is None


def test_empty_frame_is_none():
    assert OrbStrategy().propose("SPY", pd.DataFrame()) is None


def test_the_signal_time_is_the_candle_close_not_a_later_bar():
    idea = OrbStrategy().propose("SPY", make_session("2024-11-06", "UP"))
    assert idea.signal_ts == make_session("2024-11-06").index[ENTRY_BAR].to_pydatetime()


def test_candle_minutes_parameter_is_used():
    s = OrbStrategy(candle_minutes=15)
    assert s.candle_minutes == 15
