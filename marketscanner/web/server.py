"""
Lightweight Flask web server — serves live OHLCV charts over HTTP.

Runs as a background daemon thread inside the scanner process.
State (feed, strategies, signal_history) is injected at startup so it
is shared in-process with zero IPC overhead.

Endpoints:
  GET /              HTML dashboard with all 3 symbols (auto-refreshes every 60s)
  GET /chart/<SYM>   Current candlestick chart for SYM as PNG
  GET /health        JSON health check

Usage:
  from marketscanner.web.server import start_web_server
  start_web_server(symbols, feed, lock, strategies, signal_history, port=8080)
"""
import io
import logging
import threading

from flask import Flask, Response, render_template_string

from marketscanner import config

log = logging.getLogger(__name__)

# During market hours, data older than this is considered stale (a live feed
# closes a new 1-minute bar every minute).
_STALE_AGE_SECONDS = 300

app = Flask(__name__)
# Suppress Flask's per-request access logs — scanner log is noisy enough
logging.getLogger("werkzeug").setLevel(logging.WARNING)

# State injected by start_web_server() — populated before the Flask thread starts.
_state: dict = {}

# ── HTML dashboard ─────────────────────────────────────────────────────────────
_INDEX_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>MarketScanner — Live Charts</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      background: #0f172a; color: #e2e8f0;
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      padding: 20px;
    }
    header { display: flex; align-items: baseline; gap: 16px; margin-bottom: 24px; }
    header h1 { font-size: 1.4rem; color: #4ade80; }
    header span { font-size: 0.75rem; color: #64748b; }
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(580px, 1fr)); gap: 20px; }
    .card { background: #1e293b; border-radius: 10px; overflow: hidden; }
    .card h2 {
      padding: 10px 16px; font-size: 0.9rem; color: #93c5fd;
      border-bottom: 1px solid #334155;
    }
    .card img {
      display: block; width: 100%; height: auto;
      background: #0f172a;
    }
    .footer { margin-top: 20px; font-size: 0.7rem; color: #475569; }
  </style>
</head>
<body>
  <header>
    <h1>MarketScanner</h1>
    <span id="ts">loading…</span>
  </header>
  <div class="grid">
    {% for sym in symbols %}
    <div class="card">
      <h2>{{ sym }} — Opening Range Breakout</h2>
      <img id="img-{{ sym }}" src="/chart/{{ sym }}" alt="{{ sym }} chart"
           onerror="this.style.opacity='0.3'">
    </div>
    {% endfor %}
  </div>
  <p class="footer">Charts refresh every 60 s &nbsp;|&nbsp;
    <a href="/health" style="color:#60a5fa">health</a>
  </p>
  <script>
    function pad(n) { return String(n).padStart(2, '0'); }
    function ts() {
      const d = new Date();
      return d.getFullYear() + '-' +
             pad(d.getMonth()+1) + '-' + pad(d.getDate()) + ' ' +
             pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds());
    }
    function refresh() {
      document.querySelectorAll('img[id^="img-"]').forEach(img => {
        const base = img.src.split('?')[0];
        img.src = base + '?t=' + Date.now();
      });
      document.getElementById('ts').textContent = 'last refresh: ' + ts();
    }
    // Initial timestamp
    document.getElementById('ts').textContent = 'loaded: ' + ts();
    setInterval(refresh, 60000);
  </script>
</body>
</html>"""


@app.route("/")
def index():
    return render_template_string(_INDEX_HTML, symbols=_state.get("symbols", []))


def _no_data_png(symbol: str) -> bytes:
    """Render a placeholder PNG shown when the feed has no data yet."""
    import matplotlib.figure
    import matplotlib.backends.backend_agg as agg

    # Use the OO API (Figure + FigureCanvasAgg) — thread-safe unlike plt.subplots()
    fig = matplotlib.figure.Figure(figsize=(8, 3))
    agg.FigureCanvasAgg(fig)
    fig.patch.set_facecolor("#0f172a")
    ax = fig.add_subplot(111)
    ax.set_facecolor("#1e293b")
    ax.text(
        0.5, 0.6,
        f"{symbol}",
        transform=ax.transAxes,
        ha="center", va="center",
        fontsize=22, color="#93c5fd", fontweight="bold",
    )
    ax.text(
        0.5, 0.35,
        "No data yet — market may be closed or feed not yet connected",
        transform=ax.transAxes,
        ha="center", va="center",
        fontsize=10, color="#64748b",
    )
    ax.text(
        0.5, 0.18,
        "Charts populate at market open (9:30 AM ET on trading days)",
        transform=ax.transAxes,
        ha="center", va="center",
        fontsize=8, color="#475569",
    )
    for spine in ax.spines.values():
        spine.set_edgecolor("#334155")
    ax.tick_params(left=False, bottom=False, labelleft=False, labelbottom=False)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", dpi=96,
                facecolor=fig.get_facecolor())
    buf.seek(0)
    return buf.getvalue()


@app.route("/chart/<symbol>")
def chart_png(symbol: str):
    symbol = symbol.upper()
    if symbol not in _state.get("symbols", []):
        return Response(f"Unknown symbol: {symbol}", status=404)

    feed = _state["feed"]
    lock = _state["lock"]
    strategies = _state["strategies"]
    signal_history = _state["signal_history"]
    render_chart = _state["render_chart"]

    df = feed.get_df(symbol)
    if df.empty:
        # No data yet — return a styled placeholder PNG so the browser shows
        # something useful instead of a broken-image icon.
        return Response(
            _no_data_png(symbol),
            mimetype="image/png",
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache",
                "Expires": "0",
            },
        )

    with lock:
        signals = list(signal_history.get(symbol, []))

    strategy = strategies[symbol]

    # render_chart closes the figure via plt.close() before returning,
    # but the Figure object is still valid for savefig().
    fig = render_chart(
        df,
        market=symbol,
        box_top=strategy._range_high,
        box_bottom=strategy._range_low,
        signal_times=signals,
    )

    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", dpi=100)
    buf.seek(0)

    return Response(
        buf.getvalue(),
        mimetype="image/png",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@app.route("/health")
def health():
    """
    Report per-symbol data freshness, not just presence. The old endpoint
    returned {"status": "ok"} whenever the buffer was non-empty, so an 8-hour
    frozen feed still looked healthy. Now each symbol carries the last bar's
    timestamp and age, and the feed is flagged degraded when data goes stale
    during market hours.
    """
    from datetime import datetime, timezone

    import pytz

    symbols = _state.get("symbols", [])
    feed = _state.get("feed")

    et = pytz.timezone(config.TIMEZONE)
    now_utc = datetime.now(timezone.utc)
    now_et = now_utc.astimezone(et)
    market_open = (
        now_et.weekday() < 5
        and (now_et.hour, now_et.minute) >= (config.ORB_START_HOUR, config.ORB_START_MINUTE)
        and (now_et.hour, now_et.minute) < (16, 0)
    )

    data = {}
    any_stale = False
    for sym in symbols:
        ts = feed.newest_bar_time(sym) if feed else None
        if ts is None:
            data[sym] = {"status": "no_data", "last_bar": None, "age_seconds": None}
            if market_open:
                any_stale = True
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        age = (now_utc - ts).total_seconds()
        stale = market_open and age > _STALE_AGE_SECONDS
        if stale:
            any_stale = True
        data[sym] = {
            "status": "stale" if stale else "ok",
            "last_bar": ts.isoformat(),
            "age_seconds": round(age),
        }

    if any_stale:
        status = "degraded"
    elif market_open:
        status = "ok"
    else:
        status = "closed"

    return {
        "status": status,
        "market_open": market_open,
        "symbols": symbols,
        "data": data,
    }


def start_web_server(
    symbols: list,
    feed,
    lock: "threading.Lock",
    strategies: dict,
    signal_history: dict,
    render_chart_fn,
    port: int = 8080,
) -> threading.Thread:
    """
    Inject live state and start Flask in a background daemon thread.
    Returns immediately; Flask keeps running until the process exits.
    """
    _state.update({
        "symbols": symbols,
        "feed": feed,
        "lock": lock,
        "strategies": strategies,
        "signal_history": signal_history,
        "render_chart": render_chart_fn,
    })

    t = threading.Thread(
        target=lambda: app.run(
            host="0.0.0.0",
            port=port,
            debug=False,
            use_reloader=False,
        ),
        daemon=True,
        name="web-server",
    )
    t.start()
    log.info("Web server started — http://0.0.0.0:%d", port)
    return t
