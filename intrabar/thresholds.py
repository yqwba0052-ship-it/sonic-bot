"""Signal thresholds from recent days of Binance's public daily aggTrades archive.

Only days ON or AFTER CALIBRATION_FROM (2026-11-20) are used: the research hold-out period
(2024-01-01..2026-11-19) is never read. Trades are replayed through the same SymbolBars code
the live engine uses, so features match exactly.

    python -m intrabar.thresholds OUT.json [--days 7]
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import urllib.error
import urllib.request
import zipfile
from datetime import UTC, date, datetime, timedelta

from .core import SymbolBars

BASE = "https://data.binance.vision/data/futures/um/daily/aggTrades"
CALIBRATION_FROM = date(2026, 11, 20)
SYMBOLS = ("SOLUSDT", "XRPUSDT")
TIMEFRAMES = (60, 300, 900)
QS = (0.01, 0.02)


def quantile(values: list[float], q: float) -> float:
    """Linear-interpolation quantile (same as numpy's default), no numpy needed."""
    v = sorted(values)
    pos = (len(v) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (pos - lo)


def day_trades(symbol: str, day: date):
    url = f"{BASE}/{symbol}/{symbol}-aggTrades-{day.isoformat()}.zip"
    try:
        with urllib.request.urlopen(url, timeout=120) as r:  # public archive
            data = r.read()
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        text = z.read(z.namelist()[0]).decode()
    for row in csv.reader(io.StringIO(text)):
        if row and row[0][:1].isdigit():
            yield int(row[5]), float(row[1]), float(row[2]), row[6].lower() == "true"


def collect(symbol: str, days: list[date]) -> tuple[dict, list[str]]:
    rises = {tf: [] for tf in TIMEFRAMES}
    shares = {tf: [] for tf in TIMEFRAMES}
    used = []
    for d in days:
        trades = day_trades(symbol, d)
        if trades is None:
            continue
        bars = SymbolBars(TIMEFRAMES)
        for t_ms, price, qty, maker in trades:
            closed = bars.add_trade(t_ms // 1000, price, qty, maker)
            if closed is None:
                continue
            for tf in TIMEFRAMES:
                f = bars.features(closed, tf)
                if f and f["decidable"]:
                    rises[tf].append(f["rise"])
                    if f["buyshare30"] is not None:
                        shares[tf].append(f["buyshare30"])
        used.append(d.isoformat())
    out = {}
    for tf in TIMEFRAMES:
        out[tf] = {q: {"rise": quantile(rises[tf], 1 - q),
                       "buyshare30": quantile(shares[tf], 1 - q)}
                   for q in QS} if rises[tf] else {}
    return out, used


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("out")
    parser.add_argument("--days", type=int, default=7)
    args = parser.parse_args()
    today = datetime.now(UTC).date()
    days = [d for d in (today - timedelta(days=i) for i in range(1, args.days + 1))
            if d >= CALIBRATION_FROM]
    result = {"made_utc": datetime.now(UTC).isoformat(), "symbols": {}}
    for s in SYMBOLS:
        thr, used = collect(s, sorted(days))
        result["symbols"][s] = {"days": used, "thresholds": {str(k): {str(q): v for q, v in d.items()}
                                                             for k, d in thr.items()}}
        print(s, "days", used, flush=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=1)


def load(path: str) -> dict:
    """{symbol: {tf: {q: {...}}}} with numeric keys, for the engine."""
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)
    return {s: {int(tf): {float(q): v for q, v in qs.items()} for tf, qs in d["thresholds"].items()}
            for s, d in raw["symbols"].items()}



if __name__ == "__main__":
    main()
