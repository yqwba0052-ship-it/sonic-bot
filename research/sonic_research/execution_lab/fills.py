"""Fill models on real trade prints (conservative, no look-ahead).

- market_buy: walks the trades that were market BUYS (buyer not maker = hitting the ask) after the
  order arrived, until the quantity is filled; price = volume-weighted average.
- limit_buy: a resting buy at `limit` fills only from trades strictly BELOW the limit (all the
  queue at the limit price is assumed to be ahead of us); fill price = limit; quantity = the
  volume of those trades up to the order size (less = partial fill).
"""

from __future__ import annotations

from dataclasses import dataclass

from .agg_data import Trade


@dataclass(frozen=True)
class Fill:
    filled: float
    price: float
    first_ms: int | None
    last_ms: int | None
    partial: bool


NONE = Fill(0.0, 0.0, None, None, False)


def market_buy(trades: list[Trade], qty: float, start_ms: int) -> Fill:
    got = cost = 0.0
    first = last = None
    for t in trades:
        if t.ms <= start_ms or t.buyer_maker:
            continue
        take = min(t.qty, qty - got)
        got += take
        cost += take * t.price
        first = first or t.ms
        last = t.ms
        if got >= qty - 1e-12:
            break
    if got <= 0:
        return NONE
    return Fill(got, cost / got, first, last, got < qty - 1e-12)


def limit_buy(trades: list[Trade], qty: float, limit: float, start_ms: int, end_ms: int) -> Fill:
    got = 0.0
    first = last = None
    for t in trades:
        if t.ms <= start_ms or t.ms > end_ms or not t.price < limit:
            continue
        got = min(qty, got + t.qty)
        first = first or t.ms
        last = t.ms
        if got >= qty - 1e-12:
            break
    if got <= 0:
        return NONE
    return Fill(got, limit, first, last, got < qty - 1e-12)
