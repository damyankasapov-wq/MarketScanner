"""Trade gallery and the HTML page, on synthetic sessions."""
import re
from datetime import date

import pytest

from marketscanner.backtest import gallery, html_report, report
from marketscanner.backtest.__main__ import run
from marketscanner.backtest.engine import AccountConfig, find_exits, run_engine
from marketscanner.backtest.equity import build_equity
from marketscanner.backtest.ideas import generate_ideas
from marketscanner.backtest.metrics import Coverage, summarize
from marketscanner.backtest.strategy import OrbStrategy
from marketscanner.data.bar_store import InMemoryBarStore
from tests.backtest_fixtures import make_session, quiet, stack

D6, D8 = date(2024, 11, 6), date(2024, 11, 8)
CFG = AccountConfig(initial_cash=2500.0, per_deal=1000.0, min_cash=500.0)
TODAY = date(2024, 11, 20)


def _frames():
    return {
        "SPY": stack(make_session("2024-11-06", "UP", {100: (581.2, 587.0, 581.0, 586.0)}), quiet("2024-11-07"), quiet("2024-11-08")),
        "QQQ": stack(make_session("2024-11-06", "UP"), quiet("2024-11-07", {0: (570.0, 571.0, 569.0, 570.0)}), quiet("2024-11-08")),
        "GLD": stack(make_session("2024-11-06", "UP"), quiet("2024-11-07"), quiet("2024-11-08")),
    }


def _run():
    store = InMemoryBarStore()
    for s, f in _frames().items():
        store.upsert_bars(s, f)
    symbols = list(_frames())
    res = generate_ideas(store, OrbStrategy(), symbols, D6, D8)
    trades = run_engine(res.ideas, find_exits(res.ideas, res.frames), res.frames, CFG, symbols)
    eq = build_equity(trades, res.frames, CFG)
    return res, trades, eq, summarize(trades, eq, CFG, "ORB", D6, D8, symbols)


def test_one_chart_per_opened_trade_and_none_for_skipped(tmp_path):
    res, trades, _, _ = _run()
    paths, errors = gallery.write_trade_charts(trades, res.frames, tmp_path, 5)
    assert errors == {} and sorted(paths) == [1, 2]                         # GLD was skipped for cash
    assert paths[1] == "trades/001_SPY_2024-11-06.png" and paths[2] == "trades/002_QQQ_2024-11-06.png"
    for rel in paths.values():
        data = (tmp_path / rel).read_bytes()
        assert data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) > 5_000


def test_a_failing_chart_is_contained_and_reported(tmp_path, monkeypatch):
    res, trades, _, _ = _run()
    real = gallery.render_chart

    def flaky(snap, symbol):
        if symbol == "SPY":
            raise RuntimeError("matplotlib exploded")
        return real(snap, symbol)

    monkeypatch.setattr(gallery, "render_chart", flaky)
    paths, errors = gallery.write_trade_charts(trades, res.frames, tmp_path, 5)
    assert list(paths) == [2] and "matplotlib exploded" in errors[1]


def test_no_trades_no_folder(tmp_path):
    paths, errors = gallery.write_trade_charts([], {}, tmp_path, 5)
    assert paths == {} and errors == {} and not (tmp_path / "trades").exists()


def test_html_has_every_ledger_row_and_the_headline_numbers(tmp_path):
    res, trades, eq, summary = _run()
    paths, errors = gallery.write_trade_charts(trades, res.frames, tmp_path, 5)
    report.save_figure(report.render_equity_figure(eq, trades, summary), tmp_path / "equity.png")
    page = html_report.build_html(summary, trades, (tmp_path / "equity.png").read_bytes(), Coverage(sessions=res.sessions), paths, errors)
    assert page.count("<tr>") == (1 + len(trades)) + (1 + len(summary.by_symbol))  # ledger + by-market table
    assert "SKIPPED" in page and "WIN" in page and "LOSS" in page
    assert "data:image/png;base64," in page and 'src="trades/001_SPY_2024-11-06.png"' in page
    assert f"{summary.final_equity:,.2f}" in page and "Data coverage" in page and "Informational only" in page
    assert "after 1 night(s)" in page and "The chart shows the entry session only." in page
    assert "Only 2 closed trades: below ~100" in page


def test_html_without_a_gallery_has_no_trades_section(tmp_path):
    res, trades, eq, summary = _run()
    page = html_report.build_html(summary, trades, b"\x89PNG", None, {}, {})
    assert "<h2>Trades</h2>" not in page and "<h2>Data coverage</h2>" not in page


def test_html_has_a_by_market_table_best_pnl_first():
    res, trades, eq, summary = _run()
    page = html_report.build_html(summary, trades, b"x", None, {}, {})
    assert "<h2>By market</h2>" in page and "<th>Wins / losses</th>" in page
    table = page.split('<table class="mkt">')[1].split("</table>")[0]
    order = sorted(summary.by_symbol, key=lambda k: -summary.by_symbol[k]["pnl"])
    positions = [table.index(f"<td>{sym}</td>") for sym in order]
    assert positions == sorted(positions)
    for sym, d in summary.by_symbol.items():
        assert f"{d['pnl']:+,.2f}" in table
        assert f"{d['wins']} / {d['losses']}" in table
    assert ("<th>Skipped (cash)</th>" in table) == any(d["skipped"] for d in summary.by_symbol.values())


def test_html_lists_a_failed_chart_and_escapes_text():
    res, trades, eq, summary = _run()
    page = html_report.build_html(summary, trades, b"x", None, {2: "trades/002_QQQ_2024-11-06.png"}, {1: "<script>alert(1)</script>"})
    assert "chart failed: &lt;script&gt;alert(1)&lt;/script&gt;" in page and "<script>alert(1)" not in page


def test_html_open_trade_caption():
    store = InMemoryBarStore()
    store.upsert_bars("SPY", stack(make_session("2024-11-06", "UP"), quiet("2024-11-07")))
    res = generate_ideas(store, OrbStrategy(), ["SPY"], D6, date(2024, 11, 7))
    trades = run_engine(res.ideas, find_exits(res.ideas, res.frames), res.frames, CFG, ["SPY"])
    eq = build_equity(trades, res.frames, CFG)
    summary = summarize(trades, eq, CFG, "ORB", D6, date(2024, 11, 7), ["SPY"])
    assert trades[0].status == "OPEN"
    page = html_report.build_html(summary, trades, b"x", None, {1: "trades/001_SPY_2024-11-06.png"}, {})
    assert "still open at the end of the period" in page and "marked 577.00" in page


def _store():
    s = InMemoryBarStore()
    for sym, f in _frames().items():
        s.upsert_bars(sym, f)
    return s


def test_cli_writes_gallery_and_page(tmp_path):
    args = ["--from", "2024-11-06", "--to", "2024-11-08", "--markets", "SPY,QQQ,GLD", "--cash", "2500",
            "--per-deal", "1000", "--min-cash", "500", "--no-backfill"]
    assert run(args, store=_store(), today=TODAY, out_root=tmp_path) == 0
    out = next(tmp_path.iterdir())
    assert (out / "report.html").exists() and len(list((out / "trades").glob("*.png"))) == 2
    page = (out / "report.html").read_text(encoding="utf-8")
    assert re.search(r'src="trades/\d{3}_SPY_2024-11-06\.png"', page)


def test_cli_gallery_none_skips_the_charts_but_keeps_the_page(tmp_path):
    args = ["--from", "2024-11-06", "--to", "2024-11-08", "--markets", "SPY", "--cash", "2500",
            "--per-deal", "1000", "--no-backfill", "--gallery", "none"]
    assert run(args, store=_store(), today=TODAY, out_root=tmp_path) == 0
    out = next(tmp_path.iterdir())
    assert (out / "report.html").exists() and not (out / "trades").exists()


def test_cli_chart_failures_are_listed_in_the_summary_not_fatal(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(gallery, "render_chart", lambda snap, symbol: (_ for _ in ()).throw(RuntimeError("boom")))
    args = ["--from", "2024-11-06", "--to", "2024-11-08", "--markets", "SPY", "--cash", "2500",
            "--per-deal", "1000", "--no-backfill"]
    assert run(args, store=_store(), today=TODAY, out_root=tmp_path) == 0
    out = next(tmp_path.iterdir())
    assert "CHART FAILURES" in (out / "summary.txt").read_text(encoding="utf-8") and "boom" in (out / "report.html").read_text(encoding="utf-8")
