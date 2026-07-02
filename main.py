"""
MarketScanner entry point.

Usage:
    python main.py                    # live mode (Finnhub WebSocket)
    python main.py --backtest SPY     # replay today's yfinance data for SPY

Set HEADLESS=1 (or omit DISPLAY) to disable the live chart window.
Inside Docker the HEADLESS env var is set automatically.
"""
import argparse
import logging
import os
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytz

# ── Headless detection ───────────────────────────────────────────────────────
# Must happen BEFORE any matplotlib import so the backend can be set early.
_HEADLESS = (
    os.environ.get("HEADLESS", "").lower() in ("1", "true", "yes")
    or (
        not os.environ.get("DISPLAY")
        and not os.environ.get("WAYLAND_DISPLAY")
    )
)
if _HEADLESS:
    import matplotlib
    matplotlib.use("Agg")   # non-interactive PNG backend — no display needed
# ────────────────────────────────────────────────────────────────────────────

import pandas as pd
import matplotlib.animation as animation
import matplotlib.pyplot as plt
import mplfinance as mpf

from marketscanner import config

_ET = pytz.timezone(config.TIMEZONE)
from marketscanner.alerts.email_alert import send_alert
from marketscanner.data.finnhub_feed import FinnhubFeed
from marketscanner.data.yfinance_feed import fetch_today
from marketscanner.state.store import init_db, is_on_cooldown, log_signal, set_cooldown
from marketscanner.strategies.opening_range import OpeningRangeStrategy
from marketscanner.ui.chart import render_chart
from marketscanner.web.server import start_web_server

OUTPUT_DIR = Path("output/backtest")
TMP_DIR = Path("tmp")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
TMP_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
log = logging.getLogger(__name__)

_lock = threading.Lock()
_strategies = {
    symbol: OpeningRangeStrategy(symbol)
    for symbol in config.MARKETS
}
_signal_history: dict[str, list] = {s: [] for s in config.MARKETS}


def _on_bar(symbol: str, df) -> None:
    if df.empty:
        return
    # Freshness guard: never act on a bar that isn't from roughly "now". The
    # yfinance fallback (and a late process start) can hand us an entire
    # completed session; the strategy only inspects the last bar, so it would
    # detect a breakout on the 15:59 close and email hours later. Drop anything
    # staler than MAX_BAR_AGE_MINUTES so alerts only fire on live bars.
    last_ts = df.index[-1].to_pydatetime()
    if last_ts.tzinfo is None:
        last_ts = last_ts.replace(tzinfo=timezone.utc)
    age = datetime.now(timezone.utc) - last_ts
    if age > timedelta(minutes=config.MAX_BAR_AGE_MINUTES):
        log.debug(
            "Skipping stale bar for %s (last bar %s, age %s) — not a live tick",
            symbol, last_ts.isoformat(), age,
        )
        return

    strategy = _strategies[symbol]
    signal = strategy.check(df, indicators={})
    if signal is None:
        return
    if is_on_cooldown(signal.market, signal.strategy):
        log.info("Cooldown active — suppressing %s %s", signal.market, signal.strategy)
        return
    log.info("SIGNAL: %s", signal)
    set_cooldown(signal.market, signal.strategy)
    log_signal(signal)
    with _lock:
        _signal_history[symbol].append(signal.fired_at)
    fig = render_chart(
        df,
        market=symbol,
        box_top=signal.box_top,
        box_bottom=signal.box_bottom,
        signal_times=_signal_history[symbol],
    )
    send_alert(signal, chart_figure=fig)
    plt.close(fig)


def _midnight_reset(feed=None) -> None:
    """Reset strategy state machines (and clear the feed buffer) at midnight ET."""
    import pytz
    et = pytz.timezone(config.TIMEZONE)
    while True:
        now_et = datetime.now(et)
        # sleep until next midnight ET
        tomorrow = now_et.replace(hour=0, minute=0, second=5, microsecond=0)
        if tomorrow <= now_et:
            tomorrow = tomorrow + timedelta(days=1)
        sleep_s = (tomorrow - now_et).total_seconds()
        time.sleep(sleep_s)
        # Guard the whole reset: an unhandled exception here would kill the
        # thread and leave every strategy stuck in its prior state forever
        # (no more alerts until a full process restart).
        try:
            log.info("Midnight reset — resetting strategies and clearing feed buffer")
            for strategy in _strategies.values():
                strategy.reset()
            if feed is not None:
                feed.clear()
            with _lock:
                for symbol in _signal_history:
                    _signal_history[symbol].clear()
        except Exception:
            log.exception("Midnight reset failed — will retry at next rollover")


def run_live() -> None:
    init_db()
    symbols = list(config.MARKETS.keys())
    feed = FinnhubFeed(symbols=symbols, on_bar=_on_bar)
    feed.start()
    log.info("MarketScanner live — watching %s", symbols)

    # HTTP chart server — serves live candlestick charts at http://<host>:8080
    web_port = int(os.environ.get("WEB_PORT", "8080"))
    start_web_server(
        symbols=symbols,
        feed=feed,
        lock=_lock,
        strategies=_strategies,
        signal_history=_signal_history,
        render_chart_fn=render_chart,
        port=web_port,
    )

    threading.Thread(target=_midnight_reset, args=(feed,), daemon=True).start()

    if _HEADLESS:
        # Headless mode (Docker / screen / no display): keep the process alive
        # without opening a chart window.  Email alerts carry the chart image.
        log.info("Headless mode — live chart window disabled")
        while True:
            time.sleep(60)
    else:
        # Interactive mode: show a live-updating chart on the main thread
        plt.ion()
        fig_main = plt.figure(figsize=(14, 7))

        def _update_chart(frame):
            symbol = symbols[0]
            df = feed.get_df(symbol)
            if df.empty:
                return
            with _lock:
                signals = list(_signal_history.get(symbol, []))

            df_et = df.copy()
            df_et.index = df_et.index.tz_convert(_ET)

            fig_main.clf()
            ax_main = fig_main.add_subplot(211)
            ax_vol = fig_main.add_subplot(212)

            strategy = _strategies[symbol]
            box_top = strategy._range_high
            box_bottom = strategy._range_low

            add_plots = []
            if box_top is not None:
                add_plots.append(mpf.make_addplot(
                    pd.Series(box_top, index=df_et.index),
                    ax=ax_main, color="blue", linestyle="dotted", width=1.2,
                ))
            if box_bottom is not None:
                add_plots.append(mpf.make_addplot(
                    pd.Series(box_bottom, index=df_et.index),
                    ax=ax_main, color="red", linestyle="dotted", width=1.2,
                ))

            mpf.plot(
                df_et, type="candle", style="charles",
                title=f"{symbol} — Opening Range Breakout",
                ax=ax_main, volume=ax_vol,
                addplot=add_plots if add_plots else None,
            )

            for ts in signals:
                ax_main.axvline(x=ts.astimezone(_ET), color="purple", linestyle="--", linewidth=1)

            fig_main.canvas.draw()

        ani = animation.FuncAnimation(fig_main, _update_chart, interval=60_000)
        plt.show(block=True)


def run_backtest(symbol: str) -> None:
    log.info("Backtest mode — %s (today via yfinance)", symbol)
    df = fetch_today(symbol)
    if df.empty:
        log.error("No data for %s", symbol)
        return
    strategy = OpeningRangeStrategy(symbol)
    signal_times = []
    for i in range(1, len(df) + 1):
        signal = strategy.check(df.iloc[:i], indicators={})
        if signal:
            log.info("BACKTEST SIGNAL: %s", signal)
            signal_times.append(signal.fired_at)
            break  # one signal per day per design

    fig = render_chart(
        df,
        market=symbol,
        box_top=strategy._range_high,
        box_bottom=strategy._range_low,
        signal_times=signal_times,
    )
    filename = OUTPUT_DIR / f"backtest_{symbol}_{date.today()}.png"
    fig.savefig(filename, bbox_inches="tight")
    log.info("Chart saved to %s", filename)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--backtest", metavar="SYMBOL", help="Run backtest for symbol")
    args = parser.parse_args()

    if args.backtest:
        run_backtest(args.backtest.upper())
    else:
        run_live()
