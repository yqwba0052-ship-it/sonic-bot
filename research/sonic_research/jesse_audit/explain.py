"""Prove differences by recomputation (owner: every explanation must be backed by numbers).

- budget_carry: a quantity difference is accepted only when Sonic's own size_position, fed with
  EACH side's budget (capital + its earlier closed pnl) and open margin at entry, returns exactly
  that side's quantity. The rest of the trade is then re-classified at equal quantity with the
  unchanged rules; a known cause found there gives "budget_carry+<cause>".
- intrahour_order (cause 5 of the design, R1 vs R2): accepted only when the real minutes touch the
  exit level after the half fill inside that hour and the synthetic minutes of R1 do not.
Nothing here changes a tolerance, a rule or a trade; a difference that cannot be proven stays.
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "server"))

from sonic import strategy as sonic  # noqa: E402
from sonic.risk import Filters, Limits, size_position  # noqa: E402

from ..data import Bars  # noqa: E402
from .compare import H_MS, Row, _row, _same  # noqa: E402
from .costs import SLIPPAGE, costs  # noqa: E402

FOUR_MS = 4 * H_MS


def stop_distance_at(fours: Bars, opened_ms: int) -> float:
    """Sonic's stop distance from the last HISTORY_BARS 4h bars closed at opened_ms."""
    keep = np.nonzero(fours.t + FOUR_MS <= opened_ms)[0][-sonic.HISTORY_BARS:]
    bars = [sonic.Bar(int(fours.t[i]), float(fours.o[i]), float(fours.h[i]),
                      float(fours.low[i]), float(fours.c[i])) for i in keep]
    return sonic.stop_distance(bars)


def budget_before(trades: list[dict], opened_ms: int, symbol: str, order: list[str],
                  capital: float, leverage: int) -> tuple[float, float]:
    """(capital + pnl closed by opened_ms, margin of trades open then) on one side, the way
    Engine._on_bar sees it: exits of the bar first, then entries in symbol order."""
    realized = sum(t["pnl"] for t in trades if t["closed_ms"] <= opened_ms)
    rank = order.index(symbol) if symbol in order else len(order)
    margin = 0.0
    for t in trades:
        earlier = t["opened_ms"] < opened_ms or (
            t["opened_ms"] == opened_ms and t["symbol"] in order
            and order.index(t["symbol"]) < rank)
        if earlier and t["closed_ms"] > opened_ms:
            leg = t["legs"][0]
            margin += leg["qty"] * leg["raw"] * (1 + SLIPPAGE) / leverage
    return capital + realized, margin


def prove_budget(row: Row, left: list[dict], right: list[dict], fours: dict[str, Bars],
                 limits: Limits, filters: dict[str, Filters]) -> dict | None:
    lq, rq = row.left["legs"][0]["qty"], row.right["legs"][0]["qty"]
    if abs(lq - rq) < 1e-12 or not _same(row.left["legs"][0]["raw"], row.right["legs"][0]["raw"]):
        return None
    symbol, opened = row.symbol, row.opened_ms
    price = row.left["legs"][0]["raw"]
    dist = stop_distance_at(fours[symbol], opened)
    order = list(limits.symbols)
    sides = [budget_before(trades, opened, symbol, order, limits.capital_usdt, limits.leverage)
             for trades in (left, right)]
    qty = [size_position(limits, price, dist, margin, filters[symbol], budget).qty
           for budget, margin in sides]
    ok = (abs(qty[0] - lq) < 1e-9 and abs(qty[1] - rq) < 1e-9
          and abs(sides[0][0] - sides[1][0]) > 1e-12)
    return {"budget_carry": ok, "budget": [sides[0][0], sides[1][0]],
            "open_margin": [sides[0][1], sides[1][1]], "stop_distance": dist,
            "qty_recomputed": qty, "qty_actual": [lq, rq]}


def _at_left_qty(row: Row, parity: bool) -> str:
    """Category of the trade once both sides hold the left quantity (rules unchanged)."""
    left, right = copy.deepcopy(row.left), copy.deepcopy(row.right)
    if len(left["legs"]) == len(right["legs"]):
        for a, b in zip(left["legs"], right["legs"], strict=True):
            b["qty"] = a["qty"]
    else:
        right["legs"][0]["qty"] = left["legs"][0]["qty"]
    left["pnl"], right["pnl"] = costs(left["legs"])["pnl"], costs(right["legs"])["pnl"]
    return _row(left, right, parity).category


def explain_rows(rows: list[Row], left: list[dict], right: list[dict], fours: dict[str, Bars],
                 limits: Limits, filters: dict[str, Filters], parity: bool = False) -> None:
    for row in rows:
        if row.category == "identical":
            continue
        proof = prove_budget(row, left, right, fours, limits, filters)
        if proof is None:
            continue
        row.proof.update(proof)
        if not proof["budget_carry"]:
            continue
        rest = _at_left_qty(row, parity)
        if rest in ("identical", "slippage"):
            row.category = "budget_carry"
        elif rest != "unexplained":
            row.category = f"budget_carry+{rest}"
        row.note = (f"qty {proof['qty_actual']} = size_position at budgets "
                    f"{[round(b, 6) for b in proof['budget']]}")


def prove_minute_path(row: Row, synthetic: dict[str, np.ndarray],
                      real: dict[str, np.ndarray]) -> None:
    """R1 (left, synthetic) vs R2 (right, real): the right side stopped at its level after the
    half inside one hour; prove the real minutes touched it there and the synthetic ones did not."""
    if row.category != "unexplained":
        return
    left, right = row.left, row.right
    if len(right["legs"]) != 3 or len(left["legs"]) != 3:
        return
    lh, rh, rx = left["legs"][1], right["legs"][1], right["legs"][2]
    if (lh["kind"], rh["kind"], rx["kind"]) != ("half", "half", "exit") \
            or "ms" not in rh or "ms" not in lh:
        return
    if not (lh["hour"] == rh["hour"] == rx["hour"] and _same(lh["raw"], rh["raw"])
            and left["legs"][2]["hour"] > rx["hour"]):
        return
    level = right["legs"][0]["raw"] * (1 + SLIPPAGE) if right["reason"] == "breakeven" \
        else right["stop"]
    if not _same(rx["raw"], level):
        return
    hi = rx["hour"] + H_MS

    def low(minutes: np.ndarray, half_ms: int) -> float:
        """Lowest low from the minute of THIS series' own half fill to the end of the hour."""
        window = minutes[(minutes[:, 0] >= half_ms - 60_000) & (minutes[:, 0] < hi)]
        return float(window[:, 4].min()) if len(window) else float("nan")

    real_low = low(real[row.symbol], rh["ms"])
    synth_low = low(synthetic[row.symbol], lh["ms"])
    row.proof.update({"exit_level": level, "real_low_after_half": real_low,
                      "synthetic_low_after_half": synth_low})
    if real_low <= level < synth_low:
        row.category = "intrahour_order"
        row.note = (f"after the half: real minutes low {real_low} <= level {level} < "
                    f"synthetic low {synth_low}")
