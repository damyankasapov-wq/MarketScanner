# Changelog

All notable changes to MarketScanner are documented here.

## [0.4.1.0] - 2026-11-18

### Changed
- **Multi-year backtests run in seconds where they took minutes.** On 10 years of 1-minute
  bars (about 1M per symbol), splitting the history into trading days drops from about
  3 minutes to 0.3 s, finding each trade's exit from about 3 minutes for 800 trades to
  1.5 s, and building the account curve from about 30 s to 0.3 s. Results are identical:
  the same trades, exits, ledger and equity curve as before.
- Exits are found by walking 1, 6, 26, 130 and then 650 sessions after the entry before the
  rest of the period, so a trade's cost tracks how long it was held. Overnight gap fills
  are unchanged, including a 15:55 confirmation whose first bar is the next day's 09:30.
- Trade charts read each trade's day from the history split once per run, instead of
  scanning the whole period for every chart.
- One shared module now decides which New York trading day a bar belongs to, for the
  backfill and the backtester alike. Bars without a timestamp are dropped, bars that arrive
  out of order are sorted, and a strategy gets its own copy of each day's bars.

## [0.4.0.0] - 2026-11-17

### Added
- **Years of history for backtests from Alpaca.** With a free Alpaca account
  (`ALPACA_API_KEY_ID` / `ALPACA_API_SECRET_KEY` in `.env`), sessions older than yfinance's
  30-day window are fetched from Alpaca (1-minute bars since 2016, full-market SIP feed,
  raw prices, regular hours only) and stored in the `bars` table. One request per calendar
  month per symbol; recent days still come from yfinance. Without keys nothing changes.
- **NYSE trading calendar for the backfill.** Holidays are never requested, every day is cut
  at its real open and close (half-days end at 13:00 ET, so after-hours prints never reach the
  archive), and a short day counts as complete at its real length.
- The coverage summary shows how many sessions came from yfinance and from Alpaca.

### Changed
- An old session fetched from Alpaca replaces what was stored for that day (one transaction),
  so a day never mixes two vendors.
- Alpaca fails fast: wrong keys, a missing data permission (401/403) or any feed other than
  `sip` stops Alpaca for the rest of the run, as do 2 failed months in a row. The summary says
  why, once.
- The coverage summary collapses runs of missing days into one line
  (`2026-05-01 to 2026-10-16 (121 sessions)`), merges runs across weekends and market holidays,
  lists them in date order, and wraps long lines in `report.html`.
- When the database can't be reached, the error shows the host, port and database it tried and,
  if there is no `.env` (for example in a git worktree), says so.

### Fixed
- Alpaca requests retry rate limits, server errors and network errors with a capped backoff,
  stop on a repeated page token, report non-JSON answers clearly and URL-encode the symbol.

## [0.3.0.0] - 2026-11-16

### Added
- **Strategy checker (backtester)**: `python -m marketscanner.backtest --from 2026-10-19 --to 2026-11-13 --markets SPY,QQQ,GLD --cash 10000 --per-deal 1000 --min-cash 2000`
  replays a strategy (ORB for now, behind a registry) over stored 1-minute bars on a
  simulated account and writes a run folder: `ledger.csv` (every trade: open time, amount,
  win/lose state, close time, profit or loss, R, minutes held, skipped signals with the
  reason), `equity.png` (cash available vs invested, and profit/loss over time),
  `summary.txt`, a one-page `report.html` and one chart per trade in `trades/`.
  - Account rules: fixed dollar amount per deal with fractional shares; a deal opens only if
    cash minus the amount stays at or above the minimum cash; SELL is a short that reserves
    the same amount as collateral.
  - Trades are followed across days until take profit or stop loss is touched (no forced close
    at 16:00). A stop or target that the market gaps through overnight fills at the next open,
    not at the level. Trades still open at the end of the period are marked at the last close
    and kept out of the win rate.
  - Summary: profit or loss, win rate, average win and loss, profit factor, average R,
    average time held, max drawdown, average invested, per-market table, a data-coverage block
    and a warning below 100 closed trades.
  - Sessions whose 09:30-10:30 ET window has fewer than 57 of 60 minute bars are skipped and
    listed, because a hole there would draw a false box.
- **Bars archive**: a new `bars` table (additive, created at start-up) holds 1-minute bars per
  symbol. After the midnight reset the scanner saves the day's frames on a separate thread from
  copies, so the archive can never delay or break the reset. Missing sessions are backfilled
  from yfinance (raw prices, one day per request, 30-day window). History now grows by itself
  instead of expiring after 30 days.
- `/health` reports `bars_last_archived` per symbol (cached for a minute, tolerant of a dead
  database).
- Opt-in real-database test: `MS_TEST_DB=1 pytest tests/integration` (throwaway table).

### Changed
- One shared `walk_exit` take-profit / stop-loss rule in `orb_session`; `session_outcome` (the
  dashboard) and the backtester both use it. Dashboard behaviour is unchanged.
- `replay_now` moved from `main.py` into `orb_session` so the backtester needs none of the live
  scanner's imports.
- `FINNHUB_API_KEY` is no longer required at import. `run_live()` fails fast with a clear message
  only when a Finnhub mode needs it, so tests, replay and the backtester run on a clean checkout.
- The shared Postgres pool has a 5 second connect timeout.
- `fetch_range` accepts `auto_adjust` (the archive and backfill pass `False` for raw prices).

## [0.2.0.0] - 2026-11-15

### Changed
- **Alert trigger**: an alert now needs a *completed* 5-minute candle whose whole range
  (wicks included) sits outside the 09:30–10:30 ET opening range and that closes in the
  breakout direction (green above the top, red below the bottom). A 1-minute close a few
  cents outside the box no longer fires. Candle size is `CONFIRM_CANDLE_MINUTES` (must
  divide 60); a bucket counts as complete only when a later 1-minute bar exists or the
  clock is past its end plus `CANDLE_GRACE_SECONDS`, so the in-progress yfinance minute
  never confirms.
- **Every chart (email, dashboard, replay) uses 5-minute candles** and draws the orange
  first-hour box, dashed box-top/bottom lines with price labels, and states the candle
  size in the title.
- **One definition of every number**: box, candles, confirmation, proposal and outcome
  are computed by the new pure module `marketscanner/strategies/orb_session.py`
  (`snapshot(df_1m, now)`) from 1-minute bars alone; the strategy, dashboard, email and
  replay all read that snapshot, so the picture survives restarts by construction.
- **Lateness rule**: a confirmation older than `CONFIRM_MAX_LATENESS_MINUTES` (default
  2 × candle size) when first seen — a mid-day restart replaying an hours-old breakout —
  is refused and the session is latched, with one INFO line. A missed poll or two still
  fires. The dashboard still draws the refused proposal.
- `python main.py --backtest SYMBOL` is now an alias for `--replay SYMBOL <today>`.
- `render_chart(snapshot, market)` replaces `render_chart(df, market, box_top, …)`; the
  renderer is pyplot-free (OO matplotlib, mplfinance external-axes mode).
- `start_web_server(symbols, feed, port)` — the dashboard no longer needs strategies,
  the lock or the signal history.

### Added
- **Trade proposal in the alert** (informational only — nothing trades): BUY above /
  SELL below, entry = confirmation close, take profit and stop loss one box-width away
  (fixed 1:1). Subject `[MarketScanner] SPY ORB BUY @ 583.90 · TP 585.85 · SL 581.95`;
  body table with side, entry, TP, SL, range delta, risk/reward, confirmation candle
  (time, size, OHLC), box top/bottom and time sent, identical in plain text and HTML.
- **Position overlays on every chart**: green zone entry→TP and red zone entry→SL from
  the confirmation candle, dashed TP and SL levels, `BUY @ price` annotation.
- **Outcome marker**: the 1-minute bars after the confirmation are walked for a TP or SL
  touch (both in one bar counts as SL); a green ▼ `TP hit hh:mm +1R` or red ▲
  `SL hit hh:mm −1R` is drawn on the exit candle and the zones end there.
- `python main.py --replay SYMBOL YYYY-MM-DD` renders any session of the last ~30 days to
  `output/replay/replay_SYMBOL_DATE.png` and logs box, confirmation, proposal and outcome.
- `/status/<SYM>` JSON (session date, box, proposal, outcome, candle size, last candle)
  and a per-card status line on the dashboard (`BUY 583.90 · TP 585.85 · SL 581.95 ·
  open` / `· TP hit 11:05 (+1R)` / `waiting for confirmation` / `building range`).
- `signals` table gains `session_date, side, entry, take_profit, stop_loss, delta,
  confirm_open, confirm_close, candle_minutes` (additive `ALTER TABLE … IF NOT EXISTS`
  at startup); `store.signal_exists` stops a restart after the 4-hour cooldown from
  emailing the same session twice.
- Config: `CONFIRM_CANDLE_MINUTES`, `CANDLE_GRACE_SECONDS`, `CONFIRM_MAX_LATENESS_MINUTES`.
- Tests: `test_orb_session`, `test_chart_render`, `test_email_alert`, `test_main`,
  `test_web_server`; `pytest -q` needs no network, Postgres or `.env`.

### Fixed
- **Dashboard readable on phones**: the page had no viewport meta, so a phone showed a
  zoomed-out 980 px desktop layout. Cards now shrink to the screen width.
- **Outcome label placement**: the `TP hit` / `SL hit` text no longer collides with the
  right-edge level labels or gets clipped when a stop is hit near the close.

### Removed
- The legacy interactive desktop chart window in `main.py` (headless loop only).
- `OpeningRangeStrategy._State`, `_range_high/_range_low`, `main._signal_history`.
  `OpeningRangeStrategy.session_box` stays as a thin alias for one release.

## [0.1.4.1] - 2026-07-03

### Fixed
- **Dashboard charts showed no top/bottom (opening-range) lines**: the box was read
  from the live strategy state, which is empty after a restart or when the freshness
  guard skips an after-hours replay. The dashboard now derives the box directly from
  the bars being charted (`OpeningRangeStrategy.session_box`), so the top/bottom lines
  render whenever the session's data is present.

## [0.1.4.0] - 2026-07-02

### Fixed
- **Alerts fired at the close and arrived hours late**: the strategy only inspects the
  latest bar, so replaying a completed session (yfinance fallback or a late process
  start) "broke out" on the 15:59 bar and emailed at, e.g., midnight. A freshness guard
  now drops any bar older than 5 minutes, so alerts fire only on live bars.
- **Opening-range box lines didn't match the candles**: the ORB window matched
  09:30–10:30 on every date in the rolling buffer, so the box high/low were computed
  across multiple sessions and drawn as levels that didn't bracket the day's candles.
  The window, chart, and signal markers are now scoped to the current session, and the
  feed buffer is cleared at the ET day rollover.
- **Dashboard charts froze mid-session**: the stale-feed watchdog measured tick recency,
  which Finnhub free-tier trickle-trades kept fresh while no bar ever closed. It now
  trips on bar staleness and forces the yfinance repopulate.
- **Midnight reset could silently die**: the reset thread is guarded so an exception
  can't leave every strategy stuck and stop all future alerts.

### Added
- `FEED_MODE` setting (`auto` | `yfinance`). docker-compose uses `yfinance` because the
  Finnhub free tier doesn't stream US ETFs — the reliable poll becomes the primary feed
  instead of a fallback that rarely engaged.
- `/health` reports each symbol's last-bar timestamp and age, returning `degraded` when
  data is stale during market hours (`closed` outside hours) instead of always `ok`.

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
