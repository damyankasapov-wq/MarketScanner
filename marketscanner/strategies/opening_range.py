from datetime import datetime, time, timezone
from enum import Enum, auto
from typing import Optional

import pandas as pd
import pytz

from marketscanner import config
from marketscanner.strategies.base import Signal, Strategy

_ET = pytz.timezone(config.TIMEZONE)


class _State(Enum):
    IDLE = auto()       # before 09:30 ET
    BUILDING = auto()   # 09:30–10:30 ET — accumulating range
    WATCHING = auto()   # after 10:30 ET — waiting for breakout
    FIRED = auto()      # signal emitted; cooldown active


class OpeningRangeStrategy(Strategy):
    def __init__(self, market: str) -> None:
        self.market = market
        self._state = _State.IDLE
        self._range_high: Optional[float] = None
        self._range_low: Optional[float] = None

    def check(self, df: pd.DataFrame, indicators: dict) -> Optional[Signal]:
        if df.empty:
            return None

        last = df.iloc[-1]
        ts_utc: datetime = last.name.to_pydatetime().replace(tzinfo=timezone.utc)
        ts_et: datetime = ts_utc.astimezone(_ET)

        h, m = ts_et.hour, ts_et.minute

        orb_start = (config.ORB_START_HOUR, config.ORB_START_MINUTE)
        orb_end   = (config.ORB_END_HOUR,   config.ORB_END_MINUTE)

        if self._state == _State.FIRED:
            return None

        # Transition: IDLE → BUILDING at 09:30
        if self._state == _State.IDLE:
            if (h, m) >= orb_start:
                self._state = _State.BUILDING
            else:
                return None

        # Transition: BUILDING → WATCHING at 10:30
        if self._state == _State.BUILDING:
            if (h, m) < orb_end:
                # still inside window — extend range
                window = df[self._in_orb_window(df)]
                if not window.empty:
                    self._range_high = float(window["high"].max())
                    self._range_low  = float(window["low"].min())
                return None
            else:
                # just crossed 10:30 — finalise box
                window = df[self._in_orb_window(df)]
                if window.empty:
                    # incomplete — no data during window; stay IDLE
                    self._state = _State.IDLE
                    return None
                self._range_high = float(window["high"].max())
                self._range_low  = float(window["low"].min())
                self._state = _State.WATCHING

        # WATCHING — check close against box
        if self._state == _State.WATCHING:
            close = float(last["close"])
            if close > self._range_high:
                return self._fire("UP", close, ts_utc)
            if close < self._range_low:
                return self._fire("DOWN", close, ts_utc)

        return None

    def reset(self) -> None:
        self._state = _State.IDLE
        self._range_high = None
        self._range_low = None

    # -- helpers --

    def _fire(self, direction: str, price: float, ts_utc: datetime) -> Signal:
        self._state = _State.FIRED
        return Signal(
            market=self.market,
            strategy="OpeningRange",
            direction=direction,
            price=price,
            fired_at=ts_utc,
            box_top=self._range_high,
            box_bottom=self._range_low,
        )

    @staticmethod
    def _in_orb_window(df: pd.DataFrame) -> pd.Series:
        et = df.index.tz_convert(_ET)
        # Scope the opening range to the CURRENT session only — the ET date of
        # the newest bar. The live feed keeps a rolling multi-bar buffer that
        # can span more than one trading day; without a date bound the old
        # per-row `t.replace(hour=...)` matched 09:30–10:30 on *every* date in
        # the buffer, so the box high/low were computed across several sessions
        # and drawn as levels that don't bracket today's candles.
        session_date = et[-1].date()
        orb_start = _ET.localize(datetime.combine(
            session_date, time(config.ORB_START_HOUR, config.ORB_START_MINUTE)))
        orb_end = _ET.localize(datetime.combine(
            session_date, time(config.ORB_END_HOUR, config.ORB_END_MINUTE)))
        return (et >= orb_start) & (et < orb_end)
