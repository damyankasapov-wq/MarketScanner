from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import pandas as pd


@dataclass
class Signal:
    market: str
    strategy: str
    direction: str      # "UP" or "DOWN"
    price: float        # close price that triggered
    fired_at: datetime  # UTC
    box_top: Optional[float] = None
    box_bottom: Optional[float] = None


class Strategy(ABC):
    @abstractmethod
    def check(self, df: pd.DataFrame, indicators: dict) -> Optional[Signal]:
        """
        Called after each completed 1-minute bar.
        df: OHLCV DataFrame with DatetimeIndex (UTC), columns: open high low close volume
        indicators: dict of TA-Lib output arrays keyed by function name
        Returns a Signal if triggered, else None.
        """
        ...

    @abstractmethod
    def reset(self) -> None:
        """Reset intraday state (called at midnight ET)."""
        ...
