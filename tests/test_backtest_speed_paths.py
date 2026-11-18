"""
The PR1 speed paths must give exactly what the slow paths gave:

  - find_exits (prefix windows + precomputed session mask) == walk_exit over the
    whole remaining frame, for every idea;
  - walk_exit(first_of_session=mask) == walk_exit computing the mask itself;
  - sample_grid built in one step == the old one-timestamp-at-a-time union;
  - load_frames / ideas_from_frames == generate_ideas, and the gallery reads the
    same session bars as the old per-trade mask.
"""
from datetime import date, timedelta
from functools import reduce

import numpy as np
import pandas as pd
import pytest
import pytz

from marketscanner import config
from marketscanner.backtest import gallery
from marketscanner.backtest.engine import (
    EXIT_WINDOWS, AccountConfig, Trade, find_exits, run_engine,
)
from marketscanner.backtest.equity import build_equity, sample_grid
from marketscanner.backtest.ideas import generate_ideas, ideas_from_frames, load_frames
from marketscanner.backtest.strategy import OrbStrategy, TradeIdea
from marketscanner.data.bar_store import InMemoryBarStore
from marketscanner.data.sessions import session_start_mask, session_starts, split_by_et_date
from marketscanner.strategies.orb_session import walk_exit
from tests.backtest_fixtures import make_session, quiet, stack

_ET = pytz.timezone(config.TIMEZONE)


# ── synthetic multi-week history ──────────────────────────────────────────────

def weekdays_from(first: date, count: int) -> list[date]:
    out, d = [], first
    while len(out) < count:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def open_utc(d: date) -> pd.Timestamp:
    return pd.Timestamp(_ET.localize(pd.Timestamp(f"{d} 09:30").to_pydatetime())).tz_convert("UTC")


def random_history(days: list[date], seed: int = 3, step: float = 0.05, gap: float = 0.8) -> pd.DataFrame:
    """Random-walk 1-minute bars, 390 per session, with an overnight jump between sessions."""
    rng = np.random.default_rng(seed)
    frames, px = [], 100.0
    for d in days:
        px += rng.normal(0, gap)
        close = px + np.cumsum(rng.normal(0, step, 390))
        opn = np.concatenate(([px], close[:-1]))
        high = np.maximum(opn, close) + rng.uniform(0, step, 390)
        low = np.minimum(opn, close) - rng.uniform(0, step, 390)
        idx = pd.date_range(open_utc(d), periods=390, freq="1min")
        frames.append(pd.DataFrame({"open": opn, "high": high, "low": low, "close": close, "volume": 1.0}, index=idx))
        px = close[-1]
    return pd.concat(frames)


def flat_history(days: list[date], price: float = 100.0) -> pd.DataFrame:
    frames = []
    for d in days:
        idx = pd.date_range(open_utc(d), periods=390, freq="1min")
        frames.append(pd.DataFrame({"open": price, "high": price, "low": price, "close": price, "volume": 1.0}, index=idx))
    return pd.concat(frames)


def idea_at(frame: pd.DataFrame, pos: int, side: str, tp: float, sl: float, symbol: str = "SPY") -> TradeIdea:
    """An idea whose entry time is the bar at `pos` (its signal_ts is that bar's timestamp)."""
    ts = frame.index[pos]
    return TradeIdea(symbol=symbol, side=side, signal_ts=ts.to_pydatetime(), entry=float(frame["close"].iloc[pos]),
                     take_profit=tp, stop_loss=sl, session_date=ts.tz_convert(_ET).date(),
                     delta=abs(tp - float(frame["close"].iloc[pos])))


def reference_exit(frame: pd.DataFrame, idea: TradeIdea):
    """The pre-PR1 find_exits body: walk everything from the entry to the end."""
    start = frame.index.searchsorted(pd.Timestamp(idea.signal_ts))
    return walk_exit(frame.iloc[start:], idea.side, idea.take_profit, idea.stop_loss,
                     gap_fills=True, entry_session=idea.session_date)


DAYS = weekdays_from(date(2024, 10, 1), 45)        # 45 sessions, across the 2024-11-03 DST change
HIST = random_history(DAYS)


# ── exits ─────────────────────────────────────────────────────────────────────

def test_windows_widen_geometrically():
    assert EXIT_WINDOWS == (1, 6, 26, 130, 650)
    assert all(b > a for a, b in zip(EXIT_WINDOWS, EXIT_WINDOWS[1:]))


def test_bounded_exits_equal_unbounded_on_random_ideas():
    rng = np.random.default_rng(11)
    starts, _ = session_starts(HIST.index)
    ideas = []
    for _ in range(400):
        s = int(rng.integers(0, len(starts)))
        pos = int(starts[s]) + int(rng.integers(61, 390))        # after 10:30 ET
        pos = min(pos, len(HIST) - 1)
        side = "BUY" if rng.random() < 0.5 else "SELL"
        entry = float(HIST["close"].iloc[pos])
        delta = float(rng.choice([0.3, 1.0, 3.0, 8.0, 40.0]))   # same day ... weeks ... never
        sign = 1 if side == "BUY" else -1
        ideas.append(idea_at(HIST, pos, side, entry + sign * delta * float(rng.choice([1.0, 1.5, 2.0])), entry - sign * delta))
    got = find_exits(ideas, {"SPY": HIST})
    want = [reference_exit(HIST, i) for i in ideas]
    assert got == want
    kinds = {h.kind if h else None for h in want}
    assert {None, "TP", "SL"} <= kinds and ({"GAP_TP", "GAP_SL"} & kinds)   # the sample exercises every branch


@pytest.mark.parametrize("offset_sessions, bar_in_session", [
    (0, 389),    # last bar of the entry session: last bar of the w=1 window
    (1, 0),      # first bar of the next session: first bar outside w=1 (and an overnight gap)
    (5, 389),    # last bar of the w=6 window
    (6, 0),      # first bar outside w=6
    (25, 389),   # last bar of the w=26 window
    (26, 0),     # first bar outside w=26
    (30, 200),   # only the full remainder finds it
])
def test_hit_exactly_on_a_window_edge(offset_sessions, bar_in_session):
    frame = flat_history(DAYS)
    starts, _ = session_starts(frame.index)
    entry_pos = int(starts[2]) + 70
    hit_pos = int(starts[2 + offset_sessions]) + bar_in_session
    frame.iloc[hit_pos, frame.columns.get_loc("high")] = 105.0    # one spike to the target
    if bar_in_session == 0:
        frame.iloc[hit_pos, frame.columns.get_loc("open")] = 100.0  # opens inside: a level fill, not a gap
    idea = idea_at(frame, entry_pos, "BUY", tp=104.0, sl=96.0)
    got = find_exits([idea], {"SPY": frame})[0]
    assert got == reference_exit(frame, idea)
    assert got is not None and got.kind == "TP" and pd.Timestamp(got.ts) == frame.index[hit_pos]


def test_never_hit_and_entry_in_the_last_session_and_after_the_last_bar():
    frame = flat_history(DAYS)
    starts, _ = session_starts(frame.index)
    never = idea_at(frame, int(starts[3]) + 80, "SELL", tp=50.0, sl=150.0)
    last_session = idea_at(frame, int(starts[-1]) + 100, "BUY", tp=150.0, sl=50.0)   # j + w past the end: clamped
    after_end = TradeIdea(symbol="SPY", side="BUY", signal_ts=(frame.index[-1] + pd.Timedelta(minutes=5)).to_pydatetime(),
                          entry=100.0, take_profit=101.0, stop_loss=99.0, session_date=DAYS[-1], delta=1.0)
    got = find_exits([never, last_session, after_end], {"SPY": frame})
    assert got == [reference_exit(frame, i) for i in (never, last_session, after_end)] == [None, None, None]


@pytest.mark.parametrize("next_open, kind", [(95.0, "GAP_SL"), (106.0, "GAP_TP"), (100.5, None)])
def test_1555_confirmation_first_walked_bar_is_next_day_0930(next_open, kind):
    """A 15:55 candle closes at 16:00: the first bar at or after the entry is the next session's 09:30."""
    frame = flat_history(DAYS)
    starts, _ = session_starts(frame.index)
    nxt = int(starts[4])
    frame.iloc[nxt, frame.columns.get_loc("open")] = next_open
    frame.iloc[nxt, frame.columns.get_loc("high")] = max(next_open, 100.0)
    frame.iloc[nxt, frame.columns.get_loc("low")] = min(next_open, 100.0)
    entry_day = DAYS[3]
    sixteen = pd.Timestamp(_ET.localize(pd.Timestamp(f"{entry_day} 16:00").to_pydatetime())).tz_convert("UTC")
    idea = TradeIdea(symbol="SPY", side="BUY", signal_ts=sixteen.to_pydatetime(), entry=100.0,
                     take_profit=105.0, stop_loss=96.0, session_date=entry_day, delta=4.0)
    assert frame.index.searchsorted(sixteen) == nxt
    got = find_exits([idea], {"SPY": frame})[0]
    assert got == reference_exit(frame, idea)
    if kind is None:
        assert got is None
    else:
        assert got.kind == kind and got.price == next_open and pd.Timestamp(got.ts) == frame.index[nxt]


def test_walk_exit_with_a_precomputed_mask_equals_computing_it():
    rng = np.random.default_rng(5)
    mask_full = session_start_mask(HIST.index)
    starts, _ = session_starts(HIST.index)
    for _ in range(100):
        pos = int(starts[int(rng.integers(0, len(starts) - 1))]) + int(rng.integers(61, 389))
        idea = idea_at(HIST, pos, "BUY", tp=float(HIST["close"].iloc[pos]) + 4, sl=float(HIST["close"].iloc[pos]) - 4)
        bars = HIST.iloc[pos:]
        mask = mask_full[pos:].copy()
        mask[0] = False                         # the entry bar is in the entry session
        with_mask = walk_exit(bars, idea.side, idea.take_profit, idea.stop_loss,
                              gap_fills=True, entry_session=idea.session_date, first_of_session=mask)
        assert with_mask == walk_exit(bars, idea.side, idea.take_profit, idea.stop_loss,
                                      gap_fills=True, entry_session=idea.session_date)


def test_walk_exit_rejects_a_mask_of_the_wrong_length():
    with pytest.raises(ValueError, match="one entry per bar"):
        walk_exit(HIST.iloc[:10], "BUY", 200.0, 0.0, gap_fills=True, entry_session=DAYS[0],
                  first_of_session=np.zeros(9, dtype=bool))


# ── equity grid ───────────────────────────────────────────────────────────────

def old_sample_grid(trades, frames):
    """The pre-PR1 sample_grid, kept as the reference."""
    parts = []
    for f in frames.values():
        if len(f):
            parts.append(f.index[f.index.minute % 5 == 0])
            parts.append(f.index[-1:])
    for t in trades:
        for ts in (t.opened_at, t.closed_at):
            if ts is not None:
                parts.append(pd.DatetimeIndex([pd.Timestamp(ts)]).tz_convert("UTC"))
    if not parts:
        return pd.DatetimeIndex([], tz="UTC")
    return reduce(lambda a, b: a.union(b), parts).sort_values()


def fake_trades(frame: pd.DataFrame, n: int, seed: int = 1) -> list[Trade]:
    rng = np.random.default_rng(seed)
    out = []
    for k in range(n):
        a = int(rng.integers(0, len(frame) - 10))
        b = a + int(rng.integers(1, 900))
        opened = (frame.index[a] + pd.Timedelta(seconds=int(rng.integers(0, 3)) * 20)).to_pydatetime()  # some off-grid
        closed = frame.index[b].to_pydatetime() if b < len(frame) and rng.random() < 0.8 else None
        out.append(Trade(id=k + 1, symbol="SPY", side="BUY", signal_ts=opened, session_date=DAYS[0], entry=100.0,
                         take_profit=101.0, stop_loss=99.0, status="OPEN", opened_at=opened, closed_at=closed))
    return out


def test_one_step_grid_equals_the_old_union():
    frames = {"SPY": HIST, "QQQ": HIST.iloc[::2]}
    trades = fake_trades(HIST, 150)
    new, old = sample_grid(trades, frames), old_sample_grid(trades, frames)
    assert new.tz is not None and str(new.tz) == "UTC"
    assert new.is_monotonic_increasing and new.is_unique
    assert new.as_unit("ns").asi8.tolist() == old.as_unit("ns").asi8.tolist()


def test_one_step_grid_edge_cases():
    assert len(sample_grid([], {})) == 0
    assert len(sample_grid([], {"SPY": HIST.iloc[0:0]})) == 0
    only_trades = sample_grid(fake_trades(HIST, 5), {"SPY": HIST.iloc[0:0]})
    assert only_trades.equals(old_sample_grid(fake_trades(HIST, 5), {"SPY": HIST.iloc[0:0]}))


def test_build_equity_is_unchanged_on_a_real_run():
    store = InMemoryBarStore()
    store.upsert_bars("SPY", stack(make_session("2024-11-06", "UP", {100: (581.2, 587.0, 581.0, 586.0)}),
                                   quiet("2024-11-07"), quiet("2024-11-08")))
    store.upsert_bars("QQQ", stack(make_session("2024-11-06", "DOWN"), quiet("2024-11-07", box_high=578.0),
                                   quiet("2024-11-08", box_high=578.0)))
    res = generate_ideas(store, OrbStrategy(), ["SPY", "QQQ"], date(2024, 11, 6), date(2024, 11, 8))
    cfg = AccountConfig(initial_cash=5000.0, per_deal=1000.0, min_cash=0.0)
    trades = run_engine(res.ideas, find_exits(res.ideas, res.frames), res.frames, cfg, ["SPY", "QQQ"])
    eq = build_equity(trades, res.frames, cfg)
    assert eq.index.as_unit("ns").asi8.tolist() == old_sample_grid(trades, res.frames).as_unit("ns").asi8.tolist()
    assert np.isclose(eq["equity"].iloc[0], 5000.0)


# ── loading and the gallery ───────────────────────────────────────────────────

class ShuffledStore(InMemoryBarStore):
    """A store whose load() returns rows out of order."""

    def load(self, symbol, start, end):
        return super().load(symbol, start, end).sample(frac=1.0, random_state=2)


def _store(cls=InMemoryBarStore):
    store = cls()
    store.upsert_bars("SPY", stack(make_session("2024-11-06", "UP"), quiet("2024-11-07"), make_session("2024-11-08", "DOWN")))
    store.upsert_bars("GLD", stack(make_session("2024-11-06", None), make_session("2024-11-07", "UP"), quiet("2024-11-08")))
    return store


def test_load_frames_sorts_and_splits_once():
    loaded = load_frames(_store(ShuffledStore), ["SPY", "GLD"], date(2024, 11, 6), date(2024, 11, 8))
    for s in ("SPY", "GLD"):
        assert loaded.frames[s].index.is_monotonic_increasing
        assert list(loaded.sessions[s]) == [date(2024, 11, 6), date(2024, 11, 7), date(2024, 11, 8)]


def test_ideas_from_frames_equals_generate_ideas():
    args = (["SPY", "GLD"], date(2024, 11, 6), date(2024, 11, 8))
    a = generate_ideas(_store(), OrbStrategy(), *args)
    b = ideas_from_frames(load_frames(_store(), *args), OrbStrategy(), *args)
    assert a.ideas == b.ideas and a.sessions == b.sessions
    assert len(a.ideas) == 3
    # Out-of-order store rows give the same ideas.
    c = generate_ideas(_store(ShuffledStore), OrbStrategy(), *args)
    assert c.ideas == a.ideas and c.sessions == a.sessions


def test_gallery_session_frame_equals_the_old_mask():
    frame = HIST
    for d in (DAYS[0], DAYS[22], DAYS[-1], date(2024, 10, 5)):      # the last one is a Saturday: no bars
        old = frame[pd.Index(frame.index.tz_convert(_ET).date) == d]
        pd.testing.assert_frame_equal(gallery.session_frame(frame, d), old)


def test_gallery_with_and_without_presplit_sessions_draws_the_same_bars(tmp_path, monkeypatch):
    args = (["SPY", "GLD"], date(2024, 11, 6), date(2024, 11, 8))
    loaded = load_frames(_store(), *args)
    res = ideas_from_frames(loaded, OrbStrategy(), *args)
    cfg = AccountConfig(initial_cash=5000.0, per_deal=1000.0, min_cash=0.0)
    trades = run_engine(res.ideas, find_exits(res.ideas, res.frames), res.frames, cfg, args[0])
    seen = []

    class FakeFig:
        def savefig(self, path, **kw):
            open(path, "wb").close()

    def capture(snap, symbol):
        seen.append((symbol, snap.session_date, snap.box, snap.confirmation, len(snap.candles)))
        return FakeFig()

    monkeypatch.setattr(gallery, "render_chart", capture)
    p1, e1 = gallery.write_trade_charts(trades, res.frames, tmp_path / "a", 5)
    first, seen[:] = list(seen), []
    p2, e2 = gallery.write_trade_charts(trades, res.frames, tmp_path / "b", 5, sessions=loaded.sessions)
    assert p1 == p2 and e1 == e2 == {} and first == seen and len(first) == 3


# ── added after the /ship review ──────────────────────────────────────────────

LONG_DAYS = weekdays_from(date(2023, 1, 2), 700)     # long enough to reach the 130- and 650-session windows
LONG = flat_history(LONG_DAYS)


@pytest.mark.parametrize("offset_sessions, bar_in_session", [(129, 389), (130, 0), (649, 389), (650, 0), (690, 50)])
def test_hit_on_the_long_window_edges(offset_sessions, bar_in_session):
    frame = LONG.copy()
    starts, _ = session_starts(frame.index)
    hit_pos = int(starts[2 + offset_sessions]) + bar_in_session
    frame.iloc[hit_pos, frame.columns.get_loc("low")] = 95.0
    idea = idea_at(frame, int(starts[2]) + 70, "BUY", tp=110.0, sl=96.0)
    got = find_exits([idea], {"SPY": frame})[0]
    assert got == reference_exit(frame, idea)
    assert got.kind == "SL" and pd.Timestamp(got.ts) == frame.index[hit_pos]


@pytest.mark.parametrize("side, level", [("BUY", "SL"), ("SELL", "TP"), ("SELL", "SL")])
@pytest.mark.parametrize("offset_sessions, bar_in_session", [(0, 389), (1, 0), (5, 389), (6, 0)])
def test_window_edges_for_sell_and_stop_hits(side, level, offset_sessions, bar_in_session):
    frame = flat_history(DAYS)
    starts, _ = session_starts(frame.index)
    hit_pos = int(starts[2 + offset_sessions]) + bar_in_session
    # Which bar extreme reaches the level: BUY SL / SELL TP are below the entry, SELL SL above.
    below = (side, level) in {("BUY", "SL"), ("SELL", "TP")}
    frame.iloc[hit_pos, frame.columns.get_loc("low" if below else "high")] = 95.0 if below else 105.0
    tp, sl = (104.0, 96.0) if side == "BUY" else (96.0, 104.0)
    idea = idea_at(frame, int(starts[2]) + 70, side, tp=tp, sl=sl)
    got = find_exits([idea], {"SPY": frame})[0]
    assert got == reference_exit(frame, idea)
    assert got.kind == level and pd.Timestamp(got.ts) == frame.index[hit_pos]


def test_both_levels_in_one_bar_is_the_stop_on_both_paths():
    frame = flat_history(DAYS)
    starts, _ = session_starts(frame.index)
    hit_pos = int(starts[3]) + 200
    frame.iloc[hit_pos, frame.columns.get_loc("high")] = 105.0
    frame.iloc[hit_pos, frame.columns.get_loc("low")] = 95.0
    ideas = [idea_at(frame, int(starts[2]) + 70, "BUY", tp=104.0, sl=96.0),
             idea_at(frame, int(starts[2]) + 70, "SELL", tp=96.0, sl=104.0)]
    got = find_exits(ideas, {"SPY": frame})
    assert got == [reference_exit(frame, i) for i in ideas]
    assert [h.kind for h in got] == ["SL", "SL"]


def test_find_exits_multi_symbol_interleaved_matches_reference():
    frames = {"SPY": random_history(DAYS, seed=3), "QQQ": random_history(DAYS, seed=9, gap=1.5)}
    rng = np.random.default_rng(21)
    ideas = []
    for k in range(300):
        sym = ("SPY", "QQQ", "SPY")[k % 3]
        f = frames[sym]
        starts, _ = session_starts(f.index)
        pos = min(int(starts[int(rng.integers(0, len(starts)))]) + int(rng.integers(61, 390)), len(f) - 1)
        e = float(f["close"].iloc[pos])
        side = "SELL" if k % 2 else "BUY"
        d = float(rng.choice([0.5, 2.0, 6.0]))
        tp, sl = (e + 1.5 * d, e - d) if side == "BUY" else (e - 1.5 * d, e + d)
        ideas.append(idea_at(f, pos, side, tp=tp, sl=sl, symbol=sym))
    assert find_exits(ideas, frames) == [reference_exit(frames[i.symbol], i) for i in ideas]


def test_find_exits_refuses_an_unsorted_frame_and_handles_an_empty_one():
    shuffled = HIST.iloc[:2000].sample(frac=1.0, random_state=4)
    idea = idea_at(HIST, 100, "BUY", tp=200.0, sl=0.0)
    with pytest.raises(ValueError, match="sorted"):
        find_exits([idea], {"SPY": shuffled})
    assert find_exits([idea], {"SPY": HIST.iloc[0:0]}) == [None]


def test_walk_exit_mask_contract():
    bars = HIST.iloc[100:]
    mask = session_start_mask(HIST.index)[100:].copy()
    mask[0] = False
    want = walk_exit(bars, "BUY", 104.0, 96.0, gap_fills=True, entry_session=DAYS[0])
    # A mask makes entry_session unnecessary, and gives the same exit as computing it.
    assert walk_exit(bars, "BUY", 104.0, 96.0, gap_fills=True, entry_session=None, first_of_session=mask) == want
    # A list is accepted and coerced to booleans.
    assert walk_exit(bars, "BUY", 104.0, 96.0, gap_fills=True, first_of_session=mask.astype(int).tolist()) == want
    # A mask without gap fills would be silently ignored, so it is refused.
    with pytest.raises(ValueError, match="only used with gap_fills"):
        walk_exit(bars, "BUY", 104.0, 96.0, first_of_session=mask)


def test_passing_the_raw_session_start_mask_would_be_wrong():
    """Documents why engine._walk_one overrides the slice's first entry."""
    frame = flat_history(DAYS[:2])
    pos = 200
    frame.iloc[pos, frame.columns.get_loc("open")] = 95.0
    frame.iloc[pos, frame.columns.get_loc("low")] = 95.0
    bars = frame.iloc[pos:]
    right = walk_exit(bars, "BUY", 110.0, 96.0, gap_fills=True, entry_session=DAYS[0])
    assert right.kind == "SL" and right.price == 96.0
    raw = walk_exit(bars, "BUY", 110.0, 96.0, gap_fills=True, first_of_session=session_start_mask(bars.index))
    assert raw.kind == "GAP_SL"     # bar 0 marked as a new session: the trap the override avoids


@pytest.mark.parametrize("unit, tz", [("us", "UTC"), ("ns", "America/New_York"), ("ms", "UTC")])
def test_one_step_grid_other_units_and_zones(unit, tz):
    f = HIST.copy()
    f.index = f.index.as_unit(unit).tz_convert(tz)
    trades = fake_trades(HIST, 60)
    for t in trades[::2]:
        t.opened_at = pd.Timestamp(t.opened_at).tz_convert(_ET).to_pydatetime()
    new, old = sample_grid(trades, {"SPY": f}), old_sample_grid(trades, {"SPY": f})
    assert new.as_unit("ns").asi8.tolist() == old.as_unit("ns").asi8.tolist()
    assert str(new.tz) == "UTC"


def test_one_step_grid_refuses_naive_trade_times_like_the_old_one():
    trades = fake_trades(HIST, 3)
    trades[0].opened_at = pd.Timestamp(trades[0].opened_at).tz_localize(None).to_pydatetime()
    with pytest.raises(TypeError):
        old_sample_grid(trades, {"SPY": HIST})
    with pytest.raises(TypeError):
        sample_grid(trades, {"SPY": HIST})


def old_build_equity_realised(trades, grid):
    """The pre-cumsum realised column: one slice add per closed trade."""
    realised = np.zeros(len(grid))
    for t in trades:
        if t.status != "SKIPPED" and t.closed_at is not None:
            realised[int(grid.searchsorted(pd.Timestamp(t.closed_at), side="left")):] += t.pnl
    return realised


def test_build_equity_realised_equals_the_old_per_trade_loop():
    trades = fake_trades(HIST, 300, seed=8)
    rng = np.random.default_rng(8)
    for t in trades:
        t.amount, t.shares = 1000.0, 10.0
        t.pnl = float(rng.normal(0, 20)) if t.closed_at is not None else None
    cfg = AccountConfig(initial_cash=10000.0, per_deal=1000.0, min_cash=0.0)
    eq = build_equity(trades, {"SPY": HIST}, cfg)
    realised = eq["equity"].to_numpy() - cfg.initial_cash - eq["unrealised"].to_numpy()
    assert np.allclose(realised, old_build_equity_realised(trades, eq.index), atol=1e-6)


def test_gallery_partial_presplit_sessions_falls_back(tmp_path, monkeypatch):
    args = (["SPY", "GLD"], date(2024, 11, 6), date(2024, 11, 8))
    loaded = load_frames(_store(), *args)
    res = ideas_from_frames(loaded, OrbStrategy(), *args)
    cfg = AccountConfig(initial_cash=5000.0, per_deal=1000.0, min_cash=0.0)
    trades = run_engine(res.ideas, find_exits(res.ideas, res.frames), res.frames, cfg, args[0])
    seen = []

    class FakeFig:
        def savefig(self, path, **kw):
            open(path, "wb").close()

    def capture(snap, symbol):
        seen.append((symbol, snap.session_date, snap.box, len(snap.candles)))
        return FakeFig()

    monkeypatch.setattr(gallery, "render_chart", capture)
    gallery.write_trade_charts(trades, res.frames, tmp_path / "a", 5)
    full, seen[:] = list(seen), []
    partial = {"SPY": loaded.sessions["SPY"]}
    gallery.write_trade_charts(trades, res.frames, tmp_path / "b", 5, sessions=partial)
    assert seen == full and {s for s, *_ in full} == {"SPY", "GLD"}
    assert "GLD" not in partial          # the caller's dict is not modified


def test_strategy_gets_a_copy_of_the_session():
    """A strategy writing into its bars must not change the frames the exit walk uses."""
    class Vandal(OrbStrategy):
        def propose(self, symbol, df_session):
            df_session.loc[:, "close"] = -1.0
            return super().propose(symbol, df_session)

    res = generate_ideas(_store(), Vandal(), ["SPY"], date(2024, 11, 6), date(2024, 11, 8))
    assert (res.frames["SPY"]["close"] > 0).all()
