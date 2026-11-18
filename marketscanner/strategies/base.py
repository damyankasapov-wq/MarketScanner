from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Optional

import pandas as pd


@dataclass
class Signal:
    market: str
    strategy: str
    direction: str      # "UP" or "DOWN"
    price: float        # entry price (== entry; kept for compatibility)
    fired_at: datetime  # UTC wall-clock time of detection
    box_top: Optional[float] = None
    box_bottom: Optional[float] = None
    # Trade proposal (informational only — nothing trades)
    side: Optional[str] = None            # "BUY" | "SELL"
    entry: Optional[float] = None         # confirmation candle close
    take_profit: Optional[float] = None   # entry ± reward_ratio × delta
    stop_loss: Optional[float] = None     # entry ∓ delta
    delta: Optional[float] = None         # box top − box bottom
    confirm_open: Optional[datetime] = None   # UTC bucket start of the confirmation candle
    confirm_close: Optional[datetime] = None  # UTC bucket end
    candle_minutes: Optional[int] = None
    session_date: Optional[date] = None   # ET date of the session
    reward_ratio: float = 1.0             # take profit in box widths (REWARD_RATIO)
    cutoff_et: Optional[time] = None      # confirmation cut-off in force (CONFIRM_CUTOFF_ET)


class Strategy(ABC):
    @abstractmethod
    def check(
        self, df: pd.DataFrame, indicators: dict, now: Optional[datetime] = None
    ) -> Optional[Signal]:
        """
        Called after each new 1-minute bar.
        df: OHLCV DataFrame with DatetimeIndex (UTC), columns: open high low close volume
        indicators: dict of TA-Lib output arrays keyed by function name
        now: UTC wall clock (defaults to datetime.now(timezone.utc); tests inject it)
        Returns a Signal if triggered, else None.
        """
        ...

    @abstractmethod
    def reset(self) -> None:
        """Reset intraday state (called at midnight ET)."""
        ...
