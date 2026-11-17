"""
Test session setup.

- `marketscanner.config` no longer needs FINNHUB_API_KEY at import (only
  run_live() in auto mode does), so `pytest -q` runs from a clean checkout with
  no .env and no placeholder key.
- `tests/manual/` holds scripts that hit the network and send real email; they
  are run by hand and must never be collected.
"""
collect_ignore_glob = ["manual/*"]

import pytest


@pytest.fixture(autouse=True)
def _no_alpaca_keys(monkeypatch):
    """A developer's real Alpaca keys in .env must never make the suite hit the network."""
    from marketscanner import config
    monkeypatch.setattr(config, "ALPACA_API_KEY_ID", "")
    monkeypatch.setattr(config, "ALPACA_API_SECRET_KEY", "")
