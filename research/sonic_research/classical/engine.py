"""Shared research risk engine: one trade's path (outcome) and the $30 portfolio (portfolio).

Entry at the signal bar's close (or the next open with delay=True); exits from the next bar.
Inside a bar the stop is checked first, gaps fill at the open. X1 = stop + target; X2 = half at
1R, stop to break-even (entry price paid), target for the rest. Both: exit on an invalidation
close and after 30 bars. Costs (Sonic's): 0.02% slippage + 0.05% fee per side, scaled by
cost_mult; absolute prices so inverted (short) series cost the same. The stop is never widened.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "server"))

from sonic.risk import Filters, floor_to  # noqa: E402

from .core import Series  # noqa: E402
from .strategies import Signal  # noqa: E402

SLIP, FEE = 0.0002, 0.0005
TIME_LIMIT = 30
FOUR_H = 4 * 3_600_000


@dataclass(frozen=True)
class Combo:
    rr: float
    exit: str  # "X1" | "X2"
    htf: bool


GRID = [Combo(rr, ex, htf) for rr in (1.5, 2.0) for ex in ("X1", "X2") for htf in (True, False)]


def outcome(s: Series, sig: Signal, combo: Combo, delay: bool = False,
            cost_mult: float = 1.0) -> dict | None:
    if combo.htf and not sig.htf:
        return None
    i = sig.i
    if delay:
        if i + 1 >= len(s):
            return None
        entry_j, entry, start = i + 1, float(s.o[i + 1]), i + 1
    else:
        entry_j, entry, start = i, float(sig.entry), i + 1
    risk = entry - sig.stop
    if risk <= 0:
        return None
    target = sig.target if sig.target is not None else entry + combo.rr * risk
    if (target - entry) / risk < combo.rr - 1e-12:
        return None
    entry_eff = entry + abs(entry) * SLIP * cost_mult
    stop, legs, left, scaled = sig.stop, [], 1.0, False
    one_r = entry + risk
    for j in range(start, len(s)):
        o, h, low, c = float(s.o[j]), float(s.h[j]), float(s.low[j]), float(s.c[j])
        if low <= stop:
            legs.append((left, min(o, stop)))
            return _done(entry_j, entry, j, legs, "breakeven" if scaled else "stop", sig)
        if combo.exit == "X2" and not scaled and h >= one_r:
            legs.append((0.5, max(o, one_r)))
            left, scaled, stop = 0.5, True, entry_eff
        if h >= target:
            legs.append((left, max(o, target)))
            return _done(entry_j, entry, j, legs, "target", sig)
        if sig.invalid is not None and c < sig.invalid:
            legs.append((left, c))
            return _done(entry_j, entry, j, legs, "invalid", sig)
        if j - i >= TIME_LIMIT:
            legs.append((left, c))
            return _done(entry_j, entry, j, legs, "time", sig)
    return None  # still open at the end of the data: not counted


def _done(entry_j: int, entry: float, exit_j: int, legs: list, reason: str, sig: Signal) -> dict:
    return {"entry_j": entry_j, "entry_raw": entry, "exit_j": exit_j, "legs": legs,
            "reason": reason, "stop": sig.stop}


def unit_pnl(entry_raw: float, legs: list, cost_mult: float = 1.0) -> float:
    """Result for 1 unit: buy at entry (+slippage), sell each leg (-slippage), fees on both."""
    entry_eff = entry_raw + abs(entry_raw) * SLIP * cost_mult
    total = -abs(entry_eff) * FEE * cost_mult
    for frac, raw in legs:
        out = raw - abs(raw) * SLIP * cost_mult
        total += frac * (out - entry_eff) - frac * abs(out) * FEE * cost_mult
    return total


def portfolio(cands: list[dict], filters: dict[str, Filters], risk_pct: float = 0.5,
              capital: float = 30.0, max_open: int = 3, leverage: int = 3) -> dict:
    """$30 account: risk_pct of current equity per trade, one position per coin, max_open,
    margin never above equity, Binance minimum order and step size."""
    order = sorted(cands, key=lambda x: (x["entry_t"], x["strategy"], x["coin"]))
    open_, trades, skipped, realized = [], [], [], 0.0
    for c in order:
        still = []
        for pos in open_:
            if pos["exit_t"] <= c["entry_t"]:
                realized += pos["pnl"]
            else:
                still.append(pos)
        open_ = still
        why = None
        if any(p["coin"] == c["coin"] for p in open_):
            why = "coin_open"
        elif len(open_) >= max_open:
            why = "max_open"
        if why is None:
            f, equity = filters[c["coin"]], capital + realized
            risk_px = abs(c["entry_raw"] - c["stop"])
            qty = floor_to(equity * risk_pct / 100 / risk_px, f.step_size) if risk_px else 0.0
            notional = qty * abs(c["entry_raw"])
            if qty < f.min_qty or notional < f.min_notional:
                why = "not_representable"
            elif notional / leverage + sum(p["notional"] / leverage for p in open_) > equity:
                why = "capital"
        if why:
            skipped.append({**c, "why": why})
            continue
        pos = {**c, "qty": qty, "notional": notional, "pnl": qty * c["pnl_unit"]}
        open_.append(pos)
        trades.append(pos)
    return {"trades": trades, "skipped": skipped}
