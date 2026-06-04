"""
Historical / dev feed via yfinance.
Used for backtesting and as fallback when Finnhub WebSocket is unavailable.
"""
from datetime import date
from typing import Optional

import pandas as pd
import pytz
import yfinance as yf

from marketscanner import config

_ET = pytz.timezone(config.TIMEZONE)


def fetch_today(symbol: str) -> pd.DataFrame:
    """
    Download today's 1-minute bars for *symbol*.
    Returns a DataFrame with DatetimeIndex (UTC) and columns: open high low close volume.
    Empty DataFrame if market is closed or no data available.
    """
    ticker = yf.Ticker(symbol)
    df = ticker.history(period="1d", interval="1m", auto_adjust=True)
    if df.empty:
        return df
    df = _normalise(df)
    return df


def fetch_range(symbol: str, start: date, end: date) -> pd.DataFrame:
    """Download 1-minute bars for a date range (max ~30 days back on yfinance free)."""
    df = yf.download(
        symbol,
        start=start.isoformat(),
        end=end.isoformat(),
        interval="1m",
        auto_adjust=True,
        progress=False,
    )
    if df.empty:
        return df
    return _normalise(df)


def _normalise(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.columns = [c.lower() for c in df.columns]
    df = df[["open", "high", "low", "close", "volume"]]
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    else:
        df.index = df.index.tz_convert("UTC")
    df = df.sort_index()
    return df
