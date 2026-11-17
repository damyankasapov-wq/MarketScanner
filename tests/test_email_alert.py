"""
Email builders: plain and HTML bodies carry every number of the proposal
table, the subject follows the documented format, and nothing is sent when
email is not configured. No SMTP connection is ever opened.
"""
import email
import email.policy
import logging
from datetime import date

import pytest

from marketscanner import config
from marketscanner.alerts import email_alert
from marketscanner.strategies import orb_session
from marketscanner.strategies.base import Signal
from marketscanner.ui.chart import render_chart
from tests.orb_fixtures import C_GREEN_ABOVE, ORB_BARS, TRAIL, df_of, utc

NOW = utc(2024, 11, 6, 15, 36, 10)     # 10:36:10 EST


@pytest.fixture
def snap():
    return orb_session.snapshot(df_of(ORB_BARS + C_GREEN_ABOVE + TRAIL), 5, now=NOW)


@pytest.fixture
def signal(snap):
    p, c = snap.proposal, snap.confirmation
    return Signal(
        market="SPY", strategy="OpeningRange", direction="UP", price=p.entry, fired_at=NOW,
        box_top=snap.box.top, box_bottom=snap.box.bottom,
        side=p.side, entry=p.entry, take_profit=p.take_profit, stop_loss=p.stop_loss,
        delta=p.delta, confirm_open=c.candle_open, confirm_close=c.candle_close,
        candle_minutes=5, session_date=date(2024, 11, 6),
    )


EXPECTED = [
    "BUY", "581.20", "586.70", "575.70", "5.50", "1 : 1",
    "10:30–10:35 ET", "5-min", "O 580.50", "H 581.50", "L 580.20", "C 581.20",
    "580.00 / 574.50", "2024-11-06 10:36:10 EST",
    "Entry (confirmation close)", "Take profit (entry + delta)", "Stop loss (entry − delta)",
    "Range delta", "Risk / reward", "Confirmation candle", "Box top / bottom", "Time sent (ET)",
    "Informational only",
]


def test_subject(signal):
    assert email_alert.build_subject(signal) == "[MarketScanner] SPY ORB BUY @ 581.20 · TP 586.70 · SL 575.70"


def test_plain_contains_every_number(signal, snap):
    body = email_alert.build_plain(signal, snap)
    for token in EXPECTED:
        assert token in body, token
    assert body.startswith("▲ SPY BUY @ 581.20")


def test_html_contains_every_number(signal, snap):
    body = email_alert.build_html(signal, snap, has_chart=True)
    for token in EXPECTED:
        assert token in body, token
    assert 'cid:chart' in body
    assert "#16a34a" in body            # green header band for BUY
    assert "▲ SPY BUY @ 581.20" in body


def test_sell_formulas_and_colour(signal, snap):
    sell = Signal(**{**signal.__dict__, "direction": "DOWN", "side": "SELL"})
    rows = dict(email_alert.build_rows(sell, snap))
    assert "Take profit (entry − delta)" in rows and "Stop loss (entry + delta)" in rows
    html = email_alert.build_html(sell, snap)
    assert "#dc2626" in html and "▼ SPY SELL" in html


def test_rows_order(signal, snap):
    labels = [label for label, _ in email_alert.build_rows(signal, snap)]
    assert labels == [
        "Side", "Entry (confirmation close)", "Take profit (entry + delta)",
        "Stop loss (entry − delta)", "Range delta", "Risk / reward",
        "Confirmation candle", "Box top / bottom", "Time sent (ET)",
    ]


def test_without_snapshot_still_builds(signal):
    body = email_alert.build_plain(signal, None)
    assert "10:30–10:35 ET, 5-min" in body and "O 580.50" not in body


def test_message_has_plain_html_and_inline_chart(signal, snap):
    fig = render_chart(snap, "SPY")
    msg = email_alert.build_message(signal, snap, fig, sender="a@example.com", recipient="b@example.com")
    parsed = email.message_from_string(msg.as_string(), policy=email.policy.default)
    types = [part.get_content_type() for part in parsed.walk()]
    assert "text/plain" in types and "text/html" in types and "image/png" in types
    assert parsed["Subject"].startswith("[MarketScanner] SPY ORB BUY")
    img = next(p for p in parsed.walk() if p.get_content_type() == "image/png")
    assert img["Content-ID"] == "<chart>"
    assert len(img.get_payload(decode=True)) > 1_000


def test_send_alert_skips_when_unconfigured(signal, snap, monkeypatch, caplog):
    monkeypatch.setitem(config.EMAIL, "sender", "")
    monkeypatch.setattr(email_alert.smtplib, "SMTP", lambda *a, **k: pytest.fail("SMTP must not be used"))
    with caplog.at_level(logging.WARNING, logger="marketscanner.alerts.email_alert"):
        email_alert.send_alert(signal, snap)
    assert any("not configured" in r.getMessage() for r in caplog.records)


def test_send_alert_uses_smtp_when_configured(signal, snap, monkeypatch):
    monkeypatch.setitem(config.EMAIL, "sender", "a@example.com")
    monkeypatch.setitem(config.EMAIL, "password", "pw")
    monkeypatch.setitem(config.EMAIL, "recipient", "b@example.com")
    sent = {}

    class FakeSMTP:
        def __init__(self, host, port): sent["host"] = (host, port)
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def ehlo(self): pass
        def starttls(self): pass
        def login(self, u, p): sent["login"] = u
        def sendmail(self, frm, to, body): sent["to"] = to; sent["body"] = body

    monkeypatch.setattr(email_alert.smtplib, "SMTP", FakeSMTP)
    email_alert.send_alert(signal, snap, chart_figure=render_chart(snap, "SPY"))
    assert sent["to"] == ["b@example.com"]
    parsed = email.message_from_string(sent["body"], policy=email.policy.default)
    assert parsed["Subject"] == "[MarketScanner] SPY ORB BUY @ 581.20 · TP 586.70 · SL 575.70"
    assert "ORB BUY" in parsed["Subject"]


def test_send_alert_multiple_recipients(signal, snap, monkeypatch):
    monkeypatch.setitem(config.EMAIL, "sender", "a@example.com")
    monkeypatch.setitem(config.EMAIL, "password", "pw")
    monkeypatch.setitem(config.EMAIL, "recipient", "b@example.com, andrej.bele@gmail.com")
    sent = {}

    class FakeSMTP:
        def __init__(self, host, port): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def ehlo(self): pass
        def starttls(self): pass
        def login(self, u, p): pass
        def sendmail(self, frm, to, body): sent["to"] = to; sent["body"] = body

    monkeypatch.setattr(email_alert.smtplib, "SMTP", FakeSMTP)
    email_alert.send_alert(signal, snap)
    assert sent["to"] == ["b@example.com", "andrej.bele@gmail.com"]
    parsed = email.message_from_string(sent["body"], policy=email.policy.default)
    assert parsed["To"] == "b@example.com, andrej.bele@gmail.com"
