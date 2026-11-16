"""Sonic's own rules inside Jesse (research only, read-only use of the bot's code).

Signals, stop distance, sizing and rounding come from sonic.strategy and sonic.risk unchanged;
only order execution is Jesse's. Jesse runs without fees or slippage: costs() adds both, the
same way for both engines. Each 4h bar is planned once for every coin (planner.plan_bar), the
way Engine._on_bar runs it: all exits first, then entries in limits.symbols order to max_open.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "server"))

from jesse import helpers as jh  # noqa: E402
from jesse.research import backtest  # noqa: E402
from jesse.strategies import Strategy  # noqa: E402
from sonic import strategy as sonic  # noqa: E402
from sonic.risk import Filters, Limits, floor_to, stop_price  # noqa: E402

from .costs import SLIPPAGE, costs  # noqa: E402
from .offline import block_network  # noqa: E402
from .planner import BarPlan, plan_bar  # noqa: E402

EXCHANGE = "Binance Perpetual Futures"
BTC = "BTCUSDT"


def jesse_symbol(symbol: str) -> str:
    return symbol[:-4] + "-" + symbol[-4:]


@dataclass
class Ledger:
    limits: Limits = field(default_factory=Limits)
    filters: dict[str, Filters] = field(default_factory=dict)
    routes: list[str] = field(default_factory=list)
    realized: float = 0.0
    open: dict[str, dict] = field(default_factory=dict)  # symbol -> trade (with "margin")
    trades: list[dict] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)
    open_at_end: list[dict] = field(default_factory=list)
    plan_ms: int = 0
    plan: BarPlan = field(default_factory=BarPlan)
    exit_reasons: dict[str, str] = field(default_factory=dict)
    pending: set[str] = field(default_factory=set)
    plan_mismatch: list[dict] = field(default_factory=list)


LEDGER = Ledger()


def reset_ledger(limits: Limits, filters: dict[str, Filters], routes: list[str]) -> None:
    global LEDGER
    LEDGER = Ledger(limits=limits, filters=filters, routes=list(routes))


def _bars(candles: np.ndarray) -> list[sonic.Bar]:
    return [sonic.Bar(int(r[0]), float(r[1]), float(r[3]), float(r[4]), float(r[2]))
            for r in candles[-sonic.HISTORY_BARS:]]


def _plan_whole_bar(strategy: Strategy, close_ms: int) -> None:
    for symbol in sorted(LEDGER.pending):
        LEDGER.plan_mismatch.append({"at_ms": LEDGER.plan_ms, "symbol": symbol,
                                     "what": "planned_entry_not_taken"})
    LEDGER.pending = set()

    def bars_of(symbol: str) -> list[sonic.Bar]:
        bars = _bars(strategy.get_candles(EXCHANGE, jesse_symbol(symbol), "4h"))
        if bars[-1].t + sonic.BAR_MS != close_ms:
            LEDGER.plan_mismatch.append({"at_ms": close_ms, "symbol": symbol,
                                         "what": "candles_not_aligned"})
        return bars

    btc_up = sonic.btc_uptrend(bars_of(BTC))
    exits, reasons = {}, {}
    for symbol, trade in LEDGER.open.items():
        bars = bars_of(symbol)
        held = (close_ms - trade["opened_ms"]) // sonic.BAR_MS
        if sonic.exit_due(bars, "BUY"):
            reasons[symbol] = "trade_exit_signal"
        elif held >= sonic.MAX_HOLD_BARS:
            reasons[symbol] = "trade_exit_time"
        if symbol in reasons:
            exits[symbol] = bars[-1].c
    candidates = []
    for symbol in LEDGER.limits.symbols:
        if symbol not in LEDGER.routes:
            continue
        bars = bars_of(symbol)
        if sonic.entry_signal(bars, btc_up) == "BUY":
            candidates.append((symbol, bars[-1].c, sonic.stop_distance(bars)))
    plan = plan_bar(LEDGER.limits, LEDGER.filters, LEDGER.realized, LEDGER.open, exits,
                    candidates)
    LEDGER.plan_ms, LEDGER.plan, LEDGER.exit_reasons = close_ms, plan, reasons
    LEDGER.pending = set(plan.entries)
    LEDGER.skipped.extend({"symbol": s, "at_ms": close_ms, "reason": r}
                          for s, r in plan.skipped)


class SonicBridge(Strategy):
    def _symbol(self) -> str:
        return self.symbol.replace("-", "")

    def _close_ms(self) -> int:
        return int(self.candles[-1][0]) + sonic.BAR_MS

    def before(self) -> None:
        if LEDGER.plan_ms != self._close_ms():
            _plan_whole_bar(self, self._close_ms())

    def should_long(self) -> bool:
        entry = LEDGER.plan.entries.get(self._symbol())
        if entry is None or LEDGER.plan_ms != self._close_ms():
            return False
        self.vars["plan"] = entry
        return True

    def go_long(self) -> None:
        self.buy = self.vars["plan"][0], self.price

    def should_cancel_entry(self) -> bool:
        return False

    def on_open_position(self, order) -> None:
        qty, dist = self.vars["plan"]
        symbol = self._symbol()
        f = LEDGER.filters[symbol]
        eff = order.price * (1 + SLIPPAGE)
        stop = stop_price("BUY", eff, dist, f.tick_size)
        half = floor_to(qty / 2, f.step_size)
        target = floor_to(eff + sonic.HALF_AT_R * dist, f.tick_size) if half >= f.min_qty else None
        self.stop_loss = qty, stop
        if target is not None:
            self.take_profit = half, target
        trade = {
            "symbol": symbol, "opened_ms": self._close_ms(), "stop": stop, "target": target,
            "entry_eff": eff, "scaled": False, "reason": "",
            "margin": qty * eff / LEDGER.limits.leverage,
            "legs": [{"kind": "entry", "ms": self._close_ms(), "raw": float(order.price),
                      "qty": qty}],
        }
        self.vars["trade"] = trade
        LEDGER.open[symbol] = trade
        LEDGER.pending.discard(symbol)

    def on_reduced_position(self, order) -> None:
        trade = self.vars["trade"]
        trade["legs"].append({"kind": "half", "ms": int(order.executed_at),
                              "raw": float(order.price), "qty": abs(float(order.qty))})
        trade["scaled"] = True
        self.stop_loss = abs(self.position.qty), trade["entry_eff"]

    def update_position(self) -> None:
        trade = self.vars["trade"]
        reason = LEDGER.exit_reasons.get(self._symbol()) \
            if LEDGER.plan_ms == self._close_ms() else None
        if reason:
            trade["reason"] = reason
            trade["market_exit_ms"] = self._close_ms()
            self.liquidate()

    def before_terminate(self) -> None:
        """Jesse closes what is still open at the end; that close is not a Sonic exit."""
        self.vars["ending"] = True
        if self.position.is_open and self.vars.get("trade"):
            LEDGER.open_at_end.append(self.vars.pop("trade"))

    def on_close_position(self, order, closed_trade) -> None:
        if self.vars.get("ending"):
            return
        trade = self.vars["trade"]
        market = bool(trade["reason"])
        ms = trade.pop("market_exit_ms") if market else int(order.executed_at)
        if not market:
            trade["reason"] = "breakeven" if trade["scaled"] else "stop_loss"
        sold = sum(leg["qty"] for leg in trade["legs"][1:])
        trade["legs"].append({"kind": "exit", "ms": ms, "raw": float(order.price),
                              "qty": trade["legs"][0]["qty"] - sold})
        trade["closed_ms"] = ms
        result = costs(trade["legs"])
        trade.update(pnl=result["pnl"], fees=result["fees"], slippage=result["slippage"])
        planned = LEDGER.plan.exit_pnl.get(trade["symbol"]) if market else None
        if planned is not None and abs(planned - result["pnl"]) > 1e-9:
            LEDGER.plan_mismatch.append({"at_ms": ms, "symbol": trade["symbol"],
                                         "what": "exit_pnl", "planned": planned,
                                         "actual": result["pnl"]})
        LEDGER.realized += result["pnl"]
        LEDGER.open.pop(trade["symbol"], None)
        LEDGER.trades.append(trade)


def jesse_inputs(
    minutes: dict[str, np.ndarray], trade_symbols: list[str], warmup_bars: int
) -> tuple[dict, list, list, dict, dict]:
    """(config, routes, data_routes, candles, warmup_candles) for Jesse's research functions."""
    data, warm = {}, {}
    for symbol, m in minutes.items():
        split = int(np.searchsorted(m[:, 0], m[0, 0] + warmup_bars * 240 * 60_000))
        key = jh.key(EXCHANGE, jesse_symbol(symbol))
        base = {"exchange": EXCHANGE, "symbol": jesse_symbol(symbol)}
        warm[key] = {**base, "candles": m[:split]}
        data[key] = {**base, "candles": m[split:]}
    config = {"starting_balance": 10_000, "fee": 0, "type": "futures", "futures_leverage": 3,
              "futures_leverage_mode": "cross", "exchange": EXCHANGE,
              "warm_up_candles": warmup_bars}
    routes = [{"exchange": EXCHANGE, "strategy": SonicBridge, "symbol": jesse_symbol(s),
               "timeframe": "4h"} for s in trade_symbols]
    data_routes = [{"exchange": EXCHANGE, "symbol": jesse_symbol(BTC), "timeframe": "4h"}]
    return config, routes, data_routes, data, warm


def run_bridge(
    minutes: dict[str, np.ndarray],
    trade_symbols: list[str],
    filters: dict[str, Filters],
    warmup_bars: int,
    limits: Limits | None = None,
) -> Ledger:
    """Backtest Sonic in Jesse on 1-minute candles; the first warmup_bars 4h bars only warm up."""
    reset_ledger(limits or Limits(), filters, trade_symbols)
    with block_network():
        backtest(*jesse_inputs(minutes, trade_symbols, warmup_bars))
    return LEDGER
