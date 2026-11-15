"""
Manual smoke test: replay a recent session and fire a REAL email alert with the
HTML chart. Never collected by pytest (see tests/conftest.py).

Usage:  python tests/manual/test_email_backtest.py [SYMBOL] [YYYY-MM-DD]
"""
import logging
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

# Make sure project root is on sys.path when run from anywhere
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pytz

from marketscanner import config
from marketscanner.alerts.email_alert import send_alert
from marketscanner.data.yfinance_feed import fetch_range
from marketscanner.strategies import orb_session
from marketscanner.strategies.base import Signal
from marketscanner.ui.chart import render_chart

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s — %(message)s")
log = logging.getLogger(__name__)

_ET = pytz.timezone(config.TIMEZONE)
SYMBOL = (sys.argv[1] if len(sys.argv) > 1 else "GLD").upper()
DAY = date.fromisoformat(sys.argv[2]) if len(sys.argv) > 2 else datetime.now(_ET).date()

log.info("Fetching %s 1-minute bars for %s …", SYMBOL, DAY)
df = fetch_range(SYMBOL, DAY, DAY + timedelta(days=1))
if df.empty:
    log.error("No data returned — aborting")
    sys.exit(1)

close_et = _ET.localize(datetime.combine(DAY, datetime.min.time()).replace(hour=16))
snap = orb_session.snapshot(df, now=close_et + timedelta(seconds=config.CANDLE_GRACE_SECONDS))
if snap.proposal is None:
    log.error("No confirmation candle on %s for %s — pick another day", DAY, SYMBOL)
    sys.exit(1)

p, c = snap.proposal, snap.confirmation
signal = Signal(
    market=SYMBOL, strategy="OpeningRange", direction=c.direction, price=p.entry,
    fired_at=datetime.now(pytz.utc), box_top=snap.box.top, box_bottom=snap.box.bottom,
    side=p.side, entry=p.entry, take_profit=p.take_profit, stop_loss=p.stop_loss, delta=p.delta,
    confirm_open=c.candle_open, confirm_close=c.candle_close,
    candle_minutes=snap.candle_minutes, session_date=snap.session_date,
)
log.info("Proposal: %s", signal)
log.info("Sending email alert …")
send_alert(signal, snap, chart_figure=render_chart(snap, SYMBOL))
log.info("Done — check your inbox.")
