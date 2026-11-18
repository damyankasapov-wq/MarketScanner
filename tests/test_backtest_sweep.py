"""
Settings sweep (backtest-only): ORB settings, OrbStrategy.derive, the IS / OOS
split, paired ΔR vs the baseline, the verdict rule, ranking, the CLI and outputs.
"""
import csv
import math
import re
from datetime import date, time

import numpy as np
import pandas as pd
import pytest

from marketscanner import config
from marketscanner.backtest import gallery, sweep_report
from marketscanner.backtest.__main__ import run, run_id
from marketscanner.backtest.engine import AccountConfig, find_exits, run_engine
from marketscanner.backtest.equity import build_equity
from marketscanner.backtest.ideas import SessionInfo, generate_ideas, load_frames
from marketscanner.backtest.metrics import summarize
from marketscanner.backtest.strategy import OrbSettings, OrbStrategy, TradeIdea, parse_cutoff
from marketscanner.backtest.sweep import (
    Grid, HalfStats, Row, half_stats, idea_r, paired_delta, run_sweep, split_sessions_is_oos,
)
from marketscanner.data.bar_store import InMemoryBarStore
from marketscanner.strategies.orb_session import ExitHit, format_r, levels, replay_now, snapshot
from tests.backtest_fixtures import make_session, quiet, stack
from tests.test_backtest_speed_paths import random_history, weekdays_from

CFG = AccountConfig(initial_cash=10000.0, per_deal=1000.0, min_cash=2000.0)
TODAY = date(2024, 11, 20)


# ── levels and format_r ───────────────────────────────────────────────────────

def test_levels_at_one_are_bit_identical_to_entry_plus_minus_delta():
    entry, delta = 581.2, 5.5
    assert levels(entry, delta, "BUY") == (entry + delta, entry - delta)
    assert levels(entry, delta, "SELL") == (entry - delta, entry + delta)
    assert levels(entry, delta, "BUY", 1.5) == (entry + 1.5 * delta, entry - delta)
    assert levels(entry, delta, "SELL", 2.0) == (entry - 2.0 * delta, entry + delta)


@pytest.mark.parametrize("r, default, want", [
    (1.0, None, "+1R"), (-1.0, None, "−1R"), (1.5, None, "+1.5R"), (0.3712, None, "+0.37R"),
    (None, 1.0, "+1R"), (None, -1.0, "−1R"), (None, None, ""), (0.0, None, "+0R"),
])
def test_format_r(r, default, want):
    assert format_r(r, default) == want


# ── settings ──────────────────────────────────────────────────────────────────

def test_settings_validation():
    for bad in (dict(candle_minutes=7), dict(candle_minutes=5, reward_ratio=0),
                dict(candle_minutes=5, reward_ratio=-1), dict(candle_minutes=5, cutoff=time(10, 30)),
                dict(candle_minutes=5, cutoff=time(16, 1)), dict(candle_minutes=5, cutoff=time(9, 0))):
        with pytest.raises(ValueError):
            OrbSettings(**bad)
    assert OrbSettings(5, 1.0, time(16, 0)).cutoff == time(16, 0)
    assert OrbSettings(5, 1.0, time(10, 31)).cutoff == time(10, 31)


def test_settings_labels_and_args():
    s = OrbSettings(10, 1.5, time(13, 0))
    assert s.short() == "10 / 1.5 / 13:00"
    assert s.suffix() == "c10_r1.5_x1300"
    assert s.cli_args() == ["--candle", "10", "--reward", "1.5", "--cutoff", "13:00"]
    assert "10-min candle" in s.describe() and "1:1.5" in s.describe() and "13:00" in s.describe()
    base = OrbSettings(5)
    assert base.suffix() == "" and base.short() == "5 / 1 / none" and base.cli_args()[-1] == "none"


def test_current_settings_follow_this_machines_env(monkeypatch):
    monkeypatch.setattr(config, "CONFIRM_CANDLE_MINUTES", 10)
    assert OrbSettings.current() == OrbSettings(10, 1.0, None)


def test_parse_cutoff():
    assert parse_cutoff("none") is None and parse_cutoff(" NONE ") is None
    assert parse_cutoff("13:00") == time(13, 0) and parse_cutoff("9:05") == time(9, 5)
    with pytest.raises(ValueError):
        parse_cutoff("1300")
    with pytest.raises(ValueError):
        parse_cutoff("noon")


# ── derive ────────────────────────────────────────────────────────────────────

UP_DAY = make_session("2024-11-06", "UP")                       # confirmation candle closes 10:35 ET
LATE_DAY = make_session("2024-11-06", "UP", confirm_at=300)     # 14:30 candle, closes 14:35 ET


def _plain(df, candle=5):
    return OrbStrategy(settings=OrbSettings(candle)).plain_idea("SPY", df)


def test_plain_idea_is_what_snapshot_proposes():
    idea = _plain(UP_DAY)
    snap = snapshot(UP_DAY, minutes=5, now=replay_now(date(2024, 11, 6)))
    assert (idea.entry, idea.take_profit, idea.stop_loss) == (snap.proposal.entry, snap.proposal.take_profit,
                                                              snap.proposal.stop_loss)
    assert idea.meta["reward_ratio"] == 1.0 and idea.meta["cutoff"] is None


def test_derive_moves_only_the_target():
    plain = _plain(UP_DAY)
    d = OrbStrategy.derive(plain, OrbSettings(5, 1.5))
    assert d.stop_loss == plain.stop_loss and d.entry == plain.entry and d.signal_ts == plain.signal_ts
    assert d.take_profit == plain.entry + 1.5 * plain.delta
    assert d.meta["reward_ratio"] == 1.5
    assert OrbStrategy.derive(plain, OrbSettings(5)) is plain


def test_derive_cutoff_drops_late_confirmations_and_keeps_an_exact_close():
    late = _plain(LATE_DAY)
    assert pd.Timestamp(late.signal_ts).tz_convert("America/New_York").strftime("%H:%M") == "14:35"
    assert OrbStrategy.derive(late, OrbSettings(5, cutoff=time(14, 0))) is None
    assert OrbStrategy.derive(late, OrbSettings(5, cutoff=time(14, 35))) is not None   # closes exactly at the cut-off
    assert OrbStrategy.derive(late, OrbSettings(5, cutoff=time(14, 34))) is None
    assert OrbStrategy.derive(_plain(UP_DAY), OrbSettings(5, cutoff=time(13, 0))) is not None


def test_derive_refuses_a_derived_idea_and_a_different_candle():
    plain = _plain(UP_DAY)
    with pytest.raises(ValueError, match="plain idea"):
        OrbStrategy.derive(OrbStrategy.derive(plain, OrbSettings(5, 2.0)), OrbSettings(5, 1.5))
    with pytest.raises(ValueError, match="5-min candle, settings say 10"):
        OrbStrategy.derive(plain, OrbSettings(10))


@pytest.mark.parametrize("settings", [OrbSettings(5, 1.5), OrbSettings(5, 2.0, time(13, 0)),
                                      OrbSettings(10, 1.0, time(14, 0)), OrbSettings(15, 1.5)])
def test_propose_equals_derive_of_the_plain_idea_session_by_session(settings):
    days = weekdays_from(date(2024, 3, 4), 25)
    frame = random_history(days, seed=4, step=0.04, gap=0.6)
    strat = OrbStrategy(settings=settings)
    n = 0
    for d, df in load_frames(_mem({"SPY": frame}), ["SPY"], days[0], days[-1]).sessions["SPY"].items():
        plain = OrbStrategy(settings=OrbSettings(settings.candle_minutes)).plain_idea("SPY", df)
        direct = strat.propose("SPY", df)
        derived = None if plain is None else OrbStrategy.derive(plain, settings)
        assert direct == derived
        n += direct is not None
    assert n > 3


def test_orb_strategy_rejects_conflicting_candle_arguments():
    with pytest.raises(ValueError):
        OrbStrategy(candle_minutes=10, settings=OrbSettings(5))
    assert OrbStrategy(candle_minutes=10).settings == OrbSettings(10)


# ── statistics ────────────────────────────────────────────────────────────────

def _info(d, status="ok", sym="SPY"):
    return SessionInfo(sym, d, 390, 60, status, 0)


def test_split_is_ceil_two_thirds_of_dates_with_a_usable_session():
    days = weekdays_from(date(2024, 1, 1), 10)
    infos = [_info(d) for d in days] + [_info(days[0], sym="QQQ")]
    infos += [_info(date(2024, 2, 1), "no_data"), _info(date(2024, 2, 2), "skipped_opening_hole")]
    is_d, oos_d = split_sessions_is_oos(infos)
    assert len(is_d) == math.ceil(10 * 2 / 3) == 7 and oos_d == days[7:] and is_d == days[:7]
    for n, (n_is, n_oos) in {1: (1, 0), 2: (2, 0), 3: (2, 1), 4: (3, 1)}.items():
        a, b = split_sessions_is_oos([_info(d) for d in days[:n]])
        assert (len(a), len(b)) == (n_is, n_oos)
    assert split_sessions_is_oos([]) == ([], [])


def test_half_stats():
    h = half_stats([1.5, -1.0, None, 0.5, -1.0])
    assert (h.signals, h.closed, h.open, h.wins) == (5, 4, 1, 2)
    assert h.total_r == pytest.approx(0.0) and h.avg_r == pytest.approx(0.0)
    assert h.se_r == pytest.approx(np.std([1.5, -1.0, 0.5, -1.0], ddof=1) / 2)
    assert h.win_rate == 50.0
    one = half_stats([2.0])
    assert one.se_r is None and one.avg_r == 2.0
    empty = half_stats([])
    assert empty.avg_r is None and empty.se_r is None and empty.win_rate is None


def test_idea_r():
    idea = TradeIdea("SPY", "SELL", pd.Timestamp("2024-11-06 15:35", tz="UTC").to_pydatetime(), 100.0, 98.0, 101.0,
                     date(2024, 11, 6), 1.0)
    hit = ExitHit(pd.Timestamp("2024-11-06 16:00", tz="UTC").to_pydatetime(), 98.0, "TP")
    assert idea_r(idea, hit) == pytest.approx(2.0)
    assert idea_r(idea, ExitHit(hit.ts, 101.4, "GAP_SL")) == pytest.approx(-1.4)
    assert idea_r(idea, None) is None


def test_paired_delta_is_clustered_by_date():
    d1, d2, d3 = date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)
    base = {("SPY", d1): 1.0, ("QQQ", d1): -1.0, ("SPY", d2): 1.0}
    var = {("SPY", d1): 2.0, ("QQQ", d1): -1.0, ("QQQ", d2): 1.0, ("SPY", d3): -1.0}
    # D_d1 = (2-1) + (-1-(-1)) = 1; D_d2 = (0-1) + (1-0) = 0; D_d3 = -1
    total, se = paired_delta(var, base, [d1, d2, d3])
    assert total == pytest.approx(0.0)
    assert se == pytest.approx(math.sqrt(3) * np.std([1.0, 0.0, -1.0], ddof=1))
    assert paired_delta(base, base, [d1, d2, d3]) == (0.0, 0.0)
    assert paired_delta(var, base, []) == (0.0, None)
    assert paired_delta(var, base, [d1]) == (1.0, None)


def _row(d_is, d_oos, se_oos, baseline=False):
    return Row(settings=OrbSettings(5), is_baseline=baseline, is_=HalfStats(), oos=HalfStats(),
               d_is=d_is, d_oos=d_oos, se_d_oos=se_oos)


def test_verdict_rule():
    assert _row(1.0, 5.0, 2.0).passes                       # OOS 5 > 2 x 2 and IS > 0
    assert not _row(1.0, 4.0, 2.0).passes                   # OOS not above 2 SE
    assert not _row(0.0, 5.0, 2.0).passes                   # IS not positive
    assert not _row(-1.0, 5.0, 2.0).passes
    assert not _row(1.0, 5.0, None).passes                  # no SE (fewer than 2 OOS dates)
    assert not _row(1.0, 5.0, 2.0, baseline=True).passes    # the baseline never "beats" itself
    assert _row(1.0, 0.1, 0.0).passes                       # zero spread, positive both halves


# ── run_sweep ─────────────────────────────────────────────────────────────────

def _mem(frames):
    s = InMemoryBarStore()
    for sym, f in frames.items():
        s.upsert_bars(sym, f)
    return s


DAYS = weekdays_from(date(2024, 1, 2), 45)
FRAMES = {"SPY": random_history(DAYS, seed=1, step=0.04, gap=0.6),
          "QQQ": random_history(DAYS, seed=2, step=0.04, gap=0.6)}
SYMS = ["SPY", "QQQ"]


@pytest.fixture(scope="module")
def swept():
    loaded = load_frames(_mem(FRAMES), SYMS, DAYS[0], DAYS[-1])
    grid = Grid((5, 10), (1.0, 2.0), (None, time(13, 0)))
    return run_sweep(loaded, SYMS, DAYS[0], DAYS[-1], CFG, grid, baseline=OrbSettings(5))


def test_rows_are_ranked_by_in_sample_delta_and_the_baseline_is_zero(swept):
    assert len(swept.rows) == 8
    d = [r.d_is for r in swept.rows]
    assert d == sorted(d, reverse=True)
    assert [r.rank_is for r in swept.rows] == list(range(1, 9))
    assert sorted(r.rank_oos for r in swept.rows) == list(range(1, 9))
    b = swept.baseline_row
    assert b.is_baseline and b.d_is == 0.0 and b.d_oos == 0.0 and not b.passes
    assert len(swept.is_dates) == 30 and len(swept.oos_dates) == 15


def test_baseline_row_equals_a_plain_run_of_the_same_settings(swept):
    res = generate_ideas(_mem(FRAMES), OrbStrategy(settings=OrbSettings(5)), SYMS, DAYS[0], DAYS[-1])
    trades = run_engine(res.ideas, find_exits(res.ideas, res.frames), res.frames, CFG, SYMS)
    plain = summarize(trades, build_equity(trades, res.frames, CFG), CFG, "ORB", DAYS[0], DAYS[-1], SYMS)
    s = swept.baseline_row.summary
    for f in ("final_equity", "net_pnl", "realised_pnl", "unrealised_pnl", "signals", "opened", "skipped",
              "closed", "open", "wins", "losses", "max_drawdown", "max_drawdown_pct", "avg_r"):
        assert getattr(s, f) == pytest.approx(getattr(plain, f)), f
    closed_r = [t.r_multiple for t in trades if t.r_multiple is not None]
    b = swept.baseline_row
    assert b.is_.closed + b.oos.closed == len(closed_r)


def test_each_row_equals_a_direct_run_of_its_settings(swept):
    for row in swept.rows:
        res = generate_ideas(_mem(FRAMES), OrbStrategy(settings=row.settings), SYMS, DAYS[0], DAYS[-1])
        exits = find_exits(res.ideas, res.frames)
        rs = [idea_r(i, h) for i, h in zip(res.ideas, exits)]
        assert row.is_.signals + row.oos.signals == len(res.ideas), row.settings
        assert row.is_.total_r + row.oos.total_r == pytest.approx(sum(r for r in rs if r is not None))
        assert row.buy_r + row.sell_r == pytest.approx(row.is_.total_r + row.oos.total_r)


def test_baseline_is_added_when_the_grid_lacks_it():
    loaded = load_frames(_mem(FRAMES), SYMS, DAYS[0], DAYS[-1])
    res = run_sweep(loaded, SYMS, DAYS[0], DAYS[-1], CFG, Grid((10,), (2.0,), (None,)), baseline=OrbSettings(5))
    assert {r.settings for r in res.rows} == {OrbSettings(10, 2.0), OrbSettings(5)}
    assert res.baseline_row.settings == OrbSettings(5)


def test_a_variant_with_no_ideas_does_not_break_anything():
    loaded = load_frames(_mem(FRAMES), SYMS, DAYS[0], DAYS[-1])
    res = run_sweep(loaded, SYMS, DAYS[0], DAYS[-1], CFG, Grid((5,), (1.0,), (None, time(10, 31))),
                    baseline=OrbSettings(5))
    empty = next(r for r in res.rows if r.settings.cutoff == time(10, 31))
    assert empty.is_.signals == empty.oos.signals == 0 and empty.is_.avg_r is None and empty.is_.se_r is None
    assert empty.summary.signals == 0
    assert empty.d_is <= 0 or empty.d_is >= 0      # a number, not NaN
    assert not math.isnan(empty.d_is) and not math.isnan(empty.d_oos)
    sweep_report.format_text(res)                   # renders


def test_a_period_too_short_for_an_out_of_sample_half():
    days = DAYS[:2]
    loaded = load_frames(_mem({s: f for s, f in FRAMES.items()}), SYMS, days[0], days[-1])
    res = run_sweep(loaded, SYMS, days[0], days[-1], CFG, Grid((5,), (1.0, 2.0), (None,)), baseline=OrbSettings(5))
    assert res.oos_dates == [] and any("too short" in w for w in res.warnings)
    assert all(r.oos.signals == 0 and r.se_d_oos is None and not r.passes for r in res.rows)
    assert "no variant beats" in res.verdict()


def test_in_sample_trade_exiting_out_of_sample_is_counted():
    # 3 sessions: IS = first 2, OOS = the 3rd. The 11-06 BUY sits through 11-07 and hits its
    # target on 11-08 (an OOS date), so it is an IS trade that closes in OOS.
    frames = {"SPY": stack(make_session("2024-11-06", "UP"), quiet("2024-11-07"),
                           quiet("2024-11-08", {100: (581.2, 587.0, 581.0, 586.0)}))}
    loaded = load_frames(_mem(frames), ["SPY"], date(2024, 11, 6), date(2024, 11, 8))
    res = run_sweep(loaded, ["SPY"], date(2024, 11, 6), date(2024, 11, 8), CFG, Grid((5,), (1.0,), (None,)),
                    baseline=OrbSettings(5))
    row = res.baseline_row
    assert res.oos_dates == [date(2024, 11, 8)]
    assert row.is_.closed == 1 and row.is_.total_r == pytest.approx(1.0) and row.is_to_oos == 1
    assert row.oos.signals == 0


def test_many_variants_warning_and_refusing_a_strategy_without_derive():
    loaded = load_frames(_mem(FRAMES), SYMS, DAYS[0], DAYS[-1])
    big = Grid((5,), (1.0, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0, 4.0),
               (None, time(11, 0), time(12, 0), time(13, 0), time(14, 0), time(15, 0), time(15, 30), time(16, 0)))
    assert len(big.variants()) == 64
    res = run_sweep(loaded, SYMS, DAYS[0], DAYS[0 + 5], CFG, big, baseline=OrbSettings(5))
    assert any("64 variants" in w for w in res.warnings)

    class NoDerive:
        name = "X"

    with pytest.raises(ValueError, match="no derive"):
        run_sweep(loaded, SYMS, DAYS[0], DAYS[-1], CFG, Grid(), strategy_cls=NoDerive)


def test_report_marks_and_files(swept, tmp_path):
    text = sweep_report.write_all(swept, tmp_path, rerun="python -m marketscanner.backtest ...")
    assert text.isascii()
    assert "8 variants tried" in text and "current .env" in text and "VERDICT" in text
    assert "Full report for the in-sample best" in text
    rows = list(csv.DictReader(open(tmp_path / "sweep.csv", encoding="utf-8")))
    assert len(rows) == 8 and rows[0]["rank_is"] == "1"
    assert any("current .env" in r["mark"] for r in rows)
    page = (tmp_path / "sweep.html").read_text(encoding="utf-8")
    assert "<table>" in page and "data:image/png;base64," in page and "VERDICT" in page
    if swept.best is not None:
        assert "in-sample best" in rows[0]["mark"]


def test_text_table_always_shows_the_baseline(swept):
    rows = sweep_report.shown_rows(swept)
    assert any(r.is_baseline for r in rows)


# ── CLI ───────────────────────────────────────────────────────────────────────

BASE = ["--from", "2024-11-06", "--to", "2024-11-08", "--markets", "SPY,QQQ",
        "--cash", "10000", "--per-deal", "1000", "--min-cash", "2000", "--no-backfill"]


def _store():
    return _mem({
        "SPY": stack(make_session("2024-11-06", "UP", {100: (581.2, 587.0, 581.0, 586.0)}), quiet("2024-11-07"),
                     quiet("2024-11-08")),
        "QQQ": stack(make_session("2024-11-06", "DOWN"), quiet("2024-11-07", box_high=578.0),
                     quiet("2024-11-08", box_high=578.0)),
    })


@pytest.mark.parametrize("extra, msg", [
    (["--candle", "5,10"], "use --sweep"),
    (["--reward", "1,2"], "use --sweep"),
    (["--candle", "7"], "must divide 60"),
    (["--reward", "0"], "greater than 0"),
    (["--cutoff", "10:00"], "after 10:30"),
    (["--cutoff", "noon"], "HH:MM"),
    (["--sweep", "--reward", "1,1"], "twice"),
    (["--sweep", "--candle", "7"], "must divide 60"),
])
def test_cli_rejects_bad_settings(tmp_path, capsys, extra, msg):
    with pytest.raises(SystemExit) as exc:
        run(BASE + extra, store=_store(), today=TODAY, out_root=tmp_path)
    assert exc.value.code == 2
    assert msg in capsys.readouterr().err


def test_default_single_run_is_unchanged(tmp_path, capsys):
    assert run(BASE + ["--gallery", "none"], store=_store(), today=TODAY, out_root=tmp_path) == 0
    out = tmp_path / run_id("ORB", date(2024, 11, 6), date(2024, 11, 8), ["SPY", "QQQ"])
    assert out.name == "ORB_20241106-20241108_SPY-QQQ"
    assert not (out / "summary.txt").read_text(encoding="utf-8").startswith("Settings:")
    assert "Settings:" not in (out / "report.html").read_text(encoding="utf-8")


def test_single_run_with_settings_names_the_folder_and_states_them(tmp_path, capsys):
    assert run(BASE + ["--gallery", "none", "--reward", "1.5", "--cutoff", "14:00"],
               store=_store(), today=TODAY, out_root=tmp_path) == 0
    out = tmp_path / "ORB_r1.5_x1400_20241106-20241108_SPY-QQQ"
    text = (out / "summary.txt").read_text(encoding="utf-8")
    assert text.startswith("Settings: 5-min candle, reward 1:1.5, cut-off 14:00 ET")
    assert "Settings: 5-min candle" in (out / "report.html").read_text(encoding="utf-8")
    ledger = list(csv.DictReader(open(out / "ledger.csv", encoding="utf-8")))
    spy = next(r for r in ledger if r["symbol"] == "SPY")
    assert float(spy["take_profit"]) == pytest.approx(581.2 + 1.5 * 5.5)


def test_cli_sweep_end_to_end_and_the_rerun_command_reproduces_its_row(tmp_path, capsys):
    store = _mem(FRAMES)
    argv = ["--from", str(DAYS[0]), "--to", str(DAYS[-1]), "--markets", "SPY,QQQ", "--cash", "10000",
            "--per-deal", "1000", "--min-cash", "2000", "--no-backfill",
            "--sweep", "--candle", "5,10", "--reward", "1,2", "--cutoff", "none,13:00"]
    assert run(argv, store=store, today=date(2024, 6, 1), out_root=tmp_path) == 0
    out = tmp_path / f"{run_id('ORB', DAYS[0], DAYS[-1], SYMS)}_sweep_c5-10_r1-2_xnone-1300"
    for f in ("sweep.txt", "sweep.csv", "sweep.html"):
        assert (out / f).exists()
    printed = capsys.readouterr().out
    cmd = re.search(r"python -m marketscanner\.backtest (.+)", printed).group(1).split()
    rows = list(csv.DictReader(open(out / "sweep.csv", encoding="utf-8")))
    top = rows[0]
    assert run(cmd + ["--no-backfill", "--gallery", "none"], store=store, today=date(2024, 6, 1),
               out_root=tmp_path / "rerun") == 0
    summary = next((tmp_path / "rerun").iterdir()) / "summary.txt"
    pnl = re.search(r"Profit / loss\s+(-?[\d,]+\.\d\d)", summary.read_text(encoding="utf-8")).group(1)
    assert float(pnl.replace(",", "")) == pytest.approx(float(top["net_pnl"]), abs=0.005)


def test_cli_sweep_with_an_unreachable_store_gives_the_clear_error(tmp_path, capsys):
    class DeadStore(InMemoryBarStore):
        def load(self, *a, **k):
            raise ConnectionError("db down")

    rc = run(BASE + ["--sweep"], store=DeadStore(), today=TODAY, out_root=tmp_path)
    assert rc == 2 and "Cannot use the bars store" in capsys.readouterr().err


# ── gallery at a non-default reward ───────────────────────────────────────────

def test_gallery_draws_the_trades_own_levels_and_r(tmp_path):
    df = make_session("2024-11-06", "UP", {100: (581.2, 590.0, 581.0, 589.0)})   # reaches 589.45 (1.5R)
    snap = snapshot(df, minutes=5, now=replay_now(date(2024, 11, 6)))
    plain = _plain(df)
    t15 = OrbStrategy.derive(plain, OrbSettings(5, 1.5))

    class T:
        take_profit, stop_loss = t15.take_profit, t15.stop_loss

    got = gallery.with_trade_levels(snap, T, df)
    assert got.proposal.take_profit == pytest.approx(581.2 + 1.5 * 5.5)
    assert got.outcome.status == "TP_HIT" and got.outcome.r_multiple == pytest.approx(1.5)

    class Same:
        take_profit, stop_loss = snap.proposal.take_profit, snap.proposal.stop_loss

    assert gallery.with_trade_levels(snap, Same, df) is snap
