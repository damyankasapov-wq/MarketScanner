"""The account curve: sampling grid, marks carried over nights and holes, and the shape of cash vs invested."""
import numpy as np
import pandas as pd
import pytest

from marketscanner.backtest.engine import AccountConfig, find_exits, run_engine
from marketscanner.backtest.equity import COLUMNS, build_equity, max_drawdown, sample_grid
from marketscanner.backtest.ideas import generate_ideas
from marketscanner.backtest.strategy import OrbStrategy
from marketscanner.data.bar_store import InMemoryBarStore
from tests.backtest_fixtures import make_session, quiet, stack
from tests.orb_fixtures import utc
from datetime import date

CFG = AccountConfig(initial_cash=10_000.0, per_deal=1000.0, min_cash=0.0)
D6, D7 = date(2024, 11, 6), date(2024, 11, 7)


def _run(frames, start, end, cfg=CFG):
    store = InMemoryBarStore()
    for sym, f in frames.items():
        store.upsert_bars(sym, f)
    symbols = list(frames)
    res = generate_ideas(store, OrbStrategy(), symbols, start, end)
    trades = run_engine(res.ideas, find_exits(res.ideas, res.frames), res.frames, cfg, symbols)
    return res, trades, build_equity(trades, res.frames, cfg)


def test_grid_is_five_minute_boundaries_plus_events_and_period_end():
    frames = {"SPY": make_session("2024-11-06", "UP", {103: (581.2, 587.0, 581.0, 586.0)})}
    res, trades, _ = _run(frames, D6, D6)
    grid = sample_grid(trades, res.frames)
    assert grid.is_monotonic_increasing and grid.is_unique and str(grid.tz) == "UTC"
    assert (grid.minute % 5 == 0).sum() >= 78
    assert utc(2024, 11, 6, 16, 13) in grid          # the exit minute (bar 103), not on the 5-minute grid
    assert res.frames["SPY"].index[-1] in grid       # period end


def test_empty_frames_give_an_empty_curve_with_columns():
    eq = build_equity([], {"SPY": pd.DataFrame()}, CFG)
    assert eq.empty and list(eq.columns) == COLUMNS
    assert build_equity([], {}, CFG).empty


def test_no_trades_is_a_flat_line_at_the_initial_cash():
    _, trades, eq = _run({"SPY": make_session("2024-11-06", None)}, D6, D6)
    assert trades == [] and len(eq) > 0
    assert (eq["equity"] == 10_000.0).all() and (eq["cash"] == 10_000.0).all() and (eq["invested"] == 0.0).all()


def test_open_then_close_moves_cash_into_invested_and_back():
    frames = {"SPY": make_session("2024-11-06", "UP", {100: (581.2, 587.0, 581.0, 586.0)})}
    res, trades, eq = _run(frames, D6, D6)
    t = trades[0]
    before = eq[eq.index < utc(2024, 11, 6, 15, 35)]
    during = eq[(eq.index >= utc(2024, 11, 6, 15, 35)) & (eq.index < utc(2024, 11, 6, 16, 10))]
    after = eq[eq.index >= utc(2024, 11, 6, 16, 10)]
    assert (before["invested"] == 0).all() and (before["cash"] == 10_000.0).all()
    assert (during["invested"] == 1000.0).all() and (during["cash"] == 9_000.0).all()
    assert (after["invested"] == 0).all()
    np.testing.assert_allclose(after["cash"], 10_000.0 + t.pnl)
    assert (after["unrealised"] == 0).all()


def test_unrealised_follows_the_mark_for_a_buy_and_a_sell():
    # BUY marked at 583 and SELL marked at 575 from bar 120 on
    frames = {
        "SPY": make_session("2024-11-06", "UP", {i: (583.0, 583.0, 583.0, 583.0) for i in range(120, 390)}),
        "QQQ": make_session("2024-11-06", "DOWN", {i: (575.0, 575.0, 575.0, 575.0) for i in range(120, 390)}),
    }
    res, trades, eq = _run(frames, D6, D6)
    spy, qqq = trades
    last = eq.iloc[-1]
    expected = spy.shares * (583.0 - 581.2) + qqq.shares * (573.2 - 575.0)       # SELL loses when price rises
    assert last["unrealised"] == pytest.approx(expected) and last["invested"] == 2000.0
    assert last["equity"] == pytest.approx(10_000.0 + expected)


def test_marks_carry_over_the_night_and_a_data_hole():
    day1 = make_session("2024-11-06", "UP", {i: (583.0, 583.0, 583.0, 583.0) for i in range(200, 390)})
    day2 = quiet("2024-11-07")
    day2 = day2.drop(day2.index[100:200])                       # hole on day 2
    res, trades, eq = _run({"SPY": stack(day1, day2)}, D6, D7)
    t = trades[0]
    overnight = eq[(eq.index > utc(2024, 11, 6, 21, 0)) & (eq.index < utc(2024, 11, 7, 14, 30))]
    assert len(overnight) == 0 or (overnight["unrealised"] == pytest.approx(t.shares * (583.0 - 581.2))).all()
    first_day2 = eq[eq.index >= utc(2024, 11, 7, 14, 30)].iloc[0]
    assert first_day2["invested"] == 1000.0 and first_day2["unrealised"] == pytest.approx(t.shares * (577.0 - 581.2))


def test_skipped_trades_do_not_touch_the_account():
    frames = {s: make_session("2024-11-06", "UP") for s in ("SPY", "QQQ")}
    cfg = AccountConfig(initial_cash=1500.0, per_deal=1000.0, min_cash=600.0)
    res, trades, eq = _run(frames, D6, D6, cfg)
    # 1500 - 1000 = 500 < 600 minimum: both signals are skipped, so the account never moves
    assert [t.status for t in trades] == ["SKIPPED", "SKIPPED"]
    assert (eq["equity"] == 1500.0).all() and (eq["invested"] == 0.0).all() and (eq["cash"] == 1500.0).all()


def test_max_drawdown():
    idx = pd.date_range("2024-11-06 14:30", periods=4, freq="5min", tz="UTC")
    dd, pct = max_drawdown(pd.Series([100.0, 110.0, 90.0, 95.0], index=idx))
    assert dd == pytest.approx(-20.0) and pct == pytest.approx(-20.0 / 110.0 * 100)
    assert max_drawdown(pd.Series([], dtype=float)) == (0.0, 0.0)
    assert max_drawdown(pd.Series([100.0, 101.0, 102.0])) == (0.0, 0.0)
