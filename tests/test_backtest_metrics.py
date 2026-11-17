"""Summary numbers, warnings and the text report, from hand-built trades with known answers."""
from datetime import date

import pandas as pd
import pytest

from marketscanner.backtest.engine import AccountConfig, LOSS, OPEN, SKIPPED, Trade, WIN
from marketscanner.backtest.ideas import SessionInfo
from marketscanner.backtest.metrics import (
    Coverage, MIN_TRUSTWORTHY_TRADES, format_coverage, format_summary, summarize,
)
from marketscanner.data.backfill import BackfillReport
from tests.orb_fixtures import utc

CFG = AccountConfig(initial_cash=10_000.0, per_deal=1000.0, min_cash=2000.0)
START, END = date(2024, 11, 6), date(2024, 11, 8)


def T(i, sym, status, pnl=None, unreal=None, minutes=None, sessions=None, r=None):
    skipped = status == SKIPPED
    return Trade(
        id=i, symbol=sym, side="BUY", signal_ts=utc(2024, 11, 6, 15, 35), session_date=START,
        entry=100.0, take_profit=105.0, stop_loss=95.0, status=status,
        amount=0.0 if skipped else 1000.0, shares=0.0 if skipped else 10.0, risk=0.0 if skipped else 50.0,
        pnl=pnl, r_multiple=r, trading_minutes=minutes, sessions_held=sessions, unrealised=unreal,
        skip_reason="cash" if skipped else None,
    )


def _equity(values):
    idx = pd.date_range("2024-11-06 14:30", periods=len(values), freq="5min", tz="UTC")
    return pd.DataFrame({"cash": values, "invested": 0.0, "unrealised": 0.0, "equity": values}, index=idx)


def _summary(trades, equity=None, cov=None, symbols=("SPY", "QQQ")):
    return summarize(trades, equity if equity is not None else _equity([10_000.0, 10_028.0]), CFG, "ORB",
                     START, END, list(symbols), cov)


MIXED = [
    T(1, "SPY", WIN, pnl=10.0, minutes=60, sessions=0, r=1.0),
    T(2, "SPY", WIN, pnl=20.0, minutes=120, sessions=1, r=2.0),
    T(3, "QQQ", LOSS, pnl=-5.0, minutes=30, sessions=0, r=-0.5),
    T(4, "QQQ", OPEN, unreal=3.0, minutes=200, sessions=1),
    T(5, "QQQ", SKIPPED),
]


def test_core_numbers():
    s = _summary(MIXED)
    assert (s.signals, s.opened, s.skipped, s.closed, s.open, s.wins, s.losses) == (5, 4, 1, 3, 1, 2, 1)
    assert s.realised_pnl == 25.0 and s.unrealised_pnl == 3.0
    assert s.final_equity == 10_028.0 and s.net_pnl == 28.0 and s.return_pct == pytest.approx(0.28)
    assert s.win_rate == pytest.approx(200 / 3)                     # open and skipped excluded
    assert (s.avg_win, s.avg_loss) == (15.0, -5.0) and s.profit_factor == pytest.approx(6.0)
    assert (s.best_trade, s.worst_trade) == (20.0, -5.0)
    assert s.avg_r == pytest.approx((1.0 + 2.0 - 0.5) / 3)
    assert s.avg_trading_minutes == pytest.approx(70.0) and s.avg_sessions_held == pytest.approx(1 / 3)


def test_by_symbol():
    s = _summary(MIXED)
    assert s.by_symbol["SPY"] == {"signals": 2, "wins": 2, "losses": 0, "open": 0, "skipped": 0, "pnl": 30.0}
    assert s.by_symbol["QQQ"] == {"signals": 3, "wins": 0, "losses": 1, "open": 1, "skipped": 1, "pnl": -2.0}


def test_no_losses_means_no_profit_factor():
    s = _summary([T(1, "SPY", WIN, pnl=10.0, minutes=1, sessions=0, r=1.0)])
    assert s.profit_factor is None and "n/a (no losses)" in format_summary(s)


def test_zero_trades_does_not_crash():
    s = _summary([], equity=_equity([10_000.0, 10_000.0]))
    assert s.win_rate is None and s.avg_win is None and s.net_pnl == 0.0 and s.best_trade is None
    text = format_summary(s)
    assert "Closed trades       0" in text and "n/a" in text
    assert any("Only 0 closed trades" in w for w in s.warnings)


def test_empty_equity_curve_does_not_crash():
    s = _summary([], equity=pd.DataFrame(columns=["cash", "invested", "unrealised", "equity"]))
    assert s.max_drawdown == 0.0 and s.avg_invested_pct == 0.0


def test_warnings():
    s = _summary(MIXED, cov=Coverage(sessions=[SessionInfo("SPY", START, 300, 40, "skipped_opening_hole", 0)]))
    joined = " | ".join(s.warnings)
    assert f"Only 3 closed trades: below ~{MIN_TRUSTWORTHY_TRADES}" in joined
    assert "1 signal(s) skipped" in joined and "1 trade(s) still open" in joined and "1 session(s) skipped" in joined


def test_no_trade_count_warning_at_the_threshold():
    trades = [T(i, "SPY", WIN, pnl=1.0, minutes=1, sessions=0, r=1.0) for i in range(MIN_TRUSTWORTHY_TRADES)]
    assert not any("Only" in w for w in _summary(trades).warnings)


def test_drawdown_and_average_invested():
    eq = _equity([100.0, 110.0, 90.0, 95.0])
    eq["invested"] = [0.0, 50.0, 50.0, 0.0]
    s = _summary([], equity=eq)
    assert s.max_drawdown == pytest.approx(-20.0) and s.max_drawdown_pct == pytest.approx(-20 / 110 * 100)
    assert s.avg_invested_pct == pytest.approx((0 + 50 / 110 + 50 / 90 + 0) / 4 * 100)


def test_summary_text_has_every_headline_number():
    text = format_summary(_summary(MIXED))
    for needle in ["STRATEGY CHECK: ORB", "2024-11-06 to 2024-11-08", "SPY, QQQ", "10,000.00", "1,000.00 per deal",
                   "minimum cash 2,000.00", "Final equity        10,028.00", "+0.28%", "Signals 5: opened 4, skipped 1",
                   "Closed trades       3  (wins 2, losses 1)", "Still open          1", "66.7%", "15.00 / -5.00",
                   "6.00", "BY MARKET", "WARNINGS", "Informational only"]:
        assert needle in text, needle


def test_coverage_block_lists_the_problems():
    cov = Coverage(
        sessions=[
            SessionInfo("SPY", date(2024, 11, 6), 390, 60, "ok", 0),
            SessionInfo("SPY", date(2024, 11, 7), 289, 60, "ok", 1),
            SessionInfo("SPY", date(2024, 11, 8), 300, 40, "skipped_opening_hole", 0),
            SessionInfo("SPY", date(2024, 11, 11), 0, 0, "no_data", 0),
        ],
        backfill=BackfillReport(fetched=[("SPY", date(2024, 11, 6), 390)],
                                unavailable=[("SPY", date(2024, 10, 1), "older than yfinance's 30-day window and not archived")],
                                incomplete=[("SPY", date(2024, 11, 7), 289)]),
    )
    text = format_coverage(cov)
    assert "SPY: 2 of 4 weekdays used" in text
    assert "skipped 2024-11-08: only 40 of 60 opening-hour bars" in text
    assert "no data 2024-11-11" in text and "2024-11-07: 289 bars, 1 gap(s) after 10:30" in text
    assert "fetched from yfinance this run: 1" in text and "unavailable SPY 2024-10-01" in text
    assert "incomplete SPY 2024-11-07: only 289 bars" in text
