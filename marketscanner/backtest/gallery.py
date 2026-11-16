"""
Trade gallery: one chart per opened trade, drawn with the same render_chart the
alert email and dashboard use, for the trade's ENTRY session (box, levels, zones,
5-minute candles). A trade that was carried to a later day is not drawn on the
later day's chart; its exit is in the report caption.

One chart failing never aborts the run: the failure is returned and listed.
"""
from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd
import pytz

from marketscanner import config
from marketscanner.backtest.engine import SKIPPED, Trade
from marketscanner.strategies.orb_session import replay_now, snapshot
from marketscanner.ui.chart import render_chart

log = logging.getLogger(__name__)
_ET = pytz.timezone(config.TIMEZONE)
TRADES_DIR = "trades"


def trade_chart_name(t: Trade) -> str:
    return f"{t.id:03d}_{t.symbol}_{t.session_date.isoformat()}.png"


def session_frame(frame: pd.DataFrame, session_date) -> pd.DataFrame:
    if frame.empty:
        return frame
    return frame[pd.Index(frame.index.tz_convert(_ET).date) == session_date]


def write_trade_charts(
    trades: list[Trade],
    frames: dict[str, pd.DataFrame],
    out_dir: Path,
    candle_minutes: int,
) -> tuple[dict[int, str], dict[int, str]]:
    """Returns ({trade id: relative png path}, {trade id: error text})."""
    folder = Path(out_dir) / TRADES_DIR
    paths: dict[int, str] = {}
    errors: dict[int, str] = {}
    for t in trades:
        if t.status == SKIPPED:
            continue
        try:
            folder.mkdir(parents=True, exist_ok=True)
            df_s = session_frame(frames[t.symbol], t.session_date)
            snap = snapshot(df_s, minutes=candle_minutes, now=replay_now(t.session_date))
            fig = render_chart(snap, t.symbol)
            name = trade_chart_name(t)
            fig.savefig(folder / name, format="png", dpi=90, bbox_inches="tight")
            paths[t.id] = f"{TRADES_DIR}/{name}"
        except Exception as exc:  # noqa: BLE001 - one bad chart must not stop the run
            errors[t.id] = repr(exc)
            log.warning("trade chart %s failed: %r", t.id, exc)
    return paths, errors
