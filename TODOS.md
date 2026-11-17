# TODOS

Organised by component, highest priority first. P0 blocks the next release, P1 this cycle,
P2 when P0/P1 are clear, P3 nice to have, P4 someday.

## Infrastructure

### Harden the Postgres exposure (security)

**What:** Replace the default `ms`/`ms` Postgres credentials with a generated password and stop publishing port 5432 on all interfaces.

**Why:** `docker-compose.yml` publishes Postgres on port 5432 on all interfaces with user and password both `ms` (also the default in `.env.example`). The backtest reads this database from your laptop over the LAN, which makes this matter more.

**Context:** Replace with a generated password kept in `.env` and restrict the published port to the LAN address (or require an SSH tunnel). Files: `docker-compose.yml` (ports, `POSTGRES_PASSWORD`, scanner `DB_PASSWORD`), `.env.example`. Changing the password needs `ALTER USER` or a recreated volume; the live scanner and `scripts/deploy.sh` both read these values. Do it as its own small change, not inside the backtester work.

**Effort:** S
**Priority:** P1
**Depends on:** None

## Backtester

Scope accepted for the strategy checker (from /plan-eng-review, 2026-11-16): layers 0 (bars archive in Postgres), 1 (core engine) and 2 (HTML report + trade gallery). Design: `~/.gstack/projects/damyankasapov-wq-MarketScanner/damya-fix-six-dependency-design-20261115-230829.md`.

### Settings sweep with overfitting guards (layer 3)

**What:** Run the strategy under different candle sizes, reward ratios and confirmation cut-off hours side by side.

**Why:** Today only the candle size can be changed (env var); comparing settings needs code edits and gives no protection against fitting the past.

**Context:** Print trade count per variant, an in-sample / out-of-sample split (first two thirds / last third) and label the top variant "in-sample best". Needs `reward_ratio` and a confirmation cut-off argument added to `snapshot` / `build_proposal` with defaults equal to today's behaviour so live alerts are unchanged. Exits are account-independent: compute once per idea and reuse across variants. Do not trust any sweep below ~100 closed trades; wait for the bars archive to accumulate (the Alpaca backfill now provides years of history).

**Effort:** M
**Priority:** P2
**Depends on:** layers 0+1 shipped

### Splits and dividends in multi-day holds

**What:** Stop raw-price gaps from splits and dividends distorting trades held across sessions.

**Why:** The archive stores raw prices (Alpaca `adjustment=raw`, yfinance `auto_adjust=False`), and the engine follows trades across sessions with gap fills. A hold across a split gaps to a fake stop (e.g. `--markets AAPL --from 2020-08-01` over the 4:1 split), and ex-dividend gaps on SPY/QQQ hurt overnight longs without crediting the dividend.

**Context:** Found by the /ship adversarial review on 2026-11-17. The Alpaca backfill makes 10-year runs possible, so this matters more now. Options: restrict `--markets` to `config.MARKETS`, warn or refuse when a split falls inside the period, or apply an adjustment factor across session boundaries in `walk_exit`.

**Effort:** M
**Priority:** P2
**Depends on:** None

### Fees, slippage and borrow-cost model (layer 4)

**What:** Per-trade commission, slippage in basis points on both sides, daily borrow fee for shorts, plus a `conservative` preset with costs doubled.

**Why:** A 1:1 strategy with tight boxes can flip from profit to loss once costs are in.

**Context:** Applied in the engine after exits, so it does not change which trades fire.

**Effort:** M
**Priority:** P2
**Depends on:** layer 1 engine
**Blocked by:** your broker's real commission and whether SPY/QQQ/GLD can be shorted at all

## Strategy (ORB)

From /qa on historical data, 2026-11-15.

### Confirmation cut-off hour

**What:** Decide whether late confirmation candles should still alert.

**Why:** Confirmations as late as 14:30 / 14:55 ET (GLD 10-21, 11-03) leave almost no session.

**Context:** Pairs with the layer 3 settings sweep, which would measure the effect of a cut-off on past sessions.

**Effort:** S
**Priority:** P2
**Depends on:** None

### Track closure of carried-over proposals on the following days

**What:** Keep following each live proposal still open at 16:00 through the next trading days until its TP or SL is first touched, and record the closing day, time, price and R.

**Why:** 29 of 45 confirmed sessions in the 2026-10-19 to 11-13 sweep were still OPEN at 16:00 (neither TP nor SL touched). The purpose is only to score the predictions properly (a TP/SL record and win rate, no mark-to-close guesswork).

**Context:** Same rule as `session_outcome`: both in one bar counts as SL. Scope: results tracking only. Do NOT draw carried-over proposals, zones or outcome markers on the next day's charts or dashboard; the dashboard still resets at the ET day rollover and each chart shows only its own session. Needs persisted proposals (already in `signals`: entry, take_profit, stop_loss, confirm_close, session_date) plus a place to store the eventual outcome, since outcomes are recomputed from bars and not persisted today. Decision deferred by the owner on 2026-11-15; an explicit end-of-day close-out rule is not part of this item. The layer 1 backtester already follows trades across days historically; a live scoreboard for real alerts is still open.

**Effort:** M
**Priority:** P3
**Depends on:** None

### Feed gaps on the chart

**What:** Show feed gaps on the chart, e.g. a gap marker or a bar-count note in the title.

**Why:** 2026-11-05 had a two-hour yfinance gap (289 bars); the index-based x-axis hides it.

**Context:** The backtest coverage summary already lists such sessions; the live charts don't.

**Effort:** S
**Priority:** P3
**Depends on:** None

## Alerts

### ntfy.sh push notifications

**What:** On signal, POST to `https://ntfy.sh/<topic>` with market, direction, and price.

**Why:** No account required, free tier, ~1s delivery; faster to notice than email.

**Context:** User subscribes on phone via the ntfy app. Add as a second notification channel alongside email; make the topic configurable via `.env`.

**Effort:** S
**Priority:** P3
**Depends on:** None

### SMS via Twilio

**What:** On signal, send an SMS using the Twilio API (paid, ~$0.01/message).

**Why:** More reliable than email for time-sensitive alerts.

**Context:** Requires `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM`, `TWILIO_TO` in `.env`. Evaluate only if ntfy.sh latency proves insufficient.

**Effort:** S
**Priority:** P4
**Depends on:** ntfy.sh latency proving insufficient

## Research

### backtrader evaluation

**What:** When adding Strategy 2+, evaluate whether `backtrader` should replace the custom `Strategy` ABC + event loop.

**Why:** It handles event-driven candle-by-candle logic natively and has built-in analyzers.

**Context:** Trade-off: requires a custom live feed class and its `bt.plot()` is less flexible than our `render_chart() → Figure` pattern (which supports email attachments and future web embed). Re-evaluate when the custom strategy count reaches 3+.

**Effort:** M
**Priority:** P4
**Depends on:** a second strategy

## Completed

### Second history provider (layer 5)

**What:** A second data provider behind the same `BarStore` interface that backfills sessions older than the yfinance 30-day window into the Postgres `bars` table, to cover other market regimes.

**Why:** yfinance only serves 30 days of 1-minute bars; backtests over longer periods needed another source.

**Context:** Alpaca free plan (1-minute history since 2016, SIP feed), `marketscanner/data/alpaca_feed.py`, used by `ensure_bars` when the keys are set. Sessions follow the NYSE calendar (`marketscanner/data/market_calendar.py`).

**Effort:** M
**Priority:** P3
**Completed:** v0.4.0.0 (2026-11-17)

### Cap Alpaca retry time during an outage

**What:** Stop asking Alpaca for the rest of a run once it is clearly failing.

**Why:** Each month chunk waited about 31 s of backoff on a persistent 5xx, so a one-year, three-symbol backtest could spend about 18 minutes retrying.

**Context:** Found by /qa on 2026-11-17. The deep backfill now stops for the run on a 401/403 or wrong feed setting, or after 2 failed months in a row, and reports it once.

**Effort:** S
**Priority:** P2
**Completed:** v0.4.0.0 (2026-11-17)
