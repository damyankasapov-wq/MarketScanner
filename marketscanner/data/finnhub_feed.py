"""
Live feed via Finnhub WebSocket.
Aggregates raw trade ticks into completed 1-minute OHLCV bars.
On each completed bar, calls on_bar(symbol, df) on the main DataFrame.

Reconnects with exponential backoff (1→2→4→8→…→60s).
After 5 consecutive failures, falls back to yfinance polling.
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


class FinnhubFeed:
    def __init__(self, symbols: list[str], on_bar: OnBarCallback) -> None:
        self._symbols = symbols
        self._on_bar = on_bar
        # per-symbol: list of (timestamp_s, price, volume) for current open bar
        self._ticks: Dict[str, list] = defaultdict(list)
        self._current_minute: Dict[str, int] = {}   # symbol → open bar minute (epoch // 60)
        self._bars: Dict[str, pd.DataFrame] = {s: pd.DataFrame(
            columns=["open", "high", "low", "close", "volume"]
        ) for s in symbols}
        self._lock = threading.Lock()
        self._ws: websocket.WebSocketApp | None = None
        self._fail_count = 0
        self._stop = False
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop = True
        if self._ws:
            self._ws.close()

    def get_df(self, symbol: str) -> pd.DataFrame:
        with self._lock:
            return self._bars[symbol].copy()

    # -- internal --

    def _run(self) -> None:
        backoff = 1
        while not self._stop:
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
        if data.get("type") != "trade":
            return
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

        df_snapshot = self._bars[symbol].copy()
        # call outside lock to avoid holding it during strategy processing
        threading.Thread(
            target=self._on_bar, args=(symbol, df_snapshot), daemon=True
        ).start()

    def _yfinance_fallback(self) -> None:
        log.info("yfinance fallback: polling every 60s")
        while not self._stop:
            for symbol in self._symbols:
                try:
                    df = yfinance_feed.fetch_today(symbol)
                    if not df.empty:
                        with self._lock:
                            self._bars[symbol] = df
                        self._on_bar(symbol, df)
                except Exception as exc:
                    log.warning("yfinance poll error for %s: %s", symbol, exc)
            time.sleep(60)
