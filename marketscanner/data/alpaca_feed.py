"""
Deep history via Alpaca's market-data API (free plan: 1-minute bars since 2016).

Used by the backfill for sessions older than yfinance's 30-day window. Returns
raw traded prices (adjustment=raw, matching the archive's auto_adjust=False) for
the regular session only (09:30-16:00 ET, matching yfinance's prepost=False), in
the same frame shape as yfinance_feed: UTC DatetimeIndex, open high low close volume.

The free plan may query the full-market SIP feed as long as the request ends
more than 15 minutes ago, which is always true for a past session. Needs
ALPACA_API_KEY_ID and ALPACA_API_SECRET_KEY (a free Alpaca account).
"""
from __future__ import annotations

import logging
import time
from datetime import date
from typing import Callable, Optional

import pandas as pd
import pytz
import requests

from marketscanner import config

log = logging.getLogger(__name__)
_ET = pytz.timezone(config.TIMEZONE)

BARS_URL = "https://data.alpaca.markets/v2/stocks/{symbol}/bars"
PAGE_LIMIT = 10_000      # Alpaca's maximum bars per page
MAX_RETRIES = 5          # on HTTP 429 (free plan: 200 requests/minute) and 5xx
TIMEOUT_S = 30


class AlpacaError(RuntimeError):
    pass


def is_configured() -> bool:
    return bool(config.ALPACA_API_KEY_ID and config.ALPACA_API_SECRET_KEY)


def fetch_range(
    symbol: str,
    start: date,
    end: date,
    session: Optional[requests.Session] = None,
    sleep: Callable[[float], None] = time.sleep,
) -> pd.DataFrame:
    """1-minute regular-session bars for ET dates in [start, end), following pagination."""
    if not is_configured():
        raise AlpacaError("ALPACA_API_KEY_ID / ALPACA_API_SECRET_KEY are not set")
    http = session or requests.Session()
    headers = {
        "APCA-API-KEY-ID": config.ALPACA_API_KEY_ID,
        "APCA-API-SECRET-KEY": config.ALPACA_API_SECRET_KEY,
    }
    params = {
        "timeframe": "1Min",
        "start": _et_midnight(start).isoformat(),
        "end": _et_midnight(end).isoformat(),
        "adjustment": "raw",
        "feed": config.ALPACA_DATA_FEED,
        "limit": PAGE_LIMIT,
        "sort": "asc",
    }
    rows: list[dict] = []
    token: Optional[str] = None
    while True:
        page_params = dict(params, page_token=token) if token else params
        body = _get(http, BARS_URL.format(symbol=symbol), headers, page_params, sleep)
        rows.extend(body.get("bars") or [])
        token = body.get("next_page_token")
        if not token:
            break
    return bars_to_frame(rows)


def bars_to_frame(rows: list[dict]) -> pd.DataFrame:
    """Alpaca bar dicts (t o h l c v) -> normalised regular-session frame."""
    if not rows:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    df = pd.DataFrame(rows)
    df.index = pd.to_datetime(df["t"], utc=True)
    df.index.name = None
    df = df.rename(columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"})
    df = df[["open", "high", "low", "close", "volume"]].astype(float)
    et = df.index.tz_convert(_ET)
    minutes = et.hour * 60 + et.minute
    df = df[(minutes >= 9 * 60 + 30) & (minutes < 16 * 60)]
    return df[~df.index.duplicated(keep="last")].sort_index()


def _et_midnight(d: date) -> pd.Timestamp:
    return pd.Timestamp(_ET.localize(pd.Timestamp(d).to_pydatetime())).tz_convert("UTC")


def _get(http, url, headers, params, sleep) -> dict:
    for attempt in range(MAX_RETRIES + 1):
        resp = http.get(url, headers=headers, params=params, timeout=TIMEOUT_S)
        retryable = resp.status_code == 429 or resp.status_code >= 500
        if retryable and attempt < MAX_RETRIES:
            wait = float(resp.headers.get("Retry-After") or 2 ** attempt)
            log.info("alpaca HTTP %s, retrying in %.0fs", resp.status_code, wait)
            sleep(wait)
            continue
        if resp.status_code != 200:
            raise AlpacaError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        return resp.json()
    raise AlpacaError("rate limited: retries exhausted")
