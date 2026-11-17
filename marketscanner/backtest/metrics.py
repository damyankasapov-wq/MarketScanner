"""
Summary numbers for a backtest run, and the text that prints them.

Win rate and the averages use CLOSED trades only; trades still open at the end of
the period are counted separately and never as wins or losses. Every report
prints its trade count and warns below MIN_TRUSTWORTHY_TRADES closed trades.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional

import pandas as pd

from marketscanner.backtest.engine import AccountConfig, LOSS, OPEN, SKIPPED, Trade, WIN
from marketscanner.backtest.equity import max_drawdown
from marketscanner.backtest.ideas import SessionInfo
from marketscanner.data.backfill import BackfillReport

MIN_TRUSTWORTHY_TRADES = 100


@dataclass
class Coverage:
    sessions: list[SessionInfo] = field(default_factory=list)
    backfill: Optional[BackfillReport] = None


@dataclass
class Summary:
    strategy: str
    start: date
    end: date
    symbols: list[str]
    initial_cash: float
    per_deal: float
    min_cash: float
    final_equity: float
    net_pnl: float
    return_pct: float
    realised_pnl: float
    unrealised_pnl: float
    signals: int
    opened: int
    skipped: int
    closed: int
    open: int
    wins: int
    losses: int
    win_rate: Optional[float]
    avg_win: Optional[float]
    avg_loss: Optional[float]
    profit_factor: Optional[float]
    avg_r: Optional[float]
    avg_trading_minutes: Optional[float]
    avg_sessions_held: Optional[float]
    best_trade: Optional[float]
    worst_trade: Optional[float]
    max_drawdown: float
    max_drawdown_pct: float
    avg_invested_pct: float
    by_symbol: dict
    warnings: list[str] = field(default_factory=list)


def _mean(xs: list[float]) -> Optional[float]:
    return float(sum(xs) / len(xs)) if xs else None


def summarize(
    trades: list[Trade],
    equity: pd.DataFrame,
    cfg: AccountConfig,
    strategy: str,
    start: date,
    end: date,
    symbols: list[str],
    coverage: Optional[Coverage] = None,
) -> Summary:
    opened = [t for t in trades if t.status != SKIPPED]
    closed = [t for t in opened if t.status in (WIN, LOSS)]
    still_open = [t for t in opened if t.status == OPEN]
    wins = [t for t in closed if t.status == WIN]
    losses = [t for t in closed if t.status == LOSS]
    realised = sum(t.pnl for t in closed)
    unrealised = sum(t.unrealised for t in still_open)
    gross_win = sum(t.pnl for t in wins)
    gross_loss = -sum(t.pnl for t in losses)
    final_equity = cfg.initial_cash + realised + unrealised
    dd, dd_pct = max_drawdown(equity["equity"]) if len(equity) else (0.0, 0.0)
    avg_invested_pct = float((equity["invested"] / equity["equity"]).mean() * 100) if len(equity) else 0.0

    by_symbol: dict = {}
    for sym in symbols:
        ts = [t for t in trades if t.symbol == sym]
        by_symbol[sym] = {
            "signals": len(ts),
            "wins": sum(t.status == WIN for t in ts),
            "losses": sum(t.status == LOSS for t in ts),
            "open": sum(t.status == OPEN for t in ts),
            "skipped": sum(t.status == SKIPPED for t in ts),
            "pnl": float(sum(t.pnl for t in ts if t.pnl is not None) + sum(t.unrealised for t in ts if t.unrealised is not None)),
        }

    warnings: list[str] = []
    if len(closed) < MIN_TRUSTWORTHY_TRADES:
        warnings.append(
            f"Only {len(closed)} closed trades: below ~{MIN_TRUSTWORTHY_TRADES}, win rate and profit are "
            "not statistically meaningful."
        )
    n_skipped = len(trades) - len(opened)
    if n_skipped:
        warnings.append(f"{n_skipped} signal(s) skipped because cash minus the deal amount would fall below the minimum cash.")
    if still_open:
        warnings.append(f"{len(still_open)} trade(s) still open at the end of the period (marked at the last close, not counted as wins or losses).")
    if coverage is not None:
        bad = [s for s in coverage.sessions if s.status == "skipped_opening_hole"]
        if bad:
            warnings.append(f"{len(bad)} session(s) skipped: opening hour had too few minute bars (see data coverage).")

    return Summary(
        strategy=strategy, start=start, end=end, symbols=symbols,
        initial_cash=cfg.initial_cash, per_deal=cfg.per_deal, min_cash=cfg.min_cash,
        final_equity=final_equity, net_pnl=final_equity - cfg.initial_cash,
        return_pct=(final_equity - cfg.initial_cash) / cfg.initial_cash * 100.0,
        realised_pnl=realised, unrealised_pnl=unrealised,
        signals=len(trades), opened=len(opened), skipped=n_skipped,
        closed=len(closed), open=len(still_open), wins=len(wins), losses=len(losses),
        win_rate=(len(wins) / len(closed) * 100.0) if closed else None,
        avg_win=_mean([t.pnl for t in wins]), avg_loss=_mean([t.pnl for t in losses]),
        profit_factor=(gross_win / gross_loss) if gross_loss > 0 else None,
        avg_r=_mean([t.r_multiple for t in closed if t.r_multiple is not None]),
        avg_trading_minutes=_mean([t.trading_minutes for t in closed]),
        avg_sessions_held=_mean([t.sessions_held for t in closed]),
        best_trade=max((t.pnl for t in closed), default=None),
        worst_trade=min((t.pnl for t in closed), default=None),
        max_drawdown=dd, max_drawdown_pct=dd_pct, avg_invested_pct=avg_invested_pct,
        by_symbol=by_symbol, warnings=warnings,
    )


def _money(x: Optional[float]) -> str:
    return "n/a" if x is None else f"{x:,.2f}"


def _pct(x: Optional[float]) -> str:
    return "n/a" if x is None else f"{x:.1f}%"


def _hold(minutes: Optional[float], sessions: Optional[float]) -> str:
    if minutes is None:
        return "n/a"
    h, m = divmod(int(round(minutes)), 60)
    return f"{h}h {m:02d}m of trading time, {sessions:.1f} sessions"


def format_coverage(cov: Coverage) -> str:
    lines = ["DATA COVERAGE"]
    by: dict[str, list[SessionInfo]] = {}
    for s in cov.sessions:
        by.setdefault(s.symbol, []).append(s)
    for sym, ss in by.items():
        ok = [s for s in ss if s.status == "ok"]
        lines.append(f"  {sym}: {len(ok)} of {len(ss)} weekdays used")
        for s in ss:
            if s.status == "skipped_opening_hole":
                lines.append(f"    skipped {s.date}: only {s.opening_bars} of 60 opening-hour bars")
            elif s.status == "no_data":
                lines.append(f"    no data {s.date} (holiday or not stored)")
            elif s.later_holes:
                lines.append(f"    {s.date}: {s.bars} bars, {s.later_holes} gap(s) after 10:30 (exits may fill late)")
    if cov.backfill is not None:
        bf = cov.backfill
        lines.append(f"  fetched from yfinance this run: {len(bf.fetched) - bf.deep_fetched} session(s)")
        if bf.deep_fetched:
            lines.append(f"  fetched from Alpaca this run: {bf.deep_fetched} session(s)")
        for sym, d, why in bf.unavailable:
            lines.append(f"    unavailable {sym} {d}: {why}")
        for sym, d, n in bf.incomplete:
            lines.append(f"    incomplete {sym} {d}: only {n} bars from the vendor")
    return "\n".join(lines)


def format_summary(s: Summary, cov: Optional[Coverage] = None) -> str:
    lines = [
        f"STRATEGY CHECK: {s.strategy}   {s.start} to {s.end}   markets {', '.join(s.symbols)}",
        f"Account: start {_money(s.initial_cash)}, {_money(s.per_deal)} per deal, minimum cash {_money(s.min_cash)}",
        "",
        f"Final equity        {_money(s.final_equity)}",
        f"Profit / loss       {_money(s.net_pnl)}  ({s.return_pct:+.2f}%)",
        f"  realised          {_money(s.realised_pnl)}",
        f"  unrealised (open) {_money(s.unrealised_pnl)}",
        "",
        f"Signals {s.signals}: opened {s.opened}, skipped {s.skipped}",
        f"Closed trades       {s.closed}  (wins {s.wins}, losses {s.losses})",
        f"Still open          {s.open}",
        f"Win rate            {_pct(s.win_rate)}  (closed trades only)",
        f"Average win / loss  {_money(s.avg_win)} / {_money(s.avg_loss)}",
        f"Profit factor       {'n/a (no losses)' if s.profit_factor is None else f'{s.profit_factor:.2f}'}",
        f"Average R           {'n/a' if s.avg_r is None else f'{s.avg_r:+.2f}R'}",
        f"Average time held   {_hold(s.avg_trading_minutes, s.avg_sessions_held)}",
        f"Best / worst trade  {_money(s.best_trade)} / {_money(s.worst_trade)}",
        f"Max drawdown        {_money(s.max_drawdown)}  ({s.max_drawdown_pct:.2f}% of peak equity)",
        f"Average invested    {s.avg_invested_pct:.1f}% of equity",
        "",
        "BY MARKET",
    ]
    for sym, d in s.by_symbol.items():
        lines.append(
            f"  {sym}: {d['signals']} signals, {d['wins']} wins, {d['losses']} losses, "
            f"{d['open']} open, {d['skipped']} skipped, P&L {_money(d['pnl'])}"
        )
    if s.warnings:
        lines += ["", "WARNINGS"] + [f"  ! {w}" for w in s.warnings]
    if cov is not None:
        lines += ["", format_coverage(cov)]
    lines += ["", "Informational only: a simulation on stored 1-minute bars, not an order and not a promise."]
    return "\n".join(lines)
