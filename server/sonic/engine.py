"""Trading loop. A halt is latched: it stays until the owner clears it from the app."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable
from typing import Any

from . import risk, strategy
from .config import Settings
from .exchange import Exchange
from .risk import Limits
from .store import Store

MAX_FAILURES = 3
HISTORY_BARS = 100
DAY_MS = 86_400_000


class Engine:
    def __init__(
        self,
        store: Store,
        exchange: Exchange,
        settings: Settings,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.store = store
        self.exchange = exchange
        self.settings = settings
        self.clock = clock
        self.lock = asyncio.Lock()
        self.failures = 0
        self.last_tick_ms = 0
        self.last_error = ""
        self.unrealized = 0.0
        self.balance = 0.0
        self.live: dict[str, Any] = {}
        self.minimums: dict[str, float] = {}
        self.limits = Limits(**store.get("limits", {}))
        self.halted: str = store.get("halted", "")
        # Never resume trading by itself after a restart.
        self.running = False

    def now_ms(self) -> int:
        return int(self.clock() * 1000)

    def log(self, level: str, code: str, detail: str = "") -> None:
        self.store.add_event(self.now_ms(), level, code, detail)

    def day_start_ms(self) -> int:
        now = self.now_ms()
        return now - now % DAY_MS

    def realized_today(self) -> float:
        return self.store.realized_since(self.day_start_ms())

    def budget_start_ms(self) -> int:
        return int(self.store.get("budget_start_ms", 0))

    def budget_realized(self) -> float:
        """Closed-trade result since the owner last saved the amount."""
        return self.store.realized_since(self.budget_start_ms())

    def budget_left(self) -> float:
        """What is left of the amount, counting open trades at their current value."""
        return self.limits.capital_usdt + self.budget_realized() + self.unrealized

    def trading_budget(self) -> float:
        """Amount new trades may use: the remaining amount, never more than the wallet."""
        remaining = self.limits.capital_usdt + self.budget_realized()
        return min(remaining, self.balance) if self.balance > 0 else remaining

    def halt(self, reason: str, detail: str = "") -> None:
        self.running = False
        if not self.halted:
            self.halted = reason
            self.store.put("halted", reason)
            self.log("critical", reason, detail)

    async def start(self) -> None:
        async with self.lock:
            if self.halted:
                raise ValueError("halted")
            self.running = True
            self.log("info", "started")

    async def stop(self) -> None:
        async with self.lock:
            self.running = False
            self.log("info", "stopped")

    async def clear_halt(self) -> None:
        async with self.lock:
            if self.halted == "daily_loss_limit":
                if risk.daily_loss_breached(self.limits, self.realized_today(), self.unrealized):
                    raise ValueError("daily_loss_still_breached")
            if self.halted == "budget_used" and self.budget_left() <= 0:
                raise ValueError("budget_still_used")
            self.halted = ""
            self.failures = 0
            self.store.put("halted", "")
            self.log("info", "halt_cleared")

    async def set_limits(self, limits: Limits) -> None:
        async with self.lock:
            if self.running:
                raise ValueError("stop_first")
            self.limits = limits
            self.store.put("limits", limits.model_dump())
            # Saving the amount starts a new budget from now.
            self.store.put("budget_start_ms", self.now_ms())
            self.log("info", "limits_changed", limits.model_dump_json())

    async def panic(self) -> None:
        """Stop trading and close every open position."""
        async with self.lock:
            self.halt("panic_button")
            await self._close_all()
            await self._sync()

    async def _close_all(self) -> None:
        for symbol, position in (await self.exchange.positions()).items():
            await self.exchange.close_market(symbol, position.qty, self._client_id())

    def _client_id(self) -> str:
        return f"sb{uuid.uuid4().hex[:24]}"

    async def tick(self) -> None:
        async with self.lock:
            try:
                await self._sync()
                await self._refresh_minimums()
                await self._guard()
                if self.running and not self.halted:
                    await self._entries()
                self.failures = 0
                self.last_error = ""
            except Exception as error:  # noqa: BLE001 - every failure must reach the halt counter
                self.failures += 1
                self.last_error = str(error)[:300]
                self.log("error", "tick_failed", self.last_error)
                if self.failures >= MAX_FAILURES:
                    self.halt("repeated_failures", self.last_error)
            self.last_tick_ms = self.now_ms()

    async def _sync(self) -> None:
        positions = await self.exchange.positions()
        self.balance = await self.exchange.balance()
        self.unrealized = sum(item.unrealized for item in positions.values())
        self.live = {
            symbol: {"qty": item.qty, "mark": item.mark_price, "unrealized": item.unrealized}
            for symbol, item in positions.items()
        }
        tracked = {trade["symbol"]: trade for trade in self.store.open_trades()}
        for symbol, trade in tracked.items():
            if symbol not in positions:
                result = await self.exchange.closed_result(symbol, trade["opened_ms"])
                self.store.close_trade(
                    trade["id"], self.now_ms(), result.exit_price, result.pnl, result.fees
                )
                self.log("info", "trade_closed", f"{symbol} {result.pnl:+.2f}")
        for symbol in positions:
            if symbol not in tracked:
                self.halt("unknown_position", symbol)

    async def markets(self) -> list[dict[str, Any]]:
        prices = await self.exchange.prices()
        result = []
        for symbol in risk.ALLOWED_SYMBOLS:
            filters = await self.exchange.filters(symbol)
            price = prices[symbol]
            minimum = round(max(filters.min_notional, filters.min_qty * price), 2)
            result.append({"symbol": symbol, "price": price, "min_order_usdt": minimum})
        return result

    async def unaffordable(self, limits: Limits) -> list[str]:
        """Chosen coins whose smallest order is larger than the new amount allows."""
        largest = risk.max_order_usdt(limits, limits.capital_usdt)
        minimums = {item["symbol"]: item["min_order_usdt"] for item in await self.markets()}
        return [symbol for symbol in limits.symbols if minimums[symbol] > largest]

    async def _refresh_minimums(self) -> None:
        for symbol in self.limits.symbols:
            filters = await self.exchange.filters(symbol)
            price = await self.exchange.price(symbol)
            self.minimums[symbol] = round(max(filters.min_notional, filters.min_qty * price), 2)

    async def _guard(self) -> None:
        if self.budget_left() <= 0:
            if self.halted != "budget_used":
                self.running = False
                self.halted = ""
                self.halt("budget_used", f"amount {self.limits.capital_usdt}")
                await self._close_all()
                await self._sync()
            return
        if not risk.daily_loss_breached(self.limits, self.realized_today(), self.unrealized):
            return
        if self.halted != "daily_loss_limit":
            self.running = False
            self.halted = ""
            self.halt("daily_loss_limit", f"limit {self.limits.max_daily_loss_usdt}")
            await self._close_all()
            await self._sync()

    async def _entries(self) -> None:
        open_trades = self.store.open_trades()
        used = {trade["symbol"] for trade in open_trades}
        margin = sum(trade["margin"] for trade in open_trades)
        for symbol in self.limits.symbols:
            if symbol in used:
                continue
            bars = await self.exchange.bars(symbol, self.settings.interval, HISTORY_BARS)
            if not bars or self.store.get(f"bar:{symbol}") == bars[-1][0]:
                continue
            side = strategy.signal([close for _, close in bars])
            self.store.put(f"bar:{symbol}", bars[-1][0])
            if side is None:
                continue
            margin += await self._open(symbol, side, margin)
            if self.halted:
                return

    async def _open(self, symbol: str, side: str, margin: float) -> float:
        filters = await self.exchange.filters(symbol)
        price = await self.exchange.price(symbol)
        size = risk.size_position(self.limits, price, margin, filters, self.trading_budget())
        if not size.qty:
            self.log("warning", "trade_skipped", f"{symbol} {size.reason}")
            return 0.0
        await self.exchange.prepare(symbol, self.limits.leverage)
        opened_ms = self.now_ms()
        fill = await self.exchange.open_market(symbol, side, size.qty, self._client_id())
        used_margin = fill.qty * fill.price / self.limits.leverage
        stop, target = risk.protection_prices(side, fill.price, self.limits, filters.tick_size)
        trade_id = self.store.open_trade(
            symbol=symbol,
            side=side,
            qty=fill.qty,
            entry_price=fill.price,
            stop_price=stop,
            target_price=target,
            margin=used_margin,
            opened_ms=opened_ms,
        )
        self.log("info", "trade_opened", f"{side} {symbol} {fill.qty} @ {fill.price}")
        try:
            await self.exchange.protect(symbol, side, stop, target)
        except Exception as error:  # noqa: BLE001 - an unprotected position must not stay open
            self.halt("protection_failed", str(error)[:300])
            signed = fill.qty if side == "BUY" else -fill.qty
            await self.exchange.close_market(symbol, signed, self._client_id())
            await self._sync()
            return 0.0
        self.store.set_protection(trade_id, stop, target)
        return used_margin

    def status(self) -> dict[str, Any]:
        return {
            "mode": self.settings.mode,
            "strategy": strategy.NAME,
            "strategy_proven": False,
            "running": self.running,
            "halted": self.halted,
            "balance": self.balance,
            "unrealized": self.unrealized,
            "realized_today": self.realized_today(),
            "positions": self.live,
            "open_trades": self.store.open_trades(),
            "limits": self.limits.model_dump(),
            "budget_left": self.budget_left(),
            "max_order_usdt": risk.max_order_usdt(self.limits, self.trading_budget()),
            "minimums": self.minimums,
            "last_tick_ms": self.last_tick_ms,
            "last_error": self.last_error,
        }

    async def run_forever(self) -> None:
        while True:
            await self.tick()
            await asyncio.sleep(self.settings.tick_seconds)
