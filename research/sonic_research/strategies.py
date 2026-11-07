"""Three strategy families. Every indicator at bar i uses bars up to i only."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .backtest import Signals
from .data import Bars


def ema(values: np.ndarray, period: int) -> np.ndarray:
    out = np.empty_like(values)
    factor = 2 / (period + 1)
    out[0] = values[0]
    for i in range(1, len(values)):
        out[i] = values[i] * factor + out[i - 1] * (1 - factor)
    out[: period - 1] = np.nan
    return out


def atr(bars: Bars, period: int = 14) -> np.ndarray:
    prev_close = np.concatenate([[bars.c[0]], bars.c[:-1]])
    true_range = np.maximum.reduce(
        [bars.h - bars.low, np.abs(bars.h - prev_close), np.abs(bars.low - prev_close)]
    )
    out = np.full(len(bars), np.nan)
    if len(bars) > period:
        out[period] = true_range[1 : period + 1].mean()
        for i in range(period + 1, len(bars)):
            out[i] = (out[i - 1] * (period - 1) + true_range[i]) / period
    return out


def rsi(close: np.ndarray, period: int) -> np.ndarray:
    change = np.diff(close, prepend=close[0])
    gain = np.where(change > 0, change, 0.0)
    loss = np.where(change < 0, -change, 0.0)
    out = np.full(len(close), np.nan)
    avg_gain = gain[1 : period + 1].mean()
    avg_loss = loss[1 : period + 1].mean()
    for i in range(period + 1, len(close)):
        avg_gain = (avg_gain * (period - 1) + gain[i]) / period
        avg_loss = (avg_loss * (period - 1) + loss[i]) / period
        out[i] = 100.0 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)
    return out


def rolling_max_before(values: np.ndarray, window: int) -> np.ndarray:
    """Highest value of the `window` bars before bar i (bar i itself excluded)."""
    out = np.full(len(values), np.nan)
    for i in range(window, len(values)):
        out[i] = values[i - window : i].max()
    return out


def rolling_min_before(values: np.ndarray, window: int) -> np.ndarray:
    out = np.full(len(values), np.nan)
    for i in range(window, len(values)):
        out[i] = values[i - window : i].min()
    return out


@dataclass(frozen=True)
class Params:
    family: str
    a: int  # trend: fast EMA; breakout: channel; reversion: RSI period
    b: int  # trend: slow EMA; breakout: unused; reversion: oversold level
    atr_k: float
    target_r: float | None
    max_hold: int
    regime: bool  # trade only with the BTC trend

    @property
    def name(self) -> str:
        target = f"t{self.target_r:g}" if self.target_r else "tnone"
        regime = "btc" if self.regime else "any"
        return f"{self.family}-{self.a}-{self.b}-k{self.atr_k:g}-{target}-h{self.max_hold}-{regime}"


def build(bars: Bars, params: Params, btc_up: np.ndarray | None) -> Signals:
    close = bars.c
    stop_dist = params.atr_k * atr(bars)
    entry = np.zeros(len(bars), dtype=np.int8)
    if params.family == "trend":
        fast, slow = ema(close, params.a), ema(close, params.b)
        above = fast > slow
        valid = ~np.isnan(slow)
        cross_up = valid & above & np.concatenate([[False], ~above[:-1]])
        cross_down = valid & ~above & np.concatenate([[False], above[:-1]])
        entry[cross_up] = 1
        entry[cross_down] = -1
        exit_long, exit_short = valid & ~above, valid & above
    elif params.family == "breakout":
        high_before = rolling_max_before(bars.h, params.a)
        low_before = rolling_min_before(bars.low, params.a)
        entry[close > high_before] = 1
        entry[close < low_before] = -1
        half = max(2, params.a // 2)
        exit_long = close < rolling_min_before(bars.low, half)
        exit_short = close > rolling_max_before(bars.h, half)
    elif params.family == "reversion":
        level = rsi(close, params.a)
        trend = ema(close, 200)
        entry[(level < params.b) & (close > trend)] = 1
        entry[(level > 100 - params.b) & (close < trend)] = -1
        exit_long, exit_short = level > 50, level < 50
    else:
        raise ValueError(params.family)
    if params.regime and btc_up is not None:
        entry[(entry == 1) & ~btc_up] = 0
        entry[(entry == -1) & btc_up] = 0
    return Signals(entry, exit_long, exit_short, stop_dist)


def btc_trend(btc: Bars, other: Bars, period: int = 300) -> np.ndarray:
    """True where BTC closed above its long EMA at the same bar time (about 50 days on 4h)."""
    up = btc.c > ema(btc.c, period)
    lookup = dict(zip(btc.t.tolist(), up.tolist(), strict=True))
    return np.array([lookup.get(time, False) for time in other.t.tolist()], dtype=bool)
