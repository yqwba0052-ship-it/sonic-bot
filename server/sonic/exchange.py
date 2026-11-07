"""Paper exchange and Binance demo client.

The signed client is fixed to the demo URL. Real-money order URLs are not in this project.
PUBLIC_URL is used only for unsigned market data.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlencode

import httpx

from .risk import Filters
from .store import Store

PUBLIC_URL = "https://fapi.binance.com"
DEMO_URL = "https://demo-fapi.binance.com"
FEE_RATE = 0.0005
SLIPPAGE = 0.0002


class ExchangeError(Exception):
    pass


@dataclass(frozen=True)
class Position:
    symbol: str
    qty: float
    entry_price: float
    mark_price: float
    unrealized: float


@dataclass(frozen=True)
class Fill:
    price: float
    qty: float


@dataclass(frozen=True)
class Closed:
    exit_price: float
    pnl: float
    fees: float


class Exchange(Protocol):
    async def filters(self, symbol: str) -> Filters: ...
    async def price(self, symbol: str) -> float: ...
    async def prices(self) -> dict[str, float]: ...
    async def bars(self, symbol: str, interval: str, limit: int) -> list[tuple[int, float]]: ...
    async def balance(self) -> float: ...
    async def positions(self) -> dict[str, Position]: ...
    async def prepare(self, symbol: str, leverage: int) -> None: ...
    async def open_market(self, symbol: str, side: str, qty: float, client_id: str) -> Fill: ...
    async def protect(self, symbol: str, side: str, stop: float, target: float) -> None: ...
    async def close_market(self, symbol: str, qty: float, client_id: str) -> None: ...
    async def closed_result(self, symbol: str, since_ms: int) -> Closed: ...


def parse_filters(info: dict[str, Any], symbol: str) -> Filters:
    for item in info["symbols"]:
        if item["symbol"] == symbol:
            found = {entry["filterType"]: entry for entry in item["filters"]}
            return Filters(
                step_size=found["MARKET_LOT_SIZE"]["stepSize"],
                tick_size=found["PRICE_FILTER"]["tickSize"],
                min_qty=float(found["MARKET_LOT_SIZE"]["minQty"]),
                min_notional=float(found["MIN_NOTIONAL"]["notional"]),
            )
    raise ExchangeError(f"unknown symbol {symbol}")


class PublicMarket:
    """Unsigned market data. It has no way to send orders."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client
        self.info: dict[str, Any] | None = None

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        response = await self.client.get(path, params=params)
        if response.status_code != 200:
            raise ExchangeError(f"{path} HTTP {response.status_code}")
        return response.json()

    async def filters(self, symbol: str) -> Filters:
        if self.info is None:
            self.info = await self.get("/fapi/v1/exchangeInfo")
        return parse_filters(self.info, symbol)

    async def price(self, symbol: str) -> float:
        data = await self.get("/fapi/v1/ticker/price", {"symbol": symbol})
        return float(data["price"])

    async def prices(self) -> dict[str, float]:
        data = await self.get("/fapi/v1/ticker/price")
        return {item["symbol"]: float(item["price"]) for item in data}

    async def bars(self, symbol: str, interval: str, limit: int) -> list[tuple[int, float]]:
        """Return (open_time, close) for closed bars only."""
        data = await self.get(
            "/fapi/v1/klines", {"symbol": symbol, "interval": interval, "limit": limit + 1}
        )
        return [(int(row[0]), float(row[4])) for row in data[:-1]]


class PaperExchange:
    """Simulated fills at real prices, with fees and slippage. State survives restarts."""

    def __init__(self, market: Any, store: Store, start_balance: float) -> None:
        self.market = market
        self.store = store
        self.state: dict[str, Any] = store.get(
            "paper", {"balance": start_balance, "open": {}, "closed": {}}
        )

    def _save(self) -> None:
        self.store.put("paper", self.state)

    async def filters(self, symbol: str) -> Filters:
        return await self.market.filters(symbol)

    async def price(self, symbol: str) -> float:
        return await self.market.price(symbol)

    async def bars(self, symbol: str, interval: str, limit: int) -> list[tuple[int, float]]:
        return await self.market.bars(symbol, interval, limit)

    async def prices(self) -> dict[str, float]:
        return await self.market.prices()

    async def balance(self) -> float:
        return float(self.state["balance"])

    async def prepare(self, symbol: str, leverage: int) -> None:
        return None

    async def open_market(self, symbol: str, side: str, qty: float, client_id: str) -> Fill:
        last = await self.market.price(symbol)
        price = last * (1 + SLIPPAGE if side == "BUY" else 1 - SLIPPAGE)
        fee = qty * price * FEE_RATE
        signed = qty if side == "BUY" else -qty
        self.state["balance"] -= fee
        self.state["open"][symbol] = {"qty": signed, "entry": price, "fees": fee}
        self._save()
        return Fill(price, qty)

    async def protect(self, symbol: str, side: str, stop: float, target: float) -> None:
        self.state["open"][symbol].update({"stop": stop, "target": target})
        self._save()

    def _settle(self, symbol: str, last: float) -> None:
        pos = self.state["open"].pop(symbol)
        price = last * (1 - SLIPPAGE if pos["qty"] > 0 else 1 + SLIPPAGE)
        fee = abs(pos["qty"]) * price * FEE_RATE
        gross = pos["qty"] * (price - pos["entry"])
        self.state["balance"] += gross - fee
        fees = pos["fees"] + fee
        self.state["closed"][symbol] = {"exit": price, "pnl": gross - fees, "fees": fees}
        self._save()

    async def positions(self) -> dict[str, Position]:
        result: dict[str, Position] = {}
        for symbol in list(self.state["open"]):
            last = await self.market.price(symbol)
            pos = self.state["open"][symbol]
            long = pos["qty"] > 0
            stop, target = pos.get("stop"), pos.get("target")
            hit_stop = stop is not None and (last <= stop if long else last >= stop)
            hit_target = target is not None and (last >= target if long else last <= target)
            if hit_stop or hit_target:
                self._settle(symbol, last)
                continue
            unrealized = pos["qty"] * (last - pos["entry"])
            result[symbol] = Position(symbol, pos["qty"], pos["entry"], last, unrealized)
        return result

    async def close_market(self, symbol: str, qty: float, client_id: str) -> None:
        if symbol in self.state["open"]:
            self._settle(symbol, await self.market.price(symbol))

    async def closed_result(self, symbol: str, since_ms: int) -> Closed:
        data = self.state["closed"].get(symbol)
        if data is None:
            raise ExchangeError(f"no closed result for {symbol}")
        return Closed(data["exit"], data["pnl"], data["fees"])


class BinanceDemo:
    """Signed client for Binance demo futures, one-way mode, isolated margin."""

    def __init__(self, client: httpx.AsyncClient, key: str, secret: str) -> None:
        if str(client.base_url).rstrip("/") != DEMO_URL:
            raise ValueError("BinanceDemo may only use the demo URL")
        self.client = client
        self.key = key
        self.secret = secret
        self.market = PublicMarket(client)
        self.offset_ms = 0

    async def sync_time(self) -> None:
        data = await self.market.get("/fapi/v1/time")
        self.offset_ms = int(data["serverTime"]) - int(time.time() * 1000)

    def sign(self, params: dict[str, Any]) -> str:
        fields = {**params, "timestamp": int(time.time() * 1000) + self.offset_ms}
        fields["recvWindow"] = 5000
        query = urlencode(fields)
        digest = hmac.new(self.secret.encode(), query.encode(), hashlib.sha256).hexdigest()
        return f"{query}&signature={digest}"

    async def signed(self, method: str, path: str, params: dict[str, Any] | None = None) -> Any:
        response = await self.client.request(
            method,
            f"{path}?{self.sign(params or {})}",
            headers={"X-MBX-APIKEY": self.key},
        )
        data = response.json()
        if response.status_code != 200:
            code = data.get("code") if isinstance(data, dict) else None
            message = data.get("msg") if isinstance(data, dict) else response.text
            error = ExchangeError(f"{path} HTTP {response.status_code} {code} {message}")
            error.code = code  # type: ignore[attr-defined]
            raise error
        return data

    async def filters(self, symbol: str) -> Filters:
        return await self.market.filters(symbol)

    async def price(self, symbol: str) -> float:
        return await self.market.price(symbol)

    async def bars(self, symbol: str, interval: str, limit: int) -> list[tuple[int, float]]:
        return await self.market.bars(symbol, interval, limit)

    async def prices(self) -> dict[str, float]:
        return await self.market.prices()

    async def balance(self) -> float:
        for item in await self.signed("GET", "/fapi/v3/balance"):
            if item["asset"] == "USDT":
                return float(item["balance"])
        return 0.0

    async def positions(self) -> dict[str, Position]:
        result: dict[str, Position] = {}
        for item in await self.signed("GET", "/fapi/v3/positionRisk"):
            qty = float(item["positionAmt"])
            if qty:
                symbol = item["symbol"]
                result[symbol] = Position(
                    symbol,
                    qty,
                    float(item["entryPrice"]),
                    float(item["markPrice"]),
                    float(item["unRealizedProfit"]),
                )
        return result

    async def cancel_protection(self, symbol: str) -> None:
        """Remove leftover stops. A failure here must never block closing a position."""
        with suppress(ExchangeError):
            await self.signed("DELETE", "/fapi/v1/algoOpenOrders", {"symbol": symbol})

    async def prepare(self, symbol: str, leverage: int) -> None:
        await self.cancel_protection(symbol)
        mode = await self.signed("GET", "/fapi/v1/positionSide/dual")
        if mode.get("dualSidePosition"):
            raise ExchangeError("hedge mode is on; switch the demo account to one-way mode")
        try:
            await self.signed(
                "POST", "/fapi/v1/marginType", {"symbol": symbol, "marginType": "ISOLATED"}
            )
        except ExchangeError as error:
            if getattr(error, "code", None) != -4046:  # already isolated
                raise
        await self.signed("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": leverage})

    async def open_market(self, symbol: str, side: str, qty: float, client_id: str) -> Fill:
        data = await self.signed(
            "POST",
            "/fapi/v1/order",
            {
                "symbol": symbol,
                "side": side,
                "type": "MARKET",
                "quantity": f"{qty:.8f}".rstrip("0").rstrip("."),
                "newClientOrderId": client_id,
                "newOrderRespType": "RESULT",
            },
        )
        return Fill(float(data["avgPrice"]), float(data["executedQty"]))

    async def protect(self, symbol: str, side: str, stop: float, target: float) -> None:
        exit_side = "SELL" if side == "BUY" else "BUY"
        for order_type, trigger in (("STOP_MARKET", stop), ("TAKE_PROFIT_MARKET", target)):
            await self.signed(
                "POST",
                "/fapi/v1/algoOrder",
                {
                    "algoType": "CONDITIONAL",
                    "symbol": symbol,
                    "side": exit_side,
                    "type": order_type,
                    "triggerPrice": f"{trigger:.8f}".rstrip("0").rstrip("."),
                    "closePosition": "true",
                    "workingType": "MARK_PRICE",
                },
            )

    async def close_market(self, symbol: str, qty: float, client_id: str) -> None:
        await self.cancel_protection(symbol)
        await self.signed(
            "POST",
            "/fapi/v1/order",
            {
                "symbol": symbol,
                "side": "SELL" if qty > 0 else "BUY",
                "type": "MARKET",
                "quantity": f"{abs(qty):.8f}".rstrip("0").rstrip("."),
                "reduceOnly": "true",
                "newClientOrderId": client_id,
            },
        )

    async def closed_result(self, symbol: str, since_ms: int) -> Closed:
        await self.cancel_protection(symbol)
        fills = await self.signed(
            "GET", "/fapi/v1/userTrades", {"symbol": symbol, "startTime": since_ms}
        )
        if not fills:
            raise ExchangeError(f"no fills for {symbol}")
        fees = sum(float(item["commission"]) for item in fills)
        gross = sum(float(item["realizedPnl"]) for item in fills)
        return Closed(float(fills[-1]["price"]), gross - fees, fees)
