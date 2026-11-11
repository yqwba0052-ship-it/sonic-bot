"""SQLite storage for settings, trades and events."""

from __future__ import annotations

import json
import os
import sqlite3
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    qty REAL NOT NULL,
    entry_price REAL NOT NULL,
    stop_price REAL NOT NULL,
    target_price REAL NOT NULL,
    margin REAL NOT NULL,
    opened_ms INTEGER NOT NULL,
    closed_ms INTEGER,
    exit_price REAL,
    pnl REAL,
    fees REAL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at_ms INTEGER NOT NULL,
    level TEXT NOT NULL,
    code TEXT NOT NULL,
    detail TEXT NOT NULL
);
"""


class Store:
    def __init__(self, path: str) -> None:
        self.db = sqlite3.connect(path, check_same_thread=False)
        if path != ":memory:" and os.name == "posix":
            os.chmod(path, 0o600)  # holds the owner's API keys: the bot's user only
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(trades)")}
        for name, kind in (
            ("risk_usdt", "REAL"),
            ("r", "REAL"),
            ("scaled", "INTEGER DEFAULT 0"),
            ("plan", "TEXT"),
            ("exit_reason", "TEXT"),
        ):
            if name not in columns:
                self.db.execute(f"ALTER TABLE trades ADD COLUMN {name} {kind}")
        self.db.commit()

    def get(self, key: str, default: Any = None) -> Any:
        row = self.db.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def put(self, key: str, value: Any) -> None:
        self.db.execute(
            "INSERT INTO kv (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)),
        )
        self.db.commit()

    def add_event(self, at_ms: int, level: str, code: str, detail: str = "") -> None:
        self.db.execute(
            "INSERT INTO events (at_ms, level, code, detail) VALUES (?, ?, ?, ?)",
            (at_ms, level, code, detail),
        )
        self.db.commit()

    def events(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.db.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(row) for row in rows]

    def open_trade(self, **fields: Any) -> int:
        names = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        cursor = self.db.execute(
            f"INSERT INTO trades ({names}) VALUES ({marks})", tuple(fields.values())
        )
        self.db.commit()
        return int(cursor.lastrowid or 0)

    def set_protection(self, trade_id: int, stop: float, target: float) -> None:
        self.db.execute(
            "UPDATE trades SET stop_price = ?, target_price = ? WHERE id = ?",
            (stop, target, trade_id),
        )
        self.db.commit()

    def mark_scaled(self, trade_id: int, new_stop: float) -> None:
        self.db.execute(
            "UPDATE trades SET scaled = 1, stop_price = ? WHERE id = ?", (new_stop, trade_id)
        )
        self.db.commit()

    def close_trade(
        self,
        trade_id: int,
        closed_ms: int,
        exit_price: float,
        pnl: float,
        fees: float,
        exit_reason: str = "",
    ) -> None:
        self.db.execute(
            "UPDATE trades SET closed_ms = ?, exit_price = ?, pnl = ?, fees = ?, exit_reason = ?, "
            "r = CASE WHEN risk_usdt > 0 THEN ? / risk_usdt END WHERE id = ?",
            (closed_ms, exit_price, pnl, fees, exit_reason, pnl, trade_id),
        )
        self.db.commit()

    def open_trades(self) -> list[dict[str, Any]]:
        rows = self.db.execute("SELECT * FROM trades WHERE closed_ms IS NULL ORDER BY id")
        return [dict(row) for row in rows]

    def trades(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.db.execute("SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(row) for row in rows]

    def realized_since(self, since_ms: int) -> float:
        row = self.db.execute(
            "SELECT COALESCE(SUM(pnl), 0) AS total FROM trades WHERE closed_ms >= ?", (since_ms,)
        ).fetchone()
        return float(row["total"])

    def closed_r(self, limit: int) -> list[float]:
        """R of the most recent closed trades, newest first."""
        rows = self.db.execute(
            "SELECT r FROM trades WHERE closed_ms IS NOT NULL AND r IS NOT NULL "
            "ORDER BY closed_ms DESC, id DESC LIMIT ?",
            (limit,),
        )
        return [float(row["r"]) for row in rows]

    def coin_stats(self) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT symbol, COUNT(*) AS trades, AVG(r) AS avg_r, SUM(pnl) AS pnl, "
            "SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins FROM trades "
            "WHERE closed_ms IS NOT NULL GROUP BY symbol ORDER BY symbol"
        )
        return [dict(row) for row in rows]

    def trade(self, trade_id: int) -> dict[str, Any]:
        return dict(self.db.execute("SELECT * FROM trades WHERE id = ?", (trade_id,)).fetchone())

    def closed_between(self, start_ms: int, end_ms: int) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT * FROM trades WHERE closed_ms >= ? AND closed_ms < ? ORDER BY closed_ms",
            (start_ms, end_ms),
        )
        return [dict(row) for row in rows]
