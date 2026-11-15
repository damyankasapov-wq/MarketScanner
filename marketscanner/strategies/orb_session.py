"""
Pure Opening-Range-Breakout session maths.

Every number the system shows (box, confirmation candle, trade proposal,
outcome) is derived here from a UTC-indexed 1-minute OHLCV frame and nothing
else: no I/O, no clock reads, no global state. The strategy, the dashboard,
the email and the replay command all call `snapshot()` and read the result, so
there is exactly one definition of every value and it survives restarts by
construction.

Conventions
- `df_1m`: DataFrame with a tz-aware UTC DatetimeIndex (one row per minute,
  stamped at the minute's *start*) and float columns open/high/low/close/volume.
- Candle timestamps are UTC bucket starts; `candle_close` is the bucket end.
- "Session" means the ET calendar date of the newest bar in the frame.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional

import pandas as pd
import pytz

from marketscanner import config

_ET = pytz.timezone(config.TIMEZONE)
_COLUMNS = ["open", "high", "low", "close", "volume"]
_OHLCV_AGG = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}


# ── dataclasses ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Box:
    top: float
    bottom: float

    @property
    def delta(self) -> float:
        return self.top - self.bottom


@dataclass(frozen=True)
class Confirmation:
    candle_open: datetime    # UTC bucket start
    candle_close: datetime   # UTC bucket end
    direction: str           # "UP" | "DOWN"
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True)
class Proposal:
    side: str                # "BUY" | "SELL"
    entry: float
    take_profit: float
    stop_loss: float
    delta: float


@dataclass(frozen=True)
class Outcome:
    status: str                            # "OPEN" | "TP_HIT" | "SL_HIT"
    exit_ts: Optional[datetime] = None     # UTC, the 1-minute bar that touched the level
    exit_candle: Optional[datetime] = None  # UTC bucket start containing exit_ts
    exit_price: Optional[float] = None     # the level touched, not the bar close
    r_multiple: Optional[float] = None     # +1.0, -1.0, None


@dataclass(frozen=True)
class OrbSnapshot:
    session_date: Optional[date]
    candles: pd.DataFrame
    box: Optional[Box]
    confirmation: Optional[Confirmation]
    proposal: Optional[Proposal]
    outcome: Optional[Outcome]
    candle_minutes: int


# ── time helpers ─────────────────────────────────────────────────────────────

def _as_utc_index(df: pd.DataFrame) -> pd.DataFrame:
    """Return df with a UTC tz-aware index (localising naive indexes as UTC)."""
    if df.index.tz is None:
        df = df.copy()
        df.index = df.index.tz_localize("UTC")
    elif str(df.index.tz) != "UTC":
        df = df.copy()
        df.index = df.index.tz_convert("UTC")
    return df


def _to_utc(ts) -> pd.Timestamp:
    ts = pd.Timestamp(ts)
    if ts.tzinfo is None:
        return ts.tz_localize("UTC")
    return ts.tz_convert("UTC")


def session_date_of(df_1m: pd.DataFrame) -> Optional[date]:
    """ET calendar date of the newest bar, or None for an empty frame."""
    if df_1m.empty:
        return None
    return _to_utc(df_1m.index[-1]).tz_convert(_ET).date()


def et_time_utc(session_date: date, hour: int, minute: int) -> pd.Timestamp:
    """The given ET wall-clock time on session_date, as a UTC Timestamp."""
    local = _ET.localize(datetime.combine(session_date, time(hour, minute)))
    return pd.Timestamp(local).tz_convert("UTC")


def session_open_utc(session_date: date) -> pd.Timestamp:
    return et_time_utc(session_date, config.ORB_START_HOUR, config.ORB_START_MINUTE)


def orb_end_utc(session_date: date) -> pd.Timestamp:
    return et_time_utc(session_date, config.ORB_END_HOUR, config.ORB_END_MINUTE)


def candle_floor(ts, session_date: date, minutes: int) -> pd.Timestamp:
    """UTC bucket start containing `ts`, buckets anchored at the session's 09:30 ET."""
    origin = session_open_utc(session_date)
    freq = pd.Timedelta(minutes=minutes)
    ts = _to_utc(ts)
    return origin + ((ts - origin) // freq) * freq


# ── pure functions ───────────────────────────────────────────────────────────

def session_slice(df_1m: pd.DataFrame) -> pd.DataFrame:
    """
    Rows on the ET date of the newest bar. The live feed keeps a rolling
    multi-day buffer; everything below works on this slice so a prior session
    can never leak into the box, the candles or the outcome.
    """
    if df_1m.empty:
        return df_1m
    df_1m = _as_utc_index(df_1m)
    et = df_1m.index.tz_convert(_ET)
    return df_1m[et.date == et[-1].date()]


def to_candles(
    df_1m: pd.DataFrame,
    minutes: int,
    now: Optional[datetime] = None,
    grace_seconds: Optional[int] = None,
) -> pd.DataFrame:
    """
    Group the session's 1-minute bars into `minutes`-wide candles.

    Buckets are anchored at the session's 09:30 ET (`origin=`), so they start at
    09:30, 09:35, ... for any candle size that divides 60 and in both EST and
    EDT (pandas' default origin only lands on 09:30 when `minutes` divides 30).
    Empty buckets are dropped.

    Completeness rule (data-derived, because the yfinance frame ends with the
    in-progress minute): every bucket that has at least one 1-minute bar in a
    *later* bucket is complete. The bucket containing the newest bar is dropped
    unless `now` is given and `now >= bucket_end + grace` (fallback for the last
    bucket of the day or a feed gap). Consequence: in Finnhub mode (closed bars
    only) the 10:40-10:45 candle is recognised when the 10:45 bar closes, about
    60 s after the candle itself closed; under yfinance polling it is recognised
    on the first poll that returns a 10:45 row (1-2 min later).
    """
    df = session_slice(df_1m)
    if df.empty:
        return _empty_candles()
    if grace_seconds is None:
        grace_seconds = config.CANDLE_GRACE_SECONDS

    sdate = session_date_of(df)
    origin = session_open_utc(sdate)
    freq = pd.Timedelta(minutes=minutes)

    candles = (
        df[_COLUMNS]
        .resample(f"{minutes}min", label="left", closed="left", origin=origin)
        .agg(_OHLCV_AGG)
        .dropna(subset=["open"])
    )

    newest = _to_utc(df.index[-1])
    last_bucket = candle_floor(newest, sdate, minutes)
    bucket_end = last_bucket + freq
    complete = False
    if now is not None:
        complete = _to_utc(now) >= bucket_end + pd.Timedelta(seconds=grace_seconds)
    if not complete:
        candles = candles[candles.index != last_bucket]
    return candles


def _empty_candles() -> pd.DataFrame:
    return pd.DataFrame(
        {c: pd.Series(dtype="float64") for c in _COLUMNS},
        index=pd.DatetimeIndex([], tz="UTC"),
    )


def session_box(df_1m: pd.DataFrame) -> Optional[Box]:
    """Opening range (high, low) of the session's 09:30-10:30 ET window, from 1-minute bars."""
    df = session_slice(df_1m)
    if df.empty:
        return None
    sdate = session_date_of(df)
    window = df[(df.index >= session_open_utc(sdate)) & (df.index < orb_end_utc(sdate))]
    if window.empty:
        return None
    return Box(top=float(window["high"].max()), bottom=float(window["low"].min()))


def find_confirmation(
    candles: pd.DataFrame,
    box: Box,
    minutes: int = config.CONFIRM_CANDLE_MINUTES,
) -> Optional[Confirmation]:
    """
    First candle at or after the ORB end (10:30 ET) whose whole range sits
    outside the box and that closes in the breakout direction:
      UP:   low  > box.top    and close > open
      DOWN: high < box.bottom and close < open
    Candles are scanned in order, so a missed poll cannot lose the signal.
    """
    if candles.empty:
        return None
    sdate = session_date_of(candles)
    after = candles[candles.index >= orb_end_utc(sdate)]
    up = (after["low"] > box.top) & (after["close"] > after["open"])
    down = (after["high"] < box.bottom) & (after["close"] < after["open"])
    hits = after[up | down]
    if hits.empty:
        return None
    ts = hits.index[0]
    row = hits.iloc[0]
    start = _to_utc(ts).to_pydatetime()
    return Confirmation(
        candle_open=start,
        candle_close=start + timedelta(minutes=minutes),
        direction="UP" if float(row["low"]) > box.top else "DOWN",
        open=float(row["open"]),
        high=float(row["high"]),
        low=float(row["low"]),
        close=float(row["close"]),
    )


def build_proposal(confirmation: Confirmation, box: Box) -> Proposal:
    """Entry = confirmation close; TP and SL one box-width away (fixed 1:1)."""
    delta = box.delta
    entry = confirmation.close
    if confirmation.direction == "UP":
        return Proposal("BUY", entry, entry + delta, entry - delta, delta)
    return Proposal("SELL", entry, entry - delta, entry + delta, delta)


def session_outcome(
    df_1m: pd.DataFrame,
    confirmation: Confirmation,
    proposal: Proposal,
    minutes: int = config.CONFIRM_CANDLE_MINUTES,
) -> Outcome:
    """
    Walk the 1-minute bars from the confirmation candle's close (finer than
    the candles, so a touch is attributed to the right minute). BUY: high >= TP
    is TP_HIT, low <= SL is SL_HIT; both in one bar count as SL_HIT
    (conservative). SELL mirrored. Uses every bar including the still-incomplete
    final bucket, so an outcome can resolve before its candle is drawn.
    """
    df = session_slice(df_1m)
    if df.empty:
        return Outcome("OPEN")
    sdate = session_date_of(df)
    bars = df[df.index >= _to_utc(confirmation.candle_close)]
    tp, sl = proposal.take_profit, proposal.stop_loss
    if proposal.side == "BUY":
        tp_hit = bars["high"] >= tp
        sl_hit = bars["low"] <= sl
    else:
        tp_hit = bars["low"] <= tp
        sl_hit = bars["high"] >= sl
    touched = bars[tp_hit | sl_hit]
    if touched.empty:
        return Outcome("OPEN")
    ts = touched.index[0]
    is_sl = bool(sl_hit.loc[ts])
    exit_ts = _to_utc(ts)
    return Outcome(
        status="SL_HIT" if is_sl else "TP_HIT",
        exit_ts=exit_ts.to_pydatetime(),
        exit_candle=candle_floor(exit_ts, sdate, minutes).to_pydatetime(),
        exit_price=sl if is_sl else tp,
        r_multiple=-1.0 if is_sl else 1.0,
    )


def snapshot(
    df_1m: pd.DataFrame,
    minutes: Optional[int] = None,
    now: Optional[datetime] = None,
) -> OrbSnapshot:
    """
    Compose everything for the session of the newest bar. This is the only
    function the rest of the system calls.

    - Empty frame, or no bars inside 09:30-10:30 → box None, nothing else.
    - box.delta <= 0 (flat first hour) → box set, no confirmation/proposal.
    """
    if minutes is None:
        minutes = config.CONFIRM_CANDLE_MINUTES
    df = session_slice(df_1m)
    sdate = session_date_of(df)
    candles = to_candles(df, minutes, now=now)
    box = session_box(df)
    confirmation = proposal = outcome = None
    if box is not None and box.delta > 0:
        confirmation = find_confirmation(candles, box, minutes)
        if confirmation is not None:
            proposal = build_proposal(confirmation, box)
            outcome = session_outcome(df, confirmation, proposal, minutes)
    return OrbSnapshot(
        session_date=sdate,
        candles=candles,
        box=box,
        confirmation=confirmation,
        proposal=proposal,
        outcome=outcome,
        candle_minutes=minutes,
    )


def utcnow() -> datetime:
    """Wall clock in UTC. Callers pass this in; the module never reads it itself."""
    return datetime.now(timezone.utc)
