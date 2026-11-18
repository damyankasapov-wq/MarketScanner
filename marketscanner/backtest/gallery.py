"""
Trade gallery: one chart per opened trade, drawn with the same render_chart the
alert email and dashboard use, for the trade's ENTRY session (box, levels, zones,
5-minute candles). A trade that was carried to a later day is not drawn on the
later day's chart; its exit is in the report caption.

One chart failing never aborts the run: the failure is returned and listed.
"""
from __future__ import annotations

import dataclasses
import logging
from pathlib import Path
from typing import Optional

import pandas as pd

from marketscanner.backtest.engine import SKIPPED, Trade
from marketscanner.data.sessions import session_bars, split_by_et_date
from marketscanner.strategies.orb_session import OrbSnapshot, replay_now, session_outcome, snapshot
from marketscanner.ui.chart import render_chart

log = logging.getLogger(__name__)
TRADES_DIR = "trades"


def trade_chart_name(t: Trade) -> str:
    return f"{t.id:03d}_{t.symbol}_{t.session_date.isoformat()}.png"


def session_frame(frame: pd.DataFrame, session_date) -> pd.DataFrame:
    """One session's bars (empty frame if the date has none)."""
    return session_bars(frame, session_date)


def _trade_session(split: dict[str, dict], frames: dict[str, pd.DataFrame], t: Trade) -> pd.DataFrame:
    """The trade's entry-session bars from the pre-split sessions, splitting a symbol once if missing."""
    if t.symbol not in split:
        split[t.symbol] = split_by_et_date(frames[t.symbol])
    return split[t.symbol].get(t.session_date, frames[t.symbol].iloc[0:0])


def with_trade_levels(snap: OrbSnapshot, t: Trade, df_session: pd.DataFrame) -> OrbSnapshot:
    """
    The snapshot as the trade saw it. snapshot() proposes the live 1:1 levels; a
    trade from a different reward ratio gets its own target, and the in-session
    outcome is recomputed for those levels (a target hit is worth +reward R).
    Unchanged when the levels already match.
    """
    p = snap.proposal
    if p is None or snap.confirmation is None or (p.take_profit == t.take_profit and p.stop_loss == t.stop_loss):
        return snap
    prop = dataclasses.replace(p, take_profit=t.take_profit, stop_loss=t.stop_loss)
    outcome = session_outcome(df_session, snap.confirmation, prop, snap.candle_minutes)
    if outcome.status == "TP_HIT" and prop.delta:
        outcome = dataclasses.replace(outcome, r_multiple=abs(prop.take_profit - prop.entry) / prop.delta)
    return dataclasses.replace(snap, proposal=prop, outcome=outcome)


def write_trade_charts(
    trades: list[Trade],
    frames: dict[str, pd.DataFrame],
    out_dir: Path,
    candle_minutes: int,
    sessions: Optional[dict[str, dict]] = None,
) -> tuple[dict[int, str], dict[int, str]]:
    """
    Returns ({trade id: relative png path}, {trade id: error text}).
    `sessions` ({symbol: {date: bars}}, e.g. Frames.sessions) skips re-splitting;
    without it each symbol's frame is split once here.
    """
    folder = Path(out_dir) / TRADES_DIR
    paths: dict[int, str] = {}
    errors: dict[int, str] = {}
    split: dict[str, dict] = dict(sessions or {})
    for t in trades:
        if t.status == SKIPPED:
            continue
        try:
            folder.mkdir(parents=True, exist_ok=True)
            df_s = _trade_session(split, frames, t)
            snap = with_trade_levels(snapshot(df_s, minutes=candle_minutes, now=replay_now(t.session_date)), t, df_s)
            fig = render_chart(snap, t.symbol)
            name = trade_chart_name(t)
            fig.savefig(folder / name, format="png", dpi=90, bbox_inches="tight")
            paths[t.id] = f"{TRADES_DIR}/{name}"
        except Exception as exc:  # noqa: BLE001 - one bad chart must not stop the run
            errors[t.id] = repr(exc)
            log.warning("trade chart %s failed: %r", t.id, exc)
    return paths, errors
