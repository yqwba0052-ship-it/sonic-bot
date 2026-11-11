"""Money for approved trader strategies, in separate steps (owner's rule, 2026-11-11):

1. approve (the app's check mark): only a strategy that passed its out-of-sample test on at least
   one coin. No money, no trading.
2. allocate: an amount in USDT, checked against the futures wallet read through the READ-ONLY key
   (account.py), inside the limits below. Still no trading.
3. activate: a separate step that needs all of: a successful paper test of this strategy, the
   owner's own typed approval, a key with futures trading only (no withdrawal, no transfer),
   loss limits, and the emergency stop. The target is Binance Demo; real money also needs the
   server switch SONIC_LIVE_TRADING=yes, which only the owner sets in the server's .env.

Reservations are Sonic's own bookkeeping. Binance does not isolate money per strategy inside one
futures account, so nothing is "held" at Binance: Sonic keeps each strategy inside its amount
and keeps all amounts together inside the available balance. A strategy trades only the coins of
its trader. A coin is never shared by two live strategies, and a coin that already has a
position Sonic did not open (a manual trade) is skipped. Copy-trading money sits in Binance's
separate copy-trading account and is never touched. Sonic's own strategy is not affected.
The order client was written against Binance's documentation and tested with simulated replies
only; run on Binance Demo first.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from . import leader, risk
from .exchange import DEMO_URL, BinanceDemo, PublicMarket
from .risk import Filters, floor_to
from .strategy import BAR_MS, Bar

REAL_URL = "https://fapi.binance.com"
MAX_SHARE_ONE = 0.5  # one strategy: at most half of the futures wallet
MAX_SHARE_ALL = 0.8  # all strategies together: at most 80% of it
RISK = 0.01  # each trade risks 1% of the strategy's own amount
PAPER_TRADES = 20
LIVE_PHRASE = "أوافق على تداول هذه الاستراتيجية بأموال حقيقية"
FEE = 0.0005

SCHEMA = """
CREATE TABLE IF NOT EXISTS allocations (
    model_id INTEGER PRIMARY KEY,
    trader_id TEXT NOT NULL,
    symbols TEXT NOT NULL,
    leverage REAL NOT NULL,
    approved_ms INTEGER NOT NULL,
    amount REAL NOT NULL DEFAULT 0,
    realized REAL NOT NULL DEFAULT 0,
    max_loss REAL,
    daily_loss REAL,
    state TEXT NOT NULL,
    mode TEXT,
    live_approved_ms INTEGER,
    close_requested INTEGER NOT NULL DEFAULT 0,
    changed_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS live_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model_id INTEGER NOT NULL,
    mode TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    qty REAL NOT NULL,
    entry_price REAL NOT NULL,
    stop_price REAL NOT NULL,
    target_price REAL NOT NULL,
    margin REAL NOT NULL,
    opened_ms INTEGER NOT NULL,
    until_ms INTEGER NOT NULL,
    closed_ms INTEGER,
    exit_price REAL,
    pnl REAL,
    fees REAL,
    reason TEXT
);
"""

FORBIDDEN_FOR_TRADING_KEY = ("enableWithdrawals", "enableInternalTransfer",
                             "permitsUniversalTransfer", "enableMargin",
                             "enableSpotAndMarginTrading", "enableVanillaOptions",
                             "enablePortfolioMarginTrading")


def trade_key_problems(permissions: dict[str, Any]) -> list[str]:
    """A trading key must read and trade futures, and nothing else (no withdrawal/transfer)."""
    problems = [name + "_off" for name in ("enableReading", "enableFutures")
                if permissions.get(name) is not True]
    problems += [name for name in FORBIDDEN_FOR_TRADING_KEY if permissions.get(name) is True]
    return problems


class LiveClient(BinanceDemo):
    """The demo client's order code, pointed at real Binance futures. Refuses to exist unless the
    server switch allows real trading."""

    def __init__(self, client: httpx.AsyncClient, key: str, secret: str, allowed: bool) -> None:
        if not allowed:
            raise PermissionError("live_not_allowed_on_server")
        if str(client.base_url).rstrip("/") != REAL_URL:
            raise ValueError("LiveClient may only use the real futures URL")
        self.client, self.key, self.secret = client, key, secret
        self.market = PublicMarket(client)
        self.offset_ms = 0


def live_allowed() -> bool:
    """Only the owner sets SONIC_LIVE_TRADING=yes, in server/.env or the environment."""
    from pathlib import Path

    from .config import read_env_file

    values = {**read_env_file(Path(__file__).resolve().parent.parent / ".env"), **os.environ}
    return values.get("SONIC_LIVE_TRADING", "").strip().lower() == "yes"


class Capital:
    def __init__(self, db: sqlite3.Connection, lab: leader.Lab, now_ms: Callable[[], int],
                 allowed: Callable[[], bool] = live_allowed) -> None:
        self.db, self.lab, self.now_ms, self.allowed = db, lab, now_ms, allowed
        self.db.executescript(SCHEMA)
        self.db.commit()

    # ---------- step 1: approve
    def approve(self, model_id: int) -> dict[str, Any]:
        coins = (self.lab.state("coins") or {}).get("coins") or {}
        passed = {e["model_id"] for c in coins.values() for e in c.get("ranked", [])}
        if model_id not in passed:
            raise ValueError("not_eligible")  # only strategies that passed out of sample
        row = self.db.execute("SELECT trader_id, model FROM trader_models WHERE id = ?",
                              (model_id,)).fetchone()
        if row is None or not row["model"]:
            raise KeyError("unknown_strategy")
        model = json.loads(row["model"])
        if self.row(model_id) is None:
            now = self.now_ms()
            self.db.execute(
                "INSERT INTO allocations (model_id, trader_id, symbols, leverage, approved_ms, "
                "state, changed_ms) VALUES (?, ?, ?, ?, ?, 'approved', ?)",
                (model_id, row["trader_id"], json.dumps(model["symbols"]),
                 float(model.get("leverage", 3)), now, now))
            self.db.commit()
        return self.view(model_id)

    def row(self, model_id: int) -> dict[str, Any] | None:
        found = self.db.execute("SELECT * FROM allocations WHERE model_id = ?",
                                (model_id,)).fetchone()
        return dict(found) if found else None

    def approved_by_coin(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for row in self.db.execute("SELECT model_id, symbols FROM allocations "
                                   "WHERE state != 'stopped' ORDER BY approved_ms"):
            for symbol in json.loads(row["symbols"]):
                out.setdefault(symbol, row["model_id"])
        return out

    # ---------- step 2: allocate
    def reserved_by_others(self, model_id: int) -> float:
        row = self.db.execute(
            "SELECT COALESCE(SUM(amount), 0) AS total FROM allocations WHERE model_id != ? AND "
            "state IN ('allocated', 'live')", (model_id,)).fetchone()
        return float(row["total"])

    def open_trades(self, model_id: int | None = None) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT * FROM live_trades WHERE closed_ms IS NULL AND (? IS NULL OR model_id = ?)",
            (model_id, model_id))
        return [dict(r) for r in rows]

    def used(self, model_id: int) -> float:
        return sum(t["margin"] for t in self.open_trades(model_id))

    def bounds(self, model_id: int, wallet: float, available: float,
               coin_minimums: dict[str, float]) -> dict[str, Any]:
        """The smallest amount that can take one trade on every coin of the strategy, and the
        largest the wallet and the risk limits allow (other strategies' amounts excluded)."""
        if self.row(model_id) is None:
            raise KeyError("not_approved")
        others = self.reserved_by_others(model_id)
        caps = {
            "free_balance": available - others,
            "one_strategy": wallet * MAX_SHARE_ONE,
            "all_strategies": wallet * MAX_SHARE_ALL - others,
        }
        return {
            "min": round(max(coin_minimums.values(), default=0.0), 2),
            "max": round(max(0.0, min(caps.values())), 2),
            "wallet": round(wallet, 2), "available": round(available, 2),
            "reserved_by_others": round(others, 2),
            "caps": {k: round(v, 2) for k, v in caps.items()},
            "per_coin": {k: round(v, 2) for k, v in coin_minimums.items()},
            "limits": {"one_strategy": MAX_SHARE_ONE, "all_strategies": MAX_SHARE_ALL},
        }

    def allocate(self, model_id: int, amount: float, bounds: dict[str, Any]) -> dict[str, Any]:
        row = self.row(model_id)
        if row is None:
            raise KeyError("not_approved")
        if bounds["available"] <= 0:
            raise ValueError("no_balance")
        if not math.isfinite(amount) or amount <= 0:
            raise ValueError("invalid_values")
        if amount > bounds["max"] + 1e-9:
            raise ValueError("insufficient_balance")
        if amount < bounds["min"] - 1e-9:
            raise ValueError("below_minimum")
        if amount < self.used(model_id) - 1e-9:
            raise ValueError("below_used")  # open trades already use more than this
        state = "live" if row["state"] == "live" else "allocated"
        self.db.execute(
            "UPDATE allocations SET amount = ?, state = ?, close_requested = 0, changed_ms = ? "
            "WHERE model_id = ?", (round(amount, 2), state, self.now_ms(), model_id))
        if row["max_loss"] and row["max_loss"] > amount:  # limits never above the amount
            self.db.execute("UPDATE allocations SET max_loss = ?, daily_loss = MIN(daily_loss, ?) "
                            "WHERE model_id = ?", (amount, amount, model_id))
        self.db.commit()
        return self.view(model_id)

    def set_limits(self, model_id: int, max_loss: float, daily_loss: float) -> dict[str, Any]:
        row = self.row(model_id)
        if row is None:
            raise KeyError("not_approved")
        if not (0 < daily_loss <= max_loss <= max(row["amount"], 0)):
            raise ValueError("invalid_limits")
        self.db.execute("UPDATE allocations SET max_loss = ?, daily_loss = ?, changed_ms = ? "
                        "WHERE model_id = ?", (max_loss, daily_loss, self.now_ms(), model_id))
        self.db.commit()
        return self.view(model_id)

    def stop(self, model_id: int, close_open: bool) -> dict[str, Any]:
        """No new trades. close_open=False: open trades keep their stop and target on the
        exchange and end by themselves. close_open=True: they are closed at market."""
        if self.row(model_id) is None:
            raise KeyError("not_approved")
        self.db.execute("UPDATE allocations SET state = 'stopped', close_requested = ?, "
                        "changed_ms = ? WHERE model_id = ?",
                        (int(close_open), self.now_ms(), model_id))
        self.db.commit()
        return self.view(model_id)

    # ---------- step 3: activate
    def paper(self, model_id: int) -> dict[str, Any]:
        options = [self.lab.paper_summary(model_id, table=table, by="model_id")
                   for table in ("trader_paper", "champion_paper")]
        best = max(options, key=lambda p: p["trades"])
        best["passed"] = bool(
            best["trades"] >= PAPER_TRADES and (best["net_pnl"] or 0) > 0
            and (best.get("profit_factor") or 0) >= 1.2 and (best["max_drawdown"] or 1) <= 0.2)
        best["needed"] = PAPER_TRADES
        return best

    def gate(self, model_id: int, trade_key: dict[str, Any] | None, mode: str) -> list[dict]:
        row = self.row(model_id) or {}
        paper = self.paper(model_id)
        items = [
            ("approved", bool(row), "الاستراتيجية معتمدة (✓)"),
            ("allocated", (row.get("amount") or 0) > 0, "مبلغ مخصص لها"),
            ("limits", bool(row.get("max_loss") and row.get("daily_loss")),
             "حد خسارة كلي وحد خسارة يومي"),
            ("paper", paper["passed"],
             f"اختبار وهمي ناجح: {PAPER_TRADES} صفقة على الأقل، ربح بعد الرسوم، معامل ربح 1.2+، "
             f"تراجع 20% أو أقل (الآن {paper['trades']} صفقة)"),
            ("trade_key", bool(trade_key and not trade_key.get("problems")),
             "مفتاح API للعقود الآجلة فقط: بلا سحب وبلا تحويل"),
            ("emergency_stop", True, "زر وقف الطوارئ متاح"),
        ]
        if mode == "real":
            items.append(("server_switch", self.allowed(),
                          "مفتاح التداول الحقيقي مفعّل في إعدادات الخادم (يضعه المالك فقط)"))
        return [{"key": k, "ok": ok, "text": text} for k, ok, text in items]

    def activate(self, model_id: int, mode: str, phrase: str,
                 trade_key: dict[str, Any] | None) -> dict[str, Any]:
        if mode not in ("demo", "real"):
            raise ValueError("invalid_mode")
        if phrase.strip() != LIVE_PHRASE:
            raise ValueError("phrase_mismatch")
        missing = [g["key"] for g in self.gate(model_id, trade_key, mode) if not g["ok"]]
        if missing:
            raise ValueError("gate_not_met:" + ",".join(missing))
        self.db.execute(
            "UPDATE allocations SET state = 'live', mode = ?, live_approved_ms = ?, "
            "close_requested = 0, changed_ms = ? WHERE model_id = ?",
            (mode, self.now_ms(), self.now_ms(), model_id))
        self.db.commit()
        return self.view(model_id)

    # ---------- what the app shows
    def view(self, model_id: int) -> dict[str, Any]:
        row = self.row(model_id)
        if row is None:
            raise KeyError("not_approved")
        trades = self.open_trades(model_id)
        used = sum(t["margin"] for t in trades)
        exposure = sum(t["qty"] * t["entry_price"] for t in trades)
        at_risk = sum(t["qty"] * abs(t["entry_price"] - t["stop_price"])
                      + t["qty"] * (t["entry_price"] + t["stop_price"]) * FEE for t in trades)
        trader = self.lab.trader(row["trader_id"]) or {}
        return {
            **row, "symbols": json.loads(row["symbols"]),
            "trader_name": trader.get("name", row["trader_id"]),
            "used": round(used, 2),
            "available": round(row["amount"] + row["realized"] - used, 2),
            "exposure": round(exposure, 2),
            "possible_loss": round(at_risk, 2),
            "open": trades,
            "paper": self.paper(model_id),
            "phrase": LIVE_PHRASE,
        }

    def views(self) -> list[dict[str, Any]]:
        return [self.view(r["model_id"]) for r in
                self.db.execute("SELECT model_id FROM allocations ORDER BY approved_ms")]

    # ---------- running live (Binance Demo, or real when the server allows it)
    def _close(self, trade: dict[str, Any], price: float, pnl: float, fees: float,
               reason: str) -> None:
        self.db.execute("UPDATE live_trades SET closed_ms = ?, exit_price = ?, pnl = ?, fees = ?, "
                        "reason = ? WHERE id = ?",
                        (self.now_ms(), price, pnl, fees, reason, trade["id"]))
        self.db.execute("UPDATE allocations SET realized = realized + ? WHERE model_id = ?",
                        (pnl, trade["model_id"]))
        self.db.commit()

    def lost_today(self, model_id: int) -> float:
        start = self.now_ms() - self.now_ms() % 86_400_000
        row = self.db.execute("SELECT COALESCE(SUM(pnl), 0) AS total FROM live_trades WHERE "
                              "model_id = ? AND closed_ms >= ?", (model_id, start)).fetchone()
        return -float(row["total"])

    async def tick(self, client_for: Callable[[str], Awaitable[Any]],
                   bars: Callable[[str], Awaitable[list[Bar]]],
                   log: Callable[[str, str, str], None]) -> None:
        """Once per 4h bar: settle trades the exchange closed, close what must close, enforce
        the loss limits, then open new trades for live strategies inside their amounts."""
        rows = [r for r in (self.row(m["model_id"]) for m in self.db.execute(
            "SELECT model_id FROM allocations")) if r]
        work = [r for r in rows if r["state"] == "live" or self.open_trades(r["model_id"])]
        if not work:
            return
        btc = leader.Series(await bars("BTCUSDT"))
        for row in work:
            client = await client_for(row["mode"] or "demo")
            await self._manage(row, client, log)
            row = self.row(row["model_id"]) or row
            if row["state"] == "live":
                await self._enter(row, client, bars, btc, log)

    async def _manage(self, row: dict[str, Any], client: Any,
                      log: Callable[[str, str, str], None]) -> None:
        model_id = row["model_id"]
        positions = await client.positions()
        breached = (row["max_loss"] and row["realized"] <= -row["max_loss"])
        if breached and row["state"] == "live":
            self.stop(model_id, close_open=True)
            log("critical", "strategy_loss_limit", f"model {model_id}")
            row = self.row(model_id) or row
        for trade in self.open_trades(model_id):
            symbol = trade["symbol"]
            position = positions.get(symbol)
            if position is None:  # stop or target filled on the exchange
                result = await client.closed_result(symbol, trade["opened_ms"])
                self._close(trade, result.exit_price, result.pnl, result.fees, "exchange")
                continue
            if row["close_requested"] or self.now_ms() >= trade["until_ms"]:
                await client.close_market(symbol, position.qty, f"sl{uuid.uuid4().hex[:20]}")
                log("info", "strategy_trade_closing", f"{symbol} model {model_id}")

    async def _enter(self, row: dict[str, Any], client: Any,
                     bars: Callable[[str], Awaitable[list[Bar]]], btc: leader.Series,
                     log: Callable[[str, str, str], None]) -> None:
        model_id = row["model_id"]
        if row["daily_loss"] and self.lost_today(model_id) >= row["daily_loss"]:
            return  # daily loss limit: no new trades until tomorrow (UTC)
        model_row = self.db.execute("SELECT model FROM trader_models WHERE id = ?",
                                    (model_id,)).fetchone()
        model = json.loads(model_row["model"])
        positions = await client.positions()
        taken = {t["symbol"] for t in self.open_trades()}
        for symbol in json.loads(row["symbols"]):
            if symbol in taken or symbol in positions:
                continue  # one strategy per coin; never next to a position Sonic did not open
            series = leader.Series(await bars(symbol))
            i = len(series.t) - 1
            side = leader.model_signal(model, series, btc, i) if i > leader.WARMUP else None
            if side is None:
                continue
            fresh = self.row(model_id) or row
            budget = fresh["amount"] + fresh["realized"]
            free = budget - self.used(model_id)
            price, atr = series.c[i], series.atr[i]
            stop_dist = model["stop_atr"] * atr
            leverage = int(max(1, min(5, fresh["leverage"])))
            filters: Filters = await client.filters(symbol)
            qty = floor_to(min(budget * RISK / stop_dist, free * leverage / price),
                           filters.step_size) if stop_dist > 0 and free > 0 else 0.0
            if qty * price < filters.min_order(price) or qty * price / leverage > free:
                log("info", "strategy_trade_skipped", f"{symbol} model {model_id} amount")
                continue
            d = 1 if side == "BUY" else -1
            await client.prepare(symbol, leverage)
            fill = await client.open_market(symbol, side, qty, f"sl{uuid.uuid4().hex[:20]}")
            stop = fill.price - d * stop_dist
            target = fill.price + d * model["target_atr"] * atr
            now = self.now_ms()
            cur = self.db.execute(
                "INSERT INTO live_trades (model_id, mode, symbol, side, qty, entry_price, "
                "stop_price, target_price, margin, opened_ms, until_ms) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (model_id, fresh["mode"] or "demo", symbol, side, fill.qty, fill.price, stop,
                 target, fill.qty * fill.price / leverage, now,
                 now + model["max_hold_bars"] * BAR_MS))
            self.db.commit()
            taken.add(symbol)
            try:
                await client.protect(symbol, side, stop, target, fill.qty)
            except Exception as error:  # noqa: BLE001 - an unprotected trade must not stay open
                await client.close_market(symbol, d * fill.qty, f"sl{uuid.uuid4().hex[:20]}")
                self.stop(model_id, close_open=True)
                log("critical", "strategy_protection_failed", f"{symbol}: {str(error)[:120]}")
                return
            log("info", "strategy_trade_opened",
                f"{symbol} {side} model {model_id} id {cur.lastrowid}")

    async def emergency(self, client_for: Callable[[str], Awaitable[Any]],
                        log: Callable[[str, str, str], None]) -> int:
        """Stop every strategy and close every position they opened, now."""
        closed = 0
        for row in self.db.execute("SELECT model_id FROM allocations").fetchall():
            self.stop(row["model_id"], close_open=True)
        for trade in self.open_trades():
            client = await client_for(trade["mode"])
            position = (await client.positions()).get(trade["symbol"])
            if position is not None:
                await client.close_market(trade["symbol"], position.qty,
                                          f"sl{uuid.uuid4().hex[:20]}")
                closed += 1
        log("critical", "strategies_emergency_stop", f"{closed} positions closed")
        return closed


def demo_or_real_client(mode: str, key: str, secret: str, allowed: bool) -> BinanceDemo:
    if mode == "real":
        return LiveClient(httpx.AsyncClient(base_url=REAL_URL, timeout=20), key, secret, allowed)
    return BinanceDemo(httpx.AsyncClient(base_url=DEMO_URL, timeout=20), key, secret)


async def coin_minimums(market: Any, symbols: list[str], model: dict[str, Any],
                        leverage: int) -> dict[str, float]:
    """Per coin: the smallest strategy amount whose 1% risk can still place Binance's minimum
    order with this strategy's stop distance (risk.min_amount_for)."""
    out = {}
    for symbol in symbols:
        series = leader.Series(await market.bars(symbol, "4h", 400))
        if not series.t or not series.atr[-1]:
            continue
        price = series.c[-1]
        filters = await market.filters(symbol)
        stop_pct = model["stop_atr"] * series.atr[-1] / price
        out[symbol] = risk.min_amount_for(filters.min_order(price), stop_pct, leverage, 1.0)
    return out
