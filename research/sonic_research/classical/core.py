"""Building blocks of the Classical Strategy Lab. Everything is causal: a value at bar i uses bars
<= i only, and a pivot is usable only from its confirmation bar (pivot index + K).

Shorts are simulated by inverting the series (price -> -price, high <-> low): every long rule then
reads as its mirror (inverse head-and-shoulders -> head-and-shoulders top, double bottom -> top...).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

K = 3  # pivot: higher/lower than K bars on each side; known K bars after it


@dataclass
class Series:
    symbol: str
    t: np.ndarray
    o: np.ndarray
    h: np.ndarray
    low: np.ndarray
    c: np.ndarray
    side: str = "long"

    def __len__(self) -> int:
        return len(self.t)

    def inverted(self) -> Series:
        return Series(self.symbol, self.t, -self.o, -self.low, -self.h, -self.c, "short")

    def cut(self, n: int) -> Series:
        return Series(self.symbol, self.t[:n], self.o[:n], self.h[:n], self.low[:n], self.c[:n],
                      self.side)


def ema(x: np.ndarray, n: int) -> np.ndarray:
    out = np.empty_like(x, dtype=float)
    a = 2 / (n + 1)
    out[0] = x[0]
    for i in range(1, len(x)):
        out[i] = a * x[i] + (1 - a) * out[i - 1]
    return out


def atr(s: Series, n: int = 14) -> np.ndarray:
    prev = np.r_[s.c[0], s.c[:-1]]
    tr = np.maximum(s.h - s.low, np.maximum(np.abs(s.h - prev), np.abs(s.low - prev)))
    out = np.full(len(s), np.nan)
    if len(s) > n:
        out[n] = tr[1:n + 1].mean()
        for i in range(n + 1, len(s)):
            out[i] = (out[i - 1] * (n - 1) + tr[i]) / n
    return out


def pivots(s: Series) -> tuple[list[tuple[int, float]], list[tuple[int, float]]]:
    """(index, price) of pivot highs and lows. Usable at bar i only if index + K <= i."""
    highs, lows = [], []
    for j in range(K, len(s) - K):
        hw, lw = s.h[j - K:j + K + 1], s.low[j - K:j + K + 1]
        if s.h[j] > s.h[j - K:j].max() and s.h[j] >= s.h[j + 1:j + K + 1].max():
            highs.append((j, float(s.h[j])))
        if s.low[j] < s.low[j - K:j].min() and s.low[j] <= s.low[j + 1:j + K + 1].min():
            lows.append((j, float(s.low[j])))
        del hw, lw
    return highs, lows


def known(points: list[tuple[int, float]], i: int, lookback: int = 10**9) -> list:
    """Pivots confirmed by bar i (index + K <= i) and within the lookback."""
    return [p for p in points if p[0] + K <= i and p[0] >= i - lookback]


def clusters(prices: list[float], tol: float) -> list[tuple[float, int]]:
    """Group nearby prices (within tol): (mean level, touches), sorted by level."""
    out: list[list[float]] = []
    for p in sorted(prices):
        if out and p - out[-1][-1] <= tol:
            out[-1].append(p)
        else:
            out.append([p])
    return [(float(np.mean(g)), len(g)) for g in out]


def line_at(p1: tuple[int, float], p2: tuple[int, float], i: int) -> float:
    slope = (p2[1] - p1[1]) / (p2[0] - p1[0])
    return p2[1] + slope * (i - p2[0])


# ---- Japanese candles at bar i (bars <= i only); names are the long-side reading


def _body(s: Series, i: int) -> tuple[float, float, float, float]:
    o, c, h, low = s.o[i], s.c[i], s.h[i], s.low[i]
    return abs(c - o), h - max(o, c), min(o, c) - low, h - low


def candles(s: Series, i: int) -> set[str]:
    """Patterns completed at bar i. Bullish ones confirm longs; on an inverted series the same
    names are their bearish mirrors (engulfing -> bearish engulfing, hammer -> hanging man/star)."""
    out: set[str] = set()
    if i < 2:
        return out
    body, upper, lower, rng = _body(s, i)
    if rng <= 0:
        return out
    pb = abs(s.c[i - 1] - s.o[i - 1])
    if (s.c[i - 1] < s.o[i - 1] and s.c[i] > s.o[i] and s.o[i] <= s.c[i - 1]
            and s.c[i] >= s.o[i - 1] and body > pb):
        out.add("engulfing")
    if lower >= 2 * body and upper <= 0.3 * rng and body > 0:
        out.add("hammer")  # after a fall: hammer; after a rise it is the hanging-man shape
    if upper >= 2 * body and lower <= 0.3 * rng and body > 0:
        out.add("inverted_hammer")  # shooting-star shape on the other side
    if body <= 0.1 * rng:
        out.add("doji")
    if (s.c[i - 1] < s.o[i - 1] and s.c[i] > s.o[i] and max(s.o[i], s.c[i]) < s.o[i - 1]
            and min(s.o[i], s.c[i]) > s.c[i - 1]):
        out.add("harami")
    b2 = abs(s.c[i - 1] - s.o[i - 1])
    r2 = s.h[i - 1] - s.low[i - 1]
    if (s.c[i - 2] < s.o[i - 2] and r2 > 0 and b2 <= 0.3 * r2 and s.c[i] > s.o[i]
            and s.c[i] > (s.o[i - 2] + s.c[i - 2]) / 2):
        out.add("morning_star")
    if abs(s.low[i] - s.low[i - 1]) <= 0.1 * rng and s.c[i] > s.o[i] and s.c[i - 1] < s.o[i - 1]:
        out.add("tweezer_bottom")
    return out


CONFIRM = {"engulfing", "hammer", "morning_star", "tweezer_bottom"}
