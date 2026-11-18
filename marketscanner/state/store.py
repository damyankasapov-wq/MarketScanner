import re
from datetime import date, datetime, timedelta, timezone

import psycopg2
import psycopg2.pool

from marketscanner import config
from marketscanner.strategies.base import Signal

_TABLE_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


def bars_ddl(table: str = "bars") -> str:
    """
    DDL for the 1-minute bars archive (additive, idempotent). One row per symbol
    per minute, keyed (symbol, ts); prices stored to 4 decimals, which removes the
    float32 noise in the vendor data. `table` is validated because it is
    interpolated (the opt-in integration test uses a throwaway table).
    """
    if not _TABLE_RE.match(table):
        raise ValueError(f"invalid table name: {table!r}")
    return f"""
CREATE TABLE IF NOT EXISTS {table} (
    symbol VARCHAR(20)      NOT NULL,
    ts     TIMESTAMPTZ      NOT NULL,
    open   NUMERIC(12,4)    NOT NULL,
    high   NUMERIC(12,4)    NOT NULL,
    low    NUMERIC(12,4)    NOT NULL,
    close  NUMERIC(12,4)    NOT NULL,
    volume DOUBLE PRECISION NOT NULL DEFAULT 0,
    PRIMARY KEY (symbol, ts)
);
"""


_DDL = """
CREATE TABLE IF NOT EXISTS cooldowns (
    market     VARCHAR(20)  NOT NULL,
    strategy   VARCHAR(50)  NOT NULL,
    expires_at TIMESTAMPTZ  NOT NULL,
    PRIMARY KEY (market, strategy)
);

CREATE TABLE IF NOT EXISTS signals (
    id         SERIAL PRIMARY KEY,
    market     VARCHAR(20)  NOT NULL,
    strategy   VARCHAR(50)  NOT NULL,
    direction  VARCHAR(5)   NOT NULL,
    price      NUMERIC(12,4) NOT NULL,
    fired_at   TIMESTAMPTZ  NOT NULL,
    box_top    NUMERIC(12,4),
    box_bottom NUMERIC(12,4)
);

CREATE INDEX IF NOT EXISTS idx_signals_market_strategy
    ON signals (market, strategy);

-- v0.2.0.0: trade proposal columns (additive, idempotent; PostgreSQL >= 9.6)
ALTER TABLE signals ADD COLUMN IF NOT EXISTS session_date   DATE;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS side           VARCHAR(4);
ALTER TABLE signals ADD COLUMN IF NOT EXISTS entry          NUMERIC(12,4);
ALTER TABLE signals ADD COLUMN IF NOT EXISTS take_profit    NUMERIC(12,4);
ALTER TABLE signals ADD COLUMN IF NOT EXISTS stop_loss      NUMERIC(12,4);
ALTER TABLE signals ADD COLUMN IF NOT EXISTS delta          NUMERIC(12,4);
ALTER TABLE signals ADD COLUMN IF NOT EXISTS confirm_open   TIMESTAMPTZ;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS confirm_close  TIMESTAMPTZ;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS candle_minutes SMALLINT;
-- v0.6.0.0: the settings that made the alert (NULL on older rows = 1:1, no cut-off)
ALTER TABLE signals ADD COLUMN IF NOT EXISTS reward_ratio   NUMERIC(6,3);
ALTER TABLE signals ADD COLUMN IF NOT EXISTS cutoff_et      TIME;
""" + bars_ddl()

_pool: psycopg2.pool.SimpleConnectionPool | None = None


def _get_conn():
    global _pool
    if _pool is None:
        _pool = psycopg2.pool.SimpleConnectionPool(1, 3, connect_timeout=5, **config.DB)
    return _pool.getconn()


def _put_conn(conn) -> None:
    if _pool:
        _pool.putconn(conn)


def init_db() -> None:
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(_DDL)
        conn.commit()
    finally:
        _put_conn(conn)


def is_on_cooldown(market: str, strategy: str) -> bool:
    now = datetime.now(timezone.utc)
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT expires_at FROM cooldowns WHERE market=%s AND strategy=%s",
                (market, strategy),
            )
            row = cur.fetchone()
    finally:
        _put_conn(conn)
    if row is None:
        return False
    return row[0] > now


def set_cooldown(market: str, strategy: str) -> None:
    expires_at = datetime.now(timezone.utc) + timedelta(hours=config.COOLDOWN_HOURS)
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO cooldowns (market, strategy, expires_at)
                VALUES (%s, %s, %s)
                ON CONFLICT (market, strategy) DO UPDATE SET expires_at = EXCLUDED.expires_at
                """,
                (market, strategy, expires_at),
            )
        conn.commit()
    finally:
        _put_conn(conn)


def log_signal(signal: Signal) -> None:
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO signals (
                    market, strategy, direction, price, fired_at, box_top, box_bottom,
                    session_date, side, entry, take_profit, stop_loss, delta,
                    confirm_open, confirm_close, candle_minutes, reward_ratio, cutoff_et
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    signal.market,
                    signal.strategy,
                    signal.direction,
                    signal.price,
                    signal.fired_at,
                    signal.box_top,
                    signal.box_bottom,
                    signal.session_date,
                    signal.side,
                    signal.entry,
                    signal.take_profit,
                    signal.stop_loss,
                    signal.delta,
                    signal.confirm_open,
                    signal.confirm_close,
                    signal.candle_minutes,
                    signal.reward_ratio,
                    signal.cutoff_et,
                ),
            )
        conn.commit()
    finally:
        _put_conn(conn)


def signal_exists(market: str, strategy: str, session_date: date) -> bool:
    """
    True if a signal for this market/strategy was already logged for the ET
    session date. Second guard behind the in-memory latch and the 4-hour
    cooldown: a restart after the cooldown expired must not email the same
    session twice. Plain equality on the session_date column — no timezone
    arithmetic in SQL.
    """
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM signals WHERE market=%s AND strategy=%s AND session_date=%s LIMIT 1",
                (market, strategy, session_date),
            )
            return cur.fetchone() is not None
    finally:
        _put_conn(conn)
