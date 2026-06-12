# Changelog

All notable changes to MarketScanner are documented here.

## [0.1.3.0] - 2026-06-12

### Added
- Live candlestick chart server (Flask, port 8080) — dashboard auto-refreshes every 60 s,
  serves per-symbol OHLCV charts as PNG; health endpoint at `/health`

### Fixed
- **QQQ/GLD showed broken images**: `plt.subplots()` is not thread-safe under concurrent
  Flask requests; switched all chart renderers to the matplotlib OO API
  (`Figure` + `FigureCanvasAgg`)
- **No data / no email for a week**: Finnhub free tier connects successfully but sends no
  trade data for US ETFs; the WS-level exception counter never reached its threshold.
  Added in-band `type:error` message detection and a 5-minute market-hours stale-feed
  watchdog — both trigger immediate yfinance fallback
- **SPY placeholder styled; QQQ/GLD showed broken-image icon**: added a dark-themed
  "No data yet" placeholder PNG rendered server-side for symbols with no feed data
- **`addplot=None` crash**: mplfinance rejects `None` for `addplot`; kwarg is now omitted
  when there are no overlay plots
- **`TypeError: ufunc 'isnan' not supported`** (pandas 3.x): `pd.concat` no longer
  upcasts empty object-dtype frames to float64; `_bars` is now initialised with explicit
  `pd.Series(dtype="float64")` per column
- **yfinance MultiIndex columns**: added column-flattening and explicit `astype(float)`
  for all OHLCV columns after download

## [0.1.2.0] - 2026-06-06

### Changed
- Full solution now runs as a two-container Docker Compose stack (db + scanner)
  visible in OMV Services → Compose → Docker Files
- Scanner container uses headless matplotlib (Agg backend) — no DISPLAY needed;
  email alerts carry the inline chart image
- `scripts/deploy.sh`: removed screen session management; now builds Docker image
  and starts full stack; added `--logs` subcommand for `docker compose logs -f`
- OMV Compose plugin registration is idempotent — re-deploys update in place

### Added
- `Dockerfile`: `python:3.11-slim`-based image; sets `HEADLESS=1`; installs all
  Python deps via `requirements.txt`; persists backtest output via named volume
- `docker-compose.yml` scanner service: waits for db healthcheck before starting,
  injects `DB_HOST=db` so the scanner connects inside the Docker network

### Removed
- `TA-Lib` from `requirements.txt` — C library was listed but never imported

## [0.1.1.0] - 2026-06-06

### Changed
- Email alerts now use HTML with a colour-coded header banner (green for UP, red for DOWN),
  a signal details table, and the chart embedded inline — no download required to see the chart
- Plain-text fallback preserved for mail clients that strip HTML

### Added
- `tests/manual/test_email_backtest.py` — end-to-end smoke test that runs a GLD backtest
  and fires a real email alert; kept in git for manual re-verification, excluded from the
  automated regression suite

## [0.1.0.0] - 2026-06-06

### Added
- Opening Range Breakout (ORB) strategy for SPY, QQQ, and GLD — monitors 09:30–10:30 ET
  range, fires email alert when price closes above or below the box
- Finnhub WebSocket live feed with tick-to-OHLCV aggregation, exponential backoff
  reconnect, and yfinance fallback after 5 consecutive failures
- PostgreSQL state store (via Docker Compose) for cooldown tracking and signal history,
  restart-safe with `SimpleConnectionPool`
- Email alerts via Gmail SMTP with inline chart PNG attachment
- Live candlestick chart using mplfinance — ORB window shaded, box levels dotted,
  signal markers pinned to exact breakout bars
- Backtest mode (`python main.py --backtest SYMBOL`) — replays today's yfinance data
  and saves chart to `output/backtest/`
- `.env` configuration for all credentials (Finnhub API key, Gmail, PostgreSQL)
- 11 unit tests covering all ORB strategy state machine paths including exact-touch
  boundary, empty DataFrame, and BUILDING→WATCHING transition

### Changed
- Chart title now includes the data date (`SPY — Opening Range Breakout  2026-06-05`)
- Backtest charts saved to `output/backtest/` instead of project root
- `tmp/` directory available for transient files; both excluded from git
- DB default host updated to `openmediavault.local` in `.env.example`
