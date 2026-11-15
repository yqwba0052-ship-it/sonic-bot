"""One cost formula for both engines (owner amendment 3): the paper exchange's own rules.

Each leg holds the raw (pre-slippage) fill price. Buying pays raw * (1 + SLIPPAGE), selling gets
raw * (1 - SLIPPAGE); every fill pays FEE_RATE of its slipped notional. Longs only (Sonic).
"""

from __future__ import annotations

SLIPPAGE = 0.0002  # sonic.exchange.SLIPPAGE
FEE_RATE = 0.0005  # sonic.exchange.FEE_RATE


def costs(legs: list[dict]) -> dict:
    entry, exits = legs[0], legs[1:]
    entry_eff = entry["raw"] * (1 + SLIPPAGE)
    fees = entry["qty"] * entry_eff * FEE_RATE
    slippage = entry["qty"] * entry["raw"] * SLIPPAGE
    gross = 0.0
    for leg in exits:
        eff = leg["raw"] * (1 - SLIPPAGE)
        gross += leg["qty"] * (eff - entry_eff)
        fees += leg["qty"] * eff * FEE_RATE
        slippage += leg["qty"] * leg["raw"] * SLIPPAGE
    return {"gross": gross, "fees": fees, "slippage": slippage, "pnl": gross - fees}
