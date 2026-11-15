# TODOs

## Alerts (low priority)

- [ ] **ntfy.sh push notifications** — On signal, POST to `https://ntfy.sh/<topic>` with market, direction, and price. No account required, free tier, ~1s delivery. User subscribes on phone via ntfy app. Add as a second notification channel alongside email; make topic configurable via `.env`.

- [ ] **SMS via Twilio** — On signal, send SMS using Twilio API (paid, ~$0.01/message). More reliable than email for time-sensitive alerts. Requires `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM`, `TWILIO_TO` in `.env`. Evaluate only if ntfy.sh latency proves insufficient.

## Deferred Research

- [ ] **backtrader evaluation** — When adding Strategy 2+, evaluate whether `backtrader` should replace the custom `Strategy` ABC + event loop. It handles event-driven candle-by-candle logic natively and has built-in analyzers. Trade-off: requires a custom live feed class and its `bt.plot()` is less flexible than our `render_chart() → Figure` pattern (which supports email attachments and future web embed). Re-evaluate when the custom strategy count reaches 3+.

## ORB follow-ups (from /qa on historical data, 2026-11-15)

- [ ] **End-of-day rule for open proposals** — 29 of 45 confirmed sessions in the 2026-10-19 to 11-13 sweep were still OPEN at 16:00. A scoreboard needs a close-out convention (mark-to-close or flat at 15:55) before win/loss stats mean anything.
- [ ] **Confirmation cut-off hour** — confirmations as late as 14:30 / 14:55 ET (GLD 10-21, 11-03) leave almost no session; decide whether late candles should still alert.
- [ ] **Feed gaps on the chart** — 2026-11-05 had a two-hour yfinance gap (289 bars); the index-based x-axis hides it. Consider a gap marker or a bar-count note in the title.
