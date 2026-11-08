"""Download and load Binance USD-M futures history from data.binance.vision (public, free)."""

from __future__ import annotations

import csv
import io
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np

BASE = "https://data.binance.vision/data/futures/um/monthly"
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
SYMBOLS = (
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT",
    "XRPUSDT",
    "BNBUSDT",
    "DOGEUSDT",
    "ADAUSDT",
    "AVAXUSDT",
    "SUIUSDT",
    "TRXUSDT",
    "LINKUSDT",
    "LTCUSDT",
)
INTERVAL = "4h"
BAR_MS = 4 * 3600 * 1000


@dataclass(frozen=True)
class Bars:
    symbol: str
    t: np.ndarray  # open time, ms
    o: np.ndarray
    h: np.ndarray
    low: np.ndarray
    c: np.ndarray

    def __len__(self) -> int:
        return len(self.t)


def month_range(start: str, end: str) -> list[str]:
    year, month = map(int, start.split("-"))
    last = tuple(map(int, end.split("-")))
    result = []
    while (year, month) <= last:
        result.append(f"{year:04d}-{month:02d}")
        month += 1
        if month == 13:
            year, month = year + 1, 1
    return result


def _fetch(url: str, path: Path) -> bytes | None:
    if path.exists():
        return path.read_bytes() or None
    path.parent.mkdir(parents=True, exist_ok=True)
    data = b""
    for attempt in range(4):
        try:
            with urllib.request.urlopen(url, timeout=30) as response:
                data = response.read()
            break
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise
            break  # month before the coin was listed
        except (TimeoutError, urllib.error.URLError, ConnectionError):
            if attempt == 3:
                raise
            time.sleep(2 * (attempt + 1))
    path.write_bytes(data)
    return data or None


def _rows(data: bytes) -> list[list[str]]:
    archive = zipfile.ZipFile(io.BytesIO(data))
    text = archive.read(archive.namelist()[0]).decode()
    return [row for row in csv.reader(io.StringIO(text)) if row and row[0][:1].isdigit()]


def load_bars(symbol: str, months: list[str], interval: str = INTERVAL) -> Bars:
    rows: list[list[str]] = []
    for month in months:
        name = f"{symbol}-{interval}-{month}.zip"
        data = _fetch(f"{BASE}/klines/{symbol}/{interval}/{name}", DATA_DIR / "klines" / name)
        if data:
            rows.extend(_rows(data))
    rows.sort(key=lambda row: int(row[0]))
    unique = {int(row[0]): row for row in rows}
    ordered = [unique[key] for key in sorted(unique)]
    column = lambda index: np.array([float(row[index]) for row in ordered])  # noqa: E731
    return Bars(
        symbol=symbol,
        t=np.array([int(row[0]) for row in ordered], dtype=np.int64),
        o=column(1),
        h=column(2),
        low=column(3),
        c=column(4),
    )


def load_funding(symbol: str, months: list[str]) -> dict[int, float]:
    """Funding rate by settlement time, ms. Longs pay positive rates, shorts receive them."""
    rates: dict[int, float] = {}
    for month in months:
        name = f"{symbol}-fundingRate-{month}.zip"
        data = _fetch(f"{BASE}/fundingRate/{symbol}/{name}", DATA_DIR / "funding" / name)
        if data:
            for row in _rows(data):
                # Settlement times can carry a few ms of jitter; snap to the minute.
                rates[round(int(row[0]) / 60000) * 60000] = float(row[2])
    return rates
