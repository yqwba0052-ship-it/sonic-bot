"""Run the intrabar paper trader: live Binance aggTrade stream -> Engine -> SQLite.
Paper only: there is no order code here at all, and no API key is read.

    python -m intrabar.run --db intrabar.db --thresholds thresholds.json
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import sqlite3
import time
from datetime import UTC, datetime

import websockets

from . import thresholds as thr_mod
from .core import Engine, Variant

# Binance moved USD-M market streams under /market/ (the old /stream path connects but stays silent)
STREAM = "wss://fstream.binance.com/market/stream?streams="
SILENCE_S = 30  # no message for this long = a dead stream -> reconnect
SYMBOLS = ["SOLUSDT", "XRPUSDT"]
SIGNALS, TIMEFRAMES, QS = ("B1", "B2"), (60, 300, 900), (0.01, 0.02)
TARGETS, WINDOWS = (0.001, 0.002, 0.003), ("all_day", "session_opens")

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT, variant TEXT NOT NULL, symbol TEXT NOT NULL,
    decided INTEGER NOT NULL, opened INTEGER NOT NULL, closed INTEGER NOT NULL,
    entry REAL NOT NULL, exit REAL NOT NULL, kind TEXT NOT NULL,
    gross REAL NOT NULL, fees REAL NOT NULL, net REAL NOT NULL);
CREATE TABLE IF NOT EXISTS events (
    at INTEGER NOT NULL, level TEXT NOT NULL, code TEXT NOT NULL, detail TEXT NOT NULL);
"""


def all_variants() -> list[Variant]:
    return [Variant(*v) for v in itertools.product(SIGNALS, TIMEFRAMES, QS, TARGETS, WINDOWS)]


class Store:
    def __init__(self, path: str) -> None:
        self.db = sqlite3.connect(path)
        self.db.executescript(SCHEMA)

    def trade(self, r: dict) -> None:
        self.db.execute(
            "INSERT INTO trades (variant, symbol, decided, opened, closed, entry, exit, kind, "
            "gross, fees, net) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (r["variant"], r["symbol"], r["decided"], r["opened"], r["closed"], r["entry"],
             r["exit"], r["kind"], r["gross"], r["fees"], r["net"]))
        self.db.commit()

    def event(self, level: str, code: str, detail: str = "") -> None:
        self.db.execute("INSERT INTO events VALUES (?,?,?,?)", (int(time.time()), level, code, detail))
        self.db.commit()


async def stream(engine: Engine, store: Store, stop_after: float | None = None) -> None:
    url = STREAM + "/".join(f"{s.lower()}@aggTrade" for s in engine.bars)
    began = time.time()
    backoff = 1
    while True:
        try:
            async with websockets.connect(url, ping_interval=20, max_size=2**22) as ws:
                store.event("info", "connected", url)
                backoff = 1
                while True:
                    if stop_after and time.time() - began > stop_after:
                        return
                    raw = await asyncio.wait_for(ws.recv(), SILENCE_S)
                    d = json.loads(raw)["data"]
                    for rec in engine.on_trade(d["s"], int(d["T"]), float(d["p"]), float(d["q"]),
                                               bool(d["m"])):
                        store.trade(rec)
        except (OSError, TimeoutError, websockets.WebSocketException) as error:
            if stop_after and time.time() - began > stop_after:
                return
            store.event("warning", "disconnected", f"{type(error).__name__}: {error}"[:200])
            # open paper orders/positions are dropped on a gap: their outcome is unknown
            dropped = sum(1 for a in engine.accounts.values() if a.pending or a.position)
            for a in engine.accounts.values():
                a.pending = a.position = None
            if dropped:
                store.event("warning", "dropped_on_gap", str(dropped))
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--thresholds", required=True)
    parser.add_argument("--seconds", type=float, help="stop after this long (tests)")
    args = parser.parse_args()
    thresholds = thr_mod.load(args.thresholds)
    engine = Engine(all_variants(), SYMBOLS, thresholds)
    store = Store(args.db)
    store.event("info", "started", datetime.now(UTC).isoformat() + f" variants={len(engine.accounts)}")
    asyncio.run(stream(engine, store, args.seconds))


if __name__ == "__main__":
    main()
