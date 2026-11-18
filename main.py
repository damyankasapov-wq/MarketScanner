"""
MarketScanner entry point.

Usage:
    python main.py                          # live mode (headless; feed per FEED_MODE)
    python main.py --replay SPY 2026-11-10  # render a past session's ORB chart
    python main.py --backtest SPY           # alias for --replay SPY <today>

Informational only — nothing here places, modifies or closes orders.
"""
import argparse
import logging
import os
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import matplotlib

matplotlib.use("Agg")   # headless PNG backend — the renderer is OO-only anyway

import pytz

from marketscanner import config

_ET = pytz.timezone(config.TIMEZONE)
from marketscanner.alerts.email_alert import send_alert
from marketscanner.data.archive import archive_frames
from marketscanner.data.finnhub_feed import FinnhubFeed
from marketscanner.data.pg_bar_store import PostgresBarStore
from marketscanner.data.yfinance_feed import fetch_range
from marketscanner.state.store import (
    init_db, is_on_cooldown, log_signal, set_cooldown, signal_exists,
)
from marketscanner.strategies import orb_session
from marketscanner.strategies.orb_session import replay_now
from marketscanner.strategies.opening_range import OpeningRangeStrategy
from marketscanner.ui.chart import render_chart
from marketscanner.web.server import start_web_server

REPLAY_DIR = Path("output/replay")
TMP_DIR = Path("tmp")
REPLAY_DIR.mkdir(parents=True, exist_ok=True)
TMP_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
log = logging.getLogger(__name__)

_strategies = {
    symbol: OpeningRangeStrategy(symbol)
    for symbol in config.MARKETS
}


def _on_bar(symbol: str, df, now: Optional[datetime] = None) -> None:
    if df.empty:
        return
    if now is None:
        now = datetime.now(timezone.utc)
    # Freshness guard on the newest 1-MINUTE bar (never on a candle label):
    # the yfinance fallback or a late process start can hand us an entire
    # completed session; drop anything staler than MAX_BAR_AGE_MINUTES so
    # alerts only ever fire on live bars.
    last_ts = df.index[-1].to_pydatetime()
    if last_ts.tzinfo is None:
        last_ts = last_ts.replace(tzinfo=timezone.utc)
    age = now - last_ts
    if age > timedelta(minutes=config.MAX_BAR_AGE_MINUTES):
        log.debug(
            "Skipping stale bar for %s (last bar %s, age %s) — not a live tick",
            symbol, last_ts.isoformat(), age,
        )
        return

    strategy = _strategies[symbol]
    signal = strategy.check(df, indicators={}, now=now)
    if signal is None:
        return
    if is_on_cooldown(signal.market, signal.strategy):
        log.info("Cooldown active — suppressing %s %s", signal.market, signal.strategy)
        return
    if signal_exists(signal.market, signal.strategy, signal.session_date):
        log.info("Signal already logged for %s on %s — not emailing twice",
                 signal.market, signal.session_date)
        return
    log.info("SIGNAL: %s", signal)
    set_cooldown(signal.market, signal.strategy)
    log_signal(signal)

    # Everything drawn and emailed comes from the same pure snapshot the
    # strategy just evaluated (cheap to recompute; no new strategy API).
    snap = orb_session.snapshot(df, now=now)
    fig = render_chart(snap, market=symbol)
    send_alert(signal, snap, chart_figure=fig)


def _snapshot_frames(feed) -> dict:
    """Copies of every symbol's buffer, taken BEFORE the feed is cleared."""
    frames: dict = {}
    if feed is None:
        return frames
    for symbol in config.MARKETS:
        try:
            frames[symbol] = feed.get_df(symbol).copy()
        except Exception:
            log.exception("Could not copy %s buffer for the bars archive", symbol)
    return frames


def _archive_in_background(frames: dict, store_factory=None):
    """
    Save the copied frames to the bars archive on their own thread. The archive
    must never be able to delay or break the midnight reset (a database that
    hangs would otherwise freeze the reset thread and silently kill the next
    day's alerts), so it runs AFTER the reset, from copies, off-thread.
    """
    if not frames:
        return None

    def work() -> None:
        try:
            store = (store_factory or PostgresBarStore)()
            result = archive_frames(store, frames)
            log.info(
                "Bars archive: saved %s; errors: %s",
                {s: [d.isoformat() for d in ds] for s, ds in result.written.items()},
                result.errors or "none",
            )
        except Exception:
            log.exception("Bars archive crashed")

    thread = threading.Thread(target=work, name="bars-archive", daemon=True)
    thread.start()
    return thread


def _do_midnight_reset(feed=None, store_factory=None):
    """
    The reset itself: copy the frames, reset every strategy, clear the feed, and
    only then start the archive thread. Returns the archive thread (or None).
    """
    frames = _snapshot_frames(feed)
    for strategy in _strategies.values():
        strategy.reset()
    if feed is not None:
        feed.clear()
    return _archive_in_background(frames, store_factory)


def _midnight_reset(feed=None) -> None:
    """Reset strategy latches (and clear the feed buffer) at midnight ET."""
    while True:
        now_et = datetime.now(_ET)
        # sleep until next midnight ET
        tomorrow = now_et.replace(hour=0, minute=0, second=5, microsecond=0)
        if tomorrow <= now_et:
            tomorrow = tomorrow + timedelta(days=1)
        sleep_s = (tomorrow - now_et).total_seconds()
        time.sleep(sleep_s)
        # Guard the whole reset: an unhandled exception here would kill the
        # thread and leave every strategy latched forever (no more alerts
        # until a full process restart).
        try:
            log.info("Midnight reset — resetting strategies and clearing feed buffer")
            _do_midnight_reset(feed)
        except Exception:
            log.exception("Midnight reset failed — will retry at next rollover")


def run_live() -> None:
    if config.FEED_MODE != "yfinance" and not config.FINNHUB_API_KEY:
        raise RuntimeError(
            f"FEED_MODE={config.FEED_MODE!r} needs FINNHUB_API_KEY, which is not set. "
            "Set the key in .env, or set FEED_MODE=yfinance to poll yfinance only."
        )
    init_db()
    symbols = list(config.MARKETS.keys())
    feed = FinnhubFeed(symbols=symbols, on_bar=_on_bar)
    feed.start()
    log.info("MarketScanner live — watching %s", symbols)

    # HTTP chart server — serves live candlestick charts at http://<host>:8080
    web_port = int(os.environ.get("WEB_PORT", "8080"))
    start_web_server(symbols=symbols, feed=feed, port=web_port, bar_store=PostgresBarStore())

    threading.Thread(target=_midnight_reset, args=(feed,), daemon=True).start()

    # Keep the process alive; email alerts and the dashboard carry the charts.
    while True:
        time.sleep(60)


def run_replay(symbol: str, session_date: date, fetch=fetch_range) -> Optional[Path]:
    """
    Render any past session (yfinance keeps ~30 days of 1-minute bars):
    output/replay/replay_<SYMBOL>_<date>.png plus one log line with box,
    confirmation, proposal and outcome.
    """
    log.info("Replay — %s %s (yfinance 1-minute bars)", symbol, session_date)
    df = fetch(symbol, session_date, session_date + timedelta(days=1))
    if df.empty:
        log.error("No data for %s on %s", symbol, session_date)
        return None

    snap = orb_session.snapshot(df, now=replay_now(session_date))
    fig = render_chart(snap, market=symbol)
    REPLAY_DIR.mkdir(parents=True, exist_ok=True)
    filename = REPLAY_DIR / f"replay_{symbol}_{session_date.isoformat()}.png"
    fig.savefig(filename, bbox_inches="tight", dpi=110)

    log.info("REPLAY %s %s: %s", symbol, session_date, describe_snapshot(snap))
    log.info("Chart saved to %s", filename)
    return filename


def describe_snapshot(snap: orb_session.OrbSnapshot) -> str:
    if snap.box is None:
        return "no opening range (no bars in 09:30–10:30 ET)"
    parts = [f"box {snap.box.bottom:.2f}–{snap.box.top:.2f} (delta {snap.box.delta:.2f})",
             f"{len(snap.candles)} candles of {snap.candle_minutes} min"]
    if snap.confirmation is None:
        late = snap.late_confirmation
        if late is not None:
            parts.append(f"confirmation {late.candle_close.astimezone(_ET):%H:%M} ET after cut-off "
                         f"{snap.cutoff:%H:%M}, no trade")
        else:
            parts.append("no confirmation candle")
        return "; ".join(parts)
    c = snap.confirmation
    p = snap.proposal
    parts.append(
        f"confirmation {c.candle_open.astimezone(_ET):%H:%M}–{c.candle_close.astimezone(_ET):%H:%M} ET "
        f"{c.direction} O {c.open:.2f} H {c.high:.2f} L {c.low:.2f} C {c.close:.2f}"
    )
    parts.append(f"{p.side} @ {p.entry:.2f} TP {p.take_profit:.2f} SL {p.stop_loss:.2f}")
    o = snap.outcome
    if o is None or o.status == "OPEN":
        parts.append("outcome OPEN")
    else:
        parts.append(f"outcome {o.status} at {o.exit_ts.astimezone(_ET):%H:%M} ET "
                     f"({o.exit_price:.2f}, {o.r_multiple:+g}R)")
    return "; ".join(parts)


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--replay", nargs=2, metavar=("SYMBOL", "DATE"),
                        help="render a past session's ORB chart (DATE as YYYY-MM-DD)")
    parser.add_argument("--backtest", metavar="SYMBOL", help="alias for --replay SYMBOL <today>")
    return parser.parse_args(argv)


if __name__ == "__main__":
    args = _parse_args()
    if args.replay:
        symbol, day = args.replay
        run_replay(symbol.upper(), date.fromisoformat(day))
    elif args.backtest:
        run_replay(args.backtest.upper(), datetime.now(_ET).date())
    else:
        run_live()
