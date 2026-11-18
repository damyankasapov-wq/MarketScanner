# CLAUDE.md

MarketScanner: Python 3.11 service (Docker Compose: Postgres + scanner) that watches
SPY, QQQ and GLD on 1-minute yfinance bars and emails an Opening Range Breakout
proposal; Flask dashboard on port 8080. Informational only, nothing trades.

- Run tests: `pytest -q` (no network, Postgres or `.env` needed).
- Replay a past session: `python main.py --replay SPY 2026-11-11`.
- All ORB numbers come from `marketscanner/strategies/orb_session.py` (`snapshot()`); the one
  take-profit / stop-loss rule is `walk_exit` there.
- Which New York trading day a bar belongs to is decided only in `marketscanner/data/sessions.py`
  (`split_by_et_date`, `session_start_mask`), shared by the backfill and the backtester.
- Backtest a strategy on a simulated account: `python -m marketscanner.backtest --from 2026-10-19
  --to 2026-11-13 --markets SPY,QQQ,GLD --cash 10000 --per-deal 1000 --min-cash 2000` (reads the
  Postgres `bars` table via the DB_* settings, backfilling missing sessions from yfinance; the live
  scanner archives each day's bars after the midnight reset). Sessions older than yfinance's 30-day
  window come from Alpaca (free account, 1-min history since 2016) when `ALPACA_API_KEY_ID` /
  `ALPACA_API_SECRET_KEY` are set (`marketscanner/data/alpaca_feed.py`). The backfill follows the NYSE
  calendar (`marketscanner/data/market_calendar.py`: holidays skipped, half-days cut at 13:00 ET).
  Output goes to `output/backtest/<run>/`; `--gallery none` skips the per-trade charts (most of the
  run time and disk). A git worktree has no `.env` (it's gitignored): copy it from the main checkout.
- Real-Postgres test for the bars table (opt-in): `MS_TEST_DB=1 pytest tests/integration`.

## Skill routing

When the user's request matches an available skill, invoke it via the Skill tool. When in doubt, invoke the skill.

Key routing rules:
- Product ideas/brainstorming → invoke /office-hours
- Strategy/scope → invoke /plan-ceo-review
- Architecture → invoke /plan-eng-review
- Design system/plan review → invoke /design-consultation or /plan-design-review
- Full review pipeline → invoke /autoplan
- Bugs/errors → invoke /investigate
- QA/testing site behavior → invoke /qa or /qa-only
- Code review/diff check → invoke /review
- Visual polish → invoke /design-review
- Ship/deploy/PR → invoke /ship or /land-and-deploy
- Save progress → invoke /context-save
- Resume context → invoke /context-restore
