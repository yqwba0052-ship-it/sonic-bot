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
from intrabar.core import Engine, SymbolBars, Variant  # noqa: E402
from intrabar.thresholds import load as load_thresholds  # noqa: E402

from . import fastlearn  # noqa: E402
from .fastmind import Mind  # noqa: E402

SYMBOLS = ["SOLUSDT", "XRPUSDT"]
STREAM = "wss://fstream.binance.com/market/stream?streams="
SILENCE_S = 30
ERROR_EVERY_MS = 60_000  # the same error code is written at most once a minute
EVENTS_KEEP = 5000
REVIEW_EVERY_S = 60
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
        self._last_error_ms: dict[str, int] = {}
        self._suppressed: dict[str, int] = {}
        self._events_written = 0
        self._next_review = 0.0
        self._restore_today()

    # ---------- small helpers
    def _get(self, key: str, default: Any) -> Any:
        row = self.db.execute("SELECT value FROM fast_state WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def _put(self, key: str, value: Any, commit: bool = True) -> None:
        self.db.execute("INSERT INTO fast_state VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET "
                        "value=excluded.value", (key, json.dumps(value)))
        if commit:
            self.db.commit()

    def log(self, level: str, code: str, detail: str = "") -> None:
        now = self.now_ms()
        if level == "error":  # a repeating error must not flood the table or slow the loop
            if now - self._last_error_ms.get(code, -ERROR_EVERY_MS) < ERROR_EVERY_MS:
                self._suppressed[code] = self._suppressed.get(code, 0) + 1
                return
            self._last_error_ms[code] = now
            if self._suppressed.get(code):
                detail = f"(+{self._suppressed.pop(code)} similar) {detail}"
        self.db.execute("INSERT INTO fast_events VALUES (?,?,?,?)",
                        (now, level, code, detail[:300]))
        self._events_written += 1
        if self._events_written % 500 == 0:  # bounded table
            self.db.execute("DELETE FROM fast_events WHERE rowid <= "
                            "(SELECT MAX(rowid) FROM fast_events) - ?", (EVENTS_KEEP,))
        self.db.commit()

    def _safe_log(self, level: str, code: str, detail: str = "") -> None:
        try:
            self.log(level, code, detail)
        except Exception:  # noqa: BLE001, S110 - logging must never break the loop
            pass

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
        if recs:
            self.db.commit()  # one commit per live trade, however many records closed
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
            self._put("balance", self.balance, commit=False)
        self.mind.learn(features, rec["net"])

    def drop_open(self, reason: str) -> None:
        """A stream gap: open paper orders/positions have an unknown outcome -> dropped."""
        dropped = 0
        for acc in self.engine.accounts.values():
            if acc.pending or acc.position:
                dropped += 1
            acc.pending = acc.position = None
        for symbol, bars in list(self.engine.bars.items()):  # no stale pre-gap second
            self.engine.bars[symbol] = SymbolBars(bars.timeframes)
        self.engine.last_price.clear()
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
        self.wanted = choice.variant if choice.changed else None
        self._put("wanted", self.wanted)
        self.log("info", "weekly_review", choice.reason)
        self._maybe_switch()
        return choice

    def _maybe_switch(self) -> None:
        if not self.wanted or self.wanted == self.active:
            return
        old, new = self.engine.accounts[self.active], self.engine.accounts[self.wanted]
        if old.pending or old.position or new.pending or new.position:
            return  # both flat: no trade that began before the switch is credited
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
            self._safe_log("error", "desk_error", f"{type(error).__name__}: {error}")

    def _safe_review(self) -> None:
        """At most once a minute, connected or not."""
        if time.monotonic() < self._next_review:
            return
        self._next_review = time.monotonic() + REVIEW_EVERY_S
        try:
            self.review()
        except Exception as error:  # noqa: BLE001
            self._safe_log("error", "review_failed", f"{type(error).__name__}: {error}")

    async def _rebuild_mind(self) -> None:
        try:
            learned = await self.mind.rebuild_in_background()
            if learned:
                self._safe_log("info", "mind_rebuilt", f"{learned} experiences")
        except Exception as error:  # noqa: BLE001 - predictions stay off, experience is kept
            self._safe_log("error", "mind_rebuild_failed", f"{type(error).__name__}: {error}")

    async def run_forever(self, stop_after: float | None = None) -> None:
        import websockets

        url = STREAM + "/".join(f"{s.lower()}@aggTrade" for s in SYMBOLS)
        began = time.time()
        backoff = 1
        rebuild = asyncio.create_task(self._rebuild_mind()) if self.mind.needs_rebuild else None
        try:
            while True:
                if stop_after and time.time() - began > stop_after:
                    return
                self._safe_review()
                try:
                    async with websockets.connect(url, ping_interval=20, max_size=2**22) as ws:
                        self._safe_log("info", "connected", url)
                        backoff = 1
                        while True:
                            if stop_after and time.time() - began > stop_after:
                                return
                            self._safe_review()
                            await self.handle(await asyncio.wait_for(ws.recv(), SILENCE_S))
                except asyncio.CancelledError:
                    raise
                except Exception as error:  # noqa: BLE001 - reconnect on anything
                    self._safe_log("warning", "disconnected", f"{type(error).__name__}: {error}")
                    try:
                        self.drop_open("disconnected")
                    except Exception as drop_error:  # noqa: BLE001
                        self._safe_log("error", "drop_failed", str(drop_error))
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 60)
        finally:
            if rebuild and not rebuild.done():
                rebuild.cancel()
            self.mind.save()  # never raises: the model file is only a cache


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
