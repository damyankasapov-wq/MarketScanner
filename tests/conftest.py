"""
Test session setup.

- `marketscanner.config` reads FINNHUB_API_KEY at import and refuses to start
  without it. The unit tests never talk to Finnhub, so a placeholder is set
  here (only when the environment has none) so `pytest -q` runs from a clean
  checkout with no .env.
- `tests/manual/` holds scripts that hit the network and send real email; they
  are run by hand and must never be collected.
"""
import os

os.environ.setdefault("FINNHUB_API_KEY", "test-not-used")

collect_ignore_glob = ["manual/*"]
