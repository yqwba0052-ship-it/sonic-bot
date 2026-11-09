"""Trend strategy chosen by the research (research/reports/phase2.json, capital.json).

trend-20-100 on 4-hour bars:
- enter when the 20-bar EMA crosses the 100-bar EMA on a closed bar;
- buy only while BTC closes above its 300-bar EMA AND the coin closes above its own 200-bar EMA
  (sell only in the mirror case);
- stop at 2.5 x ATR(14) from the entry;
- half the position closes at +0.20 x the stop distance, then the rest's stop moves to the entry
  (owner's goal: 80-90% of each month's trades in profit);
- the rest exits when the EMAs cross back, or after 240 bars (40 days).
Stop 2.5 ATR and 40 days came from research/sonic_research/improve.py (2026-11-09). The coin
EMA-200 filter and half at 0.20R came from engine-replay experiments on 2026-11-10 (STRATEGY.md):
better share of months at 80%+ on both 2021-2023 and the unseen 2024-2026.
Engine replay 2021-06..2026-10, $30 on the 8 small-account coins with fees and slippage:
224 trades, 82.6% in profit, 67% of months at 80%+, $30 -> $83.49, deepest drop 15%.
Past results are no promise.
"""

from __future__ import annotations

from dataclasses import dataclass

NAME = "trend-20-100-4h"
INTERVAL = "4h"
BAR_MS = 4 * 3600 * 1000
FAST = 20
SLOW = 100
ATR_PERIOD = 14
ATR_K = 2.5
REGIME_PERIOD = 300
COIN_TREND_PERIOD = 200  # buy only while the coin itself closes above this EMA
MAX_HOLD_BARS = 240
HISTORY_BARS = 1000

HALF_AT_R = 0.20

# Self-check for this setup (longs only, coin above EMA 200, half at 0.20R, stop 2.5 ATR,
# 40 days): the average R of the last 30 closed trades was never below -0.17 in 5.4 years.
# Worse than this means the market no longer fits. History: 82.6% winning, average +0.51R.
DRIFT_WINDOW = 30
DRIFT_FLOOR = -0.25
EXPECTED_AVG_R = 0.50
EXPECTED_WIN_RATE = 0.82


@dataclass(frozen=True)
class Bar:
    t: int  # open time, ms
    o: float
    h: float
    low: float
    c: float


def ema(values: list[float], period: int) -> list[float]:
    factor = 2 / (period + 1)
    result = [values[0]]
    for value in values[1:]:
        result.append(value * factor + result[-1] * (1 - factor))
    return result


def atr(bars: list[Bar], period: int = ATR_PERIOD) -> float:
    """Wilder ATR at the last bar."""
    if len(bars) <= period:
        return 0.0
    ranges = [
        max(bar.h - bar.low, abs(bar.h - prev.c), abs(bar.low - prev.c))
        for prev, bar in zip(bars, bars[1:], strict=False)
    ]
    value = sum(ranges[:period]) / period
    for item in ranges[period:]:
        value = (value * (period - 1) + item) / period
    return value


def _above(bars: list[Bar]) -> tuple[bool, bool]:
    closes = [bar.c for bar in bars]
    fast, slow = ema(closes, FAST), ema(closes, SLOW)
    return fast[-1] > slow[-1], fast[-2] > slow[-2]


def btc_uptrend(btc: list[Bar]) -> bool:
    closes = [bar.c for bar in btc]
    return closes[-1] > ema(closes, REGIME_PERIOD)[-1]


def entry_signal(bars: list[Bar], btc_up: bool) -> str | None:
    """BUY or SELL when the EMAs crossed on the last closed bar and both BTC and the coin's own
    long trend (EMA 200) agree."""
    if len(bars) < SLOW * 3:
        return None
    now, before = _above(bars)
    closes = [bar.c for bar in bars]
    coin_up = closes[-1] > ema(closes, COIN_TREND_PERIOD)[-1]
    if now and not before and btc_up and coin_up:
        return "BUY"
    if not now and before and not btc_up and not coin_up:
        return "SELL"
    return None


def exit_due(bars: list[Bar], side: str) -> bool:
    """True when the trend turned against an open trade on the last closed bar."""
    now, _ = _above(bars)
    return (side == "BUY" and not now) or (side == "SELL" and now)


def stop_distance(bars: list[Bar]) -> float:
    return ATR_K * atr(bars)
