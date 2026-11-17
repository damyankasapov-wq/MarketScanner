"""
FINNHUB_API_KEY is optional at import. Only run_live() in a Finnhub mode needs
it, and it fails fast with a clear message there.
"""
import importlib

import pytest

import main
from marketscanner import config


@pytest.fixture
def no_key(monkeypatch):
    monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
    import dotenv
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: False)   # do not re-read a local .env
    yield
    monkeypatch.undo()
    importlib.reload(config)


def test_config_imports_without_key(no_key):
    importlib.reload(config)
    assert config.FINNHUB_API_KEY == ""


def test_run_live_auto_mode_without_key_fails_fast(monkeypatch):
    monkeypatch.setattr(main.config, "FEED_MODE", "auto")
    monkeypatch.setattr(main.config, "FINNHUB_API_KEY", "")
    monkeypatch.setattr(main, "init_db", lambda: pytest.fail("init_db must not run before the key check"))
    with pytest.raises(RuntimeError, match="FINNHUB_API_KEY"):
        main.run_live()


class _Stop(Exception):
    pass


def test_run_live_yfinance_mode_needs_no_key(monkeypatch):
    started = {}

    class FakeFeed:
        def __init__(self, symbols, on_bar):
            started["symbols"] = symbols
        def start(self):
            started["feed"] = True

    monkeypatch.setattr(main.config, "FEED_MODE", "yfinance")
    monkeypatch.setattr(main.config, "FINNHUB_API_KEY", "")
    monkeypatch.setattr(main, "init_db", lambda: None)
    monkeypatch.setattr(main, "FinnhubFeed", FakeFeed)
    monkeypatch.setattr(main, "start_web_server", lambda **kw: None)
    monkeypatch.setattr(main.threading, "Thread", lambda *a, **k: type("T", (), {"start": lambda self: None})())

    def stop(_):
        raise _Stop

    monkeypatch.setattr(main.time, "sleep", stop)
    with pytest.raises(_Stop):
        main.run_live()
    assert started["feed"] is True
