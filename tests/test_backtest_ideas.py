"""Idea generation and the opening-hour data-quality gate."""
from datetime import date

import pandas as pd

from marketscanner.backtest.ideas import (
    OPENING_MIN_BARS, generate_ideas, later_hole_count, opening_bar_count, split_sessions,
)
from marketscanner.backtest.strategy import OrbStrategy
from marketscanner.data.bar_store import InMemoryBarStore
from tests.backtest_fixtures import make_session, stack

D1, D2, D3 = date(2024, 11, 6), date(2024, 11, 7), date(2024, 11, 8)


def _store(**by_symbol):
    s = InMemoryBarStore()
    for sym, frame in by_symbol.items():
        s.upsert_bars(sym, frame)
    return s


def test_split_sessions_by_et_date():
    parts = split_sessions(stack(make_session("2024-11-06"), make_session("2024-11-07")))
    assert list(parts) == [D1, D2] and all(len(p) == 390 for p in parts.values())
    assert split_sessions(pd.DataFrame()) == {}


def test_one_idea_per_confirmed_session_and_all_sessions_listed():
    store = _store(SPY=stack(make_session("2024-11-06", "UP"), make_session("2024-11-07", None), make_session("2024-11-08", "DOWN")))
    res = generate_ideas(store, OrbStrategy(), ["SPY"], D1, D3)
    assert [(i.session_date, i.side) for i in res.ideas] == [(D1, "BUY"), (D3, "SELL")]
    assert [(s.date, s.status) for s in res.sessions] == [(D1, "ok"), (D2, "ok"), (D3, "ok")]
    assert len(res.frames["SPY"]) == 3 * 390


def test_gate_boundary_57_ok_56_skipped():
    full = make_session("2024-11-06", "UP")
    assert opening_bar_count(full, D1) == 60
    drop = lambda k: full.drop(full.index[[i for i in range(1, 1 + k)]])       # remove k opening bars (keep bar 0)
    ok = generate_ideas(_store(SPY=drop(60 - OPENING_MIN_BARS)), OrbStrategy(), ["SPY"], D1, D1)
    assert ok.sessions[0].status == "ok" and ok.sessions[0].opening_bars == OPENING_MIN_BARS and len(ok.ideas) == 1
    bad = generate_ideas(_store(SPY=drop(60 - OPENING_MIN_BARS + 1)), OrbStrategy(), ["SPY"], D1, D1)
    assert bad.sessions[0].status == "skipped_opening_hole" and bad.sessions[0].opening_bars == OPENING_MIN_BARS - 1
    assert bad.ideas == []


def test_hole_later_in_the_day_is_tolerated_and_counted():
    s = make_session("2024-11-06", "UP")
    holey = s.drop(s.index[120:180])                                       # a one-hour hole after 10:30
    res = generate_ideas(_store(SPY=holey), OrbStrategy(), ["SPY"], D1, D1)
    assert res.sessions[0].status == "ok" and res.sessions[0].later_holes == 1 and len(res.ideas) == 1
    assert later_hole_count(s, D1) == 0


def test_weekday_without_bars_is_no_data_and_weekends_are_ignored():
    res = generate_ideas(_store(SPY=make_session("2024-11-06", "UP")), OrbStrategy(), ["SPY"], D1, date(2024, 11, 11))
    statuses = {(s.date, s.status) for s in res.sessions}
    assert (D2, "no_data") in statuses and (D3, "no_data") in statuses and (date(2024, 11, 11), "no_data") in statuses
    assert all(s.date.weekday() < 5 for s in res.sessions)


def test_symbols_are_independent():
    store = _store(SPY=make_session("2024-11-06", "UP"), QQQ=make_session("2024-11-06", "DOWN"))
    res = generate_ideas(store, OrbStrategy(), ["SPY", "QQQ", "GLD"], D1, D1)
    assert sorted((i.symbol, i.side) for i in res.ideas) == [("QQQ", "SELL"), ("SPY", "BUY")]
    assert [s.status for s in res.sessions if s.symbol == "GLD"] == ["no_data"]


def test_prior_session_does_not_leak_into_the_box():
    prior = make_session("2024-11-05", None)
    prior["high"] = 700.0                                                  # would wreck a shared box
    res = generate_ideas(_store(SPY=stack(prior, make_session("2024-11-06", "UP"))), OrbStrategy(), ["SPY"], date(2024, 11, 5), D1)
    assert [i.session_date for i in res.ideas] == [D1]
