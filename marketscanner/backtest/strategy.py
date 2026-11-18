"""
Strategy registry for the backtester. A strategy looks at ONE finished session of
1-minute bars for one symbol and proposes at most one trade. Exits are not the
strategy's business: the engine walks them with the shared walk_exit rule.

ORB settings the backtester can vary (OrbSettings):

    candle_minutes   confirmation candle length (changes which candle confirms)
    reward_ratio     take profit = entry +/- reward x box width; stop stays one box width
    cutoff           latest New York time the confirmation candle may CLOSE

Reward and cut-off never change which candle confirms: ORB takes the first
qualifying candle of the day, so a cut-off can only drop that idea, and the
reward only moves its target. OrbStrategy.derive applies both to a plain
(reward 1, no cut-off) idea, which is what lets the sweep compute ideas once per
candle size.

    session bars ──snapshot(candle)──▶ plain idea ──derive(settings)──▶ idea | None
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Optional, Protocol

import pandas as pd
import pytz

from marketscanner import config
from marketscanner.strategies.orb_session import levels, replay_now, session_date_of, snapshot

_ET = pytz.timezone(config.TIMEZONE)
EARLIEST_CUTOFF = time(10, 30)          # confirmations start at the end of the opening hour
LATEST_CUTOFF = time(16, 0)


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


@dataclass(frozen=True)
class OrbSettings:
    """The three ORB settings a backtest can vary; today's live behaviour is (env candle, 1.0, None)."""
    candle_minutes: int
    reward_ratio: float = 1.0
    cutoff: Optional[time] = None

    def __post_init__(self) -> None:
        try:
            config._validate_candle_minutes(self.candle_minutes)
        except ValueError:
            # The config message names the env var; a --candle typo should name the setting instead.
            raise ValueError("candle minutes must divide 60 (1,2,3,4,5,6,10,12,15,20,30,60), "
                             f"got {self.candle_minutes}") from None
        if not self.reward_ratio > 0:
            raise ValueError(f"reward ratio must be greater than 0, got {self.reward_ratio}")
        if self.cutoff is not None and not (EARLIEST_CUTOFF < self.cutoff <= LATEST_CUTOFF):
            raise ValueError(f"cut-off must be after 10:30 and no later than 16:00 ET, got {self.cutoff:%H:%M}")

    @classmethod
    def current(cls) -> "OrbSettings":
        """What the live scanner does with this machine's .env: its candle size, 1:1, no cut-off."""
        return cls(config.CONFIRM_CANDLE_MINUTES)

    def describe(self) -> str:
        cut = "no cut-off" if self.cutoff is None else f"cut-off {self.cutoff:%H:%M} ET"
        return f"{self.candle_minutes}-min candle, reward 1:{self.reward_ratio:g}, {cut}"

    def short(self) -> str:
        cut = "none" if self.cutoff is None else f"{self.cutoff:%H:%M}"
        return f"{self.candle_minutes} / {self.reward_ratio:g} / {cut}"

    def suffix(self) -> str:
        """run_id part for settings that differ from the fixed baseline 5 / 1 / none ('' when equal)."""
        parts = []
        if self.candle_minutes != 5:
            parts.append(f"c{self.candle_minutes}")
        if self.reward_ratio != 1.0:
            parts.append(f"r{self.reward_ratio:g}")
        if self.cutoff is not None:
            parts.append(f"x{self.cutoff:%H%M}")
        return "_".join(parts)

    def cli_args(self) -> list[str]:
        cut = "none" if self.cutoff is None else f"{self.cutoff:%H:%M}"
        return ["--candle", str(self.candle_minutes), "--reward", f"{self.reward_ratio:g}", "--cutoff", cut]


def parse_cutoff(text: str) -> Optional[time]:
    """'none' or 'HH:MM' (ET)."""
    t = text.strip().lower()
    if t in ("none", ""):
        return None
    try:
        hh, mm = t.split(":")
        return time(int(hh), int(mm))
    except ValueError:
        raise ValueError(f"cut-off must be HH:MM (ET) or none, got {text!r}") from None


class BacktestStrategy(Protocol):
    name: str

    def propose(self, symbol: str, df_session: pd.DataFrame) -> Optional[TradeIdea]:
        """At most one idea for this symbol's session, from a complete day of 1-minute bars."""


class OrbStrategy:
    """Opening Range Breakout: the same snapshot() the live alerts use, then the settings."""

    name = "ORB"

    def __init__(self, candle_minutes: Optional[int] = None, settings: Optional[OrbSettings] = None) -> None:
        if settings is None:
            settings = OrbSettings(candle_minutes or config.CONFIRM_CANDLE_MINUTES)
        elif candle_minutes is not None and candle_minutes != settings.candle_minutes:
            raise ValueError("OrbStrategy: candle_minutes and settings.candle_minutes disagree")
        self.settings = settings
        self.candle_minutes = settings.candle_minutes

    def propose(self, symbol: str, df_session: pd.DataFrame) -> Optional[TradeIdea]:
        plain = self.plain_idea(symbol, df_session)
        return None if plain is None else self.derive(plain, self.settings)

    def plain_idea(self, symbol: str, df_session: pd.DataFrame) -> Optional[TradeIdea]:
        """The idea at reward 1:1 and no cut-off, exactly what a live alert would propose."""
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
                "reward_ratio": 1.0,
                "cutoff": None,
            },
        )

    @staticmethod
    def derive(plain: TradeIdea, settings: OrbSettings) -> Optional[TradeIdea]:
        """
        Apply a reward ratio and cut-off to a PLAIN idea (reward 1, no cut-off) of the
        same candle size. None when the confirmation candle closes after the cut-off.
        The target is recomputed from (entry, delta, side) with orb_session.levels,
        never from the plain idea's target, so it is bit-identical to a direct build.
        """
        if plain.meta.get("reward_ratio", 1.0) != 1.0 or plain.meta.get("cutoff") is not None:
            raise ValueError("OrbStrategy.derive needs a plain idea (reward 1, no cut-off)")
        made_with = plain.meta.get("candle_minutes", settings.candle_minutes)
        if made_with != settings.candle_minutes:
            raise ValueError(f"idea was made with a {made_with}-min candle, settings say {settings.candle_minutes}")
        if settings.cutoff is not None:
            if pd.Timestamp(plain.signal_ts).tz_convert(_ET).time() > settings.cutoff:
                return None
        if settings.reward_ratio == 1.0 and settings.cutoff is None:
            return plain
        tp, sl = levels(plain.entry, plain.delta, plain.side, settings.reward_ratio)
        meta = {**plain.meta, "reward_ratio": settings.reward_ratio, "cutoff": settings.cutoff}
        return dataclasses.replace(plain, take_profit=tp, stop_loss=sl, meta=meta)


STRATEGIES = {"ORB": OrbStrategy}


def get_strategy(name: str, **params) -> BacktestStrategy:
    key = name.strip().upper()
    if key not in STRATEGIES:
        raise ValueError(f"unknown strategy {name!r}; available: {', '.join(sorted(STRATEGIES))}")
    return STRATEGIES[key](**params)
