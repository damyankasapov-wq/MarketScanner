"""
Smoke test: run GLD backtest and fire a real email alert with the HTML chart.
Usage:  python tmp/test_email_backtest.py
"""
import logging
import sys
from pathlib import Path

# Make sure project root is on sys.path when run from anywhere
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from marketscanner.alerts.email_alert import send_alert
from marketscanner.data.yfinance_feed import fetch_today
from marketscanner.strategies.opening_range import OpeningRangeStrategy
from marketscanner.ui.chart import render_chart

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s — %(message)s")
log = logging.getLogger(__name__)

SYMBOL = "GLD"

log.info("Fetching today's data for %s …", SYMBOL)
df = fetch_today(SYMBOL)
if df.empty:
    log.error("No data returned — aborting")
    sys.exit(1)

strategy = OpeningRangeStrategy(SYMBOL)
signal = None
signal_times = []

for i in range(1, len(df) + 1):
    sig = strategy.check(df.iloc[:i], indicators={})
    if sig:
        signal = sig
        signal_times.append(sig.fired_at)
        log.info("Signal found: %s", sig)
        break

if signal is None:
    log.warning("No signal fired today for %s — building a synthetic one for email test", SYMBOL)
    # Use last bar price so the email is plausible even on a flat day
    from datetime import timezone
    import pandas as pd
    last = df.iloc[-1]
    from marketscanner.strategies.base import Signal
    signal = Signal(
        market=SYMBOL,
        strategy="OpeningRange",
        direction="DOWN",
        price=float(last["Close"]),
        fired_at=df.index[-1].to_pydatetime().replace(tzinfo=timezone.utc),
        box_top=strategy._range_high,
        box_bottom=strategy._range_low,
    )
    signal_times = [signal.fired_at]

fig = render_chart(
    df,
    market=SYMBOL,
    box_top=strategy._range_high,
    box_bottom=strategy._range_low,
    signal_times=signal_times,
)

log.info("Sending email alert …")
send_alert(signal, chart_figure=fig)
log.info("Done — check your inbox.")
