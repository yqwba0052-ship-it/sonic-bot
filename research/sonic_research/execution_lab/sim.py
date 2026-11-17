"""Sonic's exit rules replayed on 1-minute candles after a given entry (any execution style).

Levels come from the entry actually paid (Sonic computes them from its fill price):
stop = floor(eff - dist), half = floor(qty / 2), target = floor(eff + 0.20 * dist) if half is
tradable. Inside one minute the stop is checked before the target (cautious). After the half,
the stop moves to the entry from the next minute on. A trend-exit or 240-bar exit happens at the
4h close `market_exit_ms` (it depends only on 4h bars, not on the entry). Gaps fill at the open.
Exit costs are Sonic's for every case: 0.02% slippage + 0.05% taker fee.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "server"))

from sonic import strategy as sonic  # noqa: E402
from sonic.risk import Filters, floor_to  # noqa: E402

from ..data import Bars  # noqa: E402
from ..jesse_audit.costs import FEE_RATE, SLIPPAGE  # noqa: E402

MIN = 60_000
FOUR_H = 4 * 3_600_000


def exit_path(minutes: np.ndarray, start_ms: int, eff: float, qty: float, dist: float,
              f: Filters, market_exit_ms: int, market_reason: str = "trade_exit_signal",
              no_target_before: int | None = None) -> tuple[list[dict], str]:
    """start_ms may fall inside a minute (a fill mid-minute): that minute is checked for the
    stop, but not for the target when it began before `no_target_before` (it may have been
    reached before we were filled)."""
    stop = floor_to(eff - dist, f.tick_size)
    half = floor_to(qty / 2, f.step_size)
    target = floor_to(eff + sonic.HALF_AT_R * dist, f.tick_size) if half >= f.min_qty else None
    legs, left, scaled = [], qty, False
    i = int(np.searchsorted(minutes[:, 0], start_ms // MIN * MIN))
    while i < len(minutes):
        t, o, c, h, low = (float(x) for x in minutes[i, :5])
        end = int(t) + MIN
        if end > market_exit_ms:
            break
        if low <= stop:
            legs.append({"kind": "exit", "ms": end, "raw": min(o, stop), "qty": left})
            return legs, "breakeven" if scaled else "stop_loss"
        early = no_target_before is not None and t < no_target_before
        if target is not None and h >= target and not early:
            legs.append({"kind": "half", "ms": end, "raw": max(o, target), "qty": half})
            left, scaled, target, stop = left - half, True, None, eff
        if end == market_exit_ms:
            legs.append({"kind": "exit", "ms": end, "raw": c, "qty": left})
            return legs, market_reason
        i += 1
    return legs, "open_at_end"


def pnl(entry_eff: float, entry_fee: float, legs: list[dict]) -> float:
    """Net result: entry paid at entry_eff (+ entry_fee in $), every exit at Sonic's costs."""
    total = -entry_fee
    for leg in legs:
        out = leg["raw"] * (1 - SLIPPAGE)
        total += leg["qty"] * (out - entry_eff) - leg["qty"] * out * FEE_RATE
    return total


def market_exit_time(fours: Bars, signal_ms: int) -> tuple[int, str]:
    """First 4h close after the signal where Sonic's trend exit fires, or the 240-bar limit."""
    closes_at = fours.t + FOUR_H
    for k in range(1, sonic.MAX_HOLD_BARS + 1):
        tau = signal_ms + k * FOUR_H
        idx = np.nonzero(closes_at <= tau)[0][-sonic.HISTORY_BARS:]
        bars = [sonic.Bar(int(fours.t[j]), float(fours.o[j]), float(fours.h[j]),
                          float(fours.low[j]), float(fours.c[j])) for j in idx]
        if sonic.exit_due(bars, "BUY"):
            return tau, "trade_exit_signal"
    return signal_ms + sonic.MAX_HOLD_BARS * FOUR_H, "trade_exit_time"
