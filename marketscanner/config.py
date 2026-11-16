import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

MARKETS = {
    "SPY": {
        "finnhub_symbol": "SPY",
        "yfinance_symbol": "SPY",
    },
    "QQQ": {
        "finnhub_symbol": "QQQ",
        "yfinance_symbol": "QQQ",
    },
    "GLD": {
        "finnhub_symbol": "GLD",
        "yfinance_symbol": "GLD",
    },
}

STRATEGIES = ["OpeningRange"]

# Feed selection:
#   "auto"     — Finnhub WebSocket, fall back to yfinance polling on failure/stale
#   "yfinance" — poll yfinance directly, skip Finnhub entirely (reliable path
#                when the Finnhub tier can't stream US ETFs)
FEED_MODE = os.environ.get("FEED_MODE", "auto").lower()

# Only needed when FEED_MODE != "yfinance" (the Finnhub WebSocket). run_live()
# fails fast with a clear message in that case; everything else (backtester,
# replay, tests, a yfinance-only scanner) works without a key.
FINNHUB_API_KEY: str = os.environ.get("FINNHUB_API_KEY", "")

EMAIL = {
    "smtp_host": "smtp.gmail.com",
    "smtp_port": 587,
    "sender":    os.environ.get("EMAIL_SENDER", ""),
    "password":  os.environ.get("EMAIL_PASSWORD", ""),
    "recipient": os.environ.get("EMAIL_RECIPIENT", ""),
}

COOLDOWN_HOURS = 4

# A bar older than this (wall-clock vs the bar's own timestamp) is treated as
# replayed/stale and never fires an alert. Guards against the yfinance fallback
# handing the strategy a whole completed session after hours — which would
# "break out" on the 15:59 bar and email at, e.g., midnight. Kept generous
# because yfinance 1-min bars can lag 1–2 min plus the poll interval; a real
# replay is hours old, so 5 min still catches it without suppressing live bars.
MAX_BAR_AGE_MINUTES = 5

TIMEZONE = "America/New_York"

ORB_START_HOUR   = 9
ORB_START_MINUTE = 30
ORB_END_HOUR     = 10
ORB_END_MINUTE   = 30

# Size of the confirmation candle, built by grouping the 1-minute bars. Must
# divide 60 so the 10:30 ORB edge is also a candle edge; otherwise a candle
# would straddle the box window and could never be classified as before/after.


def _validate_candle_minutes(minutes: int) -> int:
    if minutes <= 0 or 60 % minutes:
        raise ValueError(
            "CONFIRM_CANDLE_MINUTES must divide 60 (1,2,3,4,5,6,10,12,15,20,30,60), "
            f"got {minutes}"
        )
    return minutes


CONFIRM_CANDLE_MINUTES = _validate_candle_minutes(
    int(os.environ.get("CONFIRM_CANDLE_MINUTES", "5"))
)

# A candle whose bucket holds the newest 1-minute bar is normally incomplete.
# It is accepted anyway once the caller's clock is this far past the bucket
# end (the last bucket of the day, or a feed gap, has no later bar to prove it).
CANDLE_GRACE_SECONDS = int(os.environ.get("CANDLE_GRACE_SECONDS", "90"))

# Fire only if the newest 1-minute bar is at most this far past the
# confirmation candle's close. Admits one or two missed polls but refuses a
# mid-day restart replaying an hours-old breakout.
CONFIRM_MAX_LATENESS_MINUTES = int(
    os.environ.get("CONFIRM_MAX_LATENESS_MINUTES", str(2 * CONFIRM_CANDLE_MINUTES))
)

DB = {
    "host":   os.environ.get("DB_HOST",     "localhost"),
    "port":   int(os.environ.get("DB_PORT", "5432")),
    "dbname": os.environ.get("DB_NAME",     "marketscanner"),
    "user":   os.environ.get("DB_USER",     "ms"),
    "password": os.environ.get("DB_PASSWORD", "ms"),
}
