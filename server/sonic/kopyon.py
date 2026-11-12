"""Closed positions of Binance lead traders from Kopyon (third-party, NOT an official Binance API).

The owner chose this source (2026-11-13) because Binance offers no official way to read another
trader's trades. Kopyon builds its data from what Binance shows publicly on its copy-trading
pages and does not guarantee it is accurate or complete; every trade keeps "kopyon" as its source.

- Only GET https://api.kopyon.com/v5/binance-copy-trading/positionHistory, with the owner's
  Kopyon key in X-API-KEY. The Binance keys are never sent anywhere but Binance.
- Per trader: once, the history up to 6 months back (newest closed first, at most 25 pages of
  20); then every 10 minutes the positions closed since the last one seen. Positions that are
  not fully closed are skipped until they are.
- Fields kept as given: open/close time (ms), side, leverage, entry and close price, closed
  volume, gross profit. Binance publishes no fees there, so the fee stays empty; nothing is
  invented. Each trade stores Kopyon's positionId, so it is never saved twice.
- Budget: the free plan is 10,000 requests per 30 days. Sonic counts its own requests per day,
  stops at 9,500 in the last 30 days, and widens the interval when more traders are watched.
"""

from __future__ import annotations

import math
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx

BASE = "https://api.kopyon.com/v5/binance-copy-trading"
PAGE_ROWS = 20
BACKFILL_PAGES = 25
INCREMENT_PAGES = 5
HISTORY_MS = 182 * 86_400_000
BUDGET_30D = 9_500
MIN_INTERVAL_MIN = 10
PORTFOLIO_ID = re.compile(r"^\d{8,25}$")

Transport = Callable[[], httpx.AsyncBaseTransport | None]
transport: Transport = lambda: None  # noqa: E731 - tests swap in a mock transport


class KopyonError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def interval_minutes(traders: int) -> int:
    """10 minutes, or longer when that many traders would pass the monthly budget."""
    if traders <= 0:
        return MIN_INTERVAL_MIN
    needed = math.ceil(traders * 30 * 24 * 60 / (BUDGET_30D * 0.95))
    return max(MIN_INTERVAL_MIN, needed)


def to_trade(position: dict[str, Any]) -> dict[str, Any] | None:
    """A fully closed position as a Sonic trade; None when it is not usable yet."""
    if str(position.get("status", "")).upper() != "CLOSED":
        return None
    side = {"LONG": "BUY", "SHORT": "SELL"}.get(str(position.get("side", "")).upper())
    try:
        entry = float(position["entryPrice"])
        exit_ = float(position["closePrice"])
        opened = int(position["openedAt"])
        closed = int(position["closedAt"])
    except (KeyError, TypeError, ValueError):
        return None
    symbol = str(position.get("symbol", "")).upper()
    if side is None or entry <= 0 or exit_ <= 0 or closed < opened or not symbol.endswith("USDT"):
        return None

    def num(key: str) -> float | None:
        try:
            return float(position[key]) if position.get(key) is not None else None
        except (TypeError, ValueError):
            return None

    return {
        "symbol": symbol, "side": side, "leverage": num("leverage"),
        "qty": num("closedVolume") or num("maxOpenInterest"),
        "entry_price": entry, "exit_price": exit_, "opened_ms": opened, "closed_ms": closed,
        "pnl": num("grossPnl"), "fee": None,  # not published by Binance: left empty
        "source_ref": str(position.get("positionId") or ""),
    }


class Kopyon:
    def __init__(self, key: str | None) -> None:
        self.key = (key or "").strip()
        self.requests = 0

    async def get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        headers = {"X-API-KEY": self.key} if self.key else {}
        async with httpx.AsyncClient(timeout=30, transport=transport()) as client:
            response = await client.get(f"{BASE}/{path}", params=params, headers=headers)
        self.requests += 1
        if response.status_code != 200:
            code = {401: "kopyon_key_invalid", 403: "kopyon_forbidden",
                    404: "kopyon_unknown_trader", 422: "kopyon_unknown_trader",
                    429: "kopyon_limit"}.get(response.status_code,
                                             f"kopyon_http_{response.status_code}")
            raise KopyonError(code)
        return response.json().get("data") or {}

    async def profile(self, trader_id: str) -> dict[str, Any]:
        return await self.get("traderProfile", {"traderId": trader_id})

    async def closed_since(self, trader_id: str, since_ms: int, max_pages: int,
                           stop_ms: int) -> list[dict[str, Any]]:
        """Positions closed after since_ms, newest closed first, stopping at stop_ms (the
        oldest time wanted) or after max_pages pages."""
        out: list[dict[str, Any]] = []
        cursor = None
        for _ in range(max_pages):
            params: dict[str, Any] = {"traderId": trader_id, "rows": PAGE_ROWS, "sortBy": "closed"}
            if cursor:
                params["cursor"] = cursor
            data = await self.get("positionHistory", params)
            positions = data.get("positions") or []
            done = False
            for position in positions:
                closed = int(position.get("closedAt") or 0)
                if closed and (closed <= since_ms or closed < stop_ms):
                    done = True
                    break
                out.append(position)
            cursor = data.get("nextCursor")
            if done or not cursor or not positions:
                break
        return out


def today(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%d")


def used_30d(usage: dict[str, int], now_ms: int) -> int:
    start = today(now_ms - 30 * 86_400_000)
    return sum(n for day, n in usage.items() if day > start)


def now_ms() -> int:
    return int(time.time() * 1000)
