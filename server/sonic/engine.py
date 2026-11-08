"""Trading loop. A halt is latched: it stays until the owner clears it from the app."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable
from typing import Any

from . import awake, risk, strategy
from .config import Settings
from .exchange import Exchange
from .risk import Limits
from .store import Store

MAX_FAILURES = 3
DAY_MS = 86_400_000
MARKETS_TTL_MS = 10 * 60 * 1000


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
        self.markets_cache: tuple[int, list[dict[str, Any]]] | None = None
        self.limits = Limits(**store.get("limits", {}))
        self.halted: str = store.get("halted", "")
        # Never resume trading by itself after a restart.
        self.running = False
        self.keep_awake: bool = bool(store.get("keep_awake", True))

    # ---------- small helpers

    def now_ms(self) -> int:
        return int(self.clock() * 1000)

    def log(self, level: str, code: str, detail: str = "") -> None:
        self.store.add_event(self.now_ms(), level, code, detail)

    def day_start_ms(self) -> int:
        now = self.now_ms()
        return now - now % DAY_MS

    def last_closed_bar(self) -> int:
        """Open time of the newest fully closed 4-hour bar."""
        now = self.now_ms()
        return now - now % strategy.BAR_MS - strategy.BAR_MS

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

    def _client_id(self) -> str:
        return f"sb{uuid.uuid4().hex[:24]}"

    # ---------- owner actions

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
            if self.halted == "strategy_drift":
                # The owner looked at it; judge only trades from now on.
                self.store.put("drift_reset_ms", self.now_ms())
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

    async def set_keep_awake(self, on: bool) -> None:
        async with self.lock:
            self.keep_awake = on
            self.store.put("keep_awake", on)
            awake.set_keep_awake(on)
            self.log("info", "keep_awake_on" if on else "keep_awake_off")

    async def panic(self) -> None:
        """Stop trading and close every open position."""
        async with self.lock:
            self.halt("panic_button")
            await self._close_all()
            await self._sync()

    async def _close_all(self) -> None:
        for symbol, position in (await self.exchange.positions()).items():
            await self.exchange.close_market(symbol, position.qty, self._client_id())

    # ---------- loop

    async def tick(self) -> None:
        async with self.lock:
            try:
                await self._sync()
                await self._follow_scaling()
                await self._guard()
                bar = self.last_closed_bar()
                if bar > int(self.store.get("last_bar_ms", 0)):
                    await self._on_bar()
                    self.store.put("last_bar_ms", bar)
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

    async def _follow_scaling(self) -> None:
        """When the half take-profit has filled, move the rest's stop to the entry."""
        for trade in self.store.open_trades():
            live = self.live.get(trade["symbol"])
            if trade["scaled"] or not trade["target_price"] or live is None:
                continue
            if abs(live["qty"]) < trade["qty"] - 1e-12:
                await self.exchange.move_stop(trade["symbol"], trade["side"], trade["entry_price"])
                self.store.mark_scaled(trade["id"], trade["entry_price"])
                self.log("info", "trade_scaled", trade["symbol"])

    async def _guard(self) -> None:
        if self.budget_left() <= 0:
            if self.halted != "budget_used":
                self.running = False
                self.halted = ""
                self.halt("budget_used", f"amount {self.limits.capital_usdt}")
                await self._close_all()
                await self._sync()
            return
        if risk.daily_loss_breached(self.limits, self.realized_today(), self.unrealized):
            if self.halted != "daily_loss_limit":
                self.running = False
                self.halted = ""
                self.halt("daily_loss_limit", f"limit {self.limits.max_daily_loss_usdt}")
                await self._close_all()
                await self._sync()
            return
        recent = self.recent_r()
        if (
            len(recent) >= strategy.DRIFT_WINDOW
            and sum(recent) / len(recent) < strategy.DRIFT_FLOOR
            and not self.halted
        ):
            # Open trades keep their stops; only new entries stop.
            self.halt("strategy_drift", f"avg R {sum(recent) / len(recent):+.2f}")

    def recent_r(self) -> list[float]:
        since = int(self.store.get("drift_reset_ms", 0))
        rows = self.store.db.execute(
            "SELECT r FROM trades WHERE closed_ms IS NOT NULL AND r IS NOT NULL "
            "AND closed_ms >= ? ORDER BY closed_ms DESC, id DESC LIMIT ?",
            (since, strategy.DRIFT_WINDOW),
        )
        return [float(row["r"]) for row in rows]

    async def _bars(self, symbol: str) -> list[strategy.Bar]:
        return await self.exchange.bars(symbol, strategy.INTERVAL, strategy.HISTORY_BARS)

    async def _on_bar(self) -> None:
        """Runs once per closed 4-hour bar: exits first, then entries."""
        now = self.now_ms()
        for trade in self.store.open_trades():
            bars = await self._bars(trade["symbol"])
            held_bars = (now - trade["opened_ms"]) // strategy.BAR_MS
            reason = ""
            if strategy.exit_due(bars, trade["side"]):
                reason = "trade_exit_signal"
            elif held_bars >= strategy.MAX_HOLD_BARS:
                reason = "trade_exit_time"
            live = self.live.get(trade["symbol"])
            if reason and live is not None:
                await self.exchange.close_market(trade["symbol"], live["qty"], self._client_id())
                self.log("info", reason, trade["symbol"])
        await self._sync()
        if not self.running or self.halted:
            return
        btc_up = strategy.btc_uptrend(await self._bars("BTCUSDT"))
        open_trades = self.store.open_trades()
        used = {trade["symbol"] for trade in open_trades}
        margin = sum(trade["margin"] for trade in open_trades)
        for symbol in self.limits.symbols:
            if len(used) >= self.limits.max_open:
                break
            if symbol in used:
                continue
            bars = await self._bars(symbol)
            side = strategy.entry_signal(bars, btc_up)
            if side is None or (side == "SELL" and not self.limits.allow_shorts):
                continue
            added = await self._open(symbol, side, strategy.stop_distance(bars), margin)
            if added:
                used.add(symbol)
                margin += added
            if self.halted:
                return

    async def _open(self, symbol: str, side: str, stop_dist: float, margin: float) -> float:
        filters = await self.exchange.filters(symbol)
        price = await self.exchange.price(symbol)
        size = risk.size_position(
            self.limits, price, stop_dist, margin, filters, self.trading_budget()
        )
        if not size.qty:
            self.log("warning", "trade_skipped", f"{symbol} {size.reason}")
            return 0.0
        await self.exchange.prepare(symbol, self.limits.leverage)
        opened_ms = self.now_ms()
        fill = await self.exchange.open_market(symbol, side, size.qty, self._client_id())
        used_margin = fill.qty * fill.price / self.limits.leverage
        stop = risk.stop_price(side, fill.price, stop_dist, filters.tick_size)
        direction = 1 if side == "BUY" else -1
        half = risk.floor_to(fill.qty / 2, filters.step_size)
        target = None
        if half >= filters.min_qty:
            target = risk.floor_to(
                fill.price + direction * strategy.HALF_AT_R * stop_dist, filters.tick_size
            )
        trade_id = self.store.open_trade(
            symbol=symbol,
            side=side,
            qty=fill.qty,
            entry_price=fill.price,
            stop_price=stop,
            target_price=target or 0.0,
            margin=used_margin,
            opened_ms=opened_ms,
            risk_usdt=fill.qty * abs(fill.price - stop),
        )
        self.log("info", "trade_opened", f"{side} {symbol} {fill.qty} @ {fill.price}")
        try:
            await self.exchange.protect(symbol, side, stop, target, half if target else 0.0)
        except Exception as error:  # noqa: BLE001 - an unprotected position must not stay open
            self.halt("protection_failed", str(error)[:300])
            signed = fill.qty if side == "BUY" else -fill.qty
            await self.exchange.close_market(symbol, signed, self._client_id())
            await self._sync()
            return 0.0
        self.store.set_protection(trade_id, stop, target or 0.0)
        return used_margin

    # ---------- information for the app

    async def markets(self) -> list[dict[str, Any]]:
        """Each coin with its smallest order, today's stop distance, and the smallest amount
        that can trade it under the small-account rule. Cached for ten minutes."""
        now = self.now_ms()
        if self.markets_cache and now - self.markets_cache[0] < MARKETS_TTL_MS:
            return self.markets_cache[1]
        prices = await self.exchange.prices()
        all_bars = await asyncio.gather(*(self._bars(s) for s in risk.ALLOWED_SYMBOLS))
        result = []
        for symbol, bars in zip(risk.ALLOWED_SYMBOLS, all_bars, strict=True):
            filters = await self.exchange.filters(symbol)
            price = prices[symbol]
            minimum = round(filters.min_order(price), 2)
            stop_pct = strategy.stop_distance(bars) / price if bars else 0.0
            result.append({
                "symbol": symbol,
                "price": price,
                "min_order_usdt": minimum,
                "stop_pct": round(stop_pct, 4),
                "min_amount_usdt": round(
                    risk.min_amount_for(
                        minimum, stop_pct, self.limits.leverage, self.limits.risk_per_trade_pct
                    ),
                    2,
                ),
            })
        self.markets_cache = (now, result)
        return result

    async def unaffordable(self, limits: Limits) -> list[str]:
        """Chosen coins whose smallest trade needs more than the new amount."""
        need = {item["symbol"]: item for item in await self.markets()}
        return [
            symbol
            for symbol in limits.symbols
            if risk.min_amount_for(
                need[symbol]["min_order_usdt"],
                need[symbol]["stop_pct"],
                limits.leverage,
                limits.risk_per_trade_pct,
            )
            > limits.capital_usdt
        ]

    def insights(self) -> dict[str, Any]:
        """What the bot has learned from its own closed trades."""
        recent = self.recent_r()
        coins = self.store.coin_stats()
        notes = []
        for coin in coins:
            if coin["trades"] >= 10 and (coin["avg_r"] or 0) < 0:
                notes.append({"code": "coin_weak", "symbol": coin["symbol"],
                              "trades": coin["trades"], "avg_r": coin["avg_r"]})
        return {
            "expected_avg_r": strategy.EXPECTED_AVG_R,
            "expected_win_rate": strategy.EXPECTED_WIN_RATE,
            "drift_floor": strategy.DRIFT_FLOOR,
            "window": strategy.DRIFT_WINDOW,
            "recent_trades": len(recent),
            "recent_avg_r": sum(recent) / len(recent) if recent else None,
            "recent_win_rate": sum(r > 0 for r in recent) / len(recent) if recent else None,
            "coins": coins,
            "notes": notes,
        }

    def status(self) -> dict[str, Any]:
        cached = {item["symbol"]: item for item in (self.markets_cache or (0, []))[1]}
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
            "coin_min_amount": {
                symbol: cached[symbol]["min_amount_usdt"]
                for symbol in self.limits.symbols
                if symbol in cached
            },
            "next_check_ms": self.last_closed_bar() + 2 * strategy.BAR_MS,
            "keep_awake": self.keep_awake,
            "keep_awake_supported": awake.supported(),
            "last_tick_ms": self.last_tick_ms,
            "last_error": self.last_error,
        }

    async def run_forever(self) -> None:
        awake.set_keep_awake(self.keep_awake)
        while True:
            await self.tick()
            await asyncio.sleep(self.settings.tick_seconds)
