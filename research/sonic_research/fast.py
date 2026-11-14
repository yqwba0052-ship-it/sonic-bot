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


# ---------- exits, costs, sizing (Task 3)

BASE_COST = 0.0014  # round trip: fee 0.05% x 2 + slippage 0.02% x 2
RISK = 0.005
LEVERAGE = 3


def exit_of(c: Candidate, fast: Bars, hold: int) -> tuple[int, float, str]:
    """From the fill bar on: stop first (a gap under it fills at the open), then target, then
    the time exit after `hold` bars; still open when the data ends -> last close, "end"."""
    last = len(fast) - 1
    for j in range(c.i + 1, min(c.i + hold, last) + 1):
        if fast.low[j] <= c.stop:
            return j, float(min(c.stop, fast.o[j])), "stop"
        if fast.h[j] >= c.target:
            return j, float(c.target), "target"
    if c.i + hold <= last:
        j = c.i + hold
        return j, float(fast.c[j]), "time"
    return last, float(fast.c[last]), "end"


def sized(c: Candidate, equity: float, cost: float = BASE_COST, leverage: int = LEVERAGE,
          risk: float = RISK) -> tuple[float, float]:
    """Quantity whose loss at the stop, with half the round-trip cost at entry and half at the
    stop, is `risk` of equity; capped so the position never exceeds equity x leverage."""
    per_unit = (c.entry - c.stop) + cost / 2 * (c.entry + c.stop)
    if per_unit <= 0 or equity <= 0:
        return 0.0, 0.0
    qty = min(equity * risk / per_unit, equity * leverage / c.entry)
    return qty, qty * per_unit


def pnl(c: Candidate, qty: float, exit_price: float, cost: float) -> float:
    return qty * (exit_price - c.entry) - cost / 2 * qty * (c.entry + exit_price)


# ---------- portfolio pass (Task 4)

MAX_OPEN = 2
DAILY_LIMIT = 0.02  # of Sonic Fast equity at the start of the UTC day
COMBINED_LIMIT = 0.04  # of Sonic + Sonic Fast equity: PLANNED risk at the stops, not a cap on loss
DAY_MS = 86_400_000


@dataclass
class Trade:
    symbol: str
    opened_ms: int
    closed_ms: int
    entry: float
    exit: float
    qty: float
    pnl: float
    planned_risk: float
    why: str


def run(cands: list[Candidate], bars: dict[str, Bars], hold: int, cost: float,
        sonic: list[dict], start_ms: int, end_ms: int, equity0: float = 30.0,
        sonic_capital: float = 30.0) -> list[Trade]:
    """One chronological pass: at most MAX_OPEN trades, one per coin, never a coin Sonic holds,
    and only when the daily and combined planned-risk limits still hold with the new trade.
    Sizing uses the base cost; `cost` only sets the PnL (cost-sensitivity runs)."""
    equity = equity0
    open_: list[tuple[int, Trade]] = []  # (exit time, trade)
    done: list[Trade] = []
    day_key, day_equity, day_losses = None, equity0, 0.0

    def close_until(t_ms: int) -> None:
        nonlocal equity, day_losses
        for exit_ms, trade in sorted(open_, key=lambda x: x[0]):
            if exit_ms <= t_ms:
                equity += trade.pnl
                if trade.pnl < 0 and exit_ms // DAY_MS == t_ms // DAY_MS:
                    day_losses += -trade.pnl
                done.append(trade)
                open_.remove((exit_ms, trade))

    for c in sorted((c for c in cands if start_ms <= c.t_ms < end_ms), key=lambda c: c.t_ms):
        close_until(c.t_ms)
        if c.t_ms // DAY_MS != day_key:
            day_key, day_equity, day_losses = c.t_ms // DAY_MS, equity, 0.0
            day_losses = sum(-t.pnl for t in done
                             if t.pnl < 0 and t.closed_ms // DAY_MS == day_key)
        if len(open_) >= MAX_OPEN or any(t.symbol == c.symbol for _, t in open_):
            continue
        sonic_open = [s for s in sonic
                      if s["opened_ms"] <= c.t_ms < (s.get("closed_ms") or end_ms)]
        if any(s["symbol"] == c.symbol for s in sonic_open):
            continue
        qty, planned = sized(c, equity, BASE_COST)
        if qty <= 0:
            continue
        open_planned = sum(t.planned_risk for _, t in open_)
        if day_losses + open_planned + planned > DAILY_LIMIT * day_equity + 1e-12:
            continue
        sonic_equity = sonic_capital + sum(s.get("pnl") or 0 for s in sonic
                                           if (s.get("closed_ms") or end_ms) <= c.t_ms)
        sonic_planned = sum(s.get("risk_usdt") or 0 for s in sonic_open)
        combined_cap = COMBINED_LIMIT * (equity + sonic_equity)
        if sonic_planned + open_planned + planned > combined_cap + 1e-12:
            continue
        b = bars[c.symbol]
        bar_ms = int(b.t[1] - b.t[0]) if len(b) > 1 else 0
        j, price, why = exit_of(c, b, hold)
        trade = Trade(c.symbol, c.t_ms, int(b.t[j]) + bar_ms, c.entry, price, qty,
                      pnl(c, qty, price, cost), planned, why)
        open_.append((trade.closed_ms, trade))
    close_until(2**62)
    return sorted(done, key=lambda t: t.opened_ms)
