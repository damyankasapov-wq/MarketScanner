"""
report.html: one self-contained page for a run. The equity chart is embedded; the
trade charts are linked from trades/ next to the page. Every dynamic string is
HTML-escaped.
"""
from __future__ import annotations

import base64
import html
from typing import Optional

from marketscanner.backtest import report as rpt
from marketscanner.backtest.engine import LOSS, OPEN, SKIPPED, Trade, WIN
from marketscanner.backtest.metrics import Coverage, Summary, format_coverage

_E = html.escape

_CSS = """
:root{color-scheme:light dark;--bg:#fff;--fg:#111827;--mut:#6b7280;--line:#e5e7eb;--win:#15803d;--loss:#b91c1c;--open:#b45309;--card:#f9fafb}
@media (prefers-color-scheme:dark){:root{--bg:#0f172a;--fg:#e2e8f0;--mut:#94a3b8;--line:#334155;--win:#4ade80;--loss:#f87171;--open:#fbbf24;--card:#1e293b}}
body{margin:0;padding:24px 16px;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,Segoe UI,Arial,sans-serif}
main{max-width:1100px;margin:0 auto}
h1{font-size:1.4rem;margin:0 0 4px}h2{font-size:1.1rem;margin:28px 0 8px}
.sub{color:var(--mut);margin:0 0 16px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:10px}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px 12px}
.card b{display:block;font-size:1.15rem}.card span{color:var(--mut);font-size:.8rem}
.warn{border-left:4px solid var(--open);background:var(--card);padding:8px 12px;margin:6px 0}
img{max-width:100%;height:auto;border-radius:6px;border:1px solid var(--line)}
table{border-collapse:collapse;width:100%;font-size:.85rem}th,td{padding:5px 8px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}
th:nth-child(-n+4),td:nth-child(-n+4){text-align:left}
.mkt{width:auto;min-width:min(100%,560px);font-size:.9rem}.mkt th,.mkt td{text-align:right}.mkt th:first-child,.mkt td:first-child{text-align:left}
.wrap{overflow-x:auto}.WIN{color:var(--win)}.LOSS{color:var(--loss)}.OPEN{color:var(--open)}.SKIPPED{color:var(--mut)}
figure{margin:0 0 22px}figcaption{margin-top:6px;font-size:.88rem}
pre{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px;overflow-x:auto;font-size:.8rem;white-space:pre-wrap;overflow-wrap:anywhere}
footer{margin-top:32px;color:var(--mut);font-size:.8rem}
"""


def _money(x: Optional[float]) -> str:
    return "n/a" if x is None else f"{x:,.2f}"


def _cards(s: Summary) -> str:
    items = [
        ("Final equity", _money(s.final_equity)),
        ("Profit / loss", f"{_money(s.net_pnl)} ({s.return_pct:+.2f}%)"),
        ("Closed trades", f"{s.closed} ({s.wins} wins, {s.losses} losses)"),
        ("Win rate", "n/a" if s.win_rate is None else f"{s.win_rate:.1f}%"),
        ("Still open", str(s.open)),
        ("Skipped (cash)", str(s.skipped)),
        ("Profit factor", "n/a" if s.profit_factor is None else f"{s.profit_factor:.2f}"),
        ("Average R", "n/a" if s.avg_r is None else f"{s.avg_r:+.2f}R"),
        ("Max drawdown", f"{_money(s.max_drawdown)} ({s.max_drawdown_pct:.2f}%)"),
        ("Average invested", f"{s.avg_invested_pct:.1f}% of equity"),
    ]
    return '<div class="grid">' + "".join(f'<div class="card"><b>{_E(v)}</b><span>{_E(k)}</span></div>' for k, v in items) + "</div>"


def _markets(s: Summary) -> str:
    """By-market table, best P&L first. P&L includes open trades marked at the last close."""
    if not s.by_symbol:
        return ""
    any_skipped = any(d["skipped"] for d in s.by_symbol.values())
    cols = ["Market", "Trades", "Wins / losses", "Win rate", "P&L"] + (["Skipped (cash)"] if any_skipped else [])
    rows = []
    for sym, d in sorted(s.by_symbol.items(), key=lambda kv: -kv[1]["pnl"]):
        closed = d["wins"] + d["losses"]
        wl = f"{d['wins']} / {d['losses']}" + (f", {d['open']} open" if d["open"] else "")
        rate = f"{100 * d['wins'] / closed:.1f}%" if closed else "n/a"
        cls = "WIN" if d["pnl"] > 0 else "LOSS" if d["pnl"] < 0 else ""
        cells = [_E(sym), str(d["signals"] - d["skipped"]), _E(wl), rate,
                 f'<span class="{cls}">{d["pnl"]:+,.2f}</span>']
        if any_skipped:
            cells.append(str(d["skipped"]))
        rows.append("<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>")
    head = "".join(f"<th>{_E(c)}</th>" for c in cols)
    return (f'<h2>By market</h2><div class="wrap"><table class="mkt"><thead><tr>{head}</tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div>')


def _caption(t: Trade, chart_error: Optional[str]) -> str:
    head = f"#{t.id} {t.symbol} {t.side} at {t.entry:.2f}  (TP {t.take_profit:.2f}, SL {t.stop_loss:.2f})"
    if t.status == OPEN:
        tail = f'<span class="OPEN">still open at the end of the period, marked {t.mark_price:.2f}, unrealised {t.unrealised:+.2f}</span>'
    else:
        later = f", after {t.sessions_held} night(s)" if t.sessions_held else ""
        tail = (f'<span class="{t.status}">{t.status} {t.pnl:+.2f} ({t.r_multiple:+.2f}R)</span> closed '
                f"{_E(rpt.fmt_et(t.closed_at))} ({_E(t.exit_kind or '')} at {t.exit_price:.2f}){later}")
        if t.sessions_held:
            tail += ". The chart shows the entry session only."
    return f"<strong>{_E(head)}</strong><br>{tail}"


def build_html(
    summary: Summary,
    trades: list[Trade],
    equity_png: bytes,
    coverage: Optional[Coverage] = None,
    chart_paths: Optional[dict[int, str]] = None,
    chart_errors: Optional[dict[int, str]] = None,
    note: str = "",
) -> str:
    """`note`: an extra line under the title (e.g. non-default ORB settings); omitted when empty."""
    chart_paths, chart_errors = chart_paths or {}, chart_errors or {}
    rows = rpt.ledger_rows(trades)
    head = "".join(f"<th>{_E(c)}</th>" for c in rpt.LEDGER_COLUMNS)
    body = []
    for r in rows:
        cells = []
        for c in rpt.LEDGER_COLUMNS:
            v = _E(str(r[c]))
            if c == "status":
                v = f'<span class="{_E(r[c])}">{v}</span>'
            if c == "id" and int(r["id"]) in chart_paths:
                v = f'<a href="#t{int(r["id"])}">{v}</a>'
            cells.append(f"<td>{v}</td>")
        body.append("<tr>" + "".join(cells) + "</tr>")

    gallery = ""
    opened = [t for t in trades if t.status != SKIPPED]
    if chart_paths or chart_errors:
        figs = []
        for t in opened:
            err = chart_errors.get(t.id)
            if t.id in chart_paths:
                img = f'<img src="{_E(chart_paths[t.id])}" alt="trade {t.id} {_E(t.symbol)}" loading="lazy">'
            else:
                img = f'<div class="warn">chart failed: {_E(err or "not drawn")}</div>'
            figs.append(f'<figure id="t{t.id}">{img}<figcaption>{_caption(t, err)}</figcaption></figure>')
        gallery = "<h2>Trades</h2>" + "".join(figs)

    warnings = "".join(f'<div class="warn">{_E(w)}</div>' for w in summary.warnings)
    equity = base64.b64encode(equity_png).decode("ascii")
    cov = f"<h2>Data coverage</h2><pre>{_E(format_coverage(coverage))}</pre>" if coverage is not None else ""
    title = f"{summary.strategy} strategy check {summary.start} to {summary.end}"
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_E(title)}</title><style>{_CSS}</style></head>
<body><main>
<h1>{_E(title)}</h1>
<p class="sub">Markets {_E(', '.join(summary.symbols))}. Account {_money(summary.initial_cash)}, {_money(summary.per_deal)} per deal,
minimum cash {_money(summary.min_cash)}. Informational only: a simulation on stored 1-minute bars, not an order.</p>
{f'<p class="sub">{_E(note)}</p>' if note else ''}{_cards(summary)}
{warnings}
{_markets(summary)}
<h2>Account</h2>
<img src="data:image/png;base64,{equity}" alt="account: cash, invested and total equity">
<h2>Ledger</h2>
<div class="wrap"><table><thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table></div>
{gallery}
{cov}
<footer>Generated by MarketScanner strategy checker. Results below about 100 closed trades are not statistically meaningful.</footer>
</main></body></html>
"""
