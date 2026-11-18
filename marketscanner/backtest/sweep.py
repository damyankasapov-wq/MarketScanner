"""
Settings sweep: the same period, every combination of candle size, reward ratio
and confirmation cut-off, each compared with the current .env settings.

Work is shared wherever the ORB rules allow (see strategy.py):

    load_frames ──────────────────────────────────────────── once
      for candle:   plain ideas (reward 1, no cut-off) ───── once per candle size
        for reward: derive + find_exits ──────────────────── once per (candle, reward)
          for cut-off: derive (filter only, same exits) ──── per variant
              ├─▶ R per idea ─▶ IS / OOS stats, paired ΔR vs baseline
              └─▶ run_engine + build_equity + summarize ─▶ account columns

Guards against fooling yourself:
  - chronological split: the first ceil(2/3) of the sessions with data are
    in-sample (IS), the rest out-of-sample (OOS); a trade belongs to the half of
    its entry session and is followed to its real exit, even into the other half;
  - every row is compared with the baseline (current .env) session by session:
    ΔR is the summed difference in R per (symbol, session), 0 where a side has no
    closed trade, and its standard error is clustered by date (SPY and QQQ often
    signal on the same day);
  - rows are ranked by IS ΔR only; OOS is shown next to every row;
  - a fixed verdict rule decides "better than current .env": OOS ΔR > 2 SE and IS ΔR > 0.

R columns count every signal, without the account; the account columns come
from running each variant through the simulated account.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, time
from itertools import product
from typing import Callable, Optional

import numpy as np
import pandas as pd
import pytz

from marketscanner import config
from marketscanner.backtest.engine import AccountConfig, find_exits, run_engine, session_indexes
from marketscanner.backtest.equity import build_equity, frame_grid_ns
from marketscanner.backtest.ideas import Frames, IdeasResult, SessionInfo, ideas_from_frames
from marketscanner.backtest.metrics import MIN_TRUSTWORTHY_TRADES, Summary, summarize
from marketscanner.backtest.strategy import OrbSettings, OrbStrategy, TradeIdea
from marketscanner.strategies.orb_session import ExitHit

_ET = pytz.timezone(config.TIMEZONE)

DEFAULT_CANDLES = (5, 10, 15)
DEFAULT_REWARDS = (1.0, 1.5, 2.0)
DEFAULT_CUTOFFS: tuple[Optional[time], ...] = (None, time(13, 0), time(14, 0))
MANY_VARIANTS = 60
IS_FRACTION = 2 / 3


@dataclass(frozen=True)
class Grid:
    candles: tuple[int, ...] = DEFAULT_CANDLES
    rewards: tuple[float, ...] = DEFAULT_REWARDS
    cutoffs: tuple[Optional[time], ...] = DEFAULT_CUTOFFS

    def variants(self) -> list[OrbSettings]:
        """Every combination, in grid order (candle, then reward, then cut-off)."""
        return [OrbSettings(c, r, x) for c, r, x in product(self.candles, self.rewards, self.cutoffs)]

    def tag(self) -> str:
        """Folder-name part naming a non-default grid ('' for the default), so a narrower sweep of the
        same dates gets its own folder instead of overwriting the full one."""
        if self == Grid():
            return ""
        cut = "-".join("none" if x is None else f"{x:%H%M}" for x in self.cutoffs)
        return (f"c{'-'.join(str(c) for c in self.candles)}_r{'-'.join(f'{r:g}' for r in self.rewards)}"
                f"_x{cut}")


@dataclass
class HalfStats:
    signals: int = 0
    closed: int = 0
    wins: int = 0
    open: int = 0
    total_r: float = 0.0
    avg_r: Optional[float] = None
    se_r: Optional[float] = None          # standard error of avg_r (sample SD, ddof=1, / sqrt(n))

    @property
    def win_rate(self) -> Optional[float]:
        return self.wins / self.closed * 100.0 if self.closed else None


@dataclass
class Row:
    settings: OrbSettings
    is_baseline: bool
    is_: HalfStats
    oos: HalfStats
    d_is: float = 0.0                     # ΔR vs baseline, summed over the IS sessions
    se_d_is: Optional[float] = None
    d_oos: float = 0.0
    se_d_oos: Optional[float] = None
    is_to_oos: int = 0                    # IS trades whose exit falls in the OOS half
    buy_r: float = 0.0                    # whole period, closed trades
    sell_r: float = 0.0
    summary: Optional[Summary] = None     # account run over the whole period
    rank_is: int = 0
    rank_oos: int = 0

    @property
    def too_few(self) -> bool:
        return self.is_.closed < MIN_TRUSTWORTHY_TRADES or self.oos.closed < MIN_TRUSTWORTHY_TRADES

    @property
    def passes(self) -> bool:
        """The fixed verdict rule: OOS ΔR > 2 SE and IS ΔR > 0."""
        if self.is_baseline or self.se_d_oos is None:
            return False
        return self.d_oos > 2 * self.se_d_oos and self.d_is > 0


@dataclass
class SweepResult:
    rows: list[Row]                       # ranked by IS ΔR (best first)
    baseline: OrbSettings
    is_dates: list[date]
    oos_dates: list[date]
    sessions: list[SessionInfo]
    symbols: list[str]
    start: date
    end: date
    cfg: AccountConfig
    warnings: list[str] = field(default_factory=list)

    @property
    def baseline_row(self) -> Row:
        return next(r for r in self.rows if r.is_baseline)

    @property
    def best(self) -> Optional[Row]:
        """The top-ranked row (in-sample best), or None if the baseline ranks first."""
        top = self.rows[0] if self.rows else None
        return None if top is None or top.is_baseline else top

    @property
    def winners(self) -> list[Row]:
        return [r for r in self.rows if r.passes]

    def verdict(self) -> str:
        w = self.winners
        if not w:
            return "VERDICT: no variant beats current .env (rule: OOS ΔR > 2 SE and IS ΔR > 0)."
        names = ", ".join(r.settings.short() for r in w)
        return f"VERDICT: {len(w)} variant(s) beat current .env by the fixed rule (OOS ΔR > 2 SE and IS ΔR > 0): {names}."


# ── per-idea R and statistics ────────────────────────────────────────────────

def idea_r(idea: TradeIdea, hit: Optional[ExitHit]) -> Optional[float]:
    """R of one closed idea: sign x (exit - entry) / box width; None while open."""
    if hit is None or not idea.delta:
        return None
    sign = 1.0 if idea.side == "BUY" else -1.0
    return sign * (hit.price - idea.entry) / idea.delta


def split_sessions_is_oos(sessions: list[SessionInfo]) -> tuple[list[date], list[date]]:
    """Dates where at least one symbol has a usable session, split chronologically 2/3 : 1/3."""
    ok = sorted({s.date for s in sessions if s.status == "ok"})
    k = math.ceil(len(ok) * IS_FRACTION)
    return ok[:k], ok[k:]


def half_stats(rs: list[Optional[float]]) -> HalfStats:
    closed = [r for r in rs if r is not None]
    h = HalfStats(signals=len(rs), closed=len(closed), wins=sum(r > 0 for r in closed),
                  open=len(rs) - len(closed), total_r=float(sum(closed)))
    if closed:
        h.avg_r = float(np.mean(closed))
    if len(closed) >= 2:
        h.se_r = float(np.std(closed, ddof=1) / math.sqrt(len(closed)))
    return h


def paired_delta(variant: dict, baseline: dict, dates: list[date]) -> tuple[float, Optional[float]]:
    """
    Summed R difference vs the baseline over `dates`, and its standard error
    clustered by date. `variant` / `baseline` map (symbol, session_date) -> R of a
    closed trade; a missing key counts as 0 (no trade, or still open).
    Per date d: D_d = sum over symbols of (R_variant - R_baseline);
    ΔR = sum(D_d); SE(ΔR) = sqrt(n) x SD(D_d, ddof=1).
    """
    if not dates:
        return 0.0, None
    keys_by_date: dict[date, set] = {}
    for k in set(variant) | set(baseline):
        keys_by_date.setdefault(k[1], set()).add(k)
    d = np.array([sum(variant.get(k, 0.0) - baseline.get(k, 0.0) for k in keys_by_date.get(day, ()))
                  for day in dates], dtype=float)
    total = float(d.sum())
    se = float(math.sqrt(len(d)) * np.std(d, ddof=1)) if len(d) >= 2 else None
    return total, se


# ── the sweep ────────────────────────────────────────────────────────────────

def run_sweep(
    loaded: Frames,
    symbols: list[str],
    start: date,
    end: date,
    cfg: AccountConfig,
    grid: Grid,
    baseline: Optional[OrbSettings] = None,
    strategy_cls=OrbStrategy,
    progress: Optional[Callable[[str], None]] = None,
) -> SweepResult:
    if not callable(getattr(strategy_cls, "derive", None)):
        raise ValueError(f"strategy {getattr(strategy_cls, 'name', strategy_cls)!r} cannot be swept: it has no derive()")
    say = progress or (lambda _msg: None)
    baseline = baseline or OrbSettings.current()
    variants = grid.variants()
    warnings: list[str] = []
    if baseline not in variants:
        variants.append(baseline)
    if len(variants) > MANY_VARIANTS:
        warnings.append(f"{len(variants)} variants: with this many, several will look good by chance alone.")

    frames = loaded.frames
    plain: dict[int, IdeasResult] = {}
    for c in sorted({v.candle_minutes for v in variants}):
        say(f"ideas for the {c}-min candle")
        plain[c] = ideas_from_frames(loaded, strategy_cls(settings=OrbSettings(c)), symbols, start, end)
    sessions = next(iter(plain.values())).sessions
    is_dates, oos_dates = split_sessions_is_oos(sessions)
    is_set = set(is_dates)
    split = oos_dates[0] if oos_dates else None

    exits: dict[tuple[int, float], list[Optional[ExitHit]]] = {}
    for c, r in sorted({(v.candle_minutes, v.reward_ratio) for v in variants}):
        say(f"exits for {c}-min candle, reward {r:g}")
        derived = [strategy_cls.derive(i, OrbSettings(c, r)) for i in plain[c].ideas]
        exits[(c, r)] = find_exits(derived, frames)

    sidx = session_indexes(frames)
    base_grid = frame_grid_ns(frames)
    rows: list[Row] = []
    r_maps: dict[OrbSettings, tuple[dict, dict]] = {}
    for v in variants:
        ideas, hits = [], []
        for i, h in zip(plain[v.candle_minutes].ideas, exits[(v.candle_minutes, v.reward_ratio)]):
            d = strategy_cls.derive(i, v)
            if d is not None:
                ideas.append(d)
                hits.append(h)
        rs = [idea_r(i, h) for i, h in zip(ideas, hits)]
        in_is = [i.session_date in is_set for i in ideas]
        row = Row(
            settings=v,
            is_baseline=(v == baseline),
            is_=half_stats([r for r, f in zip(rs, in_is) if f]),
            oos=half_stats([r for r, f in zip(rs, in_is) if not f]),
            buy_r=float(sum(r for r, i in zip(rs, ideas) if r is not None and i.side == "BUY")),
            sell_r=float(sum(r for r, i in zip(rs, ideas) if r is not None and i.side == "SELL")),
        )
        if split is not None:
            row.is_to_oos = sum(1 for h, f in zip(hits, in_is)
                                if f and h is not None and pd.Timestamp(h.ts).tz_convert(_ET).date() >= split)
        r_is = {(i.symbol, i.session_date): r for i, r, f in zip(ideas, rs, in_is) if r is not None and f}
        r_oos = {(i.symbol, i.session_date): r for i, r, f in zip(ideas, rs, in_is) if r is not None and not f}
        r_maps[v] = (r_is, r_oos)
        say(f"account for {v.short()}")
        trades = run_engine(ideas, hits, frames, cfg, symbols, session_index=sidx)
        equity = build_equity(trades, frames, cfg, base_grid=base_grid)
        row.summary = summarize(trades, equity, cfg, strategy_cls.name, start, end, symbols)
        rows.append(row)

    b_is, b_oos = r_maps[baseline]
    for row in rows:
        v_is, v_oos = r_maps[row.settings]
        row.d_is, row.se_d_is = paired_delta(v_is, b_is, is_dates)
        row.d_oos, row.se_d_oos = paired_delta(v_oos, b_oos, oos_dates)

    order = {v: k for k, v in enumerate(variants)}
    rows.sort(key=lambda r: (-r.d_is, order[r.settings]))
    for k, r in enumerate(rows, 1):
        r.rank_is = k
    for k, r in enumerate(sorted(rows, key=lambda r: (-r.d_oos, order[r.settings])), 1):
        r.rank_oos = k

    if rows and all(r.too_few for r in rows):
        warnings.append(f"Every variant has fewer than {MIN_TRUSTWORTHY_TRADES} closed trades in at least one half: "
                        "the comparison is not statistically meaningful.")
    if not oos_dates:
        warnings.append("The period is too short for an out-of-sample half.")
    return SweepResult(rows=rows, baseline=baseline, is_dates=is_dates, oos_dates=oos_dates, sessions=sessions,
                       symbols=symbols, start=start, end=end, cfg=cfg, warnings=warnings)
