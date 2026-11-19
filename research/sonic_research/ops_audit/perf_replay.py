"""Performance test of the deployed Sonic engine up to today (read-only for the project).

- Engine, paper exchange, sizing, limits, strategy: the server's own code (deployed ea839a7),
  default Limits() = $30, 1% risk, leverage 3, max 3 open, 8 coins, longs only.
- Data: the project's cached monthly Binance files (data.binance.vision) through 2026-10;
  November 2026 is filled IN MEMORY from Binance's public klines API, closed bars only
  (close time before the cutoff). Nothing is written to the project's data cache.
- Each run starts with a fresh $30 at its start month. The replay shows the engine only bars
  that had closed at the simulated hour (no future data).
- Optional fill months: months whose official archive file has gaps are read from the same
  public API instead (it matched the archive exactly on every shared bar), still in memory.

    server/.venv/Scripts/python -I research/sonic_research/ops_audit/perf_replay.py \
        <start YYYY-MM> <cutoff ms> <out.json> [fill months, e.g. 2022-02,2022-04,2026-10]
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "research"))

from sonic_research import data as data_mod  # noqa: E402
from sonic_research import engine_replay  # noqa: E402

CUTOFF_MS = int(sys.argv[2])  # only bars that closed before this moment
LIVE_MONTH = "2026-11"
INTERVAL_MS = {"1h": 3_600_000, "4h": 14_400_000}
original_fetch = data_mod._fetch
fetched_live: dict[str, int] = {}


FILL_MONTHS = set(sys.argv[4].split(",")) if len(sys.argv) > 4 and sys.argv[4] else set()


def live_month_zip(symbol: str, interval: str, month: str = LIVE_MONTH) -> bytes | None:
    import calendar
    from datetime import UTC, datetime
    year, mon = map(int, month.split("-"))
    start = int(datetime(year, mon, 1, tzinfo=UTC).timestamp() * 1000)
    end = int(datetime(year, mon, calendar.monthrange(year, mon)[1], 23, 59,
                       tzinfo=UTC).timestamp() * 1000)
    url = (f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}&interval={interval}"
           f"&startTime={start}&endTime={end}&limit=1500")
    with urllib.request.urlopen(url, timeout=30) as response:  # noqa: S310 - public URL
        rows = json.loads(response.read())
    rows = [r for r in rows if int(r[6]) < CUTOFF_MS]  # closed before the cutoff only
    fetched_live[f"{symbol}-{interval}-{month}"] = len(rows)
    if not rows:
        return None
    text = io.StringIO()
    csv.writer(text).writerows(rows)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(f"{symbol}-{interval}-{LIVE_MONTH}.csv", text.getvalue())
    return buffer.getvalue()


def fetch(url: str, path: Path) -> bytes | None:
    name = path.name
    month = name[-11:-4]
    if (month == LIVE_MONTH or month in FILL_MONTHS) and "/klines/" in url:
        symbol, interval = name.split("-")[0], name.split("-")[1]
        return live_month_zip(symbol, interval, month)  # in memory, never written to the cache
    return original_fetch(url, path)


data_mod._fetch = fetch


async def main() -> None:
    from sonic.risk import Limits

    start, end = sys.argv[1], LIVE_MONTH
    began = time.time()
    report = await engine_replay.replay(start, end, Limits(), save_trades=False)
    closed = sorted(report.pop("closed"), key=lambda t: t["opened_ms"])
    out = {"start": start, "end": end, "cutoff_ms": CUTOFF_MS, "live_bars": fetched_live,
           "filled_months": sorted(FILL_MONTHS),
           "seconds": round(time.time() - began),
           "summary": {k: report[k] for k in ("trades", "open_at_end", "win_rate", "avg_r",
                                               "pnl_usdt", "start_amount", "end_amount",
                                               "max_drop", "scaled", "errors", "skipped",
                                               "by_coin")},
           "events": report["events"],
           "trades": [{k: t[k] for k in ("symbol", "side", "opened_ms", "closed_ms", "entry_price",
                                         "exit_price", "qty", "pnl", "fees", "r", "exit_reason",
                                         "scaled")} for t in closed]}
    Path(sys.argv[3]).write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(json.dumps(out["summary"], indent=1))


asyncio.run(main())
