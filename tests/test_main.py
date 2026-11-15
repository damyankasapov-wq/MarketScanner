"""
main._on_bar wiring and the replay command, with the store and email mocked.
No network, no Postgres.
"""
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest

import main
from marketscanner.strategies.opening_range import OpeningRangeStrategy
from tests.orb_fixtures import C_GREEN_ABOVE, ORB_BARS, TRAIL, bar, candle_bars, df_of, flat, utc

CONF_1040 = candle_bars("2024-11-06 15:40", 580.5, 581.5, 580.2, 581.2)   # 10:40-10:45 ET


@pytest.fixture
def wiring(monkeypatch):
    """Fresh strategy for SPY, store and email replaced by recorders."""
    calls = {"cooldown": [], "logged": [], "sent": []}
    monkeypatch.setitem(main._strategies, "SPY", OpeningRangeStrategy("SPY"))
    monkeypatch.setattr(main, "is_on_cooldown", lambda m, s: False)
    monkeypatch.setattr(main, "signal_exists", lambda m, s, d: False)
    monkeypatch.setattr(main, "set_cooldown", lambda m, s: calls["cooldown"].append((m, s)))
    monkeypatch.setattr(main, "log_signal", lambda sig: calls["logged"].append(sig))
    monkeypatch.setattr(main, "send_alert", lambda sig, snap, chart_figure=None:
                        calls["sent"].append((sig, snap, chart_figure)))
    return calls


def test_fresh_confirmation_sends_once(wiring):
    df = df_of(ORB_BARS + C_GREEN_ABOVE + TRAIL)
    now = utc(2024, 11, 6, 15, 36, 10)
    main._on_bar("SPY", df, now=now)
    main._on_bar("SPY", df, now=now + timedelta(minutes=1))
    assert len(wiring["sent"]) == 1
    sig, snap, fig = wiring["sent"][0]
    assert sig.side == "BUY" and sig.entry == pytest.approx(581.2)
    assert snap.proposal.entry == pytest.approx(581.2)
    assert fig is not None and fig._suptitle is not None
    assert wiring["logged"] == [sig] and wiring["cooldown"] == [("SPY", "OpeningRange")]


def test_freshness_guard_on_1_minute_bar_not_candle(wiring, monkeypatch):
    df = df_of(ORB_BARS + C_GREEN_ABOVE + TRAIL)
    checked = []
    strategy = main._strategies["SPY"]
    orig = strategy.check
    monkeypatch.setattr(strategy, "check", lambda *a, **k: checked.append(1) or orig(*a, **k))
    # newest 1-minute bar is 10:35; at 10:41 it is 6 min old → stale, strategy never consulted
    main._on_bar("SPY", df, now=utc(2024, 11, 6, 15, 41))
    assert checked == [] and wiring["sent"] == []
    # at 10:39:30 the 1-minute bar is 4.5 min old → fresh, even though the
    # confirmation candle's label (10:30) is 9.5 min old
    main._on_bar("SPY", df, now=utc(2024, 11, 6, 15, 39, 30))
    assert checked == [1] and len(wiring["sent"]) == 1


def test_missed_poll_still_emails(wiring):
    # frame jumps from 10:44 to 10:52 → the 10:45 confirmation still goes out
    df = df_of(ORB_BARS + CONF_1040 + flat("2024-11-06 15:52", 1, 582.0))
    main._on_bar("SPY", df, now=utc(2024, 11, 6, 15, 52, 40))
    assert len(wiring["sent"]) == 1
    assert wiring["sent"][0][0].confirm_close == utc(2024, 11, 6, 15, 45)


def test_restart_at_noon_does_not_resend(wiring):
    df = df_of(ORB_BARS + CONF_1040 + flat("2024-11-06 15:45", 76, 582.0))   # through 12:00 ET
    main._on_bar("SPY", df, now=utc(2024, 11, 6, 17, 0, 30))
    assert wiring["sent"] == []
    assert main._strategies["SPY"]._fired_session == date(2024, 11, 6)


def test_signal_exists_blocks_second_email(wiring, monkeypatch):
    monkeypatch.setattr(main, "signal_exists", lambda m, s, d: True)
    main._on_bar("SPY", df_of(ORB_BARS + C_GREEN_ABOVE + TRAIL), now=utc(2024, 11, 6, 15, 36))
    assert wiring["sent"] == [] and wiring["logged"] == []


def test_cooldown_blocks(wiring, monkeypatch):
    monkeypatch.setattr(main, "is_on_cooldown", lambda m, s: True)
    main._on_bar("SPY", df_of(ORB_BARS + C_GREEN_ABOVE + TRAIL), now=utc(2024, 11, 6, 15, 36))
    assert wiring["sent"] == []


def test_empty_frame_ignored(wiring):
    main._on_bar("SPY", pd.DataFrame())
    assert wiring["sent"] == []


class TestReplay:
    def test_replay_now_counts_last_candle(self):
        now = main.replay_now(date(2024, 11, 6))
        assert now == utc(2024, 11, 6, 21, 0) + timedelta(seconds=main.config.CANDLE_GRACE_SECONDS)

    def test_run_replay_writes_png(self, tmp_path, monkeypatch, caplog):
        monkeypatch.setattr(main, "REPLAY_DIR", tmp_path)
        frame = df_of(ORB_BARS + C_GREEN_ABOVE + TRAIL + [bar("2024-11-06 15:40:00+00:00", 586.0, 587.0, 585.9, 586.5)])
        seen = {}

        def fake_fetch(symbol, start, end):
            seen.update(symbol=symbol, start=start, end=end)
            return frame

        with caplog.at_level("INFO", logger="main"):
            out = main.run_replay("SPY", date(2024, 11, 6), fetch=fake_fetch)
        assert seen == {"symbol": "SPY", "start": date(2024, 11, 6), "end": date(2024, 11, 7)}
        assert out == tmp_path / "replay_SPY_2024-11-06.png"
        assert out.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
        text = " ".join(r.getMessage() for r in caplog.records)
        assert "BUY @ 581.20" in text and "TP_HIT" in text and "box 574.50–580.00" in text

    def test_run_replay_no_data(self, tmp_path, monkeypatch):
        monkeypatch.setattr(main, "REPLAY_DIR", tmp_path)
        assert main.run_replay("SPY", date(2024, 11, 6), fetch=lambda *a: pd.DataFrame()) is None
        assert list(Path(tmp_path).iterdir()) == []

    def test_cli_parsing(self):
        args = main._parse_args(["--replay", "spy", "2026-11-10"])
        assert args.replay == ["spy", "2026-11-10"] and args.backtest is None
        args = main._parse_args(["--backtest", "QQQ"])
        assert args.backtest == "QQQ" and args.replay is None
