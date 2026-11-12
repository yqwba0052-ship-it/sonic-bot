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
    (FUTURES, "/fapi/v2/balance"),
    (FUTURES, "/fapi/v1/trade/asyn"),
    (FUTURES, "/fapi/v1/trade/asyn/id"),
}
# The finished history file is fetched from the link Binance returns; only these hosts.
DOWNLOAD_HOSTS = (".binance.com", ".bnbstatic.com", ".amazonaws.com")
HISTORY_6M_MS = 182 * 86_400_000
NOT_PERMISSIONS = {"ipRestrict", "createTime", "tradingAuthorityExpirationTime", "enableReading",
                   "enableFixReadOnly"}
WEEK_MS = 7 * 86_400_000
HISTORY_MS = 90 * 86_400_000

Transport = Callable[[], httpx.AsyncBaseTransport | None]
transport: Transport = lambda: None  # noqa: E731 - tests swap in a mock transport


def server_ip() -> str | None:
    """The cloud server's address, for the key's IP restriction (from SONIC_PUBLIC_URL like
    https://138-68-90-218.sslip.io)."""
    import os
    import re
    from pathlib import Path

    from .config import read_env_file

    values = {**read_env_file(Path(__file__).resolve().parent.parent / ".env"), **os.environ}
    found = re.search(r"(\d{1,3})-(\d{1,3})-(\d{1,3})-(\d{1,3})\.sslip\.io",
                      values.get("SONIC_PUBLIC_URL", ""))
    return ".".join(found.groups()) if found else None


def env_key() -> dict[str, Any] | None:
    """The read-only key from the environment or server/.env (BINANCE_READ_KEY and
    BINANCE_READ_SECRET), if both are set. Takes precedence over a key saved from the app."""
    import os
    from pathlib import Path

    from .config import read_env_file

    values = {**read_env_file(Path(__file__).resolve().parent.parent / ".env"), **os.environ}
    key = values.get("BINANCE_READ_KEY", "").strip()
    secret = values.get("BINANCE_READ_SECRET", "").strip()
    return {"key": key, "secret": secret, "source": "env"} if key and secret else None


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

    async def permissions(self) -> dict[str, Any]:
        """Binance's report of this key's permissions, unchecked (for the trading key)."""
        async with self._client() as client:
            return await self._get(client, SPOT, "/sapi/v1/account/apiRestrictions", {})

    async def balance(self) -> dict[str, float]:
        """USDT in the futures wallet: total and available (read-only key)."""
        await self.check()
        async with self._client() as client:
            rows = await self._get(client, FUTURES, "/fapi/v2/balance", {})
        for row in rows:
            if row.get("asset") == "USDT":
                return {"wallet": float(row["balance"]),
                        "available": float(row["availableBalance"])}
        return {"wallet": 0.0, "available": 0.0}

    async def recent_income(self, days: int = 30) -> list[dict[str, Any]]:
        """Futures income records (realized profit, fees, funding) of the last days."""
        now = self.now_ms()
        rows: list[dict[str, Any]] = []
        async with self._client() as client:
            for begin in range(now - days * 86_400_000, now, WEEK_MS):
                rows += await self._get(client, FUTURES, "/fapi/v1/income", {
                    "startTime": begin, "endTime": min(begin + WEEK_MS, now) - 1, "limit": 1000})
        return rows

    async def request_history(self, start_ms: int, end_ms: int) -> str:
        """Ask Binance to prepare the futures trade history file (official, up to 1 year,
        5 requests a month shared with the website). Returns the download id."""
        await self.check()
        async with self._client() as client:
            data = await self._get(client, FUTURES, "/fapi/v1/trade/asyn",
                                   {"startTime": start_ms, "endTime": end_ms})
        return str(data["downloadId"])

    async def history_link(self, download_id: str) -> dict[str, Any]:
        async with self._client() as client:
            return await self._get(client, FUTURES, "/fapi/v1/trade/asyn/id",
                                   {"downloadId": download_id})

    async def download(self, url: str) -> str:
        """The prepared file's text (a zip is opened). Only https and Binance's file hosts."""
        import io
        import zipfile
        from urllib.parse import urlparse

        parts = urlparse(url)
        if parts.scheme != "https" or not (parts.hostname or "").endswith(DOWNLOAD_HOSTS):
            raise PermissionError("download_host")
        async with httpx.AsyncClient(timeout=60, transport=transport()) as client:
            response = await client.get(url)
        if response.status_code != 200:
            raise RuntimeError(f"download_{response.status_code}")
        data = response.content
        if data[:2] == b"PK":
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                name = next((n for n in archive.namelist() if n.lower().endswith(".csv")), None)
                if name is None:
                    raise RuntimeError("download_no_csv")
                data = archive.read(name)
        return data.decode("utf-8-sig", errors="replace")

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
