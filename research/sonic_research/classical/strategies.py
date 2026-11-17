"""The 11 classical strategies (long rules; shorts run them on the inverted series).

Each `rule(ctx, i)` looks only backwards from bar i: confirmed pivots (index + K <= i), causal
ATR/EMA, and bars <= i. It returns a Signal (entry at close i) or None. A Signal with
`target=None` takes its target from the combo's RR (target = entry + RR x risk).
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass

import numpy as np

from .core import CONFIRM, K, Series, atr, candles, clusters, ema, line_at, pivots

FOUR_H = 4 * 3_600_000
ROUND_TRIP = 0.0014  # 2 x (0.05% fee + 0.02% slippage)


@dataclass(frozen=True)
class Signal:
    strategy: str
    side: str
    i: int
    t_ms: int
    entry: float
    stop: float
    target: float | None
    invalid: float | None
    reason: str
    htf: bool = False  # last completed day above its daily EMA50 (higher-timeframe agreement)


class Ctx:
    """Causal per-series data shared by all rules."""

    def __init__(self, s: Series) -> None:
        self.s = s
        self.atr = atr(s)
        self.ema50, self.ema200 = ema(s.c, 50), ema(s.c, 200)
        highs, lows = pivots(s)
        self.hi_idx = np.array([p[0] for p in highs], dtype=int)
        self.hi_px = np.array([p[1] for p in highs])
        self.lo_idx = np.array([p[0] for p in lows], dtype=int)
        self.lo_px = np.array([p[1] for p in lows])
        self.htf = daily_trend(s)
        self.gap = np.r_[True, np.diff(s.t) != FOUR_H]  # bar after a hole: no signal

    def _known(self, idx: np.ndarray, px: np.ndarray, i: int, lookback: int) -> list:
        hi = bisect.bisect_right(idx, i - K)  # confirmed: index + K <= i
        lo = bisect.bisect_left(idx, i - lookback)
        return list(zip(idx[lo:hi].tolist(), px[lo:hi].tolist(), strict=True))

    def highs(self, i: int, lookback: int = 120) -> list[tuple[int, float]]:
        return self._known(self.hi_idx, self.hi_px, i, lookback)

    def lows(self, i: int, lookback: int = 120) -> list[tuple[int, float]]:
        return self._known(self.lo_idx, self.lo_px, i, lookback)


def daily_trend(s: Series) -> np.ndarray:
    """At bar i: the last COMPLETED day's close is above the EMA50 of completed daily closes."""
    day = s.t // 86_400_000
    out = np.zeros(len(s), dtype=bool)
    closes: list[float] = []
    e = None
    for i in range(len(s)):
        if i > 0 and day[i] != day[i - 1]:  # bar i opens a new day: day i-1 is complete
            closes.append(float(s.c[i - 1]))
            e = closes[-1] if e is None else (2 / 51) * closes[-1] + (1 - 2 / 51) * e
        out[i] = e is not None and len(closes) >= 50 and closes[-1] > e
    return out


def wide_enough(width: float, price: float) -> bool:
    """Channel width rule: at least 2% of the price (absolute: also right for inverted series)."""
    return width >= 0.02 * abs(price)


FLAG_BARS = (5, 15)  # consolidation length after the pole (design)


def _sig(ctx: Ctx, name: str, i: int, stop: float, target: float | None, invalid: float | None,
         reason: str) -> Signal | None:
    s, entry = ctx.s, float(ctx.s.c[i])
    if not stop < entry:
        return None
    if target is not None and (target <= entry or target - entry <= ROUND_TRIP * abs(entry) * 4):
        return None
    return Signal(name, s.side, i, int(s.t[i]), entry, float(stop),
                  None if target is None else float(target),
                  None if invalid is None else float(invalid), reason, bool(ctx.htf[i]))


def _confirm(ctx: Ctx, i: int) -> set[str]:
    return candles(ctx.s, i) & CONFIRM


def sr_bounce(ctx: Ctx, i: int, need_candle: bool = True) -> Signal | None:
    a, c, s = ctx.atr[i], ctx.s.c[i], ctx.s
    sup = [lv for lv, n in clusters([p for _, p in ctx.lows(i)], 0.5 * a) if n >= 2 and lv < c]
    if not sup:
        return None
    level = max(sup)
    if not (s.low[i] <= level + 0.3 * a and c > level):
        return None
    conf = _confirm(ctx, i)
    if need_candle and not conf:
        return None
    res = [lv for lv, _ in clusters([p for _, p in ctx.highs(i)], 0.5 * a) if lv > c]
    if not res:
        return None
    name = "sr_bounce" if need_candle else "sr_bounce_nocandle"
    return _sig(ctx, name, i, level - 0.5 * a, min(res), level,
                f"support {level:.6g} ({sorted(conf) or 'no candle'})")


def breakout_retest(ctx: Ctx, i: int) -> Signal | None:
    s = ctx.s
    for b in range(i - 1, max(i - 11, 30), -1):
        ab = ctx.atr[b]
        levels = [lv for lv, n in clusters([p for _, p in ctx.highs(b)], 0.5 * ab) if n >= 2]
        for lvl in sorted(levels, reverse=True):
            if s.c[b] > lvl + 0.2 * ab and s.c[b - 1] <= lvl:
                between = range(b + 1, i)
                if any(s.c[j] < lvl - 0.2 * ctx.atr[j] for j in between):
                    return None  # breakout failed before any retest
                if any(s.low[j] <= lvl + 0.2 * ctx.atr[j] and s.c[j] > lvl for j in between):
                    return None  # the first retest already happened
                a = ctx.atr[i]
                if s.low[i] <= lvl + 0.2 * a and s.c[i] > lvl:
                    height = lvl - float(s.low[max(0, b - 30):b].min())
                    return _sig(ctx, "breakout_retest", i, min(s.low[i], lvl) - 0.3 * a,
                                s.c[i] + height, lvl - 0.2 * a, f"retest of {lvl:.6g}")
                return None
    return None


def _trend_line(ctx: Ctx, i: int):
    lows = ctx.lows(i, 200)
    if len(lows) < 2:
        return None
    p1, p2 = lows[-2], lows[-1]
    if p2[0] - p1[0] < 5 or p2[1] <= p1[1]:
        return None
    s = ctx.s
    for j in range(p1[0], i):
        if s.c[j] < line_at(p1, p2, j):
            return None  # the line was broken by a close: not a valid trend line
    return p1, p2


def trendline(ctx: Ctx, i: int) -> Signal | None:
    tl = _trend_line(ctx, i)
    if tl is None or not _confirm(ctx, i):
        return None
    lv, a, s = line_at(*tl, i), ctx.atr[i], ctx.s
    if not (s.low[i] <= lv + 0.3 * a and s.c[i] > lv):
        return None
    return _sig(ctx, "trendline", i, lv - 0.5 * a, None, lv, f"rising line {lv:.6g}")


def channel(ctx: Ctx, i: int) -> Signal | None:
    tl = _trend_line(ctx, i)
    if tl is None or not _confirm(ctx, i):
        return None
    p1, p2 = tl
    tops = [h for h in ctx.highs(i, 200) if p1[0] < h[0] < p2[0]]
    if not tops:
        return None
    top = max(tops, key=lambda h: h[1] - line_at(p1, p2, h[0]))
    width = top[1] - line_at(p1, p2, top[0])
    lv, a, s = line_at(p1, p2, i), ctx.atr[i], ctx.s
    if not wide_enough(width, s.c[i]) or not (s.low[i] <= lv + 0.3 * a and s.c[i] > lv):
        return None
    return _sig(ctx, "channel", i, lv - 0.5 * a, lv + width, lv, f"channel width {width:.6g}")


def head_shoulders(ctx: Ctx, i: int) -> Signal | None:
    lows = ctx.lows(i, 150)
    if len(lows) < 3:
        return None
    a_, b_, c_ = lows[-3], lows[-2], lows[-1]
    at = ctx.atr[i]
    if not (b_[1] < a_[1] - 0.5 * at and b_[1] < c_[1] - 0.5 * at and abs(a_[1] - c_[1]) <= at):
        return None
    necks = [h[1] for h in ctx.highs(i, 150) if a_[0] < h[0] < c_[0]]
    if not necks or i - c_[0] > 20 + K:
        return None
    neck, s = max(necks), ctx.s
    if not (s.c[i] > neck and s.c[i - 1] <= neck):
        return None
    return _sig(ctx, "head_shoulders", i, c_[1] - 0.3 * at, neck + (neck - b_[1]),
                neck - 0.3 * at, f"neck {neck:.6g} head {b_[1]:.6g}")


def double_bottom(ctx: Ctx, i: int) -> Signal | None:
    lows = ctx.lows(i, 150)
    if len(lows) < 2:
        return None
    a_, b_ = lows[-2], lows[-1]
    at, s = ctx.atr[i], ctx.s
    if b_[0] - a_[0] < 5 or abs(a_[1] - b_[1]) > 0.5 * at or i - b_[0] > 20 + K:
        return None
    triple = len(lows) >= 3 and abs(lows[-3][1] - a_[1]) <= 0.5 * at
    neck = float(s.h[a_[0]:b_[0] + 1].max())
    if not (s.c[i] > neck and s.c[i - 1] <= neck):
        return None
    bottom = min(a_[1], b_[1])
    return _sig(ctx, "double_bottom", i, bottom - 0.3 * at, neck + (neck - bottom), neck,
                ("triple" if triple else "double") + f" bottom {bottom:.6g}")


def _last2(points):
    return (points[-2], points[-1]) if len(points) >= 2 else None


def triangle(ctx: Ctx, i: int) -> Signal | None:
    hs, ls = _last2(ctx.highs(i, 60)), _last2(ctx.lows(i, 60))
    if hs is None or ls is None:
        return None
    (h1, h2), (l1, l2) = hs, ls
    at, s = ctx.atr[i], ctx.s
    if abs(h1[1] - h2[1]) <= 0.5 * at and l2[1] > l1[1] + 0.2 * at:
        kind, upper = "ascending", lambda j: max(h1[1], h2[1])
    elif h2[1] < h1[1] - 0.2 * at and l2[1] > l1[1] + 0.2 * at:
        kind, upper = "symmetric", lambda j: line_at(h1, h2, j)
    else:
        return None
    first, last = min(h1[0], l1[0]), max(h2[0], l2[0])
    w0 = upper(first) - line_at(l1, l2, first)
    w1 = upper(last) - line_at(l1, l2, last)
    if w0 <= 0 or w1 > 0.8 * w0 or i - last > 30 + K:
        return None
    if not (s.c[i] > upper(i) and s.c[i - 1] <= upper(i - 1)):
        return None
    return _sig(ctx, "triangle", i, line_at(l1, l2, i) - 0.3 * at, s.c[i] + w0, upper(i),
                f"{kind} triangle")


def flag(ctx: Ctx, i: int) -> Signal | None:
    s = ctx.s
    lo, hi = FLAG_BARS
    for j in range(i - 1 - lo, max(i - 2 - hi, 11), -1):  # consolidation j+1..i-1: 5..15 bars
        pole = float(s.c[j] - s.low[j - 10:j + 1].min())
        if pole < 3 * ctx.atr[j]:
            continue
        top = float(s.h[j + 1:i].max())
        bot = float(s.low[j + 1:i].min())
        if top - bot > 0.5 * pole or bot < s.c[j] - 0.5 * pole:
            continue
        a = ctx.atr[i]
        if s.c[i] > top and s.c[i - 1] <= top and s.c[i] <= top + a:
            return _sig(ctx, "flag", i, bot - 0.2 * a, s.c[i] + 0.5 * pole, bot,
                        f"pole {pole:.6g}")
        return None
    return None


def _range(ctx: Ctx, i: int):
    hs, ls = _last2(ctx.highs(i, 80)), _last2(ctx.lows(i, 80))
    if hs is None or ls is None:
        return None
    at = ctx.atr[i]
    if abs(hs[0][1] - hs[1][1]) > 0.5 * at or abs(ls[0][1] - ls[1][1]) > 0.5 * at:
        return None
    top, bot = (hs[0][1] + hs[1][1]) / 2, (ls[0][1] + ls[1][1]) / 2
    return (top, bot) if top - bot >= 2 * at else None


def range_bounce(ctx: Ctx, i: int) -> Signal | None:
    r = _range(ctx, i)
    if r is None or not _confirm(ctx, i):
        return None
    top, bot = r
    a, s = ctx.atr[i], ctx.s
    if not (s.low[i] <= bot + 0.3 * a and s.c[i] > bot):
        return None
    return _sig(ctx, "range_bounce", i, bot - 0.3 * a, top, bot, f"range {bot:.6g}-{top:.6g}")


def range_breakout(ctx: Ctx, i: int) -> Signal | None:
    r = _range(ctx, i)
    if r is None:
        return None
    top, bot = r
    a, s = ctx.atr[i], ctx.s
    if not (s.c[i] > top + 0.2 * a and s.c[i - 1] <= top + 0.2 * a):
        return None
    return _sig(ctx, "range_breakout", i, top - 0.3 * a, s.c[i] + (top - bot), top,
                f"range break {top:.6g}")


def wedge(ctx: Ctx, i: int) -> Signal | None:
    hs, ls = _last2(ctx.highs(i, 80)), _last2(ctx.lows(i, 80))
    if hs is None or ls is None:
        return None
    (h1, h2), (l1, l2) = hs, ls
    sh = (h2[1] - h1[1]) / (h2[0] - h1[0])
    sl = (l2[1] - l1[1]) / (l2[0] - l1[0])
    if not (sh < 0 and sl < 0 and sh < sl):  # falling and converging
        return None
    first, last = min(h1[0], l1[0]), max(h2[0], l2[0])
    w0 = line_at(h1, h2, first) - line_at(l1, l2, first)
    w1 = line_at(h1, h2, last) - line_at(l1, l2, last)
    if w0 <= 0 or w1 > 0.8 * w0 or i - last > 30 + K:
        return None
    s, at = ctx.s, ctx.atr[i]
    up = line_at(h1, h2, i)
    if not (s.c[i] > up and s.c[i - 1] <= line_at(h1, h2, i - 1)):
        return None
    return _sig(ctx, "wedge", i, l2[1] - 0.3 * at, s.c[i] + w0, up, "falling wedge")


def candle_trend(ctx: Ctx, i: int) -> Signal | None:
    conf = _confirm(ctx, i)
    if not conf or not ctx.ema50[i] > ctx.ema200[i]:
        return None
    s = ctx.s
    return _sig(ctx, "candle_trend", i, s.low[i] - 0.3 * ctx.atr[i], None, None,
                ",".join(sorted(conf)))


RULES = {
    "sr_bounce": sr_bounce,
    "sr_bounce_nocandle": lambda ctx, i: sr_bounce(ctx, i, need_candle=False),
    "breakout_retest": breakout_retest, "trendline": trendline, "channel": channel,
    "head_shoulders": head_shoulders, "double_bottom": double_bottom, "triangle": triangle,
    "flag": flag, "range_bounce": range_bounce, "range_breakout": range_breakout,
    "wedge": wedge, "candle_trend": candle_trend,
}
WARMUP = 210


def signals(s: Series, name: str, ctx: Ctx | None = None) -> list[Signal]:
    ctx = ctx or Ctx(s)
    rule, out = RULES[name], []
    for i in range(WARMUP, len(s)):
        if ctx.gap[i] or ctx.gap[i - 1] or not np.isfinite(ctx.atr[i]):
            continue
        sig = rule(ctx, i)
        if sig is not None:
            out.append(sig)
    return out
