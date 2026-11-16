"""
The simulated account: cash rule, shorts as collateral, carry-over across days,
same-minute ordering, open trades at period end, and the books-balance identity.
All on synthetic November 2024 sessions (see tests/backtest_fixtures.py).
"""
from datetime import date

import numpy as np
import pytest

from marketscanner.backtest.engine import (
    AccountConfig, LOSS, OPEN, SKIPPED, WIN, find_exits, pnl_for, run_engine,
)
from marketscanner.backtest.equity import build_equity
from marketscanner.backtest.ideas import generate_ideas
from marketscanner.backtest.strategy import OrbStrategy
from marketscanner.data.bar_store import InMemoryBarStore
from tests.backtest_fixtures import make_session, quiet, stack
from tests.orb_fixtures import utc

D = {n: date(2024, 11, n) for n in range(4, 16)}
BIG = AccountConfig(initial_cash=1_000_000.0, per_deal=1000.0, min_cash=0.0)
SHARES = 1000.0 / 581.2


def pipeline(frames, start, end, cfg=BIG, order=None):
    store = InMemoryBarStore()
    for sym, f in frames.items():
        store.upsert_bars(sym, f)
    symbols = order or list(frames)
    res = generate_ideas(store, OrbStrategy(), symbols, start, end)
    exits = find_exits(res.ideas, res.frames)
    trades = run_engine(res.ideas, exits, res.frames, cfg, symbols)
    return res, trades


def one(frames, start, end, cfg=BIG):
    _, trades = pipeline(frames, start, end, cfg)
    assert len(trades) == 1
    return trades[0]


class TestSingleTrades:
    def test_buy_hits_take_profit(self):
        t = one({"SPY": make_session("2024-11-06", "UP", {100: (581.2, 587.0, 581.0, 586.0)})}, D[6], D[6])
        assert (t.status, t.exit_kind, t.exit_price) == (WIN, "TP", 586.7)
        assert t.amount == 1000.0 and t.shares == pytest.approx(SHARES)
        assert t.pnl == pytest.approx(SHARES * 5.5) and t.r_multiple == pytest.approx(1.0)
        assert t.pnl_pct == pytest.approx(t.pnl / 1000.0)
        assert t.opened_at == utc(2024, 11, 6, 15, 35) and t.closed_at == utc(2024, 11, 6, 16, 10)
        assert t.trading_minutes == 35 and t.sessions_held == 0

    def test_buy_hits_stop(self):
        t = one({"SPY": make_session("2024-11-06", "UP", {100: (581.2, 581.3, 575.0, 576.0)})}, D[6], D[6])
        assert (t.status, t.exit_kind, t.exit_price) == (LOSS, "SL", 575.7)
        assert t.pnl == pytest.approx(-SHARES * 5.5) and t.r_multiple == pytest.approx(-1.0)

    def test_both_levels_in_one_bar_is_a_loss(self):
        t = one({"SPY": make_session("2024-11-06", "UP", {100: (581.2, 587.0, 575.0, 581.0)})}, D[6], D[6])
        assert t.status == LOSS

    def test_sell_is_the_mirror_of_a_buy(self):
        win = one({"SPY": make_session("2024-11-06", "DOWN", {100: (573.2, 573.3, 567.0, 568.0)})}, D[6], D[6])
        shares = 1000.0 / 573.2
        assert (win.side, win.status, win.exit_price) == ("SELL", WIN, 567.7)
        assert win.pnl == pytest.approx(shares * 5.5)
        loss = one({"SPY": make_session("2024-11-06", "DOWN", {100: (573.2, 579.0, 573.0, 578.0)})}, D[6], D[6])
        assert (loss.status, loss.exit_price) == (LOSS, 578.7) and loss.pnl == pytest.approx(-shares * 5.5)

    def test_pnl_for_helper(self):
        assert pnl_for("BUY", 2.0, 100.0, 103.0) == 6.0 and pnl_for("SELL", 2.0, 100.0, 103.0) == -6.0

    def test_still_open_at_period_end_is_marked_not_counted(self):
        t = one({"SPY": make_session("2024-11-06", "UP", {389: (581.2, 583.0, 581.2, 583.0)})}, D[6], D[6])
        assert t.status == OPEN and t.closed_at is None and t.pnl is None
        assert t.mark_price == 583.0 and t.unrealised == pytest.approx(SHARES * (583.0 - 581.2))
        assert t.sessions_held == 0 and t.trading_minutes == 390 - 65

    def test_no_ideas_no_trades(self):
        assert pipeline({"SPY": make_session("2024-11-06", None)}, D[6], D[6])[1] == []


class TestCarryOver:
    def test_gap_stop_on_the_next_morning(self):
        frames = {"SPY": stack(make_session("2024-11-06", "UP"), quiet("2024-11-07", {0: (570.0, 571.0, 569.0, 570.0)}))}
        t = one(frames, D[6], D[7])
        assert (t.status, t.exit_kind, t.exit_price) == (LOSS, "GAP_SL", 570.0)
        assert t.closed_at == utc(2024, 11, 7, 14, 30) and t.sessions_held == 1
        assert t.trading_minutes == (390 - 65) and t.r_multiple == pytest.approx((570.0 - 581.2) / 5.5)

    def test_survives_two_nights_then_gaps_through_the_target(self):
        frames = {"SPY": stack(make_session("2024-11-06", "UP"), quiet("2024-11-07"), quiet("2024-11-08", {0: (590.0, 591.0, 589.0, 590.0)}))}
        t = one(frames, D[6], D[8])
        assert (t.status, t.exit_kind, t.exit_price) == (WIN, "GAP_TP", 590.0) and t.sessions_held == 2
        assert t.pnl == pytest.approx(SHARES * (590.0 - 581.2))

    def test_same_symbol_can_stack_across_days(self):
        # day 2: box 576-579 and a confirmation candle above it, while day 1's trade is still open
        day2 = make_session("2024-11-07", "UP", box_low=576.0, box_high=579.0)
        frames = {"SPY": stack(make_session("2024-11-06", "UP"), day2)}
        _, trades = pipeline(frames, D[6], D[7])
        assert len(trades) == 2 and [t.session_date for t in trades] == [D[6], D[7]]


class TestCashRule:
    def _three(self):
        return {s: make_session("2024-11-06", "UP") for s in ("SPY", "QQQ", "GLD")}

    def test_deal_opens_exactly_at_the_minimum(self):
        cfg = AccountConfig(initial_cash=2500.0, per_deal=1000.0, min_cash=500.0)
        _, trades = pipeline(self._three(), D[6], D[6], cfg, order=["SPY", "QQQ", "GLD"])
        assert [(t.symbol, t.status) for t in trades] == [("SPY", OPEN), ("QQQ", OPEN), ("GLD", SKIPPED)]
        assert trades[2].skip_reason == "cash" and trades[2].amount == 0.0 and trades[2].pnl is None

    def test_one_cent_short_skips(self):
        cfg = AccountConfig(initial_cash=2499.99, per_deal=1000.0, min_cash=500.0)
        _, trades = pipeline(self._three(), D[6], D[6], cfg, order=["SPY", "QQQ", "GLD"])
        assert [t.status for t in trades] == [OPEN, SKIPPED, SKIPPED]

    def test_simultaneous_signals_follow_market_order(self):
        cfg = AccountConfig(initial_cash=2500.0, per_deal=1000.0, min_cash=500.0)
        _, trades = pipeline(self._three(), D[6], D[6], cfg, order=["GLD", "QQQ", "SPY"])
        assert [(t.id, t.symbol, t.status) for t in trades] == [(1, "GLD", OPEN), (2, "QQQ", OPEN), (3, "SPY", SKIPPED)]

    def test_a_close_at_the_same_minute_frees_cash_for_an_open(self):
        # SPY enters 15:35 and takes profit at 16:10; QQQ's confirmation candle closes at 16:10.
        frames = {
            "SPY": make_session("2024-11-06", "UP", {100: (581.2, 587.0, 581.0, 586.0)}),
            "QQQ": make_session("2024-11-06", "UP", confirm_at=95),
        }
        cfg = AccountConfig(initial_cash=1500.0, per_deal=1000.0, min_cash=500.0)
        _, trades = pipeline(frames, D[6], D[6], cfg, order=["SPY", "QQQ"])
        assert [(t.symbol, t.status) for t in trades] == [("SPY", WIN), ("QQQ", OPEN)]
        assert trades[0].closed_at == trades[1].opened_at == utc(2024, 11, 6, 16, 10)

    def test_a_still_open_trade_keeps_blocking_cash(self):
        frames = {"SPY": make_session("2024-11-06", "UP"), "QQQ": make_session("2024-11-06", "UP", confirm_at=95)}
        cfg = AccountConfig(initial_cash=1500.0, per_deal=1000.0, min_cash=500.0)
        _, trades = pipeline(frames, D[6], D[6], cfg, order=["SPY", "QQQ"])
        assert [t.status for t in trades] == [OPEN, SKIPPED]

    def test_per_deal_larger_than_spendable_cash_never_opens(self):
        cfg = AccountConfig(initial_cash=1000.0, per_deal=1000.0, min_cash=1.0)
        _, trades = pipeline({"SPY": make_session("2024-11-06", "UP")}, D[6], D[6], cfg)
        assert trades[0].status == SKIPPED


class TestBooksBalance:
    def _mixed(self):
        frames = {
            "SPY": stack(make_session("2024-11-06", "UP", {100: (581.2, 587.0, 581.0, 586.0)}), quiet("2024-11-07")),
            "QQQ": stack(make_session("2024-11-06", "DOWN"), quiet("2024-11-07", {0: (590.0, 591.0, 589.0, 590.0)})),
            "GLD": stack(make_session("2024-11-06", "UP", {389: (581.2, 583.0, 581.2, 583.0)}), quiet("2024-11-07")),
        }
        cfg = AccountConfig(initial_cash=10_000.0, per_deal=1000.0, min_cash=2000.0)
        res, trades = pipeline(frames, D[6], D[7], cfg)
        return res, trades, cfg

    def test_identity_at_every_sample(self):
        res, trades, cfg = self._mixed()
        eq = build_equity(trades, res.frames, cfg)
        assert not eq.isna().any().any()
        np.testing.assert_allclose(eq["cash"] + eq["invested"] + eq["unrealised"], eq["equity"], atol=1e-6)

    def test_final_equity_is_initial_plus_realised_plus_unrealised(self):
        res, trades, cfg = self._mixed()
        eq = build_equity(trades, res.frames, cfg)
        realised = sum(t.pnl for t in trades if t.status in (WIN, LOSS))
        unrealised = sum(t.unrealised for t in trades if t.status == OPEN)
        assert eq["equity"].iloc[-1] == pytest.approx(cfg.initial_cash + realised + unrealised, abs=1e-6)
        # same entry minute, market order SPY, QQQ, GLD: SPY takes profit, QQQ (SELL) gaps through its stop, GLD stays open
        assert [(t.symbol, t.status) for t in trades] == [("SPY", WIN), ("QQQ", LOSS), ("GLD", OPEN)]

    def test_cash_never_goes_below_the_minimum(self):
        res, trades, cfg = self._mixed()
        eq = build_equity(trades, res.frames, cfg)
        assert (eq["cash"] >= cfg.min_cash - 1e-6).all()

    def test_end_state_has_no_invested_money_when_everything_closed(self):
        frames = {"SPY": make_session("2024-11-06", "UP", {100: (581.2, 587.0, 581.0, 586.0)})}
        cfg = AccountConfig(initial_cash=5000.0, per_deal=1000.0, min_cash=0.0)
        res, trades = pipeline(frames, D[6], D[6], cfg)
        eq = build_equity(trades, res.frames, cfg)
        assert eq["invested"].iloc[-1] == 0.0 and eq["cash"].iloc[-1] == pytest.approx(5000.0 + trades[0].pnl)
