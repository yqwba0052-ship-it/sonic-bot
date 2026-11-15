"""Real 1-minute Binance USD-M futures candles (data.binance.vision, public and free).

Training months only: 2024 and later are refused before anything is downloaded. Each month is
kept as one .npy file (Jesse columns: [time_ms, open, close, high, low, volume]); the zip is not
kept, to stay inside the disk budget.
"""

from __future__ import annotations

import time
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from ..data import BASE, DATA_DIR, Bars, _rows
from . import guard_training
from .synth import MINUTE_MS, aggregate

CACHE = DATA_DIR / "klines_1m"


class MissingMonth(RuntimeError):
    def __init__(self, symbol: str, month: str) -> None:
        super().__init__(f"no 1m data for {symbol} {month}")


def _download(url: str) -> bytes | None:
    for attempt in range(4):
        try:
            with urllib.request.urlopen(url, timeout=60) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return None
            raise
        except (TimeoutError, urllib.error.URLError, ConnectionError):
            if attempt == 3:
                raise
            time.sleep(2 * (attempt + 1))
    return None


def _month_ms(month: str) -> int:
    return int(datetime.strptime(month, "%Y-%m").replace(tzinfo=UTC).timestamp() * 1000)


def load_minutes(
    symbol: str,
    months: list[str],
    fetch: Callable[[str], bytes | None] = _download,
    cache_dir: Path = CACHE,
) -> np.ndarray:
    for month in months:
        guard_training(_month_ms(month))
    parts = []
    for month in months:
        path = cache_dir / f"{symbol}-1m-{month}.npy"
        if not path.exists():
            data = fetch(f"{BASE}/klines/{symbol}/1m/{symbol}-1m-{month}.zip")
            if not data:
                raise MissingMonth(symbol, month)
            rows = _rows(data)
            arr = np.array([[float(r[0]), float(r[1]), float(r[4]), float(r[2]), float(r[3]),
                             float(r[5])] for r in rows])
            cache_dir.mkdir(parents=True, exist_ok=True)
            np.save(path, arr)
        parts.append(np.load(path))
    out = np.concatenate(parts) if parts else np.empty((0, 6))
    _, first = np.unique(out[:, 0], return_index=True)
    return out[first]


def compare_to_bars(minutes: np.ndarray, bars: Bars, minutes_per_bar: int) -> dict:
    """Bars rebuilt from the minutes against our original bars, over the minutes' time span."""
    span = minutes_per_bar * MINUTE_MS
    agg = aggregate(minutes, minutes_per_bar)
    rebuilt = {int(row[0]): row for row in agg}
    lo, hi = int(minutes[0, 0]) // span * span, int(minutes[-1, 0])
    counts = dict(zip(*np.unique(minutes[:, 0].astype(np.int64) // span * span,
                                 return_counts=True), strict=True))
    missing_bars, mismatches, missing_minutes, worst, checked = [], [], 0, 0.0, 0
    for i, t in enumerate(bars.t.astype(np.int64)):
        t = int(t)
        if t < lo or t > hi:
            continue
        checked += 1
        row = rebuilt.get(t)
        if row is None:
            missing_bars.append(t)
            continue
        missing_minutes += minutes_per_bar - int(counts.get(t, 0))
        for name, ours, col in (("o", bars.o[i], 1), ("h", bars.h[i], 3),
                                ("low", bars.low[i], 4), ("c", bars.c[i], 2)):
            if float(ours) != float(row[col]):
                rel = abs(float(row[col]) / float(ours) - 1)
                worst = max(worst, rel)
                mismatches.append((t, name, float(ours), float(row[col])))
    return {"bars": checked, "missing_bars": missing_bars, "missing_minutes": missing_minutes,
            "mismatches": mismatches, "max_rel_diff": worst}
