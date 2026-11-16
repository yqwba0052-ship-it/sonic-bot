"""Our Binance USD-M kline zips (research/data/klines) -> Freqtrade feather files. No download.

Months after `end` are refused before anything is read, so the training phase can never see
2024-2026 by accident. Empty zip files mark months before a coin was listed and are skipped.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from ..data import DATA_DIR, _rows

KLINES = DATA_DIR / "klines"
COLUMNS = ["date", "open", "high", "low", "close", "volume"]


def feather_name(symbol: str, interval: str) -> str:
    return f"{symbol[:-4]}_USDT-{interval}.feather"


def frame(symbol: str, months: list[str], interval: str, klines: Path = KLINES,
          end: str = "2023-12") -> pd.DataFrame:
    late = [m for m in months if m > end]
    if late:
        raise ValueError(f"beyond_allowed_end: {late[0]} > {end}")
    rows = []
    for month in months:
        path = Path(klines) / f"{symbol}-{interval}-{month}.zip"
        if path.exists() and path.stat().st_size:
            rows.extend(_rows(path.read_bytes()))
    df = pd.DataFrame([[int(r[0]), *map(float, r[1:6])] for r in rows], columns=COLUMNS)
    df = df.drop_duplicates("date").sort_values("date").reset_index(drop=True)
    df["date"] = pd.to_datetime(df["date"], unit="ms", utc=True)
    return df


def write_all(out_dir: Path, symbols: list[str], months: list[str], intervals: list[str],
              end: str = "2023-12") -> dict:
    """Write one feather per symbol/interval and return a data report (rows, span, gaps)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    report = {}
    for symbol in symbols:
        for interval in intervals:
            df = frame(symbol, months, interval, end=end)
            step = pd.Timedelta(interval)
            steps = df["date"].diff().iloc[1:]
            gaps = [str(d) for d, g in zip(df["date"].iloc[1:], steps, strict=True) if g != step]
            report[f"{symbol}-{interval}"] = {
                "rows": len(df), "first": str(df["date"].iloc[0]),
                "last": str(df["date"].iloc[-1]), "gaps": gaps,
            }
            df.to_feather(out_dir / feather_name(symbol, interval))
    (out_dir / "_data_report.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    return report
