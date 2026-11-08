"""Can the live setup be improved? python -m sonic_research.improve

Base = the bot today: longs only, 20/100 EMA cross on 4h, BTC above its 300-EMA, stop 3 ATR,
half closed at +0.25R then stop to entry, rest exits on the reverse cross or after 120 bars.
Each variant changes ONE thing. A variant is only worth adopting if it beats the base on the
training years AND on the unseen years, and its neighbours agree.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .backtest import FEE, SLIPPAGE
from .capital import replay
from .data import BAR_MS, Bars, load_bars, load_funding, month_range
from .phase2 import END, START
from .run import month_ms
from .strategies import atr, btc_trend, ema

SMALL = ["SOLUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT", "SUIUSDT", "TRXUSDT", "BNBUSDT", "AVAXUSDT"]


@dataclass(frozen=True)
class Variant:
    name: str
    stop_k: float = 3.0
    half_r: float = 0.25
    trail_k: float | None = None  # trail the second half by this many ATR after the half
    max_hold: int = 120
    regime: int = 300
    reentry: bool = False  # also buy pullbacks that recover above the fast EMA in an uptrend
    fast: int = 20
    slow: int = 100


@dataclass(frozen=True)
class Result:
    symbol: str
    entry_t: int
    exit_t: int
    ret: float
    r: float


def simulate(bars: Bars, v: Variant, btc_up: np.ndarray, funding: dict[int, float]) -> list[Result]:
    o, h, low, c, t = bars.o, bars.h, bars.low, bars.c, bars.t
    n = len(bars)
    fast, slow, vol = ema(c, v.fast), ema(c, v.slow), atr(bars)
    above = fast > slow
    valid = ~np.isnan(slow) & ~np.isnan(vol)
    cross = valid & above & np.concatenate([[False], ~above[:-1]])
    entries = cross.copy()
    if v.reentry:
        back_above = (c > fast) & np.concatenate([[False], c[:-1] <= fast[:-1]])
        entries |= valid & above & back_above
    entries &= btc_up
    flip = valid & ~above
    out: list[Result] = []
    next_free = 0
    for i in np.flatnonzero(entries):
        if i < next_free or i + 1 >= n:
            continue
        dist = v.stop_k * vol[i]
        if not np.isfinite(dist) or dist <= 0:
            continue
        j = i + 1
        entry = o[j] * (1 + SLIPPAGE)
        stop = entry - dist
        first = entry + v.half_r * dist
        half_done, booked, fees, size, paid = False, 0.0, FEE, 1.0, 0.0
        highest = entry
        exit_price = None
        k = j
        while k < n:
            if k > j:
                paid += size * funding.get(int(t[k]), 0.0)
            if o[k] <= stop:
                exit_price = o[k]
            elif low[k] <= stop:
                exit_price = stop
            elif not half_done and h[k] >= first:
                fill = max(o[k], first) * (1 - SLIPPAGE)
                booked += 0.5 * (fill - entry) / entry
                fees += 0.5 * FEE
                size, half_done = 0.5, True
                stop = max(stop, entry)
            if exit_price is None:
                if flip[k] or k - j + 1 >= v.max_hold or k == n - 1:
                    exit_price = c[k]
                elif v.trail_k and half_done:
                    highest = max(highest, h[k])
                    stop = max(stop, highest - v.trail_k * vol[k])
            if exit_price is not None:
                break
            k += 1
        fill = exit_price * (1 - SLIPPAGE)
        ret = booked + size * (fill - entry) / entry - fees - size * FEE - paid
        out.append(Result(bars.symbol, int(t[j]), int(t[k]) + BAR_MS, ret, ret / (dist / entry)))
        next_free = k
    return out


def run_all(v: Variant, bars, funding, regimes) -> list[Result]:
    results: list[Result] = []
    for symbol in SMALL:
        results.extend(simulate(bars[symbol], v, regimes[(symbol, v.regime)], funding[symbol]))
    return results


def line(results: list[Result]) -> str:
    if not results:
        return "n=0"
    r = np.array([x.r for x in results])
    return f"n={len(r):4} win={np.mean(r > 0):5.1%} avgR={r.mean():+.3f} totR={r.sum():+6.1f}"


def main() -> None:
    months = month_range(START, END)
    symbols = sorted(set(SMALL) | {"BTCUSDT"})
    bars = {s: load_bars(s, months) for s in symbols}
    funding = {s: load_funding(s, months) for s in symbols}
    regimes = {(s, p): btc_trend(bars["BTCUSDT"], bars[s], p) for s in SMALL for p in (200, 300)}
    split = month_ms("2024-07")
    variants = [
        Variant("base (bot today)"),
        Variant("trail 3 ATR after half", trail_k=3.0),
        Variant("trail 2 ATR after half", trail_k=2.0),
        Variant("trail 4 ATR after half", trail_k=4.0),
        Variant("re-entry on pullbacks", reentry=True),
        Variant("re-entry + trail 3 ATR", reentry=True, trail_k=3.0),
        Variant("hold 240 bars (40 days)", max_hold=240),
        Variant("BTC filter 200 EMA", regime=200),
        Variant("stop 2.5 ATR", stop_k=2.5),
        Variant("stop 3.5 ATR", stop_k=3.5),
        Variant("EMA 10/50", fast=10, slow=50),
    ]
    print(f"{'variant':26} {'TRAIN 2021-01..2024-06':42} | {'TEST 2024-07..2026-10':42} | $30")
    for v in variants:
        results = run_all(v, bars, funding, regimes)
        train = [x for x in results if x.exit_t <= split]
        test = [x for x in results if x.entry_t >= split]
        whole = replay(results, 30, 0.01, 3, 0.02)  # type: ignore[arg-type]
        part = replay(test, 30, 0.01, 3, 0.02)  # type: ignore[arg-type]
        print(f"{v.name:26} {line(train):42} | {line(test):42} | whole {whole.final:6.1f} "
              f"dd {whole.max_drawdown:4.0%} | test {part.final:5.1f} dd {part.max_drawdown:4.0%}")


if __name__ == "__main__":
    main()
