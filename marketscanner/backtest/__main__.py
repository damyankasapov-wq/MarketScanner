"""
python -m marketscanner.backtest --from 2026-10-19 --to 2026-11-13 \
    --markets SPY,QQQ,GLD --cash 10000 --per-deal 1000 --min-cash 2000

Reads stored 1-minute bars from the Postgres bars table (backfilling missing
sessions from yfinance, or Alpaca for older ones, unless --no-backfill), replays the strategy on a simulated
account and writes a run folder with ledger.csv, equity.png, summary.txt,
report.html and trades/*.png. Informational only: nothing places orders.

ORB settings for one run: --candle N --reward X --cutoff HH:MM|none (defaults:
this machine's .env candle size, 1:1, no cut-off, i.e. what live alerts do).

Settings sweep: add --sweep and give comma lists, e.g.
    --sweep --candle 5,10,15 --reward 1,1.5,2 --cutoff none,13:00,14:00
which writes sweep.txt, sweep.csv and sweep.html into <run>_sweep/ (<run>_sweep_<grid>/ for a
non-default grid, so sweeps of the same dates with different lists keep separate folders).
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

import pytz

from marketscanner import config
from marketscanner.backtest import gallery, html_report, report, sweep_report
from marketscanner.backtest.engine import AccountConfig, find_exits, run_engine
from marketscanner.backtest.equity import build_equity
from marketscanner.backtest.ideas import ideas_from_frames, load_frames
from marketscanner.backtest.metrics import Coverage, format_summary, summarize
from marketscanner.backtest.strategy import STRATEGIES, OrbSettings, get_strategy, parse_cutoff
from marketscanner.backtest.sweep import DEFAULT_CANDLES, DEFAULT_CUTOFFS, DEFAULT_REWARDS, Grid, run_sweep
from marketscanner.data.backfill import ensure_bars

_ET = pytz.timezone(config.TIMEZONE)
DEFAULT_OUT = Path("output/backtest")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m marketscanner.backtest",
        description="Replay a strategy over past sessions on a simulated account. Informational only.",
    )
    p.add_argument("--strategy", default="ORB", help=f"strategy name ({', '.join(sorted(STRATEGIES))}); default ORB")
    p.add_argument("--from", dest="start", required=True, metavar="YYYY-MM-DD", help="first session of the period")
    p.add_argument("--to", dest="end", metavar="YYYY-MM-DD", help="last session of the period (default: yesterday)")
    p.add_argument("--markets", default=",".join(config.MARKETS), help="comma-separated symbols (default: all watched markets)")
    p.add_argument("--cash", type=float, required=True, help="initial amount of money in the account")
    p.add_argument("--per-deal", dest="per_deal", type=float, required=True, help="dollar amount invested in a single deal")
    p.add_argument("--min-cash", dest="min_cash", type=float, default=0.0,
                   help="minimum amount that must stay in the account, not invested (default 0)")
    p.add_argument("--out", default=None, help=f"output root folder (default {DEFAULT_OUT})")
    p.add_argument("--gallery", choices=["all", "none"], default="all", help="draw a chart for every trade (default all)")
    p.add_argument("--no-backfill", dest="backfill", action="store_false",
                   help="do not fetch missing sessions from yfinance or Alpaca; use only what is stored")
    p.add_argument("--candle", default=None, metavar="N[,N...]",
                   help="confirmation candle minutes (default: CONFIRM_CANDLE_MINUTES from .env)")
    p.add_argument("--reward", default=None, metavar="X[,X...]",
                   help="take profit in box widths; the stop stays one box width (default 1)")
    p.add_argument("--cutoff", default=None, metavar="HH:MM|none[,...]",
                   help="latest ET time the confirmation candle may close; later ones give no trade (default none)")
    p.add_argument("--sweep", action="store_true",
                   help="compare every combination of the --candle/--reward/--cutoff lists with the current .env settings")
    return p


def _parse_date(parser: argparse.ArgumentParser, text: str, flag: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError:
        parser.error(f"{flag} must be YYYY-MM-DD, got {text!r}")


def validate(parser: argparse.ArgumentParser, args, today: date) -> tuple[date, date, list[str]]:
    start = _parse_date(parser, args.start, "--from")
    end = _parse_date(parser, args.end, "--to") if args.end else today - timedelta(days=1)
    if start > end:
        parser.error(f"--from ({start}) is after --to ({end})")
    if start >= today:
        parser.error(f"--from ({start}) must be a past session; today's session is still incomplete")
    if args.cash <= 0:
        parser.error("--cash must be greater than 0")
    if args.per_deal <= 0:
        parser.error("--per-deal must be greater than 0")
    if args.min_cash < 0:
        parser.error("--min-cash must be 0 or more")
    if args.min_cash >= args.cash:
        parser.error("--min-cash must be less than --cash")
    symbols = [s.strip().upper() for s in args.markets.split(",") if s.strip()]
    if not symbols:
        parser.error("--markets needs at least one symbol")
    if len(set(symbols)) != len(symbols):
        parser.error("--markets lists a symbol twice")
    try:
        get_strategy(args.strategy)
    except ValueError as exc:
        parser.error(str(exc))
    return start, end, symbols


def _split_list(text: Optional[str]) -> list[str]:
    return [] if text is None else [t.strip() for t in text.split(",") if t.strip()]


def parse_settings(parser: argparse.ArgumentParser, args) -> tuple[Optional[OrbSettings], Optional[Grid]]:
    """(single-run settings, None) without --sweep, (None, grid) with it. Errors go through parser.error."""
    lists = {"--candle": _split_list(args.candle), "--reward": _split_list(args.reward),
             "--cutoff": _split_list(args.cutoff)}
    try:
        candles = [int(x) for x in lists["--candle"]]
        rewards = [float(x) for x in lists["--reward"]]
        cutoffs = [parse_cutoff(x) for x in lists["--cutoff"]]
    except ValueError as exc:
        parser.error(f"bad --candle / --reward / --cutoff value: {exc}")
    for flag, vals in (("--candle", candles), ("--reward", rewards), ("--cutoff", cutoffs)):
        if len(set(vals)) != len(vals):
            parser.error(f"{flag} lists a value twice")
    if not args.sweep:
        for flag, vals in lists.items():
            if len(vals) > 1:
                parser.error(f"{flag} has several values: use --sweep to compare them")
        try:
            settings = OrbSettings(
                candles[0] if candles else config.CONFIRM_CANDLE_MINUTES,
                rewards[0] if rewards else 1.0,
                cutoffs[0] if cutoffs else None,
            )
        except ValueError as exc:
            parser.error(str(exc))
        return settings, None
    if args.strategy.strip().upper() != "ORB":
        parser.error("--sweep only supports the ORB strategy")
    grid = Grid(tuple(candles) or DEFAULT_CANDLES, tuple(rewards) or DEFAULT_REWARDS,
                tuple(cutoffs) or DEFAULT_CUTOFFS)
    try:
        grid.variants()
        OrbSettings.current()
    except ValueError as exc:
        parser.error(str(exc))
    return None, grid


def run_id(strategy: str, start: date, end: date, symbols: list[str], settings: Optional[OrbSettings] = None) -> str:
    suffix = settings.suffix() if settings is not None else ""
    name = f"{strategy.upper()}_{suffix}" if suffix else strategy.upper()
    return f"{name}_{start:%Y%m%d}-{end:%Y%m%d}_{'-'.join(symbols)}"


def rerun_command(args, start: date, end: date, symbols: list[str], settings: OrbSettings) -> str:
    parts = ["python -m marketscanner.backtest", "--from", f"{start}", "--to", f"{end}", "--markets", ",".join(symbols),
             "--cash", f"{args.cash:.15g}", "--per-deal", f"{args.per_deal:.15g}", "--min-cash", f"{args.min_cash:.15g}"]
    if not args.backfill:
        parts.append("--no-backfill")
    if args.out:
        parts += ["--out", f'"{args.out}"' if " " in str(args.out) else str(args.out)]
    return " ".join(parts + settings.cli_args())


def run(
    argv: list[str],
    store=None,
    fetch: Optional[Callable] = None,
    today: Optional[date] = None,
    out_root: Optional[Path] = None,
) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    today = today or datetime.now(_ET).date()
    start, end, symbols = validate(parser, args, today)
    cfg = AccountConfig(initial_cash=args.cash, per_deal=args.per_deal, min_cash=args.min_cash)
    if args.cash - args.min_cash < args.per_deal:
        print(f"Warning: cash {args.cash:,.2f} minus minimum {args.min_cash:,.2f} is less than one deal "
              f"({args.per_deal:,.2f}); no deal can ever open.", file=sys.stderr)

    settings, grid = parse_settings(parser, args)
    strategy = get_strategy(args.strategy, settings=settings) if settings is not None else get_strategy(args.strategy)
    try:
        if store is None:
            from marketscanner.data.pg_bar_store import PostgresBarStore
            store = PostgresBarStore()
        backfill = ensure_bars(store, symbols, start, end, fetch=fetch, today=today) if args.backfill else None
        loaded = load_frames(store, symbols, start, end)
        result = ideas_from_frames(loaded, strategy, symbols, start, end)
    except Exception as exc:  # noqa: BLE001 - one clear message beats a traceback here
        print(f"Cannot use the bars store: {exc!r}\n"
              f"Using DB_HOST={config.DB['host']} DB_PORT={config.DB['port']} DB_NAME={config.DB['dbname']}.\n"
              "Check DB_HOST / DB_PORT / DB_NAME / DB_USER / DB_PASSWORD (see .env.example).", file=sys.stderr)
        if not config.ENV_FILE.exists() and "DB_HOST" not in os.environ:
            print(f"No .env found at {config.ENV_FILE}, so the defaults above are in use. A git worktree does not "
                  "get a copy of .env: copy it from the main checkout.", file=sys.stderr)
        return 2

    if grid is not None:
        return _run_sweep(args, argv, loaded, result, backfill, grid, cfg, start, end, symbols, out_root)

    exits = find_exits(result.ideas, result.frames)
    trades = run_engine(result.ideas, exits, result.frames, cfg, symbols)
    equity = build_equity(trades, result.frames, cfg)
    coverage = Coverage(sessions=result.sessions, backfill=backfill)
    summary = summarize(trades, equity, cfg, strategy.name, start, end, symbols, coverage)
    text = format_summary(summary, coverage)
    note = f"Settings: {settings.describe()}" if settings is not None and settings.suffix() else ""
    if note:
        text = f"{note}\n{text}"

    root = out_root or (Path(args.out) if args.out else DEFAULT_OUT)
    out_dir = root / run_id(strategy.name, start, end, symbols, settings)
    out_dir.mkdir(parents=True, exist_ok=True)
    report.write_ledger_csv(trades, out_dir / "ledger.csv")
    report.write_text(out_dir / "summary.txt", text)
    report.save_figure(report.render_equity_figure(equity, trades, summary), out_dir / "equity.png")

    chart_paths: dict = {}
    chart_errors: dict = {}
    if args.gallery == "all":
        candle_minutes = getattr(strategy, "candle_minutes", config.CONFIRM_CANDLE_MINUTES)
        chart_paths, chart_errors = gallery.write_trade_charts(
            trades, result.frames, out_dir, candle_minutes, sessions=loaded.sessions)
    page = html_report.build_html(summary, trades, (out_dir / "equity.png").read_bytes(), coverage, chart_paths,
                                  chart_errors, note=note)
    (out_dir / "report.html").write_text(page, encoding="utf-8")
    if chart_errors:
        failures = [f"  trade {i}: {e}" for i, e in sorted(chart_errors.items())]
        text += "\n\nCHART FAILURES\n" + "\n".join(failures)
        report.write_text(out_dir / "summary.txt", text)

    print(text)
    print(f"\nWrote {out_dir}")
    return 0


def _run_sweep(args, argv, loaded, result, backfill, grid: Grid, cfg: AccountConfig, start: date, end: date,
               symbols: list[str], out_root: Optional[Path]) -> int:
    if any(a.startswith("--gallery") for a in argv):
        print("Note: --gallery is ignored with --sweep (no per-variant charts).", file=sys.stderr)
    res = run_sweep(loaded, symbols, start, end, cfg, grid,
                    progress=lambda msg: print(f"  sweep: {msg}", file=sys.stderr))
    coverage = Coverage(sessions=result.sessions, backfill=backfill)
    pick = res.best.settings if res.best is not None else res.baseline
    rerun = rerun_command(args, start, end, symbols, pick)
    root = out_root or (Path(args.out) if args.out else DEFAULT_OUT)
    tag = grid.tag()
    out_dir = root / (f"{run_id('ORB', start, end, symbols)}_sweep" + (f"_{tag}" if tag else ""))
    text = sweep_report.write_all(res, out_dir, coverage, rerun)
    print(text)
    print(f"\nWrote {out_dir}")
    return 0


def main() -> None:
    sys.exit(run(sys.argv[1:]))


if __name__ == "__main__":
    main()
