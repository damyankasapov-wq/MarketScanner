"""
BarStore: InMemoryBarStore semantics, and PostgresBarStore's SQL through a fake
cursor (the real-database run is tests/integration/test_bar_store_pg.py, opt-in).

Reference: 2024-11-06 and 2024-11-07 (EST). 14:30 UTC = 09:30 ET. A bar at
2024-11-07 00:30 UTC is 19:30 ET on 2024-11-06, still the 11-06 session.
"""
from datetime import date
from decimal import Decimal

import pandas as pd
import pytest

from marketscanner import config
from marketscanner.data.bar_store import (
    COLUMNS, InMemoryBarStore, clean_bars, et_date_bounds_utc, et_dates,
)
from marketscanner.data.pg_bar_store import PostgresBarStore
from marketscanner.state import store as state_store
from tests.orb_fixtures import bar, df_of, flat, utc

D1, D2 = date(2024, 11, 6), date(2024, 11, 7)


def _session(d_utc_start, n=5, price=100.0):
    return df_of(flat(d_utc_start, n, price))


class TestCleanBars:
    def test_rounds_prices_to_four_decimals(self):
        out = clean_bars(df_of([bar("2024-11-06 14:30", 758.7000122070312, 758.9000244140625, 758.5, 758.7000122070312)]))
        assert out.iloc[0]["open"] == 758.7 and out.iloc[0]["high"] == 758.9

    def test_drops_rows_with_missing_price_and_fills_volume(self):
        raw = df_of([bar("2024-11-06 14:30", 1, 1, 1, 1), bar("2024-11-06 14:31", 1, 1, 1, 1)])
        raw.iloc[0, raw.columns.get_loc("close")] = float("nan")
        raw.iloc[1, raw.columns.get_loc("volume")] = float("nan")
        out = clean_bars(raw)
        assert len(out) == 1 and out.iloc[0]["volume"] == 0.0

    def test_naive_index_is_utc_sorted_and_deduplicated_last_wins(self):
        raw = pd.DataFrame(
            {"open": [1.0, 2.0, 3.0], "high": [1, 2, 3], "low": [1, 2, 3], "close": [1.0, 2.0, 3.0], "volume": [1, 1, 1]},
            index=pd.DatetimeIndex(["2024-11-06 14:31", "2024-11-06 14:30", "2024-11-06 14:31"]),
        )
        out = clean_bars(raw)
        assert str(out.index.tz) == "UTC" and list(out["close"]) == [2.0, 3.0]
        assert out.index.is_monotonic_increasing

    def test_empty_and_none(self):
        assert clean_bars(None).empty and clean_bars(pd.DataFrame()).empty
        assert list(clean_bars(None).columns) == COLUMNS


class TestEtDates:
    def test_et_date_not_utc_date(self):
        idx = pd.DatetimeIndex(["2024-11-07 00:30", "2024-11-07 14:30"], tz="UTC")
        assert et_dates(idx) == [D1, D2]

    def test_bounds_follow_dst(self):
        lo, hi = et_date_bounds_utc(date(2024, 11, 6), date(2024, 11, 6))
        assert (lo, hi) == (pd.Timestamp("2024-11-06 05:00", tz="UTC"), pd.Timestamp("2024-11-07 05:00", tz="UTC"))
        lo, hi = et_date_bounds_utc(date(2024, 7, 10), date(2024, 7, 10))     # EDT
        assert lo == pd.Timestamp("2024-07-10 04:00", tz="UTC")


class TestInMemory:
    def test_upsert_counts_and_empty(self):
        s = InMemoryBarStore()
        assert s.upsert_bars("SPY", pd.DataFrame()) == 0
        assert s.upsert_bars("SPY", _session("2024-11-06 14:30", 5)) == 5

    def test_idempotent(self):
        s = InMemoryBarStore()
        f = _session("2024-11-06 14:30", 5)
        s.upsert_bars("SPY", f)
        s.upsert_bars("SPY", f)
        assert len(s.load("SPY", D1, D1)) == 5

    def test_later_row_wins_and_partial_becomes_full(self):
        s = InMemoryBarStore()
        s.upsert_bars("SPY", _session("2024-11-06 14:30", 5, 100.0))
        s.upsert_bars("SPY", _session("2024-11-06 14:30", 390, 101.0))
        got = s.load("SPY", D1, D1)
        assert len(got) == 390 and (got["close"] == 101.0).all()

    def test_symbols_are_separate(self):
        s = InMemoryBarStore()
        s.upsert_bars("SPY", _session("2024-11-06 14:30", 3))
        assert s.load("QQQ", D1, D1).empty

    def test_load_range_inclusive_exclusive(self):
        s = InMemoryBarStore()
        s.upsert_bars("SPY", pd.concat([_session("2024-11-06 14:30", 3), _session("2024-11-07 14:30", 4)]))
        assert len(s.load("SPY", D1, D1)) == 3
        assert len(s.load("SPY", D2, D2)) == 4
        assert len(s.load("SPY", D1, D2)) == 7
        assert s.load("SPY", date(2024, 11, 8), date(2024, 11, 9)).empty
        assert list(s.load("SPY", date(2024, 11, 8), date(2024, 11, 9)).columns) == COLUMNS

    def test_after_midnight_utc_bar_belongs_to_the_earlier_et_session(self):
        s = InMemoryBarStore()
        s.upsert_bars("SPY", df_of([bar("2024-11-07 00:30", 1, 1, 1, 1)]))
        assert len(s.load("SPY", D1, D1)) == 1 and s.load("SPY", D2, D2).empty
        assert s.session_counts("SPY", D1, D2) == {D1: 1}
        assert s.last_session("SPY") == D1

    def test_session_counts_and_last_session(self):
        s = InMemoryBarStore()
        assert s.last_session("SPY") is None and s.session_counts("SPY", D1, D2) == {}
        s.upsert_bars("SPY", pd.concat([_session("2024-11-06 14:30", 3), _session("2024-11-07 14:30", 4)]))
        assert s.session_counts("SPY", D1, D2) == {D1: 3, D2: 4}
        assert s.last_session("SPY") == D2


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def execute(self, sql, params=None):
        self.conn.executed.append((" ".join(sql.split()), params))
        if self.conn.fail:
            raise RuntimeError("boom")
    def fetchall(self): return self.conn.rows
    def fetchone(self): return self.conn.rows[0] if self.conn.rows else None


class FakeConn:
    def __init__(self, rows=None, fail=False):
        self.rows, self.fail = rows or [], fail
        self.executed, self.commits, self.rollbacks, self.puts = [], 0, 0, 0
    def cursor(self): return FakeCursor(self)
    def commit(self): self.commits += 1
    def rollback(self): self.rollbacks += 1


def _pg(conn):
    return PostgresBarStore(get_conn=lambda: conn, put_conn=lambda c: setattr(c, "puts", c.puts + 1))


class TestPostgresSql:
    def test_rejects_bad_table_name(self):
        with pytest.raises(ValueError):
            PostgresBarStore(table="bars; DROP TABLE signals")
        with pytest.raises(ValueError):
            state_store.bars_ddl("Bars")

    def test_ddl_is_part_of_init(self):
        assert "CREATE TABLE IF NOT EXISTS bars" in state_store._DDL
        assert "PRIMARY KEY (symbol, ts)" in state_store.bars_ddl()
        assert "ALTER TABLE" not in state_store.bars_ddl()      # additive create only

    def test_upsert_is_one_parameterised_insert_with_on_conflict(self):
        conn = FakeConn()
        n = _pg(conn).upsert_bars("SPY", _session("2024-11-06 14:30", 3, 758.7000122070312))
        assert n == 3 and len(conn.executed) == 1
        sql, params = conn.executed[0]
        assert sql.startswith("INSERT INTO bars (symbol, ts, open, high, low, close, volume) VALUES")
        assert "ON CONFLICT (symbol, ts) DO UPDATE SET" in sql
        assert sql.count("(%s,%s,%s,%s,%s,%s,%s)") == 3 and len(params) == 21
        assert params[0] == "SPY" and params[2] == 758.7             # rounded to 4 dp
        assert conn.commits == 1 and conn.puts == 1

    def test_upsert_chunks_rows(self):
        conn = FakeConn()
        _pg(conn).upsert_bars("SPY", _session("2024-11-06 14:30", 1200))
        assert [len(p) // 7 for _, p in conn.executed] == [500, 500, 200]

    def test_upsert_empty_runs_no_sql(self):
        conn = FakeConn()
        assert _pg(conn).upsert_bars("SPY", pd.DataFrame()) == 0
        assert conn.executed == [] and conn.puts == 0

    def test_load_uses_utc_bounds_and_returns_floats(self):
        ts = pd.Timestamp("2024-11-06 14:30", tz="UTC").to_pydatetime()
        conn = FakeConn(rows=[(ts, Decimal("100.5000"), Decimal("101"), Decimal("99.5"), Decimal("100.25"), 1234.0)])
        got = _pg(conn).load("SPY", D1, D2)
        sql, params = conn.executed[0]
        assert "WHERE symbol = %s AND ts >= %s AND ts < %s ORDER BY ts" in sql
        assert params == ("SPY", pd.Timestamp("2024-11-06 05:00", tz="UTC").to_pydatetime(),
                          pd.Timestamp("2024-11-08 05:00", tz="UTC").to_pydatetime())
        assert got.iloc[0]["close"] == 100.25 and got.dtypes.eq("float64").all()
        assert str(got.index.tz) == "UTC" and conn.puts == 1

    def test_load_empty(self):
        got = _pg(FakeConn(rows=[])).load("SPY", D1, D1)
        assert got.empty and list(got.columns) == COLUMNS

    def test_session_counts_and_last_session(self):
        conn = FakeConn(rows=[(D1, 3), (D2, 4)])
        assert _pg(conn).session_counts("SPY", D1, D2) == {D1: 3, D2: 4}
        sql, params = conn.executed[0]
        assert "AT TIME ZONE %s" in sql and params[0] == config.TIMEZONE and params[1] == "SPY"
        assert _pg(FakeConn(rows=[(D2,)])).last_session("SPY") == D2
        assert _pg(FakeConn(rows=[(None,)])).last_session("SPY") is None
        assert _pg(FakeConn(rows=[])).last_session("SPY") is None

    def test_error_rolls_back_propagates_and_returns_the_connection(self):
        conn = FakeConn(fail=True)
        with pytest.raises(RuntimeError, match="boom"):
            _pg(conn).upsert_bars("SPY", _session("2024-11-06 14:30", 2))
        assert conn.rollbacks == 1 and conn.commits == 0 and conn.puts == 1
