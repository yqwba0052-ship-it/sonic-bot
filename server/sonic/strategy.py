"""Demo strategy that exercises the system. It is not a proven profitable strategy."""

from __future__ import annotations

FAST = 9
SLOW = 21
NAME = "demo-ema-9-21"


def ema(values: list[float], period: int) -> list[float]:
    factor = 2 / (period + 1)
    result = [values[0]]
    for value in values[1:]:
        result.append(value * factor + result[-1] * (1 - factor))
    return result


def signal(closes: list[float]) -> str | None:
    """Return BUY or SELL when the fast EMA crosses the slow EMA on the last closed bar."""
    if len(closes) < SLOW * 3:
        return None
    fast = ema(closes, FAST)
    slow = ema(closes, SLOW)
    if fast[-2] <= slow[-2] and fast[-1] > slow[-1]:
        return "BUY"
    if fast[-2] >= slow[-2] and fast[-1] < slow[-1]:
        return "SELL"
    return None
