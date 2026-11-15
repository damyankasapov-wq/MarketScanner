# CLAUDE.md

MarketScanner: Python 3.11 service (Docker Compose: Postgres + scanner) that watches
SPY, QQQ and GLD on 1-minute yfinance bars and emails an Opening Range Breakout
proposal; Flask dashboard on port 8080. Informational only, nothing trades.

- Run tests: `pytest -q` (no network, Postgres or `.env` needed).
- Replay a past session: `python main.py --replay SPY 2026-11-11`.
- All ORB numbers come from `marketscanner/strategies/orb_session.py` (`snapshot()`).

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
