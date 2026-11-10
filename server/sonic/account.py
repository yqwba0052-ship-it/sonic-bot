"""Read-only link to the owner's own Binance USD-M futures history.

Safety:
- the key is accepted only if Binance itself reports it as read-only
  (GET /sapi/v1/account/apiRestrictions: enableReading on, every trading, transfer and
  withdrawal permission off). It is re-checked before every sync;
- this client can only send GET requests to three fixed paths (ALLOWED). It has no code path
  that could place, change or cancel an order, transfer or withdraw;
- the secret is kept on this server only and is never returned by the API.

Binance's official copy-trading API does not say which lead trader a copied trade came from, and
it is not documented whether copy-trading positions appear in the main futures account at all.
So synced trades go to the "my-account" bucket, and only the owner moves them to a trader.
Futures trade history through the API covers about the last 3 months.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlencode

import httpx

SPOT = "https://api.binance.com"
FUTURES = "https://fapi.binance.com"
ALLOWED = {
    (SPOT, "/sapi/v1/account/apiRestrictions"),
    (FUTURES, "/fapi/v1/income"),
    (FUTURES, "/fapi/v1/userTrades"),
}
NOT_PERMISSIONS = {"ipRestrict", "createTime", "tradingAuthorityExpirationTime", "enableReading",
                   "enableFixReadOnly"}
WEEK_MS = 7 * 86_400_000
HISTORY_MS = 90 * 86_400_000

Transport = Callable[[], httpx.AsyncBaseTransport | None]
transport: Transport = lambda: None  # noqa: E731 - tests swap in a mock transport


class NotReadOnly(Exception):
    def __init__(self, problems: list[str]) -> None:
        super().__init__(",".join(problems))
        self.problems = problems


def read_only_problems(permissions: dict[str, Any]) -> list[str]:
    """Every permission beyond reading that is switched on (empty list = read-only key)."""
    problems = [] if permissions.get("enableReading") is True else ["enableReading_off"]
    for name, value in permissions.items():
        if name not in NOT_PERMISSIONS and value is True:
            problems.append(name)
    return problems


class AccountReader:
    def __init__(self, key: str, secret: str, now_ms: Callable[[], int] | None = None) -> None:
        self.key, self.secret = key.strip(), secret.strip()
        self.now_ms = now_ms or (lambda: int(time.time() * 1000))

    async def _get(self, client: httpx.AsyncClient, base: str, path: str,
                   params: dict[str, Any]) -> Any:
        if (base, path) not in ALLOWED:  # nothing else can ever be called from here
            raise PermissionError(path)
        query = urlencode({**params, "timestamp": self.now_ms(), "recvWindow": 10000})
        signature = hmac.new(self.secret.encode(), query.encode(), hashlib.sha256).hexdigest()
        response = await client.get(f"{base}{path}?{query}&signature={signature}",
                                    headers={"X-MBX-APIKEY": self.key})
        if response.status_code != 200:
            raise RuntimeError(f"binance_{response.status_code}")
        return response.json()

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=20, transport=transport())

    async def check(self) -> dict[str, Any]:
        """Binance's own report of this key's permissions; raises NotReadOnly if it can do more
        than read."""
        async with self._client() as client:
            permissions = await self._get(client, SPOT, "/sapi/v1/account/apiRestrictions", {})
        problems = read_only_problems(permissions)
        if problems:
            raise NotReadOnly(problems)
        return permissions

    async def fills(self, start_ms: int) -> list[dict[str, Any]]:
        """Own futures fills since start_ms (at most the last ~90 days), oldest first."""
        await self.check()
        now = self.now_ms()
        start = max(start_ms, now - HISTORY_MS)
        out: list[dict[str, Any]] = []
        async with self._client() as client:
            symbols: set[str] = set()
            for begin in range(start, now, WEEK_MS):
                rows = await self._get(client, FUTURES, "/fapi/v1/income", {
                    "startTime": begin, "endTime": min(begin + WEEK_MS, now) - 1, "limit": 1000})
                symbols |= {row["symbol"] for row in rows if row.get("symbol")}
            for symbol in sorted(symbols):
                for begin in range(start, now, WEEK_MS):
                    rows = await self._get(client, FUTURES, "/fapi/v1/userTrades", {
                        "symbol": symbol, "startTime": begin,
                        "endTime": min(begin + WEEK_MS, now) - 1, "limit": 1000})
                    for row in rows:
                        out.append({
                            "at": int(row["time"]), "symbol": row["symbol"],
                            "side": row["side"], "price": float(row["price"]),
                            "qty": abs(float(row["qty"])),
                            "pnl": float(row.get("realizedPnl") or 0),
                            "fee": abs(float(row.get("commission") or 0))
                            if row.get("commissionAsset") == "USDT" else 0.0,
                        })
        out.sort(key=lambda f: f["at"])
        return out
