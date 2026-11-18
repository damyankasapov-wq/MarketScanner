"""
Idea generation: for every symbol and every session in the period, ask the
strategy for a trade, after a data-quality gate.

Gate: the strategy draws its box from 09:30-10:30 ET. If bars are missing in that
hour the box comes out too narrow and breakouts that never happened would fire.
A session with fewer than OPENING_MIN_BARS of the 60 expected minute bars is
skipped (and listed in the coverage block). Holes later in the day are tolerated:
they only affect exits, and are counted so the report can show them.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional

import pandas as pd

from marketscanner.backtest.strategy import BacktestStrategy, TradeIdea
from marketscanner.data.backfill import weekdays
from marketscanner.data.bar_store import BarStore
from marketscanner.data.sessions import sort_bars, split_by_et_date
from marketscanner.strategies.orb_session import orb_end_utc, session_open_utc

OPENING_MIN_BARS = 57
OPENING_EXPECTED_BARS = 60


@dataclass(frozen=True)
class SessionInfo:
    symbol: str
    date: date
    bars: int
    opening_bars: int
    status: str            # "ok" | "skipped_opening_hole" | "no_data"
    later_holes: int       # gaps longer than one minute after the opening hour


@dataclass
class IdeasResult:
    ideas: list[TradeIdea] = field(default_factory=list)
    sessions: list[SessionInfo] = field(default_factory=list)
    frames: dict[str, pd.DataFrame] = field(default_factory=dict)


@dataclass
class Frames:
    """Each symbol's bars for the period, loaded once, plus the same bars cut into sessions."""
    frames: dict[str, pd.DataFrame] = field(default_factory=dict)
    sessions: dict[str, dict[date, pd.DataFrame]] = field(default_factory=dict)


def split_sessions(df: pd.DataFrame) -> dict[date, pd.DataFrame]:
    """Group a UTC-indexed frame by New York session date. Kept as an alias of the shared splitter."""
    return split_by_et_date(df)


def load_frames(store: BarStore, symbols: list[str], start: date, end: date) -> Frames:
    """One query per symbol for the whole period, split into sessions once."""
    out = Frames()
    for symbol in symbols:
        frame = store.load(symbol, start, end)
        if not frame.empty:
            frame = sort_bars(frame)
        out.frames[symbol] = frame
        out.sessions[symbol] = split_by_et_date(frame)
    return out


def opening_bar_count(df_session: pd.DataFrame, session_date: date) -> int:
    lo, hi = session_open_utc(session_date), orb_end_utc(session_date)
    return int(((df_session.index >= lo) & (df_session.index < hi)).sum())


def later_hole_count(df_session: pd.DataFrame, session_date: date) -> int:
    after = df_session[df_session.index >= orb_end_utc(session_date)]
    if len(after) < 2:
        return 0
    gaps = after.index.to_series().diff().dropna()
    return int((gaps > pd.Timedelta(minutes=1)).sum())


def generate_ideas(
    store: BarStore,
    strategy: BacktestStrategy,
    symbols: list[str],
    start: date,
    end: date,
) -> IdeasResult:
    """Load the period's bars (one query per symbol) and generate ideas from them."""
    return ideas_from_frames(load_frames(store, symbols, start, end), strategy, symbols, start, end)


def ideas_from_frames(
    loaded: Frames,
    strategy: BacktestStrategy,
    symbols: list[str],
    start: date,
    end: date,
) -> IdeasResult:
    result = IdeasResult()
    for symbol in symbols:
        result.frames[symbol] = loaded.frames[symbol]
        sessions = loaded.sessions[symbol]
        for d in weekdays(start, end):
            df_s = sessions.get(d)
            if df_s is None or df_s.empty:
                result.sessions.append(SessionInfo(symbol, d, 0, 0, "no_data", 0))
                continue
            opening = opening_bar_count(df_s, d)
            holes = later_hole_count(df_s, d)
            if opening < OPENING_MIN_BARS:
                result.sessions.append(SessionInfo(symbol, d, len(df_s), opening, "skipped_opening_hole", holes))
                continue
            result.sessions.append(SessionInfo(symbol, d, len(df_s), opening, "ok", holes))
            # A copy: session slices are views of the frame that find_exits and the gallery
            # walk later, and a strategy writing into its bars must never change them.
            idea: Optional[TradeIdea] = strategy.propose(symbol, df_s.copy())
            if idea is not None:
                result.ideas.append(idea)
    return result
