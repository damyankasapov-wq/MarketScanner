"""
Opt-in: runs the real SQL of PostgresBarStore against YOUR Postgres.

    MS_TEST_DB=1 pytest tests/integration -q

Skipped unless MS_TEST_DB is set. Uses a throwaway table (bars_test_<pid>) that
is dropped afterwards; it never touches the real `bars` or `signals` tables.
Connection settings come from the usual DB_HOST / DB_PORT / DB_NAME / DB_USER /
DB_PASSWORD environment (see .env.example).
"""
import os
from datetime import date

import pandas as pd
import pytest

from marketscanner.data.pg_bar_store import PostgresBarStore
from marketscanner.state import store as state_store
from tests.orb_fixtures import bar, df_of, flat

pytestmark = pytest.mark.skipif(not os.environ.get("MS_TEST_DB"), reason="set MS_TEST_DB=1 to run against Postgres")

D1, D2 = date(2024, 11, 6), date(2024, 11, 7)


@pytest.fixture
def pg():
    table = f"bars_test_{os.getpid()}"
    store = PostgresBarStore(table=table)
    store.ensure_table()
    try:
        yield store
    finally:
        conn = state_store._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(f"DROP TABLE IF EXISTS {table}")
            conn.commit()
        finally:
            state_store._put_conn(conn)


def test_roundtrip_upsert_load_counts_last(pg):
    frame = pd.concat([df_of(flat("2024-11-06 14:30", 5, 758.7000122070312)), df_of(flat("2024-11-07 14:30", 3, 760.0))])
    assert pg.upsert_bars("SPY", frame) == 8
    got = pg.load("SPY", D1, D2)
    assert len(got) == 8 and got.iloc[0]["close"] == 758.7 and str(got.index.tz) == "UTC"
    assert pg.session_counts("SPY", D1, D2) == {D1: 5, D2: 3}
    assert pg.last_session("SPY") == D2
    assert pg.load("QQQ", D1, D2).empty and pg.last_session("QQQ") is None


def test_upsert_is_idempotent_and_latest_wins(pg):
    pg.upsert_bars("SPY", df_of(flat("2024-11-06 14:30", 5, 100.0)))
    pg.upsert_bars("SPY", df_of(flat("2024-11-06 14:30", 5, 101.0)))
    got = pg.load("SPY", D1, D1)
    assert len(got) == 5 and (got["close"] == 101.0).all()


def test_after_midnight_utc_bar_is_the_earlier_et_session(pg):
    pg.upsert_bars("SPY", df_of([bar("2024-11-07 00:30", 1, 1, 1, 1)]))
    assert pg.session_counts("SPY", D1, D2) == {D1: 1}
    assert len(pg.load("SPY", D1, D1)) == 1 and pg.load("SPY", D2, D2).empty


def test_replace_session_owns_the_day(pg):
    pg.upsert_bars("SPY", pd.concat([df_of(flat("2024-11-06 16:00", 5, 1.0)), df_of(flat("2024-11-07 14:30", 3, 1.0))]))
    assert pg.replace_session("SPY", D1, df_of(flat("2024-11-06 14:30", 4, 2.0))) == 4
    assert pg.session_counts("SPY", D1, D2) == {D1: 4, D2: 3}
    assert set(pg.load("SPY", D1, D1)["close"]) == {2.0}
