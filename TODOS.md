# TODOs

## Deferred Research

- [ ] **backtrader evaluation** — When adding Strategy 2+, evaluate whether `backtrader` should replace the custom `Strategy` ABC + event loop. It handles event-driven candle-by-candle logic natively and has built-in analyzers. Trade-off: requires a custom live feed class and its `bt.plot()` is less flexible than our `render_chart() → Figure` pattern (which supports email attachments and future web embed). Re-evaluate when the custom strategy count reaches 3+.
