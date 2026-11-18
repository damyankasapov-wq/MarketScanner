"""
The simulated account.

Rules (agreed in the design review):
  - A deal has a fixed dollar amount (`per_deal`), fractional shares.
  - A deal opens only if cash - per_deal >= min_cash; otherwise the signal is
    recorded as SKIPPED with reason "cash".
  - BUY: the amount leaves cash. SELL (short): the same amount is reserved as
    collateral, so cash accounting is symmetric; P&L is the mirror of a long.
  - Trades are followed across days until TP/SL (see walk_exit); no forced close
    at 16:00. Trades with no exit by the period end are OPEN, marked at the last
    close and kept out of win rate.
  - Events are processed chronologically; at the same minute closes come before
    opens (they free cash), and opens go in market-list order.

Equity is defined by ONE identity:
    equity = initial_cash + sum(realised P&L) + unrealised P&L
with cash and invested derived from the open trades, so the books balance by
construction (equity.py builds the curve from the same trades).

Timeline (illustrative):

    ideas sorted by (signal_ts, market order)            closes scheduled in a heap
      idea ──▶ settle closes <= signal_ts ──▶ cash rule ──▶ OPEN trade ──▶ push close(exit_ts)
                                                  └──▶ SKIPPED("cash")
    end of idea list ──▶ settle every remaining close; trades with no exit stay OPEN
"""
from __future__ import annotations

import bisect
import heapq
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

import numpy as np
import pandas as pd
import pytz

from marketscanner import config
from marketscanner.backtest.strategy import TradeIdea
from marketscanner.data.sessions import et_day_codes, session_start_mask, session_starts
from marketscanner.strategies.orb_session import ExitHit, walk_exit

_ET = pytz.timezone(config.TIMEZONE)
EPS = 1e-9

WIN, LOSS, OPEN, SKIPPED = "WIN", "LOSS", "OPEN", "SKIPPED"


@dataclass(frozen=True)
class AccountConfig:
    initial_cash: float
    per_deal: float
    min_cash: float


@dataclass
class Trade:
    id: int
    symbol: str
    side: str
    signal_ts: datetime
    session_date: date
    entry: float
    take_profit: float
    stop_loss: float
    status: str
    amount: float = 0.0
    shares: float = 0.0
    risk: float = 0.0                       # shares x box width: what a stop loses
    opened_at: Optional[datetime] = None
    closed_at: Optional[datetime] = None
    exit_price: Optional[float] = None
    exit_kind: Optional[str] = None         # TP | SL | GAP_TP | GAP_SL
    pnl: Optional[float] = None
    pnl_pct: Optional[float] = None
    r_multiple: Optional[float] = None
    trading_minutes: Optional[int] = None   # minute bars between open and close (no overnight)
    sessions_held: Optional[int] = None     # session boundaries crossed (0 = same day)
    mark_price: Optional[float] = None      # OPEN only: last close of the period
    unrealised: Optional[float] = None
    skip_reason: Optional[str] = None

    @property
    def sign(self) -> int:
        return 1 if self.side == "BUY" else -1


# Exit walk windows, in sessions from the entry session: most trades close within a
# few sessions, so the walk looks at a short prefix first and widens about 5x on each
# miss, so a long hold costs roughly its own length, not the rest of the period.
EXIT_WINDOWS = (1, 6, 26, 130, 650)


class _ExitIndex:
    """Per-symbol facts the exit walk needs, computed once per frame instead of once per idea."""

    def __init__(self, frame: pd.DataFrame) -> None:
        if not frame.index.is_monotonic_increasing:
            raise ValueError("find_exits needs bars sorted by time (load them with load_frames)")
        self.frame = frame
        self.starts, _ = session_starts(frame.index)
        self.days = et_day_codes(frame.index)
        self.first_bar = session_start_mask(frame.index)


def _walk_one(ix: _ExitIndex, idea: TradeIdea) -> Optional[ExitHit]:
    """
    Walk prefix windows frame[start : start of session j + w] for w in EXIT_WINDOWS,
    then the whole remainder. Each window is a prefix of the same slice, and
    walk_exit returns the FIRST touching bar, so a hit inside a window is exactly
    the hit of the full walk; a miss on the full remainder is None, as before.

    The gap mask is the frame's session-start mask, except on the slice's first
    bar, which counts as a new session only if its date differs from the entry
    session (walk_exit's own rule, and what makes a 16:00 entry whose first walked
    bar is the next day's 09:30 gap correctly).

        frame   ... | entry session ...[start]... | s+1 | s+2 ... s+6 | ... | end
        w=1               [start ------------)
        w=6               [start ------------------------------------)
        last              [start -------------------------------------------------)
    """
    frame = ix.frame
    n = len(frame)
    start = int(frame.index.searchsorted(pd.Timestamp(idea.signal_ts)))
    if start >= n:
        return None
    first_is_new = bool(ix.days[start] != np.datetime64(idea.session_date, "D"))
    j = int(np.searchsorted(ix.starts, start, side="right")) - 1
    hit, end = None, start
    windows = iter(EXIT_WINDOWS)
    while hit is None and end < n:
        w = next(windows, None)
        end = n if w is None or j + w >= len(ix.starts) else int(ix.starts[j + w])
        mask = ix.first_bar[start:end].copy()
        mask[0] = first_is_new
        hit = walk_exit(
            frame.iloc[start:end], idea.side, idea.take_profit, idea.stop_loss,
            gap_fills=True, entry_session=idea.session_date, first_of_session=mask,
        )
    return hit


def find_exits(ideas: list[TradeIdea], frames: dict[str, pd.DataFrame]) -> list[Optional[ExitHit]]:
    """One exit (or None) per idea. Exits do not depend on the account, so this runs once."""
    index: dict[str, _ExitIndex] = {}
    exits: list[Optional[ExitHit]] = []
    for idea in ideas:
        ix = index.get(idea.symbol)
        if ix is None:
            ix = index[idea.symbol] = _ExitIndex(frames[idea.symbol])
        exits.append(_walk_one(ix, idea))
    return exits


def pnl_for(side: str, shares: float, entry: float, exit_price: float) -> float:
    return shares * (exit_price - entry) if side == "BUY" else shares * (entry - exit_price)


class _SessionIndex:
    """Per-symbol helpers to count minutes and session boundaries between two timestamps."""

    def __init__(self, frame: pd.DataFrame) -> None:
        self.idx = frame.index
        # session_starts lists every date at least once even on unsorted input.
        self.sessions = sorted(set(session_starts(frame.index)[1]))

    def minutes(self, a: datetime, b: Optional[datetime]) -> int:
        lo = self.idx.searchsorted(pd.Timestamp(a))
        hi = len(self.idx) if b is None else self.idx.searchsorted(pd.Timestamp(b))
        return int(hi - lo)

    def sessions_between(self, a: datetime, b: Optional[datetime]) -> int:
        end = b if b is not None else self.idx[-1].to_pydatetime()
        da = pd.Timestamp(a).tz_convert(_ET).date()
        db = pd.Timestamp(end).tz_convert(_ET).date()
        return bisect.bisect_left(self.sessions, db) - bisect.bisect_left(self.sessions, da)


def run_engine(
    ideas: list[TradeIdea],
    exits: list[Optional[ExitHit]],
    frames: dict[str, pd.DataFrame],
    cfg: AccountConfig,
    market_order: list[str],
) -> list[Trade]:
    order = {s: i for i, s in enumerate(market_order)}
    sequence = sorted(range(len(ideas)), key=lambda i: (ideas[i].signal_ts, order.get(ideas[i].symbol, 99), i))
    index = {s: _SessionIndex(f) for s, f in frames.items()}

    cash = cfg.initial_cash
    trades: list[Trade] = []
    closes: list[tuple[datetime, int, Trade, ExitHit]] = []
    seq = 0

    def settle(upto: Optional[datetime]) -> None:
        nonlocal cash
        while closes and (upto is None or closes[0][0] <= upto):
            _, _, tr, hit = heapq.heappop(closes)
            tr.closed_at = hit.ts
            tr.exit_price = hit.price
            tr.exit_kind = hit.kind
            tr.pnl = pnl_for(tr.side, tr.shares, tr.entry, hit.price)
            tr.pnl_pct = tr.pnl / tr.amount
            tr.r_multiple = tr.pnl / tr.risk if tr.risk else None
            tr.status = WIN if tr.pnl > 0 else LOSS
            cash += tr.amount + tr.pnl

    for i in sequence:
        idea = ideas[i]
        settle(idea.signal_ts)
        trade = Trade(
            id=len(trades) + 1, symbol=idea.symbol, side=idea.side, signal_ts=idea.signal_ts,
            session_date=idea.session_date, entry=idea.entry, take_profit=idea.take_profit,
            stop_loss=idea.stop_loss, status=OPEN,
        )
        trades.append(trade)
        if cash - cfg.per_deal < cfg.min_cash - EPS:
            trade.status = SKIPPED
            trade.skip_reason = "cash"
            continue
        trade.amount = cfg.per_deal
        trade.shares = cfg.per_deal / idea.entry
        trade.risk = trade.shares * idea.delta
        trade.opened_at = idea.signal_ts
        cash -= cfg.per_deal
        hit = exits[i]
        if hit is not None:
            heapq.heappush(closes, (hit.ts, seq, trade, hit))
            seq += 1
    settle(None)

    for tr in trades:
        if tr.status == SKIPPED:
            continue
        si = index[tr.symbol]
        tr.trading_minutes = si.minutes(tr.opened_at, tr.closed_at)
        tr.sessions_held = si.sessions_between(tr.opened_at, tr.closed_at)
        if tr.status == OPEN:
            tr.mark_price = float(frames[tr.symbol]["close"].iloc[-1])
            tr.unrealised = pnl_for(tr.side, tr.shares, tr.entry, tr.mark_price)
    return trades
