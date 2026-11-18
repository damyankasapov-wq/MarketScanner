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

### Sweep: choose which third is out-of-sample

**What:** A sweep option to make the first third of the period out-of-sample (`--oos first|last`, default last), or to exclude dates already inspected (e.g. `--burned 2026-10-19:2026-11-13`), with the OOS dates printed in the banner.

**Why:** The cut-off idea and its 13:00 / 14:00 grid values came from looking at GLD on 2026-10-21 and 11-03, and backtests were run on 2026-10-19 to 11-13. All of that sits in the last third, which the sweep treats as unseen data, so a cut-off "confirmed out-of-sample" was partly chosen from those sessions.

**Context:** Raised by the outside voice in /plan-eng-review on 2026-11-18 (D14); the owner deferred it here. The sweep design keeps the chronological last-third split. Until this ships, read an OOS result for the cut-off with that caveat. Design: `~/.gstack/projects/damyankasapov-wq-MarketScanner/damya-claude-office-hours-todos-38e5ad-design-20261118-003041.md`.

**Effort:** S
**Priority:** P2
**Depends on:** settings sweep (layer 3)

### Sweep: walk-forward and neighbour robustness

**What:** Instead of one 2/3-1/3 split, roll year by year: pick the in-sample best on year N, score it on year N+1, and chain the picks into one out-of-sample record. Add a neighbour score per grid cell (a cell only counts as good if adjacent settings are good too).

**Why:** With years of Alpaca history, a single split is a weak guard; walk-forward is the most honest answer to "would choosing settings this way have worked?", and isolated lucky cells are the typical overfit signature.

**Context:** Approach C in the settings sweep design (office-hours, 2026-11-18), deferred until the cached-grid sweep shows a variant worth checking. Reuses the sweep's cached ideas and exits; only the split and scoring change. Meaningless on a single month.

**Effort:** L
**Priority:** P3
**Depends on:** settings sweep (layer 3)

## Strategy (ORB)

From /qa on historical data, 2026-11-15.

### Confirmation cut-off hour

**What:** Decide whether late confirmation candles should still alert.

**Why:** Confirmations as late as 14:30 / 14:55 ET (GLD 10-21, 11-03) leave almost no session.

**Context:** Pairs with the layer 3 settings sweep. First real sweep (SPY/QQQ/GLD, 2024-01-01 to 2026-11-13): a 13:00 or 14:00 cut-off did not beat no cut-off in any row, in-sample or out-of-sample. Since v0.6.0.0 a cut-off can be set live with `CONFIRM_CUTOFF_ET` (unset = none); what is left is the decision itself.

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

### Settings sweep with overfitting guards (layer 3)

**What:** Run the strategy under different candle sizes, reward ratios and confirmation cut-off hours side by side.

**Why:** Today only the candle size can be changed (env var); comparing settings needs code edits and gives no protection against fitting the past.

**Context:** Designed and reviewed on 2026-11-18: `~/.gstack/projects/damyankasapov-wq-MarketScanner/damya-claude-office-hours-todos-38e5ad-design-20261118-003041.md` (the "Eng Review Decisions" section at its end wins over the earlier sections). The speed prerequisite shipped in v0.4.1.0. The sweep itself is backtest-only: reward and cut-off are applied after a plain `propose()` through `OrbStrategy.derive`, so `snapshot` and live alerts stay unchanged. Each variant is compared with the current .env settings, as a paired R difference in-sample and out-of-sample with a verdict rule fixed in code, plus BUY R / SELL R columns. Exits are account-independent: compute once per (candle, reward) and reuse across cut-offs.

**Effort:** M
**Priority:** P2
**Depends on:** layers 0+1 shipped; backtester speed-up (v0.4.1.0)
**Completed:** v0.5.0.0 (2026-11-18). Backtest-only; live settings (`REWARD_RATIO` / `CONFIRM_CUTOFF_ET`) follow separately.

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
