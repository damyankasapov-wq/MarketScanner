"""
Lightweight Flask web server — serves live ORB charts over HTTP.

Runs as a background daemon thread inside the scanner process. The feed is
injected at startup so it is shared in-process with zero IPC overhead.
Everything shown is derived from `orb_session.snapshot(feed.get_df(sym))`,
never from live strategy memory (which is empty after a restart).

Endpoints:
  GET /               HTML dashboard with all symbols (auto-refreshes every 60s)
  GET /chart/<SYM>    Current 5-minute chart for SYM as PNG
  GET /status/<SYM>   JSON: session_date, box, proposal, outcome, candle_minutes, last_candle
  GET /health         JSON health check

Usage:
  from marketscanner.web.server import start_web_server
  start_web_server(symbols, feed, port=8080)
"""
import io
import logging
import threading
import time
from datetime import datetime, timezone

import pytz
from flask import Flask, Response, jsonify, render_template_string

from marketscanner import config
from marketscanner.strategies import orb_session
from marketscanner.ui.chart import render_chart

log = logging.getLogger(__name__)

_ET = pytz.timezone(config.TIMEZONE)

# During market hours, data older than this is considered stale (a live feed
# closes a new 1-minute bar every minute).
_STALE_AGE_SECONDS = 300

app = Flask(__name__)
# Suppress Flask's per-request access logs — scanner log is noisy enough
logging.getLogger("werkzeug").setLevel(logging.WARNING)

# State injected by start_web_server() — populated before the Flask thread starts.
_state: dict = {}

# /health reports the newest archived session per symbol. The database is asked at
# most once a minute so a slow database cannot make /health slow.
_BARS_TTL_SECONDS = 60
_bars_cache: dict = {"at": None, "value": None}


def _bars_last_archived() -> dict | None:
    store = _state.get("bar_store")
    if store is None:
        return None
    now = time.monotonic()
    if _bars_cache["at"] is not None and now - _bars_cache["at"] < _BARS_TTL_SECONDS:
        return _bars_cache["value"]
    out = {}
    for sym in _state.get("symbols", []):
        try:
            last = store.last_session(sym)
            out[sym] = last.isoformat() if last else None
        except Exception as exc:  # a dead database must not break /health
            log.warning("bars_last_archived(%s) failed: %r", sym, exc)
            out[sym] = "error"
    _bars_cache.update(at=now, value=out)
    return out


_NO_CACHE = {
    "Cache-Control": "no-cache, no-store, must-revalidate",
    "Pragma": "no-cache",
    "Expires": "0",
}

# ── HTML dashboard ─────────────────────────────────────────────────────────────
_INDEX_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
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
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(580px, 100%), 1fr)); gap: 20px; }
    .card { background: #1e293b; border-radius: 10px; overflow: hidden; }
    .card h2 {
      padding: 10px 16px; font-size: 0.9rem; color: #93c5fd;
      border-bottom: 1px solid #334155;
    }
    .card .status {
      padding: 6px 16px; font-size: 0.8rem; color: #cbd5e1;
      border-bottom: 1px solid #334155; min-height: 1.9em;
    }
    .card .status.buy { color: #4ade80; }
    .card .status.sell { color: #f87171; }
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
      <p class="status" id="status-{{ sym }}">…</p>
      <img id="img-{{ sym }}" src="/chart/{{ sym }}" alt="{{ sym }} chart"
           onerror="this.style.opacity='0.3'">
    </div>
    {% endfor %}
  </div>
  <p class="footer">Charts refresh every 60 s &nbsp;|&nbsp; informational only — not an order
    &nbsp;|&nbsp; <a href="/health" style="color:#60a5fa">health</a>
  </p>
  <script>
    function pad(n) { return String(n).padStart(2, '0'); }
    function ts() {
      const d = new Date();
      return d.getFullYear() + '-' +
             pad(d.getMonth()+1) + '-' + pad(d.getDate()) + ' ' +
             pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds());
    }
    function refreshStatus() {
      document.querySelectorAll('p[id^="status-"]').forEach(p => {
        const sym = p.id.slice(7);
        fetch('/status/' + sym + '?t=' + Date.now())
          .then(r => r.json())
          .then(s => {
            p.textContent = s.status_line;
            p.className = 'status' + (s.proposal ? ' ' + s.proposal.side.toLowerCase() : '');
          })
          .catch(() => { p.textContent = 'status unavailable'; });
      });
    }
    function refresh() {
      document.querySelectorAll('img[id^="img-"]').forEach(img => {
        const base = img.src.split('?')[0];
        img.src = base + '?t=' + Date.now();
      });
      refreshStatus();
      document.getElementById('ts').textContent = 'last refresh: ' + ts();
    }
    document.getElementById('ts').textContent = 'loaded: ' + ts();
    refreshStatus();
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


def _snapshot_for(symbol: str) -> orb_session.OrbSnapshot:
    df = _state["feed"].get_df(symbol)
    return orb_session.snapshot(df, now=datetime.now(timezone.utc))


@app.route("/chart/<symbol>")
def chart_png(symbol: str):
    symbol = symbol.upper()
    if symbol not in _state.get("symbols", []):
        return Response(f"Unknown symbol: {symbol}", status=404)

    df = _state["feed"].get_df(symbol)
    if df.empty:
        # No data yet — return a styled placeholder PNG so the browser shows
        # something useful instead of a broken-image icon.
        return Response(_no_data_png(symbol), mimetype="image/png", headers=_NO_CACHE)

    snap = orb_session.snapshot(df, now=datetime.now(timezone.utc))
    fig = render_chart(snap, market=symbol)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", dpi=100)
    buf.seek(0)
    return Response(buf.getvalue(), mimetype="image/png", headers=_NO_CACHE)


def status_line(snap: orb_session.OrbSnapshot) -> str:
    """One-line card status: proposal numbers and where the trade stands."""
    if snap.box is None:
        return "waiting for market open" if snap.session_date is None else "building range"
    if not snap.range_final:
        return f"building range until 10:30 ET · so far {snap.box.bottom:.2f}–{snap.box.top:.2f}"
    if snap.proposal is None:
        if snap.box.delta <= 0:
            return "flat opening range — no proposal"
        late = snap.late_confirmation
        if late is not None:
            return (f"confirmation {late.candle_close.astimezone(_ET):%H:%M} after cut-off "
                    f"{snap.cutoff:%H:%M}, no trade today")
        return f"waiting for confirmation · box {snap.box.bottom:.2f}–{snap.box.top:.2f}"
    p = snap.proposal
    line = f"{p.side} {p.entry:.2f} · TP {p.take_profit:.2f} · SL {p.stop_loss:.2f}"
    o = snap.outcome
    if o is None or o.status == "OPEN":
        return line + " · open"
    hit = "TP hit" if o.status == "TP_HIT" else "SL hit"
    return f"{line} · {hit} {o.exit_ts.astimezone(_ET):%H:%M} ({o.r_multiple:+g}R)"


def status_payload(snap: orb_session.OrbSnapshot) -> dict:
    iso = lambda ts: None if ts is None else ts.isoformat()
    last_candle = None if snap.candles.empty else snap.candles.index[-1].isoformat()
    box = None if snap.box is None else {
        "top": snap.box.top, "bottom": snap.box.bottom, "delta": snap.box.delta}
    proposal = None if snap.proposal is None else {
        "side": snap.proposal.side,
        "entry": snap.proposal.entry,
        "take_profit": snap.proposal.take_profit,
        "stop_loss": snap.proposal.stop_loss,
        "delta": snap.proposal.delta,
        "reward_ratio": snap.proposal.reward_ratio,
        "confirm_open": iso(snap.confirmation.candle_open),
        "confirm_close": iso(snap.confirmation.candle_close),
    }
    outcome = None if snap.outcome is None else {
        "status": snap.outcome.status,
        "exit_ts": iso(snap.outcome.exit_ts),
        "exit_price": snap.outcome.exit_price,
        "r_multiple": snap.outcome.r_multiple,
    }
    return {
        "session_date": iso(snap.session_date),
        "candle_minutes": snap.candle_minutes,
        "last_candle": last_candle,
        "box": box,
        "proposal": proposal,
        "outcome": outcome,
        "late_confirmation_ts": iso(None if snap.late_confirmation is None else snap.late_confirmation.candle_close),
        "cutoff": None if snap.cutoff is None else f"{snap.cutoff:%H:%M}",
        "range_final": snap.range_final,
        "status_line": status_line(snap),
    }


@app.route("/status/<symbol>")
def status(symbol: str):
    symbol = symbol.upper()
    if symbol not in _state.get("symbols", []):
        return Response(f"Unknown symbol: {symbol}", status=404)
    resp = jsonify(status_payload(_snapshot_for(symbol)))
    resp.headers.update(_NO_CACHE)
    return resp


@app.route("/health")
def health():
    """
    Report per-symbol data freshness, not just presence. Each symbol carries
    the last bar's timestamp and age, and the feed is flagged degraded when
    data goes stale during market hours.
    """
    symbols = _state.get("symbols", [])
    feed = _state.get("feed")

    now_utc = datetime.now(timezone.utc)
    now_et = now_utc.astimezone(_ET)
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

    result = {
        "status": status,
        "market_open": market_open,
        "symbols": symbols,
        "data": data,
    }
    archived = _bars_last_archived()
    if archived is not None:
        result["bars_last_archived"] = archived
    return result


def start_web_server(symbols: list, feed, port: int = 8080, bar_store=None) -> threading.Thread:
    """
    Inject live state and start Flask in a background daemon thread.
    Returns immediately; Flask keeps running until the process exits.
    """
    _state.update({"symbols": symbols, "feed": feed, "bar_store": bar_store})
    _bars_cache.update(at=None, value=None)

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
