"""Fast desk: the owner's intrabar style inside Sonic, beside the untouched 4-hour strategy.

PAPER ONLY: no order code, no API key. Own tables in sonic.db (fast_*), never Sonic's
trades/events/kv. All 72 lab variants run in shadow; only the ACTIVE variant moves the desk's
own 30 USDT paper balance, at x1 (no leverage while learning: owner's rule 2026-11-23).
Every finished shadow trade is stored in fast_experience together with what the bot saw at the
decision: this is the bot's tool-neutral experience (fastmind.py learns from it).
A failure here never reaches Sonic's engine: run_forever catches and logs everything.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import os
import sqlite3
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

try:
    import intrabar  # noqa: F401
except ImportError:  # the repo root holds the intrabar package
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from intrabar.core import Engine, Variant  # noqa: E402
from intrabar.thresholds import load as load_thresholds  # noqa: E402

from . import fastlearn  # noqa: E402
from .fastmind import Mind  # noqa: E402

SYMBOLS = ["SOLUSDT", "XRPUSDT"]
STREAM = "wss://fstream.binance.com/market/stream?streams="
SILENCE_S = 30
START_VARIANT = "B2-1m-q1-t0.1-all_day"
START_BALANCE = 30.0
LEVERAGE = 1
LEVERAGE_INFO = 24  # shown for information only, never applied

SIGNALS, TIMEFRAMES, QS = ("B1", "B2"), (60, 300, 900), (0.01, 0.02)
TARGETS, WINDOWS = (0.001, 0.002, 0.003), ("all_day", "session_opens")

SCHEMA = """
CREATE TABLE IF NOT EXISTS fast_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS fast_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT, variant TEXT NOT NULL, symbol TEXT NOT NULL,
    decided INTEGER NOT NULL, opened INTEGER NOT NULL, closed INTEGER NOT NULL,
    entry REAL NOT NULL, exit REAL NOT NULL, kind TEXT NOT NULL, gross REAL NOT NULL,
    fees REAL NOT NULL, net REAL NOT NULL, balance_after REAL NOT NULL);
CREATE TABLE IF NOT EXISTS fast_experience (
    id INTEGER PRIMARY KEY AUTOINCREMENT, variant TEXT NOT NULL, symbol TEXT NOT NULL,
    decided INTEGER NOT NULL, closed INTEGER, features TEXT NOT NULL,
    predicted REAL, net REAL, kind TEXT);
CREATE INDEX IF NOT EXISTS fast_experience_closed ON fast_experience (closed);
CREATE TABLE IF NOT EXISTS fast_events (
    at_ms INTEGER NOT NULL, level TEXT NOT NULL, code TEXT NOT NULL, detail TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS fast_choices (
    at_ms INTEGER NOT NULL, old TEXT NOT NULL, new TEXT NOT NULL, changed INTEGER NOT NULL,
    no_profitable INTEGER NOT NULL, reason TEXT NOT NULL, stats TEXT NOT NULL);
"""


def all_variants() -> list[Variant]:
    return [Variant(*v) for v in itertools.product(SIGNALS, TIMEFRAMES, QS, TARGETS, WINDOWS)]


def parse_variant(name: str) -> Variant:
    for v in all_variants():
        if v.name == name:
            return v
    raise ValueError(f"unknown variant {name}")


class FastDesk:
    def __init__(self, db: sqlite3.Connection, thresholds: dict,
                 now_ms: Callable[[], int] | None = None, model_path: str | None = None) -> None:
        self.db = db
        self.db.executescript(SCHEMA)
        self.now_ms = now_ms or (lambda: int(time.time() * 1000))
        self.engine = Engine(all_variants(), SYMBOLS, thresholds)
        self.active = self._get("active", START_VARIANT)
        parse_variant(self.active)
        self.balance = float(self._get("balance", START_BALANCE))
        self.wanted: str | None = self._get("wanted", None)
        if self._get("last_review_ms", None) is None:
            self._put("last_review_ms", self.now_ms())
        self.mind = Mind(db, model_path)
        self._restore_today()

    # ---------- small helpers
    def _get(self, key: str, default: Any) -> Any:
        row = self.db.execute("SELECT value FROM fast_state WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def _put(self, key: str, value: Any) -> None:
        self.db.execute("INSERT INTO fast_state VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET "
                        "value=excluded.value", (key, json.dumps(value)))
        self.db.commit()

    def log(self, level: str, code: str, detail: str = "") -> None:
        self.db.execute("INSERT INTO fast_events VALUES (?,?,?,?)",
                        (self.now_ms(), level, code, detail[:300]))
        self.db.commit()

    def _restore_today(self) -> None:
        """Day rules continue after a restart: replay today's finished shadow trades."""
        day_start = (self.now_ms() // 1000) // 86400 * 86400
        for variant, symbol, decided, net in self.db.execute(
                "SELECT variant, symbol, decided, net FROM fast_experience "
                "WHERE decided >= ? AND net IS NOT NULL ORDER BY id", (day_start,)):
            acc = self.engine.accounts.get(variant)
            if acc:
                acc.count_day(symbol, decided, net)

    # ---------- trading (paper)
    def on_trade(self, symbol: str, time_ms: int, price: float, qty: float,
                 buyer_is_maker: bool) -> list[dict]:
        recs = self.engine.on_trade(symbol, time_ms, price, qty, buyer_is_maker)
        for acc in self.engine.accounts.values():  # new decisions: predict BEFORE any outcome
            extra = acc.pending["extra"] if acc.pending else None
            if extra is not None and "predicted" not in extra:
                extra["predicted"] = self.mind.predict(extra.get("features", {}))
        out = []
        for rec in recs:
            self._record(rec)
            if rec["variant"] == self.active:
                out.append(rec)
        self._maybe_switch()
        return out

    def _record(self, rec: dict) -> None:
        extra = rec.get("extra") or {}
        features = extra.get("features", {})
        self.db.execute(
            "INSERT INTO fast_experience (variant, symbol, decided, closed, features, predicted, "
            "net, kind) VALUES (?,?,?,?,?,?,?,?)",
            (rec["variant"], rec["symbol"], rec["decided"], rec["closed"], json.dumps(features),
             extra.get("predicted"), rec["net"], rec["kind"]))
        if rec["variant"] == self.active:
            self.balance *= 1 + rec["net"] * LEVERAGE
            self.db.execute(
                "INSERT INTO fast_trades (variant, symbol, decided, opened, closed, entry, exit, "
                "kind, gross, fees, net, balance_after) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (rec["variant"], rec["symbol"], rec["decided"], rec["opened"], rec["closed"],
                 rec["entry"], rec["exit"], rec["kind"], rec["gross"], rec["fees"], rec["net"],
                 self.balance))
            self._put("balance", self.balance)
        self.db.commit()
        self.mind.learn(features, rec["net"])

    def drop_open(self, reason: str) -> None:
        """A stream gap: open paper orders/positions have an unknown outcome -> dropped."""
        dropped = 0
        for acc in self.engine.accounts.values():
            if acc.pending or acc.position:
                dropped += 1
            acc.pending = acc.position = None
        if dropped:
            self.log("warning", "dropped_on_gap", f"{dropped} ({reason})")
        self._maybe_switch()

    # ---------- weekly learning
    def review(self) -> fastlearn.Choice | None:
        now = self.now_ms()
        if not fastlearn.is_review_due(int(self._get("last_review_ms", now)), now):
            return None
        rows = self.db.execute(
            "SELECT variant, closed * 1000, net FROM fast_experience WHERE net IS NOT NULL "
            "AND closed >= ?", ((now - 35 * fastlearn.DAY_MS) // 1000,)).fetchall()
        choice = fastlearn.choose(rows, now, self.active)
        self.db.execute("INSERT INTO fast_choices VALUES (?,?,?,?,?,?,?)",
                        (now, self.active, choice.variant, int(choice.changed),
                         int(choice.no_profitable), choice.reason, json.dumps(choice.stats)))
        self._put("last_review_ms", now)
        self._put("no_profitable", choice.no_profitable)
        if choice.changed:
            self.wanted = choice.variant
            self._put("wanted", self.wanted)
        self.log("info", "weekly_review", choice.reason)
        self._maybe_switch()
        return choice

    def _maybe_switch(self) -> None:
        if not self.wanted or self.wanted == self.active:
            return
        acc = self.engine.accounts[self.active]
        if acc.pending or acc.position:
            return  # never switch in the middle of a trade
        self.log("info", "variant_switched", f"{self.active} -> {self.wanted}")
        self.active, self.wanted = self.wanted, None
        self._put("active", self.active)
        self._put("wanted", None)

    # ---------- what the owner sees
    def status(self) -> dict[str, Any]:
        total = self.balance / START_BALANCE - 1
        day_start = (self.now_ms() // 1000) // 86400 * 86400
        cols = ("variant", "symbol", "decided", "closed", "entry", "exit", "kind", "net",
                "balance_after")
        last = [dict(zip(cols, r, strict=True)) for r in self.db.execute(
            f"SELECT {', '.join(cols)} FROM fast_trades ORDER BY id DESC LIMIT 20")]
        today = self.db.execute("SELECT COUNT(*), COALESCE(SUM(net > 0), 0) FROM fast_trades "
                                "WHERE decided >= ?", (day_start,)).fetchone()
        choice = self.db.execute("SELECT at_ms, old, new, reason FROM fast_choices "
                                 "ORDER BY at_ms DESC LIMIT 1").fetchone()
        warning = "no_profitable_variant" if self._get("no_profitable", False) else None
        acc = self.engine.accounts[self.active]
        return {
            "variant": self.active, "switch_waiting": self.wanted,
            "balance": round(self.balance, 6), "start_balance": START_BALANCE,
            "leverage": LEVERAGE, "total_net_pct": round(100 * total, 4),
            "x24_info_pct": round(100 * total * LEVERAGE_INFO, 4),
            "today": {"trades": today[0], "wins": today[1]},
            "open": bool(acc.pending or acc.position),
            "last_trades": last, "warning": warning,
            "last_choice": dict(zip(("at_ms", "old", "new", "reason"), choice, strict=True))
            if choice else None,
            "mind": self.mind.shadow_report(self.now_ms()),
        }

    # ---------- live stream
    async def handle(self, raw: str | bytes) -> None:
        try:
            d = json.loads(raw)["data"]
            self.on_trade(d["s"], int(d["T"]), float(d["p"]), float(d["q"]), bool(d["m"]))
        except Exception as error:  # noqa: BLE001 - never let the desk stop Sonic
            self.log("error", "desk_error", f"{type(error).__name__}: {error}")

    async def run_forever(self, stop_after: float | None = None) -> None:
        import websockets

        url = STREAM + "/".join(f"{s.lower()}@aggTrade" for s in SYMBOLS)
        began = time.time()
        backoff = 1
        next_review = 0.0
        while True:
            try:
                async with websockets.connect(url, ping_interval=20, max_size=2**22) as ws:
                    self.log("info", "connected", url)
                    backoff = 1
                    while True:
                        if stop_after and time.time() - began > stop_after:
                            return
                        if time.time() >= next_review:
                            next_review = time.time() + 60
                            try:
                                self.review()
                            except Exception as error:  # noqa: BLE001
                                self.log("error", "review_failed", str(error))
                        await self.handle(await asyncio.wait_for(ws.recv(), SILENCE_S))
            except asyncio.CancelledError:
                self.mind.save()
                raise
            except Exception as error:  # noqa: BLE001 - reconnect on anything
                self.log("warning", "disconnected", f"{type(error).__name__}: {error}")
                self.drop_open("disconnected")
                if stop_after and time.time() - began > stop_after:
                    return
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)


def from_env(db: sqlite3.Connection, root: Path,
             now_ms: Callable[[], int] | None = None) -> FastDesk | None:
    """On only when SONIC_FAST_THRESHOLDS names an existing file (env or server/.env)."""
    from .config import read_env_file

    values = {**read_env_file(root / ".env"), **os.environ}
    path = values.get("SONIC_FAST_THRESHOLDS", "").strip()
    if not path or not Path(path).is_file():
        return None
    model = values.get("SONIC_FAST_MODEL", "").strip() or str(root / "fast_model.pkl")
    return FastDesk(db, load_thresholds(path), now_ms, model)
