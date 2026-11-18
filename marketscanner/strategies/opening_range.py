"""
Opening Range Breakout strategy.

All maths lives in the pure module `orb_session`; this class only decides
*whether to alert* on a snapshot: once per symbol per session, and only when
the confirmation candle is fresh enough that the alert is still actionable.
"""
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import pandas as pd
import pytz

from marketscanner import config
from marketscanner.strategies import orb_session
from marketscanner.strategies.base import Signal, Strategy

_ET = pytz.timezone(config.TIMEZONE)
log = logging.getLogger(__name__)


class OpeningRangeStrategy(Strategy):
    def __init__(self, market: str) -> None:
        self.market = market
        # The only per-symbol state: the session we already alerted (or refused)
        # on, and the session we already warned about a flat first hour.
        self._fired_session: Optional[date] = None
        self._warned_flat_session: Optional[date] = None

    def check(
        self, df: pd.DataFrame, indicators: dict, now: Optional[datetime] = None
    ) -> Optional[Signal]:
        if df.empty:
            return None
        if now is None:
            now = datetime.now(timezone.utc)

        snap = orb_session.snapshot(df, now=now)
        if snap.box is None or not snap.range_final:
            return None   # nothing to judge until 10:30 ET (a lone 09:30 bar can look flat)
        if self._fired_session == snap.session_date:
            return None   # one signal per symbol per day

        if snap.box.delta <= 0:
            if self._warned_flat_session != snap.session_date:
                log.warning(
                    "%s: flat opening range on %s (top == bottom == %.2f) — no proposal today",
                    self.market, snap.session_date, snap.box.top,
                )
                self._warned_flat_session = snap.session_date
            return None
        if snap.late_confirmation is not None:
            # The cut-off is final for the session: every later candle would be later still.
            self._fired_session = snap.session_date
            log.info(
                "%s: confirmation %s ET closed after the %s cut-off — no alert today",
                self.market,
                snap.late_confirmation.candle_close.astimezone(_ET).strftime("%H:%M"),
                snap.cutoff.strftime("%H:%M"),
            )
            return None
        if snap.proposal is None:
            return None

        conf = snap.confirmation
        newest = pd.Timestamp(df.index[-1])
        newest = newest.tz_localize("UTC") if newest.tzinfo is None else newest.tz_convert("UTC")
        lateness = newest.to_pydatetime() - conf.candle_close
        if lateness > timedelta(minutes=config.CONFIRM_MAX_LATENESS_MINUTES):
            # A stale confirmation (mid-day restart, long feed outage) is never
            # alerted. The refusal is final for the session: find_confirmation
            # would return this same first candle on every later poll.
            self._fired_session = snap.session_date
            log.info(
                "%s: confirmation %s refused as stale (newest bar %s, %s late) — no alert today",
                self.market,
                conf.candle_open.astimezone(_ET).strftime("%H:%M"),
                newest.tz_convert(_ET).strftime("%H:%M"),
                lateness,
            )
            return None

        self._fired_session = snap.session_date
        prop = snap.proposal
        return Signal(
            market=self.market,
            strategy="OpeningRange",
            direction=conf.direction,
            price=prop.entry,
            fired_at=now,
            box_top=snap.box.top,
            box_bottom=snap.box.bottom,
            side=prop.side,
            entry=prop.entry,
            take_profit=prop.take_profit,
            stop_loss=prop.stop_loss,
            delta=prop.delta,
            confirm_open=conf.candle_open,
            confirm_close=conf.candle_close,
            candle_minutes=snap.candle_minutes,
            session_date=snap.session_date,
            reward_ratio=prop.reward_ratio,
            cutoff_et=snap.cutoff,
        )

    def reset(self) -> None:
        self._fired_session = None
        self._warned_flat_session = None

    @classmethod
    def session_box(cls, df: pd.DataFrame) -> tuple[Optional[float], Optional[float]]:
        """Thin alias for orb_session.session_box, kept for one release."""
        box = orb_session.session_box(df)
        if box is None:
            return None, None
        return box.top, box.bottom
