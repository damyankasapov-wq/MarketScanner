"""
Sweep paths the /ship coverage audit found untested: a passing verdict, the
top-10 cut with the baseline appended, a baseline-only sweep's rerun command,
CLI guards, and the gallery's no-proposal shortcut.
"""
import csv
import re
from datetime import date

import pytest

from marketscanner.backtest import gallery, sweep_report
from marketscanner.backtest.__main__ import run, run_id
from marketscanner.backtest.strategy import OrbSettings
from marketscanner.backtest.sweep import HalfStats, Row, SweepResult
from marketscanner.strategies.orb_session import replay_now, snapshot
from tests.backtest_fixtures import quiet
from tests.test_backtest_sweep import BASE, CFG, TODAY, _store


def _r(settings, d_is, d_oos, se_oos, rank, baseline=False):
    return Row(settings=settings, is_baseline=baseline, is_=HalfStats(closed=150), oos=HalfStats(closed=150),
               d_is=d_is, se_d_is=1.0, d_oos=d_oos, se_d_oos=se_oos, rank_is=rank)


def _result(rows):
    return SweepResult(rows=rows, baseline=OrbSettings(5), is_dates=[date(2024, 1, 2)], oos_dates=[date(2024, 2, 1)],
                       sessions=[], symbols=["SPY"], start=date(2024, 1, 1), end=date(2024, 3, 1), cfg=CFG)


def test_a_passing_variant_is_named_in_the_verdict_marks_and_csv(tmp_path):
    win = _r(OrbSettings(10, 1.5), 3.0, 9.0, 2.0, 1)
    base = _r(OrbSettings(5), 0.0, 0.0, 0.0, 2, baseline=True)
    res = _result([win, base])
    assert res.winners == [win] and res.best is win
    assert "1 variant(s) beat current .env" in res.verdict() and win.settings.short() in res.verdict()
    assert "passes rule" in sweep_report.mark(win, res) and "in-sample best" in sweep_report.mark(win, res)
    path = tmp_path / "sweep.csv"
    sweep_report.write_csv(res, path)
    rows = list(csv.DictReader(open(path, encoding="utf-8")))
    assert rows[0]["passes_rule"] == "True" and rows[1]["passes_rule"] == "False"


def test_more_than_ten_rows_shows_the_top_ten_plus_the_baseline():
    rows = [_r(OrbSettings(5, 1.0 + k / 10), 20.0 - k, -1.0, 1.0, k + 1) for k in range(11)]
    rows.append(_r(OrbSettings(5), 0.0, 0.0, 0.0, 12, baseline=True))
    res = _result(rows)
    shown = sweep_report.shown_rows(res)
    assert len(shown) == sweep_report.TOP_ROWS + 1 and shown[-1].is_baseline
    assert "(top 10 by IS dR plus current .env; all 12 rows in sweep.csv / sweep.html)" in sweep_report.format_text(res)


def test_a_baseline_only_sweep_reruns_the_baseline_and_notes_ignored_gallery(tmp_path, capsys):
    argv = BASE + ["--sweep", "--candle", "5", "--reward", "1", "--cutoff", "none", "--gallery", "all"]
    assert run(argv, store=_store(), today=TODAY, out_root=tmp_path) == 0
    captured = capsys.readouterr()
    assert "--gallery is ignored with --sweep" in captured.err
    cmd = re.search(r"python -m marketscanner\.backtest (.+)", captured.out).group(1)
    assert cmd.endswith("--candle 5 --reward 1 --cutoff none")
    out = tmp_path / (f"{run_id('ORB', date(2024, 11, 6), date(2024, 11, 8), ['SPY', 'QQQ'])}"
                      "_sweep_c5_r1_xnone")
    assert (out / "sweep.txt").exists() and not (out / "trades").exists()


@pytest.mark.parametrize("extra, msg", [
    (["--candle", "5,5"], "twice"),
    (["--sweep", "--cutoff", "none,none"], "twice"),
])
def test_duplicate_values_are_refused_with_or_without_sweep(tmp_path, capsys, extra, msg):
    with pytest.raises(SystemExit) as exc:
        run(BASE + extra, store=_store(), today=TODAY, out_root=tmp_path)
    assert exc.value.code == 2 and msg in capsys.readouterr().err


def test_gallery_leaves_a_snapshot_without_a_proposal_alone():
    df = quiet("2024-11-07")
    snap = snapshot(df, minutes=5, now=replay_now(date(2024, 11, 7)))
    assert snap.proposal is None

    class T:
        take_profit, stop_loss = 1.0, 2.0

    assert gallery.with_trade_levels(snap, T, df) is snap


# ── /ship pre-landing review fixes ────────────────────────────────────────────

def test_infinite_reward_is_refused():
    with pytest.raises(ValueError, match="finite number greater than 0"):
        OrbSettings(5, float("inf"))


def test_reward_and_account_keep_full_precision_in_the_rerun_command():
    s = OrbSettings(5, 1.2345678)
    assert s.cli_args()[3] == "1.2345678" and s.suffix() == "r1.2345678"
    assert OrbSettings(5, 1.5).cli_args()[3] == "1.5" and OrbSettings(5, 2.0).cli_args()[3] == "2"


def test_rerun_command_keeps_no_backfill_and_out(tmp_path):
    from argparse import Namespace

    from marketscanner.backtest.__main__ import rerun_command

    args = Namespace(cash=12345.67, per_deal=1000.0, min_cash=2000.0, backfill=False, out="runs")
    cmd = rerun_command(args, date(2024, 1, 2), date(2024, 3, 1), ["SPY"], OrbSettings(5, 2.0))
    assert "--cash 12345.67 " in cmd and "--no-backfill" in cmd and "--out runs" in cmd
    assert "--no-backfill" not in rerun_command(Namespace(cash=1.0, per_deal=1.0, min_cash=0.0, backfill=True,
                                                          out=None), date(2024, 1, 2), date(2024, 3, 1), ["SPY"],
                                                OrbSettings(5))


def test_a_variant_tied_with_the_baseline_does_not_rank_above_it():
    from datetime import time

    from marketscanner.backtest.ideas import load_frames
    from marketscanner.backtest.sweep import Grid, run_sweep
    from tests.test_backtest_sweep import DAYS, FRAMES, SYMS, _mem

    loaded = load_frames(_mem(FRAMES), SYMS, DAYS[0], DAYS[-1])
    # A 16:00 cut-off drops nothing, so it ties the baseline exactly; it is listed first in grid order.
    res = run_sweep(loaded, SYMS, DAYS[0], DAYS[-1], CFG, Grid((5,), (1.0,), (time(16, 0), None)),
                    baseline=OrbSettings(5))
    tie = next(r for r in res.rows if not r.is_baseline)
    assert tie.d_is == 0.0 and tie.d_oos == 0.0
    assert res.rows[0].is_baseline and res.best is None
    assert res.baseline_row.rank_oos < tie.rank_oos


def test_a_winner_with_too_few_trades_is_named_in_the_verdict():
    few = Row(settings=OrbSettings(10, 1.5), is_baseline=False, is_=HalfStats(closed=12), oos=HalfStats(closed=6),
              d_is=2.0, se_d_is=1.0, d_oos=4.0, se_d_oos=0.0, rank_is=1)
    base = _r(OrbSettings(5), 0.0, 0.0, 0.0, 2, baseline=True)
    res = _result([few, base])
    assert few.passes and few.too_few
    v = res.verdict()
    assert "1 variant(s) beat current .env" in v and "Too few trades to trust (under 100 closed in a half): 10 / 1.5 / none." in v
    assert "Too few" not in _result([_r(OrbSettings(10, 1.5), 3.0, 9.0, 2.0, 1), base]).verdict()
