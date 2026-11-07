"""Bar-by-bar trade simulation with realistic costs.

Rules that keep the test honest:
- A signal seen at the close of bar i is filled at the open of bar i + 1 (no look-ahead).
- If a bar touches both the stop and the target, the stop counts (worst case).
- A gap through the stop fills at the open, not at the stop price.
- Every fill pays the taker fee and slippage; held positions pay or receive funding.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .data import BAR_MS, Bars

FEE = 0.0005
SLIPPAGE = 0.0002


@dataclass(frozen=True)
class Trade:
    symbol: str
    side: int  # +1 long, -1 short
    entry_t: int
    exit_t: int
    entry: float
    exit: float
    ret: float  # net return on the position value, after fees, slippage and funding
    r: float  # net result in units of the planned stop distance
    reason: str


@dataclass(frozen=True)
class Signals:
    entry: np.ndarray  # +1, -1 or 0 at each bar close
    exit_long: np.ndarray  # bool: close longs at this bar's close
    exit_short: np.ndarray  # bool: close shorts at this bar's close
    stop_dist: np.ndarray  # price distance from entry to stop, per signal bar


def simulate(
    bars: Bars,
    signals: Signals,
    target_r: float | None,
    max_hold: int,
    funding: dict[int, float],
    slippage: float = SLIPPAGE,
    fee: float = FEE,
) -> list[Trade]:
    o, h, low, c, t = bars.o, bars.h, bars.low, bars.c, bars.t
    n = len(bars)
    trades: list[Trade] = []
    candidates = np.flatnonzero(signals.entry)
    position = 0
    next_free = 0
    for i in candidates:
        if i < next_free or i + 1 >= n:
            continue
        side = int(signals.entry[i])
        dist = float(signals.stop_dist[i])
        if not np.isfinite(dist) or dist <= 0:
            continue
        j = i + 1
        entry = o[j] * (1 + side * slippage)
        stop = entry - side * dist
        target = entry + side * target_r * dist if target_r else None
        funding_paid = 0.0
        exit_price = None
        reason = ""
        k = j
        while k < n:
            if k > j:
                funding_paid += side * _funding_in_bar(funding, int(t[k]))
            if side == 1:
                if o[k] <= stop:
                    exit_price, reason = o[k], "stop_gap"
                elif low[k] <= stop:
                    exit_price, reason = stop, "stop"
                elif target is not None and o[k] >= target:
                    exit_price, reason = o[k], "target_gap"
                elif target is not None and h[k] >= target:
                    exit_price, reason = target, "target"
            else:
                if o[k] >= stop:
                    exit_price, reason = o[k], "stop_gap"
                elif h[k] >= stop:
                    exit_price, reason = stop, "stop"
                elif target is not None and o[k] <= target:
                    exit_price, reason = o[k], "target_gap"
                elif target is not None and low[k] <= target:
                    exit_price, reason = target, "target"
            if exit_price is None:
                flip = signals.exit_long[k] if side == 1 else signals.exit_short[k]
                if flip:
                    exit_price, reason = c[k], "signal"
                elif k - j + 1 >= max_hold:
                    exit_price, reason = c[k], "time"
                elif k == n - 1:
                    exit_price, reason = c[k], "end"
            if exit_price is not None:
                break
            k += 1
        filled = exit_price * (1 - side * slippage)
        gross = side * (filled - entry) / entry
        ret = gross - 2 * fee - funding_paid
        trades.append(
            Trade(
                symbol=bars.symbol,
                side=side,
                entry_t=int(t[j]),
                exit_t=int(t[k]) + BAR_MS,
                entry=float(entry),
                exit=float(filled),
                ret=float(ret),
                r=float(ret / (dist / entry)),
                reason=reason,
            )
        )
        position = 0
        next_free = k  # a new signal at this bar's close may enter on the next bar
    del position
    return trades


def _funding_in_bar(funding: dict[int, float], bar_open: int) -> float:
    """Funding settled at this bar's open, while the position was already held."""
    return funding.get(bar_open, 0.0)
