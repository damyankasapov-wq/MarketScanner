"""
Live ORB settings: REWARD_RATIO and CONFIRM_CUTOFF_ET.

Unset (the default) must leave every live path exactly as before; set, they
move the take profit, drop late confirmations, and every display, the email,
the Signal and the signals row say so. The backtester's plain ideas never
depend on this machine's .env.
"""
import logging
import math
from datetime import date, time

import pytest

import main
from marketscanner import config
from marketscanner.alerts import email_alert
from marketscanner.backtest.strategy import OrbSettings, OrbStrategy
from marketscanner.state import store
from marketscanner.strategies import orb_session
from marketscanner.strategies.base import Signal
from marketscanner.strategies.opening_range import OpeningRangeStrategy
from marketscanner.web import server
from tests.orb_fixtures import C_GREEN_ABOVE, ORB_BARS, TRAIL, bar, df_of, utc

NOW = utc(2024, 11, 6, 15, 36, 10)          # 10:36:10 ET; confirmation candle 10:30-10:35 ET
DF = df_of(ORB_BARS + C_GREEN_ABOVE + TRAIL)
# Rises to 590.00 at 10:40 ET: past 1.5R (589.45) and 1R (586.70).
DF_UP = df_of(ORB_BARS + C_GREEN_ABOVE + TRAIL + [bar("2024-11-06 15:40:00+00:00", 586.0, 590.0, 585.9, 589.8)])
LATE = utc(2024, 11, 6, 17, 0)


@pytest.fixture
def unset(monkeypatch):
    monkeypatch.setattr(config, "REWARD_RATIO", 1.0)
    monkeypatch.setattr(config, "CONFIRM_CUTOFF_ET", None)


# ── config ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("bad", [0.0, -1.0, math.inf, math.nan])
def test_reward_ratio_must_be_finite_and_positive(bad):
    with pytest.raises(ValueError, match="REWARD_RATIO"):
        config._validate_reward_ratio(bad)
    assert config._validate_reward_ratio(1.5) == 1.5


@pytest.mark.parametrize("text, want", [(None, None), ("", None), ("none", None), (" 14:00 ", time(14, 0)),
                                        ("16:00", time(16, 0)), ("10:31", time(10, 31))])
def test_parse_cutoff(text, want):
    assert config.parse_cutoff(text) == want


@pytest.mark.parametrize("text, msg", [("10:30", "after 10:30"), ("16:01", "no later than 16:00"),
                                       ("noon", "HH:MM"), ("25:00", "HH:MM")])
def test_parse_cutoff_rejects(text, msg):
    with pytest.raises(ValueError, match=msg):
        config.parse_cutoff(text)


# ── snapshot ──────────────────────────────────────────────────────────────────

def test_unset_is_bit_identical_to_explicit_one_to_one(unset):
    a = orb_session.snapshot(DF_UP, 5, now=LATE)
    b = orb_session.snapshot(DF_UP, 5, now=LATE, reward_ratio=1.0, cutoff=None)
    assert a.proposal == b.proposal and a.outcome == b.outcome
    assert a.proposal.take_profit == 581.2 + 5.5 and a.proposal.reward_ratio == 1.0
    assert a.outcome.r_multiple == 1.0 and a.late_confirmation is None and a.cutoff is None


def test_reward_moves_only_the_target_and_the_r_of_a_win(unset):
    s = orb_session.snapshot(DF_UP, 5, now=LATE, reward_ratio=1.5)
    p = s.proposal
    assert p.take_profit == 581.2 + 1.5 * 5.5 and p.stop_loss == 581.2 - 5.5 and p.delta == 5.5
    assert p.reward_ratio == 1.5
    assert s.outcome.status == "TP_HIT" and s.outcome.r_multiple == 1.5


def test_unset_reads_config_at_call_time(monkeypatch):
    monkeypatch.setattr(config, "REWARD_RATIO", 2.0)
    monkeypatch.setattr(config, "CONFIRM_CUTOFF_ET", None)
    assert orb_session.snapshot(DF, 5, now=NOW).proposal.take_profit == 581.2 + 2.0 * 5.5
    monkeypatch.setattr(config, "CONFIRM_CUTOFF_ET", time(10, 34))
    late = orb_session.snapshot(DF, 5, now=NOW)
    assert late.proposal is None and late.confirmation is None and late.outcome is None
    assert late.late_confirmation.candle_close == utc(2024, 11, 6, 15, 35) and late.cutoff == time(10, 34)
    # an explicit None overrides the .env cut-off
    assert orb_session.snapshot(DF, 5, now=NOW, cutoff=None).proposal is not None


def test_a_confirmation_closing_exactly_at_the_cutoff_is_kept(unset):
    s = orb_session.snapshot(DF, 5, now=NOW, cutoff=time(10, 35))
    assert s.proposal is not None and s.late_confirmation is None and s.cutoff == time(10, 35)


@pytest.mark.parametrize("settings", [
    OrbSettings(5, 1.5), OrbSettings(5, 2.0, time(10, 35)), OrbSettings(5, 1.0, time(10, 34)), OrbSettings(5),
])
def test_live_snapshot_equals_the_backtesters_derive(settings, unset):
    """The parity that lets a sweep row stand for what live alerts would have done."""
    live = orb_session.snapshot(DF, 5, now=LATE, reward_ratio=settings.reward_ratio, cutoff=settings.cutoff)
    idea = OrbStrategy(settings=settings).propose("SPY", DF)
    if idea is None:
        assert live.proposal is None and live.late_confirmation is not None
    else:
        p = live.proposal
        assert (idea.take_profit, idea.stop_loss, idea.entry, idea.delta) == (p.take_profit, p.stop_loss, p.entry, p.delta)


# ── backtester stays independent of this machine's .env for plain ideas ──────

def test_plain_ideas_ignore_the_env_but_defaults_follow_it(monkeypatch):
    monkeypatch.setattr(config, "REWARD_RATIO", 2.0)
    monkeypatch.setattr(config, "CONFIRM_CUTOFF_ET", time(10, 34))
    plain = OrbStrategy(5).plain_idea("SPY", DF)
    assert plain is not None and plain.take_profit == 581.2 + 5.5
    assert OrbSettings.current() == OrbSettings(config.CONFIRM_CANDLE_MINUTES, 2.0, time(10, 34))
    assert OrbStrategy(5).settings == OrbSettings(5, 2.0, time(10, 34))
    assert OrbStrategy(5).propose("SPY", DF) is None          # the .env cut-off applies by default


# ── displays ──────────────────────────────────────────────────────────────────

def test_dashboard_and_replay_say_why_there_is_no_trade(unset):
    s = orb_session.snapshot(DF, 5, now=NOW, cutoff=time(10, 34))
    assert server.status_line(s) == "confirmation 10:35 after cut-off 10:34, no trade today"
    payload = server.status_payload(s)
    assert payload["late_confirmation_ts"] == "2024-11-06T15:35:00+00:00" and payload["cutoff"] == "10:34"
    assert payload["proposal"] is None
    assert "confirmation 10:35 ET after cut-off 10:34, no trade" in main.describe_snapshot(s)


def test_dashboard_and_replay_show_the_real_r(unset):
    s = orb_session.snapshot(DF_UP, 5, now=LATE, reward_ratio=1.5)
    assert server.status_line(s).endswith("TP hit 10:40 (+1.5R)")
    payload = server.status_payload(s)
    assert payload["proposal"]["reward_ratio"] == 1.5 and payload["late_confirmation_ts"] is None
    assert payload["cutoff"] is None
    assert "(589.45, +1.5R)" in main.describe_snapshot(s)
    base = orb_session.snapshot(DF_UP, 5, now=LATE)
    assert server.status_line(base).endswith("(+1R)") and "+1R)" in main.describe_snapshot(base)


def _signal(snap, reward=1.0, cutoff=None):
    p, c = snap.proposal, snap.confirmation
    return Signal(market="SPY", strategy="OpeningRange", direction="UP", price=p.entry, fired_at=NOW,
                  box_top=snap.box.top, box_bottom=snap.box.bottom, side=p.side, entry=p.entry,
                  take_profit=p.take_profit, stop_loss=p.stop_loss, delta=p.delta, confirm_open=c.candle_open,
                  confirm_close=c.candle_close, candle_minutes=5, session_date=date(2024, 11, 6),
                  reward_ratio=reward, cutoff_et=cutoff)


def test_email_states_the_reward(unset):
    snap = orb_session.snapshot(DF, 5, now=NOW, reward_ratio=1.5)
    rows = dict(email_alert.build_rows(_signal(snap, 1.5), snap))
    assert rows["Risk / reward"] == "1 : 1.5"
    assert rows["Take profit (entry + 1.5 × delta)"] == "589.45"
    plain = dict(email_alert.build_rows(_signal(orb_session.snapshot(DF, 5, now=NOW))))
    assert plain["Risk / reward"] == "1 : 1" and "Take profit (entry + delta)" in plain


# ── live strategy ─────────────────────────────────────────────────────────────

def test_live_signal_carries_the_settings(monkeypatch):
    monkeypatch.setattr(config, "REWARD_RATIO", 1.5)
    monkeypatch.setattr(config, "CONFIRM_CUTOFF_ET", time(14, 0))
    sig = OpeningRangeStrategy("SPY").check(DF, {}, now=NOW)
    assert sig.take_profit == 581.2 + 1.5 * 5.5 and sig.reward_ratio == 1.5 and sig.cutoff_et == time(14, 0)


def test_live_default_signal_is_one_to_one(unset):
    sig = OpeningRangeStrategy("SPY").check(DF, {}, now=NOW)
    assert sig.take_profit == 581.2 + 5.5 and sig.reward_ratio == 1.0 and sig.cutoff_et is None


def test_a_late_confirmation_never_alerts_and_is_logged_once(monkeypatch, caplog):
    monkeypatch.setattr(config, "REWARD_RATIO", 1.0)
    monkeypatch.setattr(config, "CONFIRM_CUTOFF_ET", time(10, 34))
    strat = OpeningRangeStrategy("SPY")
    with caplog.at_level(logging.INFO):
        assert strat.check(DF, {}, now=NOW) is None
        assert strat.check(DF, {}, now=NOW) is None
    msgs = [r.getMessage() for r in caplog.records if "after the 10:34 cut-off" in r.getMessage()]
    assert len(msgs) == 1 and "10:35 ET" in msgs[0]


# ── signals table ─────────────────────────────────────────────────────────────

def test_log_signal_writes_the_settings(monkeypatch, unset):
    seen = {}

    class Cur:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params):
            seen["sql"], seen["params"] = sql, params

    class Conn:
        def cursor(self):
            return Cur()

        def commit(self):
            seen["commit"] = True

    monkeypatch.setattr(store, "_get_conn", lambda: Conn())
    monkeypatch.setattr(store, "_put_conn", lambda c: None)
    snap = orb_session.snapshot(DF, 5, now=NOW, reward_ratio=1.5)
    store.log_signal(_signal(snap, 1.5, time(14, 0)))
    assert seen["sql"].count("%s") == len(seen["params"]) == 18
    assert "reward_ratio, cutoff_et" in seen["sql"] and seen["params"][-2:] == (1.5, time(14, 0))
    assert "ADD COLUMN IF NOT EXISTS reward_ratio" in store._DDL and "ADD COLUMN IF NOT EXISTS cutoff_et" in store._DDL
