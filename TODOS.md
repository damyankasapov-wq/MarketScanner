# TODOs

## Alerts (low priority)

- [ ] **ntfy.sh push notifications** — On signal, POST to `https://ntfy.sh/<topic>` with market, direction, and price. No account required, free tier, ~1s delivery. User subscribes on phone via ntfy app. Add as a second notification channel alongside email; make topic configurable via `.env`.

- [ ] **SMS via Twilio** — On signal, send SMS using Twilio API (paid, ~$0.01/message). More reliable than email for time-sensitive alerts. Requires `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM`, `TWILIO_TO` in `.env`. Evaluate only if ntfy.sh latency proves insufficient.

## Deferred Research

- [ ] **backtrader evaluation** — When adding Strategy 2+, evaluate whether `backtrader` should replace the custom `Strategy` ABC + event loop. It handles event-driven candle-by-candle logic natively and has built-in analyzers. Trade-off: requires a custom live feed class and its `bt.plot()` is less flexible than our `render_chart() → Figure` pattern (which supports email attachments and future web embed). Re-evaluate when the custom strategy count reaches 3+.
