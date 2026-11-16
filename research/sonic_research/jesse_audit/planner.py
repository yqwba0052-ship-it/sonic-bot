"""One 4h bar the way Engine._on_bar runs it, for the Jesse bridge (research only).

The engine closes every exit due on the bar first, syncs (their pnl joins the budget and their
margin is freed), then opens entries in limits.symbols order until max_open. Jesse runs coin by
coin, so the bridge plans the whole bar once, with this function, before any coin acts.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "server"))

from sonic.risk import Filters, Limits, size_position  # noqa: E402

from .costs import SLIPPAGE, costs  # noqa: E402


@dataclass
class BarPlan:
    entries: dict[str, tuple[float, float]] = field(default_factory=dict)  # symbol: (qty, dist)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    projected_realized: float = 0.0
    exit_pnl: dict[str, float] = field(default_factory=dict)


def plan_bar(
    limits: Limits,
    filters: dict[str, Filters],
    realized: float,
    open_trades: dict[str, dict],
    exits: dict[str, float],
    candidates: list[tuple[str, float, float]],
) -> BarPlan:
    """open_trades: symbol -> {"legs", "margin"}; exits: symbol -> raw close it exits at;
    candidates: (symbol, raw close, stop distance) with a BUY signal, in limits.symbols order."""
    plan = BarPlan(projected_realized=realized)
    for symbol, raw in exits.items():
        legs = open_trades[symbol]["legs"]
        left = legs[0]["qty"] - sum(leg["qty"] for leg in legs[1:])
        pnl = costs(legs + [{"kind": "exit", "ms": 0, "raw": raw, "qty": left}])["pnl"]
        plan.exit_pnl[symbol] = pnl
        plan.projected_realized += pnl
    still_open = [s for s in open_trades if s not in exits]
    margin = sum(open_trades[s]["margin"] for s in still_open)
    used = len(still_open)
    for symbol, price, dist in candidates:
        if used >= limits.max_open:
            break
        if symbol in still_open:
            continue
        size = size_position(limits, price, dist, margin, filters[symbol],
                             limits.capital_usdt + plan.projected_realized)
        if not size.qty:
            plan.skipped.append((symbol, size.reason))
            continue
        plan.entries[symbol] = (size.qty, dist)
        margin += size.qty * price * (1 + SLIPPAGE) / limits.leverage
        used += 1
    return plan
