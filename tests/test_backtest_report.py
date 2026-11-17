"""Ledger CSV, equity chart and summary file: columns, formatting, determinism."""
import csv
from datetime import date

import pytest

from marketscanner.backtest import report
from marketscanner.backtest.engine import AccountConfig, LOSS, OPEN, SKIPPED, WIN, find_exits, run_engine
from marketscanner.backtest.equity import build_equity
from marketscanner.backtest.ideas import generate_ideas
from marketscanner.backtest.metrics import summarize
from marketscanner.backtest.strategy import OrbStrategy
from marketscanner.data.bar_store import InMemoryBarStore
from tests.backtest_fixtures import make_session, quiet, stack

D6, D7 = date(2024, 11, 6), date(2024, 11, 7)
CFG = AccountConfig(initial_cash=2500.0, per_deal=1000.0, min_cash=500.0)


def _run(cfg=CFG):
    frames = {
        "SPY": stack(make_session("2024-11-06", "UP", {100: (581.2, 587.0, 581.0, 586.0)}), quiet("2024-11-07")),
        "QQQ": stack(make_session("2024-11-06", "DOWN"), quiet("2024-11-07", box_high=578.0)),
        "GLD": stack(make_session("2024-11-06", "UP"), quiet("2024-11-07")),
    }
    store = InMemoryBarStore()
    for s, f in frames.items():
        store.upsert_bars(s, f)
    symbols = list(frames)
    res = generate_ideas(store, OrbStrategy(), symbols, D6, D7)
    trades = run_engine(res.ideas, find_exits(res.ideas, res.frames), res.frames, cfg, symbols)
    eq = build_equity(trades, res.frames, cfg)
    return res, trades, eq, summarize(trades, eq, cfg, "ORB", D6, D7, symbols)


def test_time_formatting_is_eastern():
    from tests.orb_fixtures import utc
    assert report.fmt_et(utc(2024, 11, 6, 15, 35)) == "2024-11-06 10:35 ET"
    assert report.fmt_et(utc(2024, 7, 10, 14, 35)) == "2024-07-10 10:35 ET"     # EDT
    assert report.fmt_et(None) == ""


def test_ledger_rows_for_win_open_and_skipped():
    _, trades, _, _ = _run()
    rows = {r["symbol"]: r for r in report.ledger_rows(trades)}
    spy, qqq, gld = rows["SPY"], rows["QQQ"], rows["GLD"]
    assert (spy["status"], spy["side"], spy["exit_kind"]) == (WIN, "BUY", "TP")
    assert spy["amount"] == "1000.00" and spy["shares"] == f"{1000.0 / 581.2:.6f}"
    assert spy["signal_time_et"] == "2024-11-06 10:35 ET" and spy["closed_time_et"] == "2024-11-06 11:10 ET"
    assert spy["exit_price"] == "586.7000" and spy["r_multiple"] == "1.00" and spy["trading_minutes"] == 35
    assert qqq["status"] == OPEN and qqq["closed_time_et"] == "" and qqq["unrealised"] != "" and qqq["mark_price"] != ""
    assert gld["status"] == SKIPPED and gld["skip_reason"] == "cash" and gld["amount"] == "" and gld["pnl"] == ""


def test_csv_header_order_and_row_count(tmp_path):
    _, trades, _, _ = _run()
    path = tmp_path / "ledger.csv"
    report.write_ledger_csv(trades, path)
    with open(path, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert list(rows[0].keys()) == report.LEDGER_COLUMNS and len(rows) == 3
    assert [r["id"] for r in rows] == ["1", "2", "3"]


def test_csv_is_deterministic(tmp_path):
    for name in ("a.csv", "b.csv"):
        report.write_ledger_csv(_run()[1], tmp_path / name)
    assert (tmp_path / "a.csv").read_bytes() == (tmp_path / "b.csv").read_bytes()


def test_equity_figure_renders_and_saves_a_png(tmp_path):
    _, trades, eq, summary = _run()
    fig = report.render_equity_figure(eq, trades, summary)
    path = tmp_path / "equity.png"
    report.save_figure(fig, path)
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) > 5_000
    assert "ORB account" in fig.axes[0].get_title() and "SPY, QQQ, GLD" in fig.axes[0].get_title()


def test_equity_figure_with_no_trades_and_with_no_data(tmp_path):
    store = InMemoryBarStore()
    store.upsert_bars("SPY", make_session("2024-11-06", None))
    res = generate_ideas(store, OrbStrategy(), ["SPY"], D6, D6)
    eq = build_equity([], res.frames, CFG)
    summary = summarize([], eq, CFG, "ORB", D6, D6, ["SPY"])
    report.save_figure(report.render_equity_figure(eq, [], summary), tmp_path / "flat.png")
    empty = build_equity([], {}, CFG)
    fig = report.render_equity_figure(empty, [], summary)
    report.save_figure(fig, tmp_path / "empty.png")
    assert (tmp_path / "flat.png").stat().st_size > 1_000 and (tmp_path / "empty.png").stat().st_size > 1_000


def test_write_text_adds_a_newline(tmp_path):
    report.write_text(tmp_path / "s.txt", "hello")
    assert (tmp_path / "s.txt").read_text(encoding="utf-8") == "hello\n"
