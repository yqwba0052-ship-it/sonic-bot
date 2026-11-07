"""Details for one parameter set: python -m sonic_research.inspect_best <set name>"""

from __future__ import annotations

import sys

import numpy as np

from .data import SYMBOLS, load_bars, load_funding, month_range
from .metrics import summarize
from .phase2 import END, START, run_set
from .run import grid, month_ms
from .strategies import btc_trend


def concurrent(trades) -> tuple[float, int]:
    events = sorted([(t.entry_t, 1) for t in trades] + [(t.exit_t, -1) for t in trades])
    level, peak, weighted, last_t = 0, 0, 0.0, events[0][0]
    for time, step in events:
        weighted += level * (time - last_t)
        last_t = time
        level += step
        peak = max(peak, level)
    return weighted / (events[-1][0] - events[0][0]), peak


def drawdown(trades, risk: float) -> tuple[float, float]:
    r = np.clip(np.array([t.r for t in sorted(trades, key=lambda t: t.exit_t)]), -3, None)
    equity = np.cumprod(1 + risk * r)
    peak = np.maximum.accumulate(np.concatenate([[1.0], equity]))[1:]
    return float(equity[-1] - 1), float((1 - equity / peak).max())


def main() -> None:
    name = sys.argv[1]
    params = {p.name: p for p in grid()}[name]
    months = month_range(START, END)
    bars = {s: load_bars(s, months) for s in SYMBOLS}
    funding = {s: load_funding(s, months) for s in SYMBOLS}
    regime = {s: btc_trend(bars["BTCUSDT"], bars[s]) for s in SYMBOLS}
    trades = run_set(params, bars, funding, regime)
    split = month_ms("2024-07")
    test = [t for t in trades if t.entry_t >= split]
    stop_pct = np.array([abs(t.ret / t.r) for t in trades if t.r])
    wins = [t.ret for t in trades if t.ret > 0]
    losses = [t.ret for t in trades if t.ret <= 0]
    avg_open, max_open = concurrent(trades)
    years = (trades[-1].exit_t - trades[0].entry_t) / (365.25 * 86400000)
    print(f"set {name}: {len(trades)} trades over {years:.1f} years, longs "
          f"{sum(t.side == 1 for t in trades)}, shorts {sum(t.side == -1 for t in trades)}")
    print(f"win rate {len(wins) / len(trades):.1%}, avg win {np.mean(wins):.2%}, "
          f"avg loss {np.mean(losses):.2%} (of position value)")
    median, p90 = np.median(stop_pct), np.percentile(stop_pct, 90)
    print(f"stop distance median {median:.1%}, 90th pct {p90:.1%}")
    print(f"open at once: average {avg_open:.1f}, most {max_open}")
    for risk in (0.01, 0.005, 0.0025):
        total, dd = drawdown(trades, risk)
        ttotal, tdd = drawdown(test, risk)
        print(f"risk {risk:.2%}/trade: whole {total:+.0%} DD {dd:.0%} | "
              f"test {ttotal:+.0%} DD {tdd:.0%}")
    print("by coin avg R:", {s: round(float(np.mean([t.r for t in trades if t.symbol == s])), 2)
                             for s in SYMBOLS})
    print("test summary:", summarize(test))


if __name__ == "__main__":
    main()
