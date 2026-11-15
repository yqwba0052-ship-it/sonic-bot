"""Sonic's own rules inside Jesse (research only, read-only use of the bot's code).

Signals, stop distance, sizing and rounding come from sonic.strategy and sonic.risk unchanged;
only order execution is Jesse's. Jesse runs without fees or slippage: costs() adds both, the
same way for both engines. The ledger is shared by every coin, like the engine's budget.
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
from sonic.risk import Filters, Limits, floor_to, size_position, stop_price  # noqa: E402

from .costs import SLIPPAGE, costs  # noqa: E402
from .offline import block_network  # noqa: E402

EXCHANGE = "Binance Perpetual Futures"
BTC = "BTCUSDT"


def jesse_symbol(symbol: str) -> str:
    return symbol[:-4] + "-" + symbol[-4:]


@dataclass
class Ledger:
    limits: Limits = field(default_factory=Limits)
    filters: dict[str, Filters] = field(default_factory=dict)
    realized: float = 0.0
    margin: dict[str, float] = field(default_factory=dict)
    trades: list[dict] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)


LEDGER = Ledger()


def reset_ledger(limits: Limits, filters: dict[str, Filters]) -> None:
    global LEDGER
    LEDGER = Ledger(limits=limits, filters=filters)


def _bars(candles: np.ndarray) -> list[sonic.Bar]:
    return [sonic.Bar(int(r[0]), float(r[1]), float(r[3]), float(r[4]), float(r[2]))
            for r in candles[-sonic.HISTORY_BARS:]]


class SonicBridge(Strategy):
    def _symbol(self) -> str:
        return self.symbol.replace("-", "")

    def _close_ms(self) -> int:
        return int(self.candles[-1][0]) + sonic.BAR_MS

    def should_long(self) -> bool:
        bars = _bars(self.candles)
        btc = _bars(self.get_candles(self.exchange, jesse_symbol(BTC), "4h"))
        if sonic.entry_signal(bars, sonic.btc_uptrend(btc)) != "BUY":
            return False
        symbol, dist = self._symbol(), sonic.stop_distance(bars)
        size = size_position(LEDGER.limits, self.price, dist, sum(LEDGER.margin.values()),
                             LEDGER.filters[symbol], LEDGER.limits.capital_usdt + LEDGER.realized)
        if not size.qty:
            LEDGER.skipped.append({"symbol": symbol, "at_ms": self._close_ms(),
                                   "reason": size.reason})
            return False
        self.vars["plan"] = (size.qty, dist)
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
        LEDGER.margin[symbol] = qty * eff / LEDGER.limits.leverage
        self.vars["trade"] = {
            "symbol": symbol, "opened_ms": self._close_ms(), "stop": stop, "target": target,
            "entry_eff": eff, "scaled": False, "reason": "",
            "legs": [{"kind": "entry", "ms": self._close_ms(), "raw": float(order.price),
                      "qty": qty}],
        }

    def on_reduced_position(self, order) -> None:
        trade = self.vars["trade"]
        trade["legs"].append({"kind": "half", "ms": int(order.executed_at),
                              "raw": float(order.price), "qty": abs(float(order.qty))})
        trade["scaled"] = True
        self.stop_loss = abs(self.position.qty), trade["entry_eff"]

    def update_position(self) -> None:
        trade = self.vars["trade"]
        held = (self._close_ms() - trade["opened_ms"]) // sonic.BAR_MS
        if sonic.exit_due(_bars(self.candles), "BUY"):
            trade["reason"] = "trade_exit_signal"
        elif held >= sonic.MAX_HOLD_BARS:
            trade["reason"] = "trade_exit_time"
        if trade["reason"]:
            trade["market_exit_ms"] = self._close_ms()
            self.liquidate()

    def on_close_position(self, order, closed_trade) -> None:
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
        LEDGER.realized += result["pnl"]
        LEDGER.margin.pop(trade["symbol"], None)
        LEDGER.trades.append(trade)


def run_bridge(
    minutes: dict[str, np.ndarray],
    trade_symbols: list[str],
    filters: dict[str, Filters],
    warmup_bars: int,
    limits: Limits | None = None,
) -> Ledger:
    """Backtest Sonic in Jesse on 1-minute candles; the first warmup_bars 4h bars only warm up."""
    reset_ledger(limits or Limits(), filters)
    split = warmup_bars * 240
    data, warm = {}, {}
    for symbol, m in minutes.items():
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
    with block_network():
        backtest(config, routes, data_routes, data, warm)
    return LEDGER
