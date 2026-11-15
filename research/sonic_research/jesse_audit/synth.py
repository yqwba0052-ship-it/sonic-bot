"""One-minute candles made from 1-hour bars, for the engine-parity check (R1) only.

Each hour walks open -> low -> high -> close in three 20-minute legs: the same "stop before
target inside the hour" order our replay assumes. These minutes are NOT a model of real price
movement inside the hour; only R2 (real Binance minutes) speaks to that.
Columns follow Jesse: [time_ms, open, close, high, low, volume].
"""

from __future__ import annotations

import numpy as np

from . import guard_training

MINUTE_MS = 60_000
LEG = 20


def _leg(start: np.ndarray, stop: np.ndarray) -> np.ndarray:
    """Closes of LEG minutes moving from start to stop (exactly stop at the end), clipped."""
    path = np.linspace(start, stop, LEG + 1, axis=1)[:, 1:]
    return np.clip(path, np.minimum(start, stop)[:, None], np.maximum(start, stop)[:, None])


def minutes_from_hours(t, o, h, low, c) -> np.ndarray:
    t = np.asarray(t, dtype=np.int64)
    if len(t):
        guard_training(int(t[-1]) + 60 * MINUTE_MS - 1)
    o, h, low, c = (np.asarray(a, dtype=float) for a in (o, h, low, c))
    closes = np.concatenate([_leg(o, low), _leg(low, h), _leg(h, c)], axis=1)  # (n, 60)
    opens = np.concatenate([o[:, None], closes[:, :-1]], axis=1)
    out = np.empty((len(t), 60, 6))
    out[:, :, 0] = t[:, None] + MINUTE_MS * np.arange(60)
    out[:, :, 1] = opens
    out[:, :, 2] = closes
    out[:, :, 3] = np.maximum(opens, closes)
    out[:, :, 4] = np.minimum(opens, closes)
    out[:, :, 5] = 0.0
    return out.reshape(-1, 6)


def aggregate(minutes: np.ndarray, minutes_per_bar: int) -> np.ndarray:
    """Group minutes into UTC-aligned bars of minutes_per_bar (Jesse columns)."""
    span = minutes_per_bar * MINUTE_MS
    keys = minutes[:, 0].astype(np.int64) // span
    starts = np.flatnonzero(np.r_[True, keys[1:] != keys[:-1]])
    ends = np.r_[starts[1:], len(minutes)]
    out = np.empty((len(starts), 6))
    out[:, 0] = keys[starts] * span
    out[:, 1] = minutes[starts, 1]
    out[:, 2] = minutes[ends - 1, 2]
    out[:, 3] = np.maximum.reduceat(minutes[:, 3], starts)
    out[:, 4] = np.minimum.reduceat(minutes[:, 4], starts)
    out[:, 5] = np.add.reduceat(minutes[:, 5], starts)
    return out
