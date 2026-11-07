"""Safety limits and position sizing. Pure functions, no network."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from typing import Self

from pydantic import BaseModel, Field, model_validator

# Liquid USDT perpetuals the owner can pick from. New or thin coins are left out on purpose.
ALLOWED_SYMBOLS = (
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT",
    "XRPUSDT",
    "BNBUSDT",
    "DOGEUSDT",
    "ADAUSDT",
    "AVAXUSDT",
    "SUIUSDT",
    "TRXUSDT",
    "LINKUSDT",
    "LTCUSDT",
)


class Limits(BaseModel):
    capital_usdt: float = Field(default=1000.0, gt=0, le=100000)
    max_daily_loss_usdt: float = Field(default=30.0, gt=0)
    leverage: int = Field(default=2, ge=1, le=5)
    risk_per_trade_pct: float = Field(default=1.0, ge=0.1, le=5)
    stop_loss_pct: float = Field(default=1.0, ge=0.2, le=10)
    take_profit_pct: float = Field(default=2.0, ge=0.2, le=30)
    symbols: list[str] = Field(default_factory=lambda: ["BTCUSDT", "ETHUSDT"], min_length=1)

    @model_validator(mode="after")
    def check_relations(self) -> Self:
        if self.max_daily_loss_usdt > self.capital_usdt:
            raise ValueError("daily_loss_above_capital")
        unknown = [symbol for symbol in self.symbols if symbol not in ALLOWED_SYMBOLS]
        if unknown:
            raise ValueError("unknown_symbol")
        self.symbols = list(dict.fromkeys(self.symbols))
        return self


@dataclass(frozen=True)
class Filters:
    step_size: str
    tick_size: str
    min_qty: float
    min_notional: float


@dataclass(frozen=True)
class Size:
    qty: float
    reason: str = ""


def floor_to(value: float, step: str) -> float:
    unit = Decimal(step)
    return float((Decimal(str(value)) / unit).to_integral_value(ROUND_DOWN) * unit)


def max_order_usdt(limits: Limits, budget: float, open_margin: float = 0.0) -> float:
    """Largest order value the limits allow: the stop risks the set share of the budget,
    and all margin stays inside the remaining budget."""
    free_margin = budget - open_margin
    if free_margin <= 0:
        return 0.0
    risk_usdt = budget * limits.risk_per_trade_pct / 100
    return min(risk_usdt / (limits.stop_loss_pct / 100), free_margin * limits.leverage)


def size_position(
    limits: Limits,
    price: float,
    open_margin: float,
    filters: Filters,
    budget: float | None = None,
) -> Size:
    """Size a trade inside the remaining budget (the set amount when budget is None)."""
    budget = limits.capital_usdt if budget is None else budget
    if budget - open_margin <= 0:
        return Size(0.0, "capital_in_use")
    notional = max_order_usdt(limits, budget, open_margin)
    qty = floor_to(notional / price, filters.step_size)
    if qty < filters.min_qty or qty * price < filters.min_notional:
        return Size(0.0, "below_exchange_minimum")
    return Size(qty)


def protection_prices(
    side: str, entry: float, limits: Limits, tick_size: str
) -> tuple[float, float]:
    """Return (stop, take_profit) for a BUY or SELL entry."""
    direction = 1 if side == "BUY" else -1
    stop = entry * (1 - direction * limits.stop_loss_pct / 100)
    target = entry * (1 + direction * limits.take_profit_pct / 100)
    return floor_to(stop, tick_size), floor_to(target, tick_size)


def daily_loss_breached(limits: Limits, realized_today: float, unrealized: float) -> bool:
    return realized_today + unrealized <= -limits.max_daily_loss_usdt
