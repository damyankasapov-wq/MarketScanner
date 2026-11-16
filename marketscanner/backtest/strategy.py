"""
Strategy registry for the backtester. A strategy looks at ONE finished session of
1-minute bars for one symbol and proposes at most one trade. Exits are not the
strategy's business: the engine walks them with the shared walk_exit rule.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional, Protocol

import pandas as pd

from marketscanner import config
from marketscanner.strategies.orb_session import replay_now, session_date_of, snapshot


@dataclass(frozen=True)
class TradeIdea:
    symbol: str
    side: str                    # "BUY" | "SELL"
    signal_ts: datetime          # UTC: the confirmation candle's close = the entry time
    entry: float
    take_profit: float
    stop_loss: float
    session_date: date           # New York date of the entry session
    delta: float                 # one box-width: the risk per share (|entry - stop|)
    meta: dict = field(default_factory=dict, compare=False)


class BacktestStrategy(Protocol):
    name: str

    def propose(self, symbol: str, df_session: pd.DataFrame) -> Optional[TradeIdea]:
        """At most one idea for this symbol's session, from a complete day of 1-minute bars."""


class OrbStrategy:
    """Opening Range Breakout: the same snapshot() the live alerts use."""

    name = "ORB"

    def __init__(self, candle_minutes: Optional[int] = None) -> None:
        self.candle_minutes = candle_minutes or config.CONFIRM_CANDLE_MINUTES

    def propose(self, symbol: str, df_session: pd.DataFrame) -> Optional[TradeIdea]:
        if df_session.empty:
            return None
        sdate = session_date_of(df_session)
        snap = snapshot(df_session, minutes=self.candle_minutes, now=replay_now(sdate))
        if snap.confirmation is None or snap.proposal is None:
            return None
        c, p = snap.confirmation, snap.proposal
        return TradeIdea(
            symbol=symbol,
            side=p.side,
            signal_ts=c.candle_close,
            entry=p.entry,
            take_profit=p.take_profit,
            stop_loss=p.stop_loss,
            session_date=sdate,
            delta=p.delta,
            meta={
                "candle_minutes": snap.candle_minutes,
                "confirmation": (c.open, c.high, c.low, c.close),
                "box": (snap.box.bottom, snap.box.top),
            },
        )


STRATEGIES = {"ORB": OrbStrategy}


def get_strategy(name: str, **params) -> BacktestStrategy:
    key = name.strip().upper()
    if key not in STRATEGIES:
        raise ValueError(f"unknown strategy {name!r}; available: {', '.join(sorted(STRATEGIES))}")
    return STRATEGIES[key](**params)
