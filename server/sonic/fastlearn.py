"""Weekly choice of the fast desk's active variant, from the desk's own shadow results.

Rules (design 2026-11-23, approved by the owner):
- review every Monday 00:00 UTC over the last 14 days;
- a variant needs at least 20 finished trades in that window;
- a variant whose weekly mean was negative in 3 consecutive weeks ending within the last 4
  weeks is excluded;
- nothing qualifies -> keep the current variant;
- the best qualifying variant is chosen even if it is negative (paper, to keep learning),
  with no_profitable=True so the owner sees a clear warning.
Pure functions: no network, no writes.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

DAY_MS = 86_400_000
WEEK_MS = 7 * DAY_MS
MONDAY_OFFSET_MS = 4 * DAY_MS  # 1970-01-01 was a Thursday; Monday 1970-01-05 = 4 days later
WINDOW_MS = 14 * DAY_MS
MIN_TRADES = 20
BAD_WEEKS = 3
BAD_LOOKBACK_WEEKS = 4


@dataclass
class Choice:
    variant: str
    changed: bool
    no_profitable: bool
    reason: str
    stats: dict = field(default_factory=dict)


def week_index(ms: int) -> int:
    return (ms - MONDAY_OFFSET_MS) // WEEK_MS


def is_review_due(last_review_ms: int, now_ms: int) -> bool:
    """True when a Monday 00:00 UTC lies in (last_review_ms, now_ms]."""
    return week_index(now_ms) > week_index(last_review_ms)


def excluded(rows: list[tuple[str, int, float]], now_ms: int) -> set[str]:
    weekly: dict[str, dict[int, list[float]]] = defaultdict(lambda: defaultdict(list))
    for variant, closed_ms, net in rows:
        weekly[variant][week_index(closed_ms)].append(net)
    this_week = week_index(now_ms)
    recent = range(this_week - BAD_LOOKBACK_WEEKS, this_week)  # finished weeks only
    out = set()
    for variant, weeks in weekly.items():
        run = 0
        for w in recent:
            nets = weeks.get(w)
            run = run + 1 if nets and sum(nets) / len(nets) < 0 else 0
            if run >= BAD_WEEKS:
                out.add(variant)
                break
    return out


def choose(rows: list[tuple[str, int, float]], now_ms: int, current: str) -> Choice:
    """rows: (variant, closed_ms, net) of finished shadow trades."""
    window = [r for r in rows if now_ms - WINDOW_MS <= r[1] <= now_ms]
    by_variant: dict[str, list[float]] = defaultdict(list)
    for variant, _, net in window:
        by_variant[variant].append(net)
    bad = excluded(rows, now_ms)
    stats = {v: {"n": len(n), "mean": sum(n) / len(n)} for v, n in by_variant.items()}
    ok = {v: s for v, s in stats.items() if s["n"] >= MIN_TRADES and v not in bad}
    if not ok:
        return Choice(current, False, False,
                      f"min_trades: no variant has {MIN_TRADES}+ trades in 14 days "
                      f"(excluded {len(bad)})", stats)
    best = max(ok, key=lambda v: (ok[v]["mean"], ok[v]["n"]))
    no_profit = ok[best]["mean"] <= 0
    reason = (f"best mean {100 * ok[best]['mean']:+.4f}% over {ok[best]['n']} trades "
              f"in 14 days; {len(ok)} qualified, {len(bad)} excluded")
    return Choice(best, best != current, no_profit, reason, stats)
