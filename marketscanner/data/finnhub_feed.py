"""
Live feed via Finnhub WebSocket.
Aggregates raw trade ticks into completed 1-minute OHLCV bars.
On each completed bar, calls on_bar(symbol, df) on the main DataFrame.

Reconnects with exponential backoff (1→2→4→8→…→60s).
Falls back to yfinance polling when:
  - 5 consecutive WS-level exceptions, OR
  - Finnhub sends a type:error message (expired key, rate limit, etc.), OR
  - No trade data received for _STALE_MINUTES during market hours.
"""
import json
import logging
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Callable, Dict

import pandas as pd
import websocket

from marketscanner import config
from marketscanner.data import yfinance_feed

log = logging.getLogger(__name__)

OnBarCallback = Callable[[str, pd.DataFrame], None]

# Finnhub WebSocket endpoint
_WS_URL = f"wss://ws.finnhub.io?token={config.FINNHUB_API_KEY}"

# Ticks arriving ≤2s after minute boundary are attributed to the closed bar
_LATE_TICK_GRACE_S = 2

# If no trade ticks arrive for this many minutes during market hours, force
# the yfinance fallback — catches a connected-but-silent WebSocket (e.g. key
# expired or free-tier subscription silently rejected).
_STALE_MINUTES = 5


class FinnhubFeed:
    def __init__(self, symbols: list[str], on_bar: OnBarCallback) -> None:
        self._symbols = symbols
        self._on_bar = on_bar
        # per-symbol: list of (timestamp_s, price, volume) for current open bar
        self._ticks: Dict[str, list] = defaultdict(list)
        self._current_minute: Dict[str, int] = {}   # symbol → open bar minute (epoch // 60)
        # Initialise with explicit float64 dtypes. pandas ≥3.0 no longer upcasts
        # when concat-ing an empty object frame with a float frame; keeping dtype
        # correct here avoids mplfinance's np.isnan() crashing on object arrays.
        self._bars: Dict[str, pd.DataFrame] = {
            s: self._empty_frame() for s in symbols
        }
        self._lock = threading.Lock()
        self._ws: websocket.WebSocketApp | None = None
        self._fail_count = 0
        self._stop = False
        self._last_tick_time: float | None = None   # monotonic time of last trade tick
        self._last_bar_time: float | None = None    # monotonic time a bar last CLOSED
        self._use_fallback = False                  # set True to abort WS and use yfinance
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._watchdog = threading.Thread(target=self._stale_watchdog, daemon=True)

    def start(self) -> None:
        if config.FEED_MODE == "yfinance":
            # Finnhub free tier connects but streams no US-ETF trades, so the WS
            # path just sits silent until the watchdog trips. Skip it entirely
            # and poll yfinance from the start — the reliable path for this
            # deployment.
            log.info("FEED_MODE=yfinance — polling yfinance, Finnhub WS disabled")
            self._use_fallback = True
            threading.Thread(
                target=self._yfinance_fallback, daemon=True, name="yfinance-feed"
            ).start()
            return
        self._thread.start()
        self._watchdog.start()

    def newest_bar_time(self, symbol: str) -> "datetime | None":
        """UTC timestamp of the most recent buffered bar, or None if empty."""
        with self._lock:
            df = self._bars.get(symbol)
            if df is None or df.empty:
                return None
            return df.index[-1].to_pydatetime()

    def stop(self) -> None:
        self._stop = True
        if self._ws:
            self._ws.close()

    def get_df(self, symbol: str) -> pd.DataFrame:
        with self._lock:
            return self._bars[symbol].copy()

    def clear(self) -> None:
        """
        Drop all buffered bars and in-progress ticks so the next session starts
        clean. Called at the ET day rollover — without it the rolling buffer
        carries prior-session bars into today, contaminating the opening-range
        box (see OpeningRangeStrategy._in_orb_window).
        """
        with self._lock:
            for s in self._symbols:
                self._bars[s] = self._empty_frame()
            self._ticks.clear()
            self._current_minute.clear()

    @staticmethod
    def _empty_frame() -> pd.DataFrame:
        return pd.DataFrame({
            col: pd.Series(dtype="float64")
            for col in ["open", "high", "low", "close", "volume"]
        })

    # -- internal --

    def _run(self) -> None:
        backoff = 1
        while not self._stop and not self._use_fallback:
            try:
                self._connect()
                self._fail_count = 0
                backoff = 1
            except Exception as exc:
                self._fail_count += 1
                log.warning("WebSocket error (attempt %d): %s", self._fail_count, exc)
                if self._fail_count >= 5:
                    log.error("5 consecutive WS failures — falling back to yfinance polling")
                    self._yfinance_fallback()
                    return
                sleep = min(backoff, 60)
                log.info("Reconnecting in %ds", sleep)
                time.sleep(sleep)
                backoff *= 2
        if self._use_fallback:
            self._yfinance_fallback()

    def _connect(self) -> None:
        ws = websocket.WebSocketApp(
            _WS_URL,
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )
        self._ws = ws
        ws.run_forever()

    def _on_open(self, ws) -> None:
        log.info("Finnhub WS connected — subscribing to %s", self._symbols)
        for symbol in self._symbols:
            ws.send(json.dumps({"type": "subscribe", "symbol": symbol}))

    def _on_message(self, ws, message: str) -> None:
        try:
            data = json.loads(message)
        except json.JSONDecodeError:
            return
        msg_type = data.get("type")
        if msg_type == "error":
            # Finnhub rejects the key or rate-limits via an in-band error message.
            # The WS stays open but will never send trades — force fallback now.
            log.error(
                "Finnhub error message received — falling back to yfinance: %s",
                data.get("msg", data),
            )
            self._use_fallback = True
            ws.close()
            return
        if msg_type != "trade":
            return
        self._last_tick_time = time.monotonic()
        for trade in data.get("data", []):
            self._ingest_tick(
                symbol=trade["s"],
                ts_ms=trade["t"],
                price=trade["p"],
                volume=trade.get("v", 0),
            )

    def _on_error(self, ws, error) -> None:
        log.warning("Finnhub WS error: %s", error)

    def _on_close(self, ws, code, msg) -> None:
        log.info("Finnhub WS closed: %s %s", code, msg)

    def _ingest_tick(self, symbol: str, ts_ms: int, price: float, volume: float) -> None:
        if symbol not in self._symbols:
            return
        ts_s = ts_ms / 1000.0
        bar_minute = int(ts_s // 60)

        with self._lock:
            prev_minute = self._current_minute.get(symbol)

            if prev_minute is None:
                self._current_minute[symbol] = bar_minute
                prev_minute = bar_minute

            grace_cutoff = prev_minute + 1 + (_LATE_TICK_GRACE_S / 60)

            if bar_minute == prev_minute:
                # same bar
                self._ticks[symbol].append((ts_s, price, volume))
            elif bar_minute == prev_minute + 1 or ts_s / 60 <= grace_cutoff:
                # late tick — attribute to previous bar
                self._ticks[symbol].append((ts_s, price, volume))
                self._close_bar(symbol, prev_minute)
                self._current_minute[symbol] = bar_minute
                self._ticks[symbol] = [(ts_s, price, volume)]
            elif bar_minute > prev_minute + 1:
                # gap — close current bar, discard gap, start new
                self._close_bar(symbol, prev_minute)
                self._current_minute[symbol] = bar_minute
                self._ticks[symbol] = [(ts_s, price, volume)]
            # ticks far in the past are silently discarded

    def _close_bar(self, symbol: str, minute_epoch: int) -> None:
        ticks = self._ticks.get(symbol, [])
        if not ticks:
            return
        prices = [t[1] for t in ticks]
        volumes = [t[2] for t in ticks]
        bar_ts = datetime.fromtimestamp(minute_epoch * 60, tz=timezone.utc)
        new_row = pd.DataFrame(
            [{
                "open":   prices[0],
                "high":   max(prices),
                "low":    min(prices),
                "close":  prices[-1],
                "volume": sum(volumes),
            }],
            index=pd.DatetimeIndex([bar_ts], tz="UTC"),
        )
        self._bars[symbol] = pd.concat(
            [self._bars[symbol], new_row]
        ).sort_index().iloc[-500:]   # keep last 500 bars in memory
        self._ticks[symbol] = []
        self._last_bar_time = time.monotonic()

        df_snapshot = self._bars[symbol].copy()
        # call outside lock to avoid holding it during strategy processing
        threading.Thread(
            target=self._on_bar, args=(symbol, df_snapshot), daemon=True
        ).start()

    def _stale_watchdog(self) -> None:
        """
        Check every minute whether the WS feed is producing *bars* during market
        hours. Staleness is measured against the last CLOSED bar, not the last
        tick: Finnhub free tier can dribble occasional trades that keep a
        tick-based timer fresh while no bar ever closes, freezing the buffer
        (observed live — QQQ/SPY stuck at 09:40 for 8 hours). If no new bar
        closes for _STALE_MINUTES while the session is open, force the yfinance
        fallback, which repopulates the full day.
        """
        import pytz
        et = pytz.timezone(config.TIMEZONE)
        stale_limit = _STALE_MINUTES * 60
        while not self._stop and not self._use_fallback:
            time.sleep(60)
            if self._use_fallback:
                break
            now_et = datetime.now(et)
            # Only check during regular US session (09:30–16:00 ET, Mon–Fri)
            is_weekday = now_et.weekday() < 5
            session_open = now_et.replace(
                hour=config.ORB_START_HOUR, minute=config.ORB_START_MINUTE,
                second=0, microsecond=0,
            )
            session_close = now_et.replace(hour=16, minute=0, second=0, microsecond=0)
            if not (is_weekday and session_open <= now_et <= session_close):
                continue  # outside market hours — nothing to check
            if self._last_bar_time is None:
                elapsed = stale_limit + 1  # no bar ever closed
            else:
                elapsed = time.monotonic() - self._last_bar_time
            if elapsed > stale_limit:
                log.error(
                    "No Finnhub bars for %.0fs during market hours — "
                    "falling back to yfinance (check API key / subscription tier)",
                    elapsed,
                )
                self._use_fallback = True
                if self._ws:
                    self._ws.close()

    def _yfinance_fallback(self) -> None:
        log.info("yfinance polling: every 60s")
        while not self._stop:
            for symbol in self._symbols:
                try:
                    df = yfinance_feed.fetch_today(symbol)
                    if not df.empty:
                        with self._lock:
                            self._bars[symbol] = df
                            self._last_bar_time = time.monotonic()
                        # _on_bar applies its own freshness guard, so an
                        # after-hours poll that returns a completed session
                        # updates the dashboard without firing a late alert.
                        self._on_bar(symbol, df)
                except Exception as exc:
                    log.warning("yfinance poll error for %s: %s", symbol, exc)
            time.sleep(60)
