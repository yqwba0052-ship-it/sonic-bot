"""Safety limits and position sizing. Pure functions, no network."""

from __future__ import annotations

import math
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
SMALL_ACCOUNT_SYMBOLS = [
    "SOLUSDT",
    "XRPUSDT",
    "DOGEUSDT",
    "ADAUSDT",
    "SUIUSDT",
    "TRXUSDT",
    "BNBUSDT",
    "AVAXUSDT",
]
# A trade too small for the exchange minimum may be raised to the minimum only while its stop
# then risks at most this share of the amount (the tested small-account rule).
MAX_RISK_PCT = 2.0


class Limits(BaseModel):
    capital_usdt: float = Field(default=30.0, gt=0, le=100000)
    max_daily_loss_usdt: float = Field(default=6.0, gt=0)
    leverage: int = Field(default=3, ge=1, le=5)
    risk_per_trade_pct: float = Field(default=1.0, ge=0.25, le=2)
    max_open: int = Field(default=3, ge=1, le=6)
    # Shorts lost money in 2024-2026 while longs won every year; off unless the owner opts in.
    allow_shorts: bool = False
    symbols: list[str] = Field(default_factory=lambda: list(SMALL_ACCOUNT_SYMBOLS), min_length=1)

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

    def min_order(self, price: float) -> float:
        return max(self.min_notional, self.min_qty * price)


@dataclass(frozen=True)
class Size:
    qty: float
    reason: str = ""


def floor_to(value: float, step: str) -> float:
    unit = Decimal(step)
    return float((Decimal(str(value)) / unit).to_integral_value(ROUND_DOWN) * unit)


def ceil_to(value: float, step: str) -> float:
    unit = float(Decimal(step))
    return float(Decimal(str(math.ceil(value / unit - 1e-9))) * Decimal(step))


def min_amount_for(min_order: float, stop_pct: float, leverage: int) -> float:
    """Smallest amount that can take one trade: the minimum order's stop must fit inside
    MAX_RISK_PCT of the amount, and its margin inside the amount."""
    return max(min_order * stop_pct / (MAX_RISK_PCT / 100), min_order / leverage)


def size_position(
    limits: Limits,
    price: float,
    stop_dist: float,
    open_margin: float,
    filters: Filters,
    budget: float,
) -> Size:
    """Quantity whose stop risks risk_per_trade_pct of the remaining amount, inside margin."""
    if budget <= 0:
        return Size(0.0, "budget_used")
    if stop_dist <= 0 or price <= 0:
        return Size(0.0, "no_stop_distance")
    free_margin = budget - open_margin
    max_notional = free_margin * limits.leverage
    notional = budget * limits.risk_per_trade_pct / 100 * price / stop_dist
    minimum = filters.min_order(price)
    if notional < minimum:
        qty = ceil_to(minimum / price, filters.step_size)
        if qty * stop_dist > budget * MAX_RISK_PCT / 100:
            return Size(0.0, "amount_too_small")
    else:
        qty = floor_to(min(notional, max_notional) / price, filters.step_size)
    if qty * price > max_notional:
        return Size(0.0, "capital_in_use")
    if qty < filters.min_qty or qty * price < filters.min_notional:
        return Size(0.0, "capital_in_use")
    return Size(qty)


def stop_price(side: str, entry: float, stop_dist: float, tick_size: str) -> float:
    direction = 1 if side == "BUY" else -1
    return floor_to(entry - direction * stop_dist, tick_size)


def daily_loss_breached(limits: Limits, realized_today: float, unrealized: float) -> bool:
    return realized_today + unrealized <= -limits.max_daily_loss_usdt
