"""Settings and checks for the full training run (no Jesse import, no download here).

Window 2021-06..2023-12 with 6 warm-up months from 2020-12; nothing from 2024 on. The large
1-minute data goes to an external drive the owner connects (--data-dir), never silently to C:.
"""

from __future__ import annotations

import calendar
import urllib.error
import urllib.request
from pathlib import Path

from ..data import BASE, month_range
from . import HOLDOUT_START_MS
from .data_1m import _month_ms

FULL_FIRST, FULL_START, FULL_END = "2020-12", "2021-06", "2023-12"
BTC = "BTCUSDT"
NPY_HEADER = 128


def months_needed() -> list[str]:
    months = month_range(FULL_FIRST, FULL_END)
    assert all(_month_ms(m) < HOLDOUT_START_MS for m in months)
    return months


def zip_size(symbol: str, month: str) -> int | None:
    """Content-Length of one monthly 1m file, by a HEAD request (nothing is downloaded)."""
    url = f"{BASE}/klines/{symbol}/1m/{symbol}-1m-{month}.zip"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, method="HEAD"), timeout=30) as r:
            return int(r.headers["Content-Length"])
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise


def usable_coins(sizes: dict[str, list[int | None]], symbols: list[str],
                 months: list[str]) -> list[str]:
    """Coins with 1m data for every month, in the bot's symbol order; BTC is required."""
    if any(size is None for size in sizes.get(BTC, [None] * len(months))):
        raise SystemExit(f"{BTC} lacks 1m data for some month: the BTC filter needs it")
    return [s for s in symbols if s in sizes and all(x is not None for x in sizes[s])]


def estimate(sizes: dict[str, list[int | None]], months: list[str]) -> dict:
    minutes = sum(calendar.monthrange(*map(int, m.split("-")))[1] * 1440 for m in months)
    return {"zip_bytes": sum(x or 0 for v in sizes.values() for x in v),
            "npy_bytes": len(sizes) * (minutes * 6 * 8 + NPY_HEADER * len(months))}


def check_data_dir(path: Path | None) -> Path:
    if path is None:
        raise SystemExit("the full run needs a data dir (--data-dir) on the external drive")
    if not Path(path).exists():
        raise SystemExit(f"{path} does not exist: connect the drive first")
    return Path(path)


def on_c_drive(path: Path) -> bool:
    return str(Path(path).resolve() if Path(path).exists() else path).upper().startswith("C:")
