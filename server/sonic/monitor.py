"""Audit records that never decide anything: one row per 4-hour scan, one balance snapshot
per UTC day. Every write here is wrapped by the engine so a failure can never stop trading.

- scan_log: keyed by the bar's open time. A missed scan is a bar with no row; a scan that ran
  and found nothing is a row with signals = 0. Rows older than SCAN_KEEP_DAYS are pruned.
- equity_daily: one real observation per UTC day, taken when the server sees that day. Days
  the server did not see stay missing: nothing is back-filled.
- equity_peak (kv): the highest equity and the deepest drop seen at any tick since tracking
  started, so the drawdown counts open trades at market prices, not only the cash.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from .strategy import BAR_MS

SCAN_KEEP_DAYS = 400  # about 2400 rows of a few hundred bytes each
DAY_MS = 86_400_000

SCHEMA = """
CREATE TABLE IF NOT EXISTS scan_log (
    bar_ms INTEGER PRIMARY KEY,
    at_ms INTEGER NOT NULL,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 1,
    strategy TEXT,
    market TEXT,
    btc_close REAL,
    btc_ema300 REAL,
    btc_gap_pct REAL,
    data_ok INTEGER,
    coins TEXT,
    signals INTEGER,
    opened INTEGER,
    open_trades INTEGER,
    trading_budget REAL,
    error TEXT
);
CREATE TABLE IF NOT EXISTS equity_daily (
    day TEXT PRIMARY KEY,
    at_ms INTEGER NOT NULL,
    mode TEXT NOT NULL,
    capital REAL NOT NULL,
    wallet_balance REAL NOT NULL,
    realized REAL NOT NULL,
    unrealized REAL NOT NULL,
    equity REAL NOT NULL,
    open_value REAL NOT NULL,
    fees REAL NOT NULL,
    peak REAL NOT NULL,
    drawdown_pct REAL NOT NULL,
    max_drawdown_pct REAL NOT NULL,
    open_count INTEGER NOT NULL,
    closed_count INTEGER NOT NULL,
    unproven_count INTEGER NOT NULL,
    tracking_since_ms INTEGER NOT NULL
);
"""


def ensure(db: sqlite3.Connection) -> None:
    db.executescript(SCHEMA)
    db.commit()


def record_scan(db: sqlite3.Connection, bar_ms: int, at_ms: int, row: dict[str, Any]) -> None:
    """Write (or rewrite, after a retry) the row of one bar, then prune old rows."""
    previous = db.execute("SELECT attempts FROM scan_log WHERE bar_ms = ?", (bar_ms,)).fetchone()
    attempts = (previous[0] + 1) if previous else 1
    fields = {
        "bar_ms": bar_ms, "at_ms": at_ms, "attempts": attempts,
        "status": row.get("status", "scanned"), "strategy": row.get("strategy"),
        "market": row.get("market"), "btc_close": row.get("btc_close"),
        "btc_ema300": row.get("btc_ema300"), "btc_gap_pct": row.get("btc_gap_pct"),
        "data_ok": None if row.get("data_ok") is None else int(bool(row["data_ok"])),
        "coins": json.dumps(row.get("coins") or {}, separators=(",", ":")),
        "signals": row.get("signals"), "opened": row.get("opened"),
        "open_trades": row.get("open_trades"), "trading_budget": row.get("trading_budget"),
        "error": (row.get("error") or None) and str(row["error"])[:300],
    }
    names = ", ".join(fields)
    marks = ", ".join("?" for _ in fields)
    db.execute(f"INSERT OR REPLACE INTO scan_log ({names}) VALUES ({marks})",
               tuple(fields.values()))
    db.execute("DELETE FROM scan_log WHERE bar_ms < ?", (bar_ms - SCAN_KEEP_DAYS * DAY_MS,))
    db.commit()


def scans(db: sqlite3.Connection, limit: int = 60) -> list[dict[str, Any]]:
    rows = db.execute("SELECT * FROM scan_log ORDER BY bar_ms DESC LIMIT ?", (limit,))
    result = []
    for row in rows:
        item = dict(zip([c[0] for c in rows.description], row, strict=True))
        item["coins"] = json.loads(item["coins"] or "{}")
        result.append(item)
    return result


def scan_gaps(db: sqlite3.Connection, until_bar_ms: int) -> dict[str, Any]:
    """Bars between the first logged scan and until_bar_ms that have no row (missed scans),
    and bars whose last attempt ended in an error. A scan with no signal is not a gap."""
    first = db.execute("SELECT MIN(bar_ms) FROM scan_log").fetchone()[0]
    if first is None:
        return {"since_bar_ms": None, "expected": 0, "missed": [], "errors": []}
    seen = {r[0] for r in db.execute("SELECT bar_ms FROM scan_log WHERE bar_ms <= ?",
                                     (until_bar_ms,))}
    expected = list(range(first, until_bar_ms + 1, BAR_MS))
    errors = [r[0] for r in db.execute(
        "SELECT bar_ms FROM scan_log WHERE status = 'error' AND bar_ms <= ?", (until_bar_ms,))]
    return {"since_bar_ms": first, "expected": len(expected),
            "missed": [b for b in expected if b not in seen], "errors": errors}


def update_peak(store: Any, now_ms: int, equity: float,
                basis: list[Any] | None = None) -> dict[str, Any]:
    """Track the highest equity and the deepest fall from it, at every tick (open trades at
    market price). Written only when something changes. When the owner saves a new amount
    (basis changes), tracking restarts: a lowered amount is not a drawdown."""
    state = store.get("equity_peak") or {}
    if not state or (basis is not None and state.get("basis") not in (None, basis)):
        state = {"since_ms": now_ms, "peak": equity, "peak_ms": now_ms, "max_dd_pct": 0.0,
                 "basis": basis}
        store.put("equity_peak", state)
        return state
    changed = False
    if equity > state["peak"]:
        state.update(peak=equity, peak_ms=now_ms)
        changed = True
    dd = (state["peak"] - equity) / state["peak"] * 100 if state["peak"] > 0 else 0.0
    if dd > state["max_dd_pct"] + 1e-9:
        state.update(max_dd_pct=dd, max_dd_ms=now_ms)
        changed = True
    if changed:
        store.put("equity_peak", state)
    return state


def snapshot_due(db: sqlite3.Connection, day: str) -> bool:
    return db.execute("SELECT 1 FROM equity_daily WHERE day = ?", (day,)).fetchone() is None


def write_snapshot(db: sqlite3.Connection, row: dict[str, Any]) -> None:
    names = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    db.execute(f"INSERT OR IGNORE INTO equity_daily ({names}) VALUES ({marks})",
               tuple(row.values()))
    db.commit()


def snapshots(db: sqlite3.Connection, limit: int = 120) -> list[dict[str, Any]]:
    rows = db.execute("SELECT * FROM equity_daily ORDER BY day DESC LIMIT ?", (limit,))
    return [dict(zip([c[0] for c in rows.description], row, strict=True)) for row in rows]
