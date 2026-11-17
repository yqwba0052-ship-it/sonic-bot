"""Binance USD-M aggregated trades (data.binance.vision, public): only the days Sonic entered.

Each row: price, quantity, transaction time (ms) and is_buyer_maker. A trade with
is_buyer_maker == False was started by a buyer hitting the ask (a market buy). Files stay zipped
on D:; only the needed time window is read.
"""

from __future__ import annotations

import csv
import io
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from . import AGG, TRAIN_END_MS

BASE = "https://data.binance.vision/data/futures/um/daily/aggTrades"


@dataclass(frozen=True)
class Trade:
    ms: int
    price: float
    qty: float
    buyer_maker: bool


def _day(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%d")


def days_needed(entries: list[tuple[str, int]], wait_min: int,
                allow_holdout: bool = False) -> list[tuple[str, str]]:
    """(symbol, day) files covering each entry time and its wait window."""
    out = set()
    for symbol, ms in entries:
        if ms >= TRAIN_END_MS and not allow_holdout:
            raise ValueError("holdout_locked")
        for t in (ms, ms + (wait_min + 1) * 60_000):
            out.add((symbol, _day(t)))
    return sorted(out)


def file_for(symbol: str, day: str, root: Path = AGG) -> Path:
    return Path(root) / symbol / f"{symbol}-aggTrades-{day}.zip"


def download(symbol: str, day: str, root: Path = AGG) -> Path:
    path = file_for(symbol, day, root)
    if path.exists() and path.stat().st_size > 0:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    url = f"{BASE}/{symbol}/{symbol}-aggTrades-{day}.zip"
    for attempt in range(4):
        try:
            with urllib.request.urlopen(url, timeout=120) as response:
                data = response.read()
            tmp = path.with_suffix(".part")
            tmp.write_bytes(data)
            tmp.replace(path)  # complete files only: an interrupted download leaves no .zip
            return path
        except (TimeoutError, urllib.error.URLError, ConnectionError):
            if attempt == 3:
                raise
            time.sleep(5 * (attempt + 1))
    return path


def trades_after(paths: list[Path], start_ms: int, end_ms: int) -> list[Trade]:
    """All aggregated trades with start_ms < time <= end_ms, in time order."""
    out = []
    for path in paths:
        with zipfile.ZipFile(path) as z, z.open(z.namelist()[0]) as raw:
            for row in csv.reader(io.TextIOWrapper(raw, encoding="utf-8")):
                if not row or not row[0][:1].isdigit():
                    continue
                ms = int(row[5])
                if start_ms < ms <= end_ms:
                    out.append(Trade(ms, float(row[1]), float(row[2]),
                                     row[6].strip().lower() == "true"))
    return sorted(out, key=lambda t: t.ms)
