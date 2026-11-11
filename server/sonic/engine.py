"""Trading loop. A halt is latched: it stays until the owner clears it from the app."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import account, awake, capital, chart, journal, leader, risk, strategy
from .config import Settings
from .exchange import Exchange
from .risk import Limits
from .store import Store

# Halt only when the market feed has failed for this long without a break. Short outages
# (Wi-Fi drops, waking from sleep) should not stop the bot; open trades keep their stops.
FAILURE_HALT_MS = 30 * 60 * 1000
DAY_MS = 86_400_000
MARKETS_TTL_MS = 10 * 60 * 1000
GOAL_WIN_RATE = (0.80, 0.90)  # the owner's goal for a month of trades
ACCOUNT_SYNC_MS = 6 * 3600 * 1000  # read the owner's futures history this often


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
        # Learning from traders the owner added: separate tables, paper only (leader.py).
        self.lab = leader.Lab(store.db, self.now_ms)
        # Money for approved strategies: approve, allocate and activate are separate steps.
        self.capital = capital.Capital(store.db, self.lab, self.now_ms)
        self.strategy_clients: dict[str, Any] = {}
        self.sync_task: asyncio.Task[None] | None = None
        self.sync_failed_ms = 0
        self.failures = 0
        self.failing_since_ms: int | None = None
        self.last_tick_ms = 0
        self.last_error = ""
        self.unrealized = 0.0
        self.balance = 0.0
        self.live: dict[str, Any] = {}
        self.markets_cache: tuple[int, list[dict[str, Any]]] | None = None
        self.limits = Limits(**store.get("limits", {}))
        self.halted: str = store.get("halted", "")
        # Never resume trading by itself after a restart, unless the owner switched on
        # "resume after restart" (for a cloud server that may reboot during a month-long run)
        # and the bot was running, not halted, when the server went down.
        self.auto_resume: bool = bool(store.get("auto_resume", False))
        self.running = bool(
            self.auto_resume and store.get("was_running", False) and not self.halted
        )
        self.resumed = self.running
        self.keep_awake: bool = bool(store.get("keep_awake", True))
        # Why the bot itself is closing a trade, read when the close is seen in _sync.
        self.pending_exit: dict[str, str] = {}
        self.journal_dir: Path | None = (
            Path(settings.db_path).resolve().parent / "journal"
            if settings.db_path != ":memory:"
            else None
        )

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
        self.store.put("was_running", False)
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
            self.store.put("was_running", True)
            last = self.store.get("last_scan", {}) or {}
            if last.get("market") == "not_running":
                # The bar that closed while the bot was off was never scanned for entries:
                # scan it on the next tick instead of waiting up to 4 hours.
                self.store.put("last_bar_ms", self.last_closed_bar() - 1)
            self.log("info", "started")

    async def stop(self) -> None:
        async with self.lock:
            self.running = False
            self.store.put("was_running", False)
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
            self.failing_since_ms = None
            self.store.put("halted", "")
            self.log("info", "halt_cleared")

    async def set_limits(self, limits: Limits) -> None:
        async with self.lock:
            if self.running:
                raise ValueError("stop_first")
            new_amount = limits.capital_usdt != self.limits.capital_usdt
            self.limits = limits
            self.store.put("limits", limits.model_dump())
            # Only a new amount starts a new budget; re-saving other limits keeps past losses.
            if new_amount or not self.store.get("budget_start_ms"):
                self.store.put("budget_start_ms", self.now_ms())
                self.log("info", "budget_restarted", f"amount {limits.capital_usdt}")
            self.log("info", "limits_changed", limits.model_dump_json())

    async def set_keep_awake(self, on: bool) -> None:
        async with self.lock:
            self.keep_awake = on
            self.store.put("keep_awake", on)
            awake.set_keep_awake(on)
            self.log("info", "keep_awake_on" if on else "keep_awake_off")

    async def set_auto_resume(self, on: bool) -> None:
        async with self.lock:
            self.auto_resume = on
            self.store.put("auto_resume", on)
            self.log("info", "auto_resume_on" if on else "auto_resume_off")

    async def panic(self) -> None:
        """Stop trading and close every open position."""
        async with self.lock:
            self.halt("panic_button")
            await self._close_all("panic_button")
            await self._sync()

    async def _close_all(self, reason: str) -> None:
        for symbol, position in (await self.exchange.positions()).items():
            self.pending_exit[symbol] = reason
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
                    await self._lab_tick()
                if self.failing_since_ms is not None:
                    minutes = (self.now_ms() - self.failing_since_ms) // 60000
                    self.log("info", "connection_restored", f"after {minutes} min")
                self.failures = 0
                self.failing_since_ms = None
                self.last_error = ""
            except Exception as error:  # noqa: BLE001 - every failure must reach the halt counter
                self.failures += 1
                self.last_error = str(error)[:300]
                if self.failing_since_ms is None:
                    self.failing_since_ms = self.now_ms()
                    self.log("error", "tick_failed", self.last_error)
                if self.now_ms() - self.failing_since_ms >= FAILURE_HALT_MS:
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
                reason = self.pending_exit.pop(symbol, "") or (
                    "breakeven" if trade["scaled"] else "stop_loss"
                )
                self.store.close_trade(
                    trade["id"], self.now_ms(), result.exit_price, result.pnl, result.fees, reason
                )
                self.log("info", "trade_closed", f"{symbol} {result.pnl:+.2f}")
                if self.journal_dir is not None:
                    journal.write_trade(self.journal_dir, self.store.trade(trade["id"]))
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
                await self._close_all("budget_used")
                await self._sync()
            return
        if risk.daily_loss_breached(self.limits, self.realized_today(), self.unrealized):
            if self.halted != "daily_loss_limit":
                self.running = False
                self.halted = ""
                self.halt("daily_loss_limit", f"limit {self.limits.max_daily_loss_usdt}")
                await self._close_all("daily_loss_limit")
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

    async def _lab_tick(self) -> None:
        """Paper-trade the learned models and refresh the owner's synced trades. Never allowed
        to disturb Sonic: any failure is logged and ignored."""
        try:
            await self.lab.paper_tick(
                lambda symbol: self.exchange.bars(symbol, strategy.INTERVAL, 600))
            made = (self.lab.state("coins") or {}).get("made_ms") or 0
            if self.lab.strategies() and self.now_ms() - made >= DAY_MS:
                # new prices and paper results: compare the strategies per coin again daily
                await self.lab.compare_coins(self.exchange.bars_until)
        except Exception as error:  # noqa: BLE001 - the lab must never stop the trading loop
            self.log("warning", "lab_failed", str(error)[:200])
        try:
            await self.capital.tick(
                self.strategy_client,
                lambda symbol: self.exchange.bars(symbol, strategy.INTERVAL, 600), self.log)
        except Exception as error:  # noqa: BLE001 - logged loudly, Sonic carries on
            self.log("critical", "strategies_failed", str(error)[:200])

    async def strategy_client(self, mode: str) -> Any:
        """Signed client for approved strategies: Binance Demo, or real futures only when the
        server switch allows it. Uses the owner's trading key (futures only)."""
        saved = self.store.get("trade_key")
        if not saved:
            raise ValueError("no_trade_key")
        if mode not in self.strategy_clients:
            client = capital.demo_or_real_client(mode, saved["key"], saved["secret"],
                                                 capital.live_allowed())
            await client.sync_time()
            self.strategy_clients[mode] = client
        return self.strategy_clients[mode]

    async def sync_account(self, trader_id: str | None) -> dict[str, Any]:
        """Read the owner's own futures fills with the read-only key and store them as closed
        positions: in the trader the owner chose, or else in the "my-account" bucket."""
        saved = self.store.get("account_key")
        if not saved:
            raise ValueError("no_account_key")
        target = trader_id or self.store.get("account_target") or leader.ACCOUNT_ID
        if target == leader.ACCOUNT_ID and self.lab.trader(target) is None:
            self.lab.add(leader.ACCOUNT_ID, "صفقات حسابي", kind="account")
        if self.lab.trader(target) is None:
            raise KeyError("unknown_trader")
        reader = account.AccountReader(saved["key"], saved["secret"], self.now_ms)
        since = int(self.store.get("account_synced_ms", 0)) - 7 * DAY_MS
        fills = await reader.fills(max(since, 0))
        paired = leader.pair_fills({"fills": fills, "errors": []}, has_pnl=True)
        added = self.lab.import_trades(target, paired["trades"], "account")
        self.store.put("account_synced_ms", self.now_ms())
        self.store.put("account_target", target)
        self.log("info", "account_synced", f"{added} new trades to {target}")
        return {"trader_id": target, "fills": len(fills), "added": added,
                "still_open": paired["skipped"]}

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
                self.pending_exit[trade["symbol"]] = reason
                await self.exchange.close_market(trade["symbol"], live["qty"], self._client_id())
                self.log("info", reason, trade["symbol"])
        await self._sync()
        if not self.running or self.halted:
            self._record_scan("not_running", 0, 0)
            return
        btc_up = strategy.btc_uptrend(await self._bars("BTCUSDT"))
        signals = opened = 0
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
            signals += 1
            closes = [bar.c for bar in bars]
            plan = {
                "strategy": strategy.NAME,
                "mode": self.settings.mode,
                "fast": float(strategy.ema(closes, strategy.FAST)[-1]),
                "slow": float(strategy.ema(closes, strategy.SLOW)[-1]),
                "btc_up": bool(btc_up),
                "atr": float(strategy.atr(bars)),
                "stop_pct": float(strategy.stop_distance(bars) / closes[-1]),
                "half_at_r": float(strategy.HALF_AT_R),
                "risk_pct": float(self.limits.risk_per_trade_pct),
            }
            added = await self._open(symbol, side, strategy.stop_distance(bars), margin, plan)
            if added:
                used.add(symbol)
                margin += added
                opened += 1
            if self.halted:
                break
        self._record_scan("btc_up" if btc_up else "btc_down", signals, opened)

    def _record_scan(self, market: str, signals: int, opened: int) -> None:
        self.store.put("last_scan", {
            "at_ms": self.now_ms(),
            "market": market,
            "coins": len(self.limits.symbols),
            "signals": signals,
            "opened": opened,
        })

    async def _open(
        self,
        symbol: str,
        side: str,
        stop_dist: float,
        margin: float,
        plan: dict[str, Any] | None = None,
    ) -> float:
        # Serialise the plan before any order: a failure here must never leave a position
        # open that the bot does not know about.
        plan_json = json.dumps(plan or {}, default=str)
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
            plan=plan_json,
        )
        self.log("info", "trade_opened", f"{side} {symbol} {fill.qty} @ {fill.price}")
        try:
            await self.exchange.protect(symbol, side, stop, target, half if target else 0.0)
        except Exception as error:  # noqa: BLE001 - an unprotected position must not stay open
            self.halt("protection_failed", str(error)[:300])
            self.pending_exit[symbol] = "protection_failed"
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

    async def chart(
        self, symbol: str, source: str, trade_id: int | None, shown: int
    ) -> dict[str, Any]:
        """Candles, EMAs and trades for the app; centred on a trade when one is chosen."""
        if source == "replay":
            trades = [t for t in chart.replay_trades()["trades"] if t["symbol"] == symbol]
        else:
            trades = [t for t in self.store.trades(500) if t["symbol"] == symbol]
        focus = next((t for t in trades if t["id"] == trade_id), None) if trade_id else None
        total = shown + chart.WARMUP
        if focus is None and source == "live":
            bars = await self.exchange.bars(symbol, strategy.INTERVAL, total)
        else:
            if focus is not None:
                end = (focus.get("closed_ms") or self.now_ms()) + chart.PAD * strategy.BAR_MS
                hold = ((focus.get("closed_ms") or self.now_ms()) - focus["opened_ms"])
                shown = max(shown, int(hold // strategy.BAR_MS) + 2 * chart.PAD)
                total = shown + chart.WARMUP
            else:
                end = max((t.get("closed_ms") or 0 for t in trades), default=self.now_ms())
            end = min(end, self.now_ms())
            bars = await self.exchange.bars_until(symbol, strategy.INTERVAL, end, total)
        data = chart.build(bars, shown, trades)
        data.update({"symbol": symbol, "source": source, "focus": trade_id})
        return data

    async def simulate(
        self, symbol: str, source: str, months: int, end_ms: int | None
    ) -> dict[str, Any]:
        """A period of candles and trades for the app's simulator to replay."""
        shown = months * 30 * 6
        total = shown + chart.LEAD + chart.WARMUP
        now = self.now_ms()
        forming = None
        if source == "replay":
            saved = chart.replay_trades()
            trades = [t for t in saved["trades"] if t["symbol"] == symbol]
            end = end_ms or max((t.get("closed_ms") or 0 for t in trades), default=now)
            bars = await self.exchange.bars_until(symbol, strategy.INTERVAL, min(end, now), total)
            bars = [b for b in bars if b.t < min(end, now) and b.t + strategy.BAR_MS <= now]
        else:
            trades = [t for t in self.store.trades(500) if t["symbol"] == symbol]
            bars = await self.exchange.bars_until(symbol, strategy.INTERVAL, now, total + 1)
            if bars and bars[-1].t + strategy.BAR_MS > now:
                forming, bars = bars[-1], bars[:-1]
                price = (await self.exchange.prices()).get(symbol)
                if price:  # the newest price, so the forming candle moves every refresh
                    forming = strategy.Bar(
                        forming.t, forming.o, max(forming.h, price), min(forming.low, price), price
                    )
        data = chart.simulation(bars, shown, trades, forming)
        data["lead"] = max(0, len(data["bars"]) - shown)
        data.update({"symbol": symbol, "source": source, "now_ms": now,
                     "period": chart.replay_trades().get("period") if source == "replay" else None})
        return data

    def replay_list(self, symbol: str | None) -> dict[str, Any]:
        saved = chart.replay_trades()
        trades = [chart.trade_view(t) for t in saved["trades"]
                  if symbol is None or t["symbol"] == symbol]
        return {"period": saved.get("period"), "trades": trades[-200:]}

    def month_report(self, month: str | None = None) -> dict[str, Any]:
        """Closed trades of one UTC month against the owner's goal of 80-90% winning trades."""
        now = datetime.fromtimestamp(self.now_ms() / 1000, UTC)
        month = month or now.strftime("%Y-%m")
        start = datetime.strptime(month, "%Y-%m").replace(tzinfo=UTC)
        end = (start.replace(day=28) + __import__("datetime").timedelta(days=4)).replace(day=1)
        trades = self.store.closed_between(
            int(start.timestamp() * 1000), int(end.timestamp() * 1000)
        )
        wins = sum(1 for t in trades if (t["pnl"] or 0) > 0)
        win_rate = wins / len(trades) if trades else None
        low, high = GOAL_WIN_RATE
        verdict = (
            "no_trades" if win_rate is None
            else "below_goal" if win_rate < low
            else "in_goal" if win_rate <= high
            else "above_goal"
        )
        by_reason: dict[str, int] = {}
        for t in trades:
            key = t["exit_reason"] or "unknown"
            by_reason[key] = by_reason.get(key, 0) + 1
        return {
            "month": month,
            "trades": len(trades),
            "wins": wins,
            "win_rate": win_rate,
            "goal": {"min": low, "max": high},
            "verdict": verdict,
            "pnl": round(sum(t["pnl"] or 0 for t in trades), 2),
            "avg_r": (sum(t["r"] or 0 for t in trades) / len(trades)) if trades else None,
            "by_exit": by_reason,
            "strategy": strategy.NAME,
            "mode": self.settings.mode,
        }

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
            "last_scan": self.store.get("last_scan"),
            "keep_awake": self.keep_awake,
            "auto_resume": self.auto_resume,
            "keep_awake_supported": awake.supported(),
            "last_tick_ms": self.last_tick_ms,
            "last_error": self.last_error,
            "failing_since_ms": self.failing_since_ms,
        }

    async def run_forever(self) -> None:
        awake.set_keep_awake(self.keep_awake)
        if self.resumed:
            self.resumed = False
            self.log("info", "resumed_after_restart")
        while True:
            await self.tick()
            self.maybe_sync_account()
            await asyncio.sleep(self.settings.tick_seconds)

    def maybe_sync_account(self) -> None:
        """Every 6 hours, read the owner's futures history with the read-only key, in the
        background so Sonic's own loop never waits for it. After a failure, retry in 30 min."""
        if not self.store.get("account_key") or (self.sync_task and not self.sync_task.done()):
            return
        now = self.now_ms()
        due = now - int(self.store.get("account_synced_ms", 0)) >= ACCOUNT_SYNC_MS
        if due and now - self.sync_failed_ms >= 30 * 60 * 1000:
            self.sync_task = asyncio.create_task(self._sync_in_background())

    async def _sync_in_background(self) -> None:
        try:
            await self.sync_account(None)
        except Exception as error:  # noqa: BLE001 - logged without any key, retried later
            self.sync_failed_ms = self.now_ms()
            self.log("warning", "account_sync_failed", type(error).__name__ + ": "
                     + str(error)[:120])
