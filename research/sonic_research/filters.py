"""Does a stricter entry filter make trades better? python -m sonic_research.filters

Each filter only uses information known at the signal bar. A filter is worth keeping only if
it improves the unseen test period, not just the training one.
"""

from __future__ import annotations

import numpy as np

from .backtest import Trade
from .data import BAR_MS, SYMBOLS, load_bars, load_funding, month_range
from .phase2 import END, START, run_set
from .run import grid, month_ms
from .strategies import atr, btc_trend, ema

BEST = "trend-20-100-k3-tnone-h120-btc"


def features(bars) -> dict[str, np.ndarray]:
    close = bars.c
    fast, slow, long_trend = ema(close, 20), ema(close, 100), ema(close, 300)
    volatility = atr(bars)
    return {
        "coin_long_trend": np.sign(close - long_trend),
        "strength": (fast - slow) / volatility,  # EMA gap in ATR units
        "slow_slope": (slow - np.roll(slow, 6)) / volatility,  # slow EMA change over 24 hours
    }


def stats(trades: list[Trade]) -> str:
    if not trades:
        return "n=0"
    r = np.array([t.r for t in sorted(trades, key=lambda t: t.exit_t)])
    equity = np.cumprod(1 + 0.01 * np.clip(r, -3, None))
    peak = np.maximum.accumulate(np.concatenate([[1.0], equity]))[1:]
    return (f"n={len(r):4} win={np.mean(r > 0):5.1%} avgR={r.mean():+.3f} "
            f"totalR={r.sum():+6.1f} DD={np.max(1 - equity / peak):4.0%}")


def main() -> None:
    months = month_range(START, END)
    bars = {s: load_bars(s, months) for s in SYMBOLS}
    funding = {s: load_funding(s, months) for s in SYMBOLS}
    regime = {s: btc_trend(bars["BTCUSDT"], bars[s]) for s in SYMBOLS}
    params = {p.name: p for p in grid()}[BEST]
    trades = run_set(params, bars, funding, regime)
    feats = {s: features(bars[s]) for s in SYMBOLS}
    index = {s: {int(t): i for i, t in enumerate(bars[s].t)} for s in SYMBOLS}

    def at_signal(trade: Trade, name: str) -> float:
        i = index[trade.symbol][trade.entry_t - BAR_MS]
        return float(feats[trade.symbol][name][i])

    filters = {
        "none (current bot)": lambda t: True,
        "coin also in its long trend": lambda t: at_signal(t, "coin_long_trend") == t.side,
        "slow EMA already moving our way": lambda t: at_signal(t, "slow_slope") * t.side > 0,
        "strong slow slope (>0.5 ATR/day)": lambda t: at_signal(t, "slow_slope") * t.side > 0.5,
        "longs only": lambda t: t.side == 1,
        "long trend + slope": lambda t: at_signal(t, "coin_long_trend") == t.side
        and at_signal(t, "slow_slope") * t.side > 0,
    }
    split = month_ms("2024-07")
    for name, keep in filters.items():
        chosen = [t for t in trades if keep(t)]
        train = [t for t in chosen if t.exit_t <= split]
        test = [t for t in chosen if t.entry_t >= split]
        print(f"{name:36} TRAIN {stats(train)}\n{'':36} TEST  {stats(test)}")


if __name__ == "__main__":
    main()
