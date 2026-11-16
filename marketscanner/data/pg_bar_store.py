"""
PostgresBarStore: the bars archive in the existing Postgres (table `bars`, created
by state.store.init_db). All SQL is parameterised; only the table name is
interpolated and it is validated by bars_ddl().

Connections come from the shared pool in marketscanner.state.store, which now has
a 5 second connect timeout so a dead database fails fast instead of hanging.
"""
from __future__ import annotations

from datetime import date
from typing import Callable, Optional

import pandas as pd

from marketscanner import config
from marketscanner.data.bar_store import COLUMNS, clean_bars, empty_bars, et_date_bounds_utc
from marketscanner.state import store as _store

_CHUNK_ROWS = 500   # rows per INSERT statement (7 params each)


class PostgresBarStore:
    def __init__(
        self,
        table: str = "bars",
        get_conn: Optional[Callable] = None,
        put_conn: Optional[Callable] = None,
    ) -> None:
        _store.bars_ddl(table)          # validates the name
        self.table = table
        self._get = get_conn or _store._get_conn
        self._put = put_conn or _store._put_conn

    def ensure_table(self) -> None:
        self._run(lambda cur: cur.execute(_store.bars_ddl(self.table)), commit=True)

    def upsert_bars(self, symbol: str, df: pd.DataFrame) -> int:
        clean = clean_bars(df)
        if clean.empty:
            return 0
        rows = [
            (symbol, ts.to_pydatetime(), r.open, r.high, r.low, r.close, r.volume)
            for ts, r in zip(clean.index, clean.itertuples(index=False))
        ]

        def write(cur):
            for i in range(0, len(rows), _CHUNK_ROWS):
                chunk = rows[i:i + _CHUNK_ROWS]
                values = ",".join(["(%s,%s,%s,%s,%s,%s,%s)"] * len(chunk))
                params = [v for row in chunk for v in row]
                cur.execute(
                    f"INSERT INTO {self.table} (symbol, ts, open, high, low, close, volume) "
                    f"VALUES {values} "
                    "ON CONFLICT (symbol, ts) DO UPDATE SET "
                    "open=EXCLUDED.open, high=EXCLUDED.high, low=EXCLUDED.low, "
                    "close=EXCLUDED.close, volume=EXCLUDED.volume",
                    params,
                )

        self._run(write, commit=True)
        return len(rows)

    def load(self, symbol: str, start: date, end: date) -> pd.DataFrame:
        lo, hi = et_date_bounds_utc(start, end)

        def read(cur):
            cur.execute(
                f"SELECT ts, open, high, low, close, volume FROM {self.table} "
                "WHERE symbol = %s AND ts >= %s AND ts < %s ORDER BY ts",
                (symbol, lo.to_pydatetime(), hi.to_pydatetime()),
            )
            return cur.fetchall()

        rows = self._run(read)
        if not rows:
            return empty_bars()
        df = pd.DataFrame(rows, columns=["ts"] + COLUMNS)
        df.index = pd.DatetimeIndex(pd.to_datetime(df.pop("ts"), utc=True))
        return df.astype(float)

    def session_counts(self, symbol: str, start: date, end: date) -> dict[date, int]:
        lo, hi = et_date_bounds_utc(start, end)

        def read(cur):
            cur.execute(
                f"SELECT (ts AT TIME ZONE %s)::date AS d, count(*) FROM {self.table} "
                "WHERE symbol = %s AND ts >= %s AND ts < %s GROUP BY d",
                (config.TIMEZONE, symbol, lo.to_pydatetime(), hi.to_pydatetime()),
            )
            return cur.fetchall()

        return {d: int(n) for d, n in self._run(read)}

    def last_session(self, symbol: str) -> Optional[date]:
        def read(cur):
            cur.execute(
                f"SELECT max((ts AT TIME ZONE %s)::date) FROM {self.table} WHERE symbol = %s",
                (config.TIMEZONE, symbol),
            )
            return cur.fetchone()

        row = self._run(read)
        return row[0] if row and row[0] is not None else None

    def _run(self, fn, commit: bool = False):
        conn = self._get()
        try:
            with conn.cursor() as cur:
                result = fn(cur)
            if commit:
                conn.commit()
            return result
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        finally:
            self._put(conn)
