"""
Deep history via Alpaca's market-data API (free plan: 1-minute bars since 2016).

Used by the backfill for sessions older than yfinance's 30-day window. Returns
raw traded prices (adjustment=raw, matching the archive's auto_adjust=False) for
the regular session only (09:30-16:00 ET, matching yfinance's prepost=False), in
the same frame shape as yfinance_feed: UTC DatetimeIndex, open high low close volume.

The free plan may query the full-market SIP feed as long as the request ends
more than 15 minutes ago, which is always true for a past session. Only the SIP
feed is accepted: IEX bars come from a few percent of the volume, so their highs
and lows (and the ORB box) differ from the real market. Needs ALPACA_API_KEY_ID
and ALPACA_API_SECRET_KEY (a free Alpaca account).

AlpacaFatalError (bad keys, missing permission, wrong feed setting) means every
further request would fail the same way: the backfill stops asking for the run.
"""
from __future__ import annotations

import logging
import time
from datetime import date
from typing import Callable, Optional
from urllib.parse import quote

import pandas as pd
import pytz
import requests

from marketscanner import config

log = logging.getLogger(__name__)
_ET = pytz.timezone(config.TIMEZONE)

BARS_URL = "https://data.alpaca.markets/v2/stocks/{symbol}/bars"
PAGE_LIMIT = 10_000      # Alpaca's maximum bars per page
MAX_PAGES = 100          # one month of extended-hours bars is about 3 pages
MAX_RETRIES = 5          # on HTTP 429 (free plan: 200 requests/minute), 5xx and network errors
MAX_WAIT_S = 65.0        # cap on one retry wait (the rate-limit window is a minute)
TIMEOUT_S = 30
FEED = "sip"


class AlpacaError(RuntimeError):
    pass


class AlpacaFatalError(AlpacaError):
    """Every further request would fail the same way (keys, permission, settings)."""


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
        raise AlpacaFatalError("ALPACA_API_KEY_ID / ALPACA_API_SECRET_KEY are not set")
    if config.ALPACA_DATA_FEED != FEED:
        raise AlpacaFatalError(
            f"ALPACA_DATA_FEED={config.ALPACA_DATA_FEED!r} is not supported: only 'sip' (all US exchanges) "
            "is used for backfill, because IEX-only bars understate highs and lows"
        )
    if session is not None:
        return _fetch(session, symbol, start, end, sleep)
    with requests.Session() as http:
        return _fetch(http, symbol, start, end, sleep)


def _fetch(http, symbol: str, start: date, end: date, sleep) -> pd.DataFrame:
    headers = {
        "APCA-API-KEY-ID": config.ALPACA_API_KEY_ID,
        "APCA-API-SECRET-KEY": config.ALPACA_API_SECRET_KEY,
    }
    params = {
        "timeframe": "1Min",
        "start": _et_midnight(start).isoformat(),
        "end": _et_midnight(end).isoformat(),
        "adjustment": "raw",
        "feed": FEED,
        "limit": PAGE_LIMIT,
        "sort": "asc",
    }
    url = BARS_URL.format(symbol=quote(symbol, safe=""))
    rows: list[dict] = []
    token: Optional[str] = None
    seen: set[str] = set()
    for _ in range(MAX_PAGES):
        page_params = dict(params, page_token=token) if token else params
        body = _get(http, url, headers, page_params, sleep)
        rows.extend(body.get("bars") or [])
        token = body.get("next_page_token")
        if not token:
            return bars_to_frame(rows)
        if token in seen:
            raise AlpacaError(f"pagination loop: page token {token!r} returned twice")
        seen.add(token)
    raise AlpacaError(f"more than {MAX_PAGES} pages for {symbol} {start}..{end}")


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


def _retry_wait(resp, attempt: int) -> float:
    """Seconds to wait: Retry-After when it is a number, else 1, 2, 4, 8, 16; clamped to [1, MAX_WAIT_S]."""
    try:
        wait = float(resp.headers.get("Retry-After")) if resp is not None else float(2 ** attempt)
    except (TypeError, ValueError):
        wait = float(2 ** attempt)
    return min(max(wait, 1.0), MAX_WAIT_S)


def _get(http, url, headers, params, sleep) -> dict:
    last = ""
    for attempt in range(MAX_RETRIES + 1):
        try:
            resp = http.get(url, headers=headers, params=params, timeout=TIMEOUT_S)
        except requests.RequestException as exc:
            resp, last = None, f"network error: {exc!r}"
        else:
            if resp.status_code in (401, 403):
                raise AlpacaFatalError(f"HTTP {resp.status_code} (check the Alpaca keys and data permissions): "
                                       f"{resp.text[:200]}")
            if resp.status_code == 200:
                try:
                    return resp.json()
                except ValueError:
                    raise AlpacaError(f"HTTP 200 but not JSON ({resp.headers.get('Content-Type')}): "
                                      f"{resp.text[:200]}") from None
            if resp.status_code != 429 and resp.status_code < 500:
                raise AlpacaError(f"HTTP {resp.status_code}: {resp.text[:200]}")
            last = f"HTTP {resp.status_code}: {resp.text[:200]}"
        if attempt < MAX_RETRIES:
            wait = _retry_wait(resp, attempt)
            log.info("alpaca %s, retrying in %.0fs", last.split(":")[0], wait)
            sleep(wait)
    raise AlpacaError(f"gave up after {MAX_RETRIES + 1} attempts, last: {last}")
