from datetime import datetime, timedelta, timezone

import psycopg2
import psycopg2.pool

from marketscanner import config
from marketscanner.strategies.base import Signal

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
"""

_pool: psycopg2.pool.SimpleConnectionPool | None = None


def _get_conn():
    global _pool
    if _pool is None:
        _pool = psycopg2.pool.SimpleConnectionPool(1, 3, **config.DB)
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
                INSERT INTO signals (market, strategy, direction, price, fired_at, box_top, box_bottom)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    signal.market,
                    signal.strategy,
                    signal.direction,
                    signal.price,
                    signal.fired_at,
                    signal.box_top,
                    signal.box_bottom,
                ),
            )
        conn.commit()
    finally:
        _put_conn(conn)
