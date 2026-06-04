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
}

STRATEGIES = ["OpeningRange"]

FINNHUB_API_KEY: str = os.environ["FINNHUB_API_KEY"]

EMAIL = {
    "smtp_host": "smtp.gmail.com",
    "smtp_port": 587,
    "sender":    os.environ.get("EMAIL_SENDER", ""),
    "password":  os.environ.get("EMAIL_PASSWORD", ""),
    "recipient": os.environ.get("EMAIL_RECIPIENT", ""),
}

COOLDOWN_HOURS = 4

TIMEZONE = "America/New_York"

ORB_START_HOUR   = 9
ORB_START_MINUTE = 30
ORB_END_HOUR     = 10
ORB_END_MINUTE   = 30

DB = {
    "host":   os.environ.get("DB_HOST",     "localhost"),
    "port":   int(os.environ.get("DB_PORT", "5432")),
    "dbname": os.environ.get("DB_NAME",     "marketscanner"),
    "user":   os.environ.get("DB_USER",     "ms"),
    "password": os.environ.get("DB_PASSWORD", "ms"),
}
