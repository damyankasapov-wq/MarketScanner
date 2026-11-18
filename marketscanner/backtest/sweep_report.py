"""
Sweep output: a text table (terminal and sweep.txt), sweep.csv with every column,
and sweep.html with the full table plus an in-sample vs out-of-sample scatter.

The banner always says how many variants were tried, that the top row is only
the in-sample best, what the R and account columns count, and the verdict from
the fixed rule. The current .env row is always shown and marked.
"""
from __future__ import annotations

import base64
import csv
import html
import io
from pathlib import Path
from typing import Optional

from marketscanner.backtest.html_report import _CSS
from marketscanner.backtest.metrics import Coverage, format_coverage
from marketscanner.backtest.sweep import HalfStats, Row, SweepResult

_E = html.escape
TOP_ROWS = 10

CSV_COLUMNS = [
    "rank_is", "rank_oos", "candle_minutes", "reward_ratio", "cutoff", "mark",
    "is_signals", "is_closed", "is_win_rate", "is_total_r", "is_avg_r", "is_se_avg_r",
    "oos_signals", "oos_closed", "oos_win_rate", "oos_total_r", "oos_avg_r", "oos_se_avg_r",
    "is_delta_r", "is_delta_r_se", "oos_delta_r", "oos_delta_r_se", "passes_rule",
    "is_to_oos", "buy_r", "sell_r", "too_few",
    "net_pnl", "return_pct", "max_drawdown_pct", "skipped_for_cash", "open_at_end",
]


def mark(row: Row, result: SweepResult) -> str:
    tags = []
    if row.is_baseline:
        tags.append("current .env")
    if result.best is row:
        tags.append("in-sample best")
    if row.passes:
        tags.append("passes rule")
    if row.too_few:
        tags.append("too few")
    return ", ".join(tags)


def _f(x: Optional[float], nd: int = 2) -> str:
    return "—" if x is None else f"{x:+.{nd}f}"


def _pm(x: Optional[float], se: Optional[float]) -> str:
    if x is None:
        return "—"
    return f"{x:+.2f} ± {2 * se:.2f}" if se is not None else f"{x:+.2f} ± —"


def _avg(h: HalfStats) -> str:
    return _pm(h.avg_r, h.se_r)


def banner(result: SweepResult, rerun: Optional[str] = None) -> list[str]:
    n = len(result.rows)
    b = result.baseline
    lines = [
        f"SETTINGS SWEEP: ORB   {result.start} to {result.end}   markets {', '.join(result.symbols)}",
        f"Account: start {result.cfg.initial_cash:,.2f}, {result.cfg.per_deal:,.2f} per deal, "
        f"minimum cash {result.cfg.min_cash:,.2f}",
        "",
        f"{n} variants tried. With this many, one usually looks good by chance: the top row is only the in-sample best.",
        f"In-sample: {len(result.is_dates)} sessions"
        + (f" ({result.is_dates[0]} to {result.is_dates[-1]})" if result.is_dates else "")
        + f". Out-of-sample: {len(result.oos_dates)} sessions"
        + (f" ({result.oos_dates[0]} to {result.oos_dates[-1]})." if result.oos_dates else "."),
        f"Baseline 'current .env' = {b.describe()} (this machine's .env; the scanner uses the server's .env, check they match).",
        "ΔR = summed R difference vs current .env, session by session, ± 2 SE clustered by date. Rows ranked by IS ΔR.",
        "R columns: every signal, no account. Account columns: what the simulated account could open, whole period.",
        "",
        result.verdict(),
    ]
    lines += [f"! {w}" for w in result.warnings]
    if rerun:
        lines += ["", "Full report for the in-sample best:", f"  {rerun}"]
    return lines


def table_lines(result: SweepResult, rows: list[Row]) -> list[str]:
    """ASCII cells, padded after translation: '±' becomes the 3-character '+/-', so translating
    afterwards would push every wide row out of its column."""
    a = lambda text: text.translate(_ASCII)  # noqa: E731
    head = (f"{'#':>3} {'c / r / cut-off':<17} {'IS n':>5} {'IS avg R':>16} {'OOS n':>5} {'OOS avg R':>16} "
            f"{'IS dR':>18} {'OOS dR':>18} {'IS->OOS':>7} {'BUY R':>7} {'SELL R':>7} {'P&L':>10} {'DD%':>7}  mark")
    out = [head, "-" * len(head)]
    for r in rows:
        s = r.summary
        out.append(
            f"{r.rank_is:>3} {r.settings.short():<17} {r.is_.closed:>5} {a(_avg(r.is_)):>16} {r.oos.closed:>5} "
            f"{a(_avg(r.oos)):>16} {a(_pm(r.d_is, r.se_d_is)):>18} {a(_pm(r.d_oos, r.se_d_oos)):>18} {r.is_to_oos:>7} "
            f"{r.buy_r:>+7.1f} {r.sell_r:>+7.1f} "
            f"{(s.net_pnl if s else 0):>10,.2f} {(s.max_drawdown_pct if s else 0):>7.2f}  {mark(r, result)}"
        )
    return out


def shown_rows(result: SweepResult) -> list[Row]:
    rows = result.rows[:TOP_ROWS]
    if not any(r.is_baseline for r in rows):
        rows = rows + [result.baseline_row]
    return rows


_ASCII = str.maketrans({"Δ": "d", "±": "+/-", "→": "->", "—": "-", "−": "-"})


def format_text(result: SweepResult, coverage: Optional[Coverage] = None, rerun: Optional[str] = None) -> str:
    """Plain ASCII (dR, +/-, ->) so it prints on any console, including Windows cp1252."""
    lines = banner(result, rerun) + [""]
    lines += table_lines(result, shown_rows(result))
    if len(result.rows) > TOP_ROWS:
        lines.append(f"(top {TOP_ROWS} by IS ΔR plus current .env; all {len(result.rows)} rows in sweep.csv / sweep.html)")
    if coverage is not None:
        lines += ["", format_coverage(coverage)]
    lines += ["", "Informational only: a simulation on stored 1-minute bars, not an order and not a promise."]
    return "\n".join(lines).translate(_ASCII)


def csv_rows(result: SweepResult) -> list[dict]:
    out = []
    for r in result.rows:
        s = r.summary
        cut = r.settings.cutoff
        out.append({
            "rank_is": r.rank_is, "rank_oos": r.rank_oos,
            "candle_minutes": r.settings.candle_minutes, "reward_ratio": r.settings.reward_ratio,
            "cutoff": "" if cut is None else f"{cut:%H:%M}", "mark": mark(r, result),
            "is_signals": r.is_.signals, "is_closed": r.is_.closed, "is_win_rate": r.is_.win_rate,
            "is_total_r": r.is_.total_r, "is_avg_r": r.is_.avg_r, "is_se_avg_r": r.is_.se_r,
            "oos_signals": r.oos.signals, "oos_closed": r.oos.closed, "oos_win_rate": r.oos.win_rate,
            "oos_total_r": r.oos.total_r, "oos_avg_r": r.oos.avg_r, "oos_se_avg_r": r.oos.se_r,
            "is_delta_r": r.d_is, "is_delta_r_se": r.se_d_is, "oos_delta_r": r.d_oos, "oos_delta_r_se": r.se_d_oos,
            "passes_rule": r.passes, "is_to_oos": r.is_to_oos, "buy_r": r.buy_r, "sell_r": r.sell_r,
            "too_few": r.too_few,
            "net_pnl": s.net_pnl if s else None, "return_pct": s.return_pct if s else None,
            "max_drawdown_pct": s.max_drawdown_pct if s else None,
            "skipped_for_cash": s.skipped if s else None, "open_at_end": s.open if s else None,
        })
    return out


def write_csv(result: SweepResult, path: Path) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
        w.writeheader()
        for row in csv_rows(result):
            w.writerow({k: ("" if v is None else (round(v, 6) if isinstance(v, float) else v)) for k, v in row.items()})


def scatter_png(result: SweepResult) -> bytes:
    """IS avg R (x) vs OOS avg R (y), one dot per variant; points far below the diagonal are overfit."""
    from matplotlib.figure import Figure

    fig = Figure(figsize=(6.4, 4.6), dpi=100)
    ax = fig.add_subplot(1, 1, 1)
    pts = [(r.is_.avg_r, r.oos.avg_r, r) for r in result.rows if r.is_.avg_r is not None and r.oos.avg_r is not None]
    if pts:
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        lo, hi = min(xs + ys + [0.0]), max(xs + ys + [0.0])
        pad = (hi - lo) * 0.1 or 0.05
        ax.plot([lo - pad, hi + pad], [lo - pad, hi + pad], color="#9ca3af", linewidth=1, linestyle="--", zorder=1)
        ax.axhline(0, color="#d1d5db", linewidth=0.8, zorder=0)
        ax.axvline(0, color="#d1d5db", linewidth=0.8, zorder=0)
        ax.scatter(xs, ys, s=22, color="#2563eb", alpha=0.75, zorder=2)
        for x, y, r in pts:
            if r.is_baseline or result.best is r:
                label = "current .env" if r.is_baseline else "in-sample best"
                ax.scatter([x], [y], s=60, facecolors="none", edgecolors="#111827", linewidths=1.4, zorder=3)
                ax.annotate(f"{label}\n{r.settings.short()}", (x, y), xytext=(6, 6), textcoords="offset points",
                            fontsize=7, color="#111827")
    else:
        ax.text(0.5, 0.5, "no variant has closed trades in both halves", ha="center", va="center",
                transform=ax.transAxes, color="#6b7280")
    ax.set_xlabel("in-sample average R")
    ax.set_ylabel("out-of-sample average R")
    ax.set_title("Each dot is one variant (dashed line: OOS = IS)", fontsize=9)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight")
    return buf.getvalue()


def build_html(result: SweepResult, coverage: Optional[Coverage] = None, rerun: Optional[str] = None) -> str:
    cols = ["#", "candle / reward / cut-off", "IS closed", "IS avg R ± 2SE", "OOS closed", "OOS avg R ± 2SE",
            "IS ΔR ± 2SE", "OOS ΔR ± 2SE", "IS→OOS", "BUY R", "SELL R", "P&L", "max DD %", "mark"]
    head = "".join(f"<th>{_E(c)}</th>" for c in cols)
    body = []
    for r in result.rows:
        s = r.summary
        cls = []
        if r.too_few:
            cls.append("few")
        if r.is_baseline:
            cls.append("base")
        sign = "WIN" if r.d_oos > 0 else ("LOSS" if r.d_oos < 0 else "")
        cells = [
            str(r.rank_is), r.settings.short(), str(r.is_.closed), _avg(r.is_), str(r.oos.closed), _avg(r.oos),
            _pm(r.d_is, r.se_d_is), f'<span class="{sign}">{_E(_pm(r.d_oos, r.se_d_oos))}</span>',
            str(r.is_to_oos), f"{r.buy_r:+.1f}", f"{r.sell_r:+.1f}",
            f"{(s.net_pnl if s else 0):,.2f}", f"{(s.max_drawdown_pct if s else 0):.2f}", _E(mark(r, result)),
        ]
        cells = [c if c.startswith("<span") else _E(c) for c in cells]
        body.append(f'<tr class="{" ".join(cls)}">' + "".join(f"<td>{c}</td>" for c in cells) + "</tr>")
    lines = banner(result, rerun)
    title, sub = lines[0], lines[1]
    notes = "".join(f"<p>{_E(l)}</p>" for l in lines[3:] if l and not l.startswith("!") and not l.startswith("VERDICT"))
    warns = "".join(f'<div class="warn">{_E(l[2:])}</div>' for l in lines if l.startswith("! "))
    verdict = f'<div class="warn"><b>{_E(result.verdict())}</b></div>'
    png = base64.b64encode(scatter_png(result)).decode("ascii")
    cov = f"<h2>Data coverage</h2><pre>{_E(format_coverage(coverage))}</pre>" if coverage is not None else ""
    css = _CSS + "tr.few td{color:var(--mut)}tr.base td{font-weight:600}.sub p{margin:2px 0}"
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_E(title)}</title><style>{css}</style></head>
<body><main>
<h1>{_E(title)}</h1>
<p class="sub">{_E(sub)}. Informational only: a simulation on stored 1-minute bars, not an order.</p>
{verdict}
{warns}
<div class="sub">{notes}</div>
<h2>Variants</h2>
<div class="wrap"><table><thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table></div>
<h2>In-sample vs out-of-sample</h2>
<img src="data:image/png;base64,{png}" alt="in-sample vs out-of-sample average R per variant">
{cov}
<footer>Generated by MarketScanner settings sweep. Rows marked "too few" have under 100 closed trades in a half.</footer>
</main></body></html>
"""


def write_all(result: SweepResult, out_dir: Path, coverage: Optional[Coverage] = None,
              rerun: Optional[str] = None) -> str:
    out_dir.mkdir(parents=True, exist_ok=True)
    text = format_text(result, coverage, rerun)
    (out_dir / "sweep.txt").write_text(text, encoding="utf-8")
    write_csv(result, out_dir / "sweep.csv")
    (out_dir / "sweep.html").write_text(build_html(result, coverage, rerun), encoding="utf-8")
    return text
