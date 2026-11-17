"""
python -m marketscanner.backtest --from 2026-10-19 --to 2026-11-13 \
    --markets SPY,QQQ,GLD --cash 10000 --per-deal 1000 --min-cash 2000

Reads stored 1-minute bars from the Postgres bars table (backfilling missing
sessions from yfinance, or Alpaca for older ones, unless --no-backfill), replays the strategy on a simulated
account and writes a run folder with ledger.csv, equity.png, summary.txt,
report.html and trades/*.png. Informational only: nothing places orders.
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

import pytz

from marketscanner import config
from marketscanner.backtest import gallery, html_report, report
from marketscanner.backtest.engine import AccountConfig, find_exits, run_engine
from marketscanner.backtest.equity import build_equity
from marketscanner.backtest.ideas import generate_ideas
from marketscanner.backtest.metrics import Coverage, format_summary, summarize
from marketscanner.backtest.strategy import STRATEGIES, get_strategy
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


def run_id(strategy: str, start: date, end: date, symbols: list[str]) -> str:
    return f"{strategy.upper()}_{start:%Y%m%d}-{end:%Y%m%d}_{'-'.join(symbols)}"


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

    strategy = get_strategy(args.strategy)
    try:
        if store is None:
            from marketscanner.data.pg_bar_store import PostgresBarStore
            store = PostgresBarStore()
        backfill = ensure_bars(store, symbols, start, end, fetch=fetch, today=today) if args.backfill else None
        result = generate_ideas(store, strategy, symbols, start, end)
    except Exception as exc:  # noqa: BLE001 - one clear message beats a traceback here
        print(f"Cannot use the bars store: {exc!r}\n"
              f"Using DB_HOST={config.DB['host']} DB_PORT={config.DB['port']} DB_NAME={config.DB['dbname']}.\n"
              "Check DB_HOST / DB_PORT / DB_NAME / DB_USER / DB_PASSWORD (see .env.example).", file=sys.stderr)
        if not config.ENV_FILE.exists():
            print(f"No .env found at {config.ENV_FILE}, so the defaults above are in use. A git worktree does not "
                  "get a copy of .env: copy it from the main checkout.", file=sys.stderr)
        return 2

    exits = find_exits(result.ideas, result.frames)
    trades = run_engine(result.ideas, exits, result.frames, cfg, symbols)
    equity = build_equity(trades, result.frames, cfg)
    coverage = Coverage(sessions=result.sessions, backfill=backfill)
    summary = summarize(trades, equity, cfg, strategy.name, start, end, symbols, coverage)
    text = format_summary(summary, coverage)

    root = out_root or (Path(args.out) if args.out else DEFAULT_OUT)
    out_dir = root / run_id(strategy.name, start, end, symbols)
    out_dir.mkdir(parents=True, exist_ok=True)
    report.write_ledger_csv(trades, out_dir / "ledger.csv")
    report.write_text(out_dir / "summary.txt", text)
    report.save_figure(report.render_equity_figure(equity, trades, summary), out_dir / "equity.png")

    chart_paths: dict = {}
    chart_errors: dict = {}
    if args.gallery == "all":
        candle_minutes = getattr(strategy, "candle_minutes", config.CONFIRM_CANDLE_MINUTES)
        chart_paths, chart_errors = gallery.write_trade_charts(trades, result.frames, out_dir, candle_minutes)
    page = html_report.build_html(summary, trades, (out_dir / "equity.png").read_bytes(), coverage, chart_paths, chart_errors)
    (out_dir / "report.html").write_text(page, encoding="utf-8")
    if chart_errors:
        failures = [f"  trade {i}: {e}" for i, e in sorted(chart_errors.items())]
        text += "\n\nCHART FAILURES\n" + "\n".join(failures)
        report.write_text(out_dir / "summary.txt", text)

    print(text)
    print(f"\nWrote {out_dir}")
    return 0


def main() -> None:
    sys.exit(run(sys.argv[1:]))


if __name__ == "__main__":
    main()
