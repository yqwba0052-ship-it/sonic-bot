"""Sonic Fast (research only): buying short pullbacks inside Sonic's 4h uptrend on 15m/30m/1h.

Design: docs/superpowers/specs/2026-11-14-sonic-fast-design.md. Nothing here touches the bot.
Every value seen at a fast bar comes from bars that had closed by that bar's close.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .data import Bars

H4_MS = 4 * 3600 * 1000


def ema(x: np.ndarray, n: int) -> np.ndarray:
    out = np.empty(len(x))
    if len(x) == 0:
        return out
    k = 2 / (n + 1)
    out[0] = x[0]
    for i in range(1, len(x)):
        out[i] = x[i] * k + out[i - 1] * (1 - k)
    return out


def rsi(c: np.ndarray, n: int = 14) -> np.ndarray:
    out = np.full(len(c), 50.0)
    if len(c) <= n:
        return out
    change = np.diff(c)
    up, down = np.maximum(change, 0), np.maximum(-change, 0)
    gain, loss = up[:n].mean(), down[:n].mean()
    for i in range(n, len(c)):
        if i > n:
            gain = (gain * (n - 1) + up[i - 1]) / n
            loss = (loss * (n - 1) + down[i - 1]) / n
        out[i] = 100.0 if loss == 0 else 100 - 100 / (1 + gain / loss)
    return out


def atr(h: np.ndarray, low: np.ndarray, c: np.ndarray, n: int = 14) -> np.ndarray:
    out = np.zeros(len(c))
    if len(c) <= n:
        return out
    prev = np.concatenate([[c[0]], c[:-1]])
    tr = np.maximum(h - low, np.maximum(np.abs(h - prev), np.abs(low - prev)))
    value = tr[1:n + 1].mean()
    out[n] = value
    for i in range(n + 1, len(c)):
        value = (value * (n - 1) + tr[i]) / n
        out[i] = value
    return out


def closed_4h_index(fast_t: np.ndarray, fast_ms: int, t4: np.ndarray) -> np.ndarray:
    """For each fast bar, the index of the last 4h bar that had closed by the fast bar's close
    (-1 when none). Gaps in either series are fine: only close times are compared."""
    return np.searchsorted(t4 + H4_MS, fast_t + fast_ms, side="right") - 1


def regime(fast_t: np.ndarray, fast_ms: int, coin4: Bars, btc4: Bars) -> dict[str, np.ndarray]:
    """Sonic's 4h trend state at each fast bar, from the last closed 4h bars only:
    BTC > EMA300, coin > EMA200, coin EMA20 > EMA100. Also the values the reversal guards use."""
    n = len(fast_t)
    ok = np.zeros(n, dtype=bool)
    btc_drop = np.zeros(n)
    btc_atr_pct = np.zeros(n)
    above20 = np.zeros(n, dtype=bool)
    if len(coin4) == 0 or len(btc4) == 0:
        return {"ok": ok, "btc_drop_4h": btc_drop, "btc_atr_pct_4h": btc_atr_pct,
                "coin_above_ema20_4h": above20}
    bi = closed_4h_index(fast_t, fast_ms, btc4.t)
    ci = closed_4h_index(fast_t, fast_ms, coin4.t)
    b300 = ema(btc4.c, 300)
    b_atr = atr(btc4.h, btc4.low, btc4.c)
    c20, c100, c200 = ema(coin4.c, 20), ema(coin4.c, 100), ema(coin4.c, 200)
    seen = (bi >= 300) & (ci >= 200)  # enough closed history for the averages
    b, c = np.clip(bi, 0, None), np.clip(ci, 0, None)
    ok = seen & (btc4.c[b] > b300[b]) & (coin4.c[c] > c200[c]) & (c20[c] > c100[c])
    prev = np.clip(b - 1, 0, None)
    btc_drop = np.where(bi >= 1, 1 - btc4.c[b] / btc4.c[prev], 0.0)
    btc_atr_pct = np.where(bi >= 14, b_atr[b] / btc4.c[b], 0.0)
    above20 = (ci >= 20) & (coin4.c[c] >= c20[c])
    return {"ok": ok, "btc_drop_4h": btc_drop, "btc_atr_pct_4h": btc_atr_pct,
            "coin_above_ema20_4h": above20}


# ---------- candidate entries (Task 2)

FEE_MARGIN = 0.0042  # target distance must be >= 3 x the 0.14% round-trip cost
BTC_FIXED_DROP = 0.02
BTC_VOL_MULT = 1.5


@dataclass(frozen=True)
class Params:
    L: int  # RSI level the pullback dips under
    k: int  # bars the dip may be back
    ks: float  # stop distance in ATR
    kt: float  # target distance in ATR
    guard: str  # BTC crash guard: "fixed" (2%) or "vol" (1.5 x BTC 4h ATR)


@dataclass(frozen=True)
class Candidate:
    symbol: str
    i: int  # signal bar (closed); the fill is at the open of bar i + 1
    t_ms: int  # fill time
    entry: float
    stop: float
    target: float
    atr: float


def candidates(symbol: str, fast: Bars, fast_ms: int, reg: dict, p: Params,
               slip: float = 0.0002) -> list[Candidate]:
    """Pullback-and-bounce entries inside the 4h regime, after the reversal guards and the fee
    margin. Only data up to the signal bar is used; the fill is the next bar's open."""
    n = len(fast)
    if n < 120:
        return []
    c, h = fast.c, fast.h
    r = rsi(c)
    a = atr(fast.h, fast.low, c)
    e50, e100 = ema(c, 50), ema(c, 100)
    out: list[Candidate] = []
    for i in range(max(110, p.k), n - 1):
        if not reg["ok"][i] or not reg["coin_above_ema20_4h"][i] or a[i] <= 0:
            continue
        drop = reg["btc_drop_4h"][i]
        limit = BTC_FIXED_DROP if p.guard == "fixed" else BTC_VOL_MULT * reg["btc_atr_pct_4h"][i]
        if drop > limit:
            continue
        if c[i] < e100[i] or e50[i] - e50[i - 10] < 0:
            continue
        if not (r[i - p.k:i].min() < p.L <= r[i] and c[i] > h[i - 1]):
            continue
        entry = fast.o[i + 1] * (1 + slip)
        if p.kt * a[i] / entry < FEE_MARGIN:
            continue
        out.append(Candidate(symbol, i, int(fast.t[i + 1]), entry, entry - p.ks * a[i],
                             entry + p.kt * a[i], float(a[i])))
    return out
