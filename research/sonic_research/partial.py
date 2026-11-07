"""Half off at a small profit, rest at break-even: python -m sonic_research.partial

Same longs-only trend entries. When the price reaches `first_r` (in stop units), half the
position closes there and the stop on the other half moves to the entry price. The other
half exits like the current bot (trend reversal, 20 days, or its stop). Fees are paid on
every fill. Counts how often a trade ends with money lost, not only win rate.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .backtest import FEE, SLIPPAGE, Signals
from .capital import replay
from .data import BAR_MS, SYMBOLS, Bars, load_bars, load_funding, month_range
from .phase2 import END, START
from .run import month_ms
from .strategies import Params, btc_trend, build

SMALL = {"SOLUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT", "SUIUSDT", "TRXUSDT", "BNBUSDT", "AVAXUSDT"}
BASE = Params("trend", 20, 100, 3.0, None, 120, True)


@dataclass(frozen=True)
class Result:
    symbol: str
    side: int
    entry_t: int
    exit_t: int
    ret: float
    r: float


def simulate_partial(bars: Bars, signals: Signals, funding: dict[int, float],
                     first_r: float, max_hold: int = 120) -> list[Result]:
    o, h, low, c, t = bars.o, bars.h, bars.low, bars.c, bars.t
    n = len(bars)
    out: list[Result] = []
    next_free = 0
    for i in np.flatnonzero(signals.entry == 1):
        if i < next_free or i + 1 >= n:
            continue
        dist = float(signals.stop_dist[i])
        if not np.isfinite(dist) or dist <= 0:
            continue
        j = i + 1
        entry = o[j] * (1 + SLIPPAGE)
        stop = entry - dist
        first = entry + first_r * dist
        half_done = False
        booked = 0.0  # return on full position value from the first half
        fees = FEE  # entry fee on the whole position
        funding_paid = 0.0
        size = 1.0
        k = j
        exit_price = None
        while k < n:
            if k > j:
                funding_paid += size * funding.get(int(t[k]), 0.0)
            if o[k] <= stop:
                exit_price = o[k]
            elif low[k] <= stop:
                exit_price = stop
            elif not half_done and h[k] >= first:
                fill = max(o[k], first) * (1 - SLIPPAGE)
                booked += 0.5 * (fill - entry) / entry
                fees += 0.5 * FEE
                size = 0.5
                half_done = True
                stop = entry  # the rest can no longer lose (except gaps and costs)
            if exit_price is None:
                if signals.exit_long[k] or k - j + 1 >= max_hold or k == n - 1:
                    exit_price = c[k]
            if exit_price is not None:
                break
            k += 1
        fill = exit_price * (1 - SLIPPAGE)
        ret = booked + size * (fill - entry) / entry - fees - size * FEE - funding_paid
        out.append(Result(bars.symbol, 1, int(t[j]), int(t[k]) + BAR_MS, ret,
                          ret / (dist / entry)))
        next_free = k
    return out


def describe(results: list[Result]) -> str:
    r = np.array([x.r for x in results])
    return (f"n={len(r):4} win={np.mean(r > 0):5.1%} lost money={np.mean(r < -0.05):5.1%} "
            f"avgR={r.mean():+.3f} totalR={r.sum():+7.1f}")


def main() -> None:
    months = month_range(START, END)
    bars = {s: load_bars(s, months) for s in SYMBOLS}
    funding = {s: load_funding(s, months) for s in SYMBOLS}
    regime = {s: btc_trend(bars["BTCUSDT"], bars[s]) for s in SYMBOLS}
    split = month_ms("2024-07")
    for first_r in (0.5, 0.75, 1.0, 1.5):
        results: list[Result] = []
        for symbol in SYMBOLS:
            signals = build(bars[symbol], BASE, regime[symbol])
            results.extend(simulate_partial(bars[symbol], signals, funding[symbol], first_r))
        train = [x for x in results if x.exit_t <= split]
        test = [x for x in results if x.entry_t >= split]
        small = [x for x in results if x.symbol in SMALL]
        whole = replay(small, 30, 0.01, 3, 0.02)  # type: ignore[arg-type]
        print(f"half at {first_r}R | TRAIN {describe(train)}\n{'':12}| TEST  {describe(test)}"
              f"\n{'':12}| $30 account 2021-2026 -> {whole.final:.2f}, "
              f"max drop {whole.max_drawdown:.0%}")


if __name__ == "__main__":
    main()
