"""
python -m marketscanner.backtest end to end on an in-memory store: argument
validation, store failures, the run folder, determinism, backfill and the
reconciliation of ledger and summary.
"""
import csv
import re
from datetime import date

import pytest

from marketscanner.backtest.__main__ import run, run_id
from marketscanner.data.bar_store import InMemoryBarStore
from tests.backtest_fixtures import make_session, quiet, stack

TODAY = date(2024, 11, 20)
BASE = ["--from", "2024-11-06", "--to", "2024-11-08", "--markets", "SPY,QQQ,GLD",
        "--cash", "10000", "--per-deal", "1000", "--min-cash", "2000"]


def _frames():
    return {
        "SPY": stack(make_session("2024-11-06", "UP", {100: (581.2, 587.0, 581.0, 586.0)}), quiet("2024-11-07"), quiet("2024-11-08")),
        "QQQ": stack(make_session("2024-11-06", "UP"), quiet("2024-11-07", {0: (570.0, 571.0, 569.0, 570.0)}), quiet("2024-11-08")),
        "GLD": stack(make_session("2024-11-06", "DOWN"), quiet("2024-11-07", box_high=578.0), quiet("2024-11-08", {389: (577.0, 578.0, 577.0, 578.0)}, box_high=578.0)),
    }


def _store():
    s = InMemoryBarStore()
    for sym, f in _frames().items():
        s.upsert_bars(sym, f)
    return s


def _go(tmp_path, extra=(), store=None, **kw):
    return run(BASE + list(extra) + ["--no-backfill", "--gallery", "none"], store=store or _store(),
               today=TODAY, out_root=tmp_path, **kw)


def test_full_run_writes_the_run_folder_and_prints_the_summary(tmp_path, capsys):
    assert _go(tmp_path) == 0
    out = tmp_path / run_id("ORB", date(2024, 11, 6), date(2024, 11, 8), ["SPY", "QQQ", "GLD"])
    assert out.name == "ORB_20241106-20241108_SPY-QQQ-GLD"
    assert {p.name for p in out.iterdir()} >= {"ledger.csv", "summary.txt", "equity.png"}
    text = capsys.readouterr().out
    assert "STRATEGY CHECK: ORB" in text and "Final equity" in text and "DATA COVERAGE" in text and f"Wrote {out}" in text
    assert (out / "summary.txt").read_text(encoding="utf-8").strip() in text


def test_ledger_and_summary_reconcile(tmp_path):
    _go(tmp_path)
    out = next(tmp_path.iterdir())
    with open(out / "ledger.csv", newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert [r["status"] for r in rows] == ["WIN", "LOSS", "OPEN"]               # SPY TP, QQQ gap stop, GLD still open
    pnl = sum(float(r["pnl"]) for r in rows if r["pnl"]) + sum(float(r["unrealised"]) for r in rows if r["unrealised"])
    summary = (out / "summary.txt").read_text(encoding="utf-8")
    net = float(re.search(r"Profit / loss\s+(-?[\d,]+\.\d\d)", summary).group(1).replace(",", ""))
    assert net == pytest.approx(pnl, abs=0.02)
    assert float(re.search(r"Final equity\s+([\d,]+\.\d\d)", summary).group(1).replace(",", "")) == pytest.approx(10_000 + pnl, abs=0.02)


def test_rerun_is_deterministic(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    assert _go(a) == 0 and _go(b) == 0
    fa, fb = next(a.iterdir()), next(b.iterdir())
    for name in ("ledger.csv", "summary.txt", "equity.png"):
        assert (fa / name).read_bytes() == (fb / name).read_bytes(), name


def test_to_defaults_to_yesterday(tmp_path):
    args = ["--from", "2024-11-18", "--markets", "SPY", "--cash", "10000", "--per-deal", "1000", "--no-backfill", "--gallery", "none"]
    assert run(args, store=_store(), today=TODAY, out_root=tmp_path) == 0
    assert next(tmp_path.iterdir()).name == "ORB_20241118-20241119_SPY"


def test_backfill_fetches_missing_sessions_from_the_fetcher(tmp_path):
    calls = []

    def fetch(symbol, start, end):
        calls.append((symbol, start))
        return _frames()[symbol][_frames()[symbol].index.tz_convert("America/New_York").date == start]

    store = InMemoryBarStore()
    assert run(BASE + ["--gallery", "none"], store=store, fetch=fetch, today=TODAY, out_root=tmp_path) == 0
    assert len(calls) == 9 and store.session_counts("SPY", date(2024, 11, 6), date(2024, 11, 8))
    out = next(tmp_path.iterdir())
    assert "fetched from yfinance this run: 9" in (out / "summary.txt").read_text(encoding="utf-8")


def test_money_warning_when_no_deal_can_ever_open(tmp_path, capsys):
    args = ["--from", "2024-11-06", "--to", "2024-11-06", "--markets", "SPY", "--cash", "2500", "--per-deal", "1000",
            "--min-cash", "2000", "--no-backfill", "--gallery", "none"]
    assert run(args, store=_store(), today=TODAY, out_root=tmp_path) == 0
    captured = capsys.readouterr()
    assert "no deal can ever open" in captured.err
    assert "Signals 1: opened 0, skipped 1" in captured.out


@pytest.mark.parametrize("bad", [
    ["--from", "2024-11-09", "--to", "2024-11-08"],                 # from after to
    ["--from", "2024-13-40"],                                       # bad date
    ["--from", "2024-11-20"],                                       # today's session is incomplete
    ["--cash", "0"], ["--cash", "-5"], ["--per-deal", "0"],
    ["--min-cash", "-1"], ["--min-cash", "10000"],                  # min cash must be below cash
    ["--strategy", "nope"], ["--markets", ""], ["--markets", "SPY,SPY"],
])
def test_bad_arguments_exit_2(tmp_path, bad, capsys):
    argv = BASE + ["--no-backfill"]
    for i in range(0, len(bad), 2):
        key, value = bad[i], bad[i + 1]
        if key in argv:
            argv[argv.index(key) + 1] = value
        else:
            argv += [key, value]
    with pytest.raises(SystemExit) as exc:
        run(argv, store=_store(), today=TODAY, out_root=tmp_path)
    assert exc.value.code == 2 and capsys.readouterr().err


def test_missing_required_arguments_exit_2(tmp_path):
    with pytest.raises(SystemExit) as exc:
        run(["--from", "2024-11-06"], store=_store(), today=TODAY, out_root=tmp_path)
    assert exc.value.code == 2


def test_unreachable_store_is_a_clear_exit_2_not_a_traceback(tmp_path, capsys):
    class DeadStore:
        def session_counts(self, *a): raise ConnectionError("no route to host")
        def load(self, *a): raise ConnectionError("no route to host")

    assert run(BASE + ["--gallery", "none"], store=DeadStore(), today=TODAY, out_root=tmp_path) == 2
    err = capsys.readouterr().err
    assert "Cannot use the bars store" in err and "no route to host" in err and "DB_HOST" in err
    assert run(BASE + ["--no-backfill", "--gallery", "none"], store=DeadStore(), today=TODAY, out_root=tmp_path) == 2
    assert list(tmp_path.iterdir()) == []


def test_period_with_no_stored_data_still_reports(tmp_path, capsys):
    assert run(BASE + ["--no-backfill", "--gallery", "none"], store=InMemoryBarStore(), today=TODAY, out_root=tmp_path) == 0
    text = capsys.readouterr().out
    assert "Closed trades       0" in text and "no data 2024-11-06" in text and "Only 0 closed trades" in text
