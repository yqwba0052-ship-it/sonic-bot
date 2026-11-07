"""Trend strategy chosen by the research (research/reports/phase2.json, capital.json).

trend-20-100-k3-tnone-h120-btc on 4-hour bars:
- enter when the 20-bar EMA crosses the 100-bar EMA on a closed bar;
- buy only while BTC closes above its 300-bar EMA, sell only while it closes below;
- stop at 3 x ATR(14) from the entry;
- half the position closes at +0.25 x the stop distance, then the rest's stop moves to the entry
  (owner's choice: most trades end in profit, smaller total; research/sonic_research/partial.py);
- the rest exits when the EMAs cross back, or after 120 bars (20 days).
Tested 2021-01..2026-10 with fees, slippage and funding: 78% of trades ended in profit (80% in
the unseen 2024-07..2026-10 part); half of all months had 80%+ winning trades, the worst month
40%. A $30 account grew to about $56, deepest drop 7%. Past results are no promise.
"""

from __future__ import annotations

from dataclasses import dataclass

NAME = "trend-20-100-4h"
INTERVAL = "4h"
BAR_MS = 4 * 3600 * 1000
FAST = 20
SLOW = 100
ATR_PERIOD = 14
ATR_K = 3.0
REGIME_PERIOD = 300
MAX_HOLD_BARS = 120
HISTORY_BARS = 1000

HALF_AT_R = 0.25

# Self-check for this exit style (longs only, half at 0.25R): the average R of the last 30
# closed trades was never below -0.39 in 5.8 years (1st percentile -0.29). Worse than this
# means the market no longer fits. History on the 8 small-account coins: 78% of trades ended
# in profit, average +0.21R.
DRIFT_WINDOW = 30
DRIFT_FLOOR = -0.40
EXPECTED_AVG_R = 0.21
EXPECTED_WIN_RATE = 0.78


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
    """BUY or SELL when the EMAs crossed on the last closed bar and BTC agrees."""
    if len(bars) < SLOW * 3:
        return None
    now, before = _above(bars)
    if now and not before and btc_up:
        return "BUY"
    if not now and before and not btc_up:
        return "SELL"
    return None


def exit_due(bars: list[Bar], side: str) -> bool:
    """True when the trend turned against an open trade on the last closed bar."""
    now, _ = _above(bars)
    return (side == "BUY" and not now) or (side == "SELL" and now)


def stop_distance(bars: list[Bar]) -> float:
    return ATR_K * atr(bars)
