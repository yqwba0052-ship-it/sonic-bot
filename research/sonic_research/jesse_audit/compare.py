"""Trade-by-trade comparison of two runs, every difference put under a fixed cause.

Both sides are first normalized to raw (pre-slippage) legs, each tied to the hour in which it
happened. Execution differences (times, raw prices, quantities, exits) are measured with ONE cost
formula for both sides; cost differences (what each engine itself charged versus that formula)
are kept apart (owner amendment 3).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from math import floor

from ..data import Bars
from .costs import SLIPPAGE, costs

H_MS = 3_600_000
PRICE_TOL = 0.00005
MARKET_EXITS = ("trade_exit_signal", "trade_exit_time")
CATEGORIES = ("identical", "slippage", "rounding", "breakeven_same_hour", "gap_fill",
              "intrahour_order", "missing", "unexplained")


def _floor_to(value: float, step: str) -> float:
    unit = float(step)
    return round(floor(value / unit + 1e-9) * unit, 12)


def normalize_engine(
    trade: dict, scale_events: list[tuple[str, int]], hours: Bars, step: str, tick: str
) -> dict:
    """Our replay's trade row -> raw legs. Half fills come from the trade_scaled event (tick
    time) and the hour bar it closed: raw = max(open, target), as the paper exchange fills."""
    qty = float(trade["qty"])
    target = float(trade["target_price"]) or None
    legs = [{"kind": "entry", "hour": int(trade["opened_ms"]),
             "raw": float(trade["entry_price"]) / (1 + SLIPPAGE), "qty": qty}]
    sold = 0.0
    if trade.get("scaled"):
        at = next(at for symbol, at in scale_events if symbol == trade["symbol"]
                  and trade["opened_ms"] < at <= trade["closed_ms"])
        index = int((hours.t == at - H_MS).nonzero()[0][0])
        sold = _floor_to(qty / 2, step)
        legs.append({"kind": "half", "hour": at - H_MS,
                     "raw": max(float(hours.o[index]), target), "qty": sold})
    legs.append({"kind": "exit", "hour": int(trade["closed_ms"]) - H_MS,
                 "raw": float(trade["exit_price"]) / (1 - SLIPPAGE), "qty": qty - sold})
    # The engine overwrites stop_price with the entry when it scales; risk_usdt keeps the
    # original distance (qty * |entry - stop|), and the original stop sat on the tick grid.
    unit = float(tick)
    stop = round(round((float(trade["entry_price"]) - float(trade["risk_usdt"]) / qty) / unit)
                 * unit, 12)
    return {"symbol": trade["symbol"], "opened_ms": int(trade["opened_ms"]),
            "closed_ms": int(trade["closed_ms"]), "stop": stop,
            "target": target, "reason": trade["exit_reason"], "pnl": float(trade["pnl"]),
            "legs": legs, "step": float(step), "tick": float(tick)}


def normalize_jesse(trade: dict, step: str, tick: str) -> dict:
    """The bridge's trade -> the same shape. Stop and half fills carry Jesse's minute time."""
    legs = []
    for leg in trade["legs"]:
        if leg["kind"] == "entry":
            hour = leg["ms"]
        elif leg["kind"] == "exit" and trade["reason"] in MARKET_EXITS:
            hour = leg["ms"] - H_MS
        else:
            hour = leg["ms"] // H_MS * H_MS
        legs.append({"kind": leg["kind"], "hour": int(hour), "raw": leg["raw"], "qty": leg["qty"]})
    return {"symbol": trade["symbol"], "opened_ms": trade["opened_ms"],
            "closed_ms": trade["closed_ms"], "stop": trade["stop"], "target": trade["target"],
            "reason": trade["reason"], "pnl": trade["pnl"], "legs": legs,
            "step": float(step), "tick": float(tick)}


@dataclass
class Row:
    symbol: str
    opened_ms: int
    left: dict
    right: dict
    fields: dict = field(default_factory=dict)
    category: str = ""
    note: str = ""
    execution_diff: float = 0.0
    cost_diff: float = 0.0


def _same(a: float | None, b: float | None) -> bool:
    if a is None or b is None:
        return a is b
    return abs(a - b) <= PRICE_TOL * max(abs(a), abs(b), 1e-12)


def _legs_same(a: list[dict], b: list[dict]) -> bool:
    return len(a) == len(b) and all(
        x["kind"] == y["kind"] and x["hour"] == y["hour"] and _same(x["raw"], y["raw"])
        and abs(x["qty"] - y["qty"]) < 1e-9 for x, y in zip(a, b, strict=True))


def classify(row: Row) -> str:
    left, right = row.left, row.right
    lq, rq = left["legs"][0]["qty"], right["legs"][0]["qty"]
    if not _same(left["legs"][0]["raw"], right["legs"][0]["raw"]) or abs(lq - rq) > 1e-9:
        within = abs(lq - rq) <= left["step"] + 1e-9
        return "rounding" if within and _same(left["legs"][0]["raw"], right["legs"][0]["raw"]) \
            else "unexplained"
    for name in ("stop", "target"):
        if not _same(left[name], right[name]):
            gap = abs((left[name] or 0) - (right[name] or 0))
            return "rounding" if gap <= left["tick"] + 1e-9 else "unexplained"
    if _legs_same(left["legs"], right["legs"]) and left["reason"] == right["reason"]:
        return "identical" if abs(row.cost_diff) < 1e-9 else "slippage"
    lk = [(x["kind"], x["hour"]) for x in left["legs"]]
    rk = [(x["kind"], x["hour"]) for x in right["legs"]]
    if lk == rk:
        lx, rx = left["legs"][-1], right["legs"][-1]
        if _legs_same(left["legs"][:-1], right["legs"][:-1]):
            level = left["legs"][0]["raw"] * (1 + SLIPPAGE) if left["reason"] == "breakeven" \
                else left["stop"]
            half_same_hour = len(left["legs"]) == 3 and left["legs"][1]["hour"] == lx["hour"]
            if left["reason"] == right["reason"] == "breakeven" and half_same_hour and (
                    _same(rx["raw"], level) or _same(lx["raw"], level)):
                return "breakeven_same_hour"
            if left["reason"] == right["reason"] and left["reason"] in ("stop_loss", "breakeven"):
                low, high = sorted((lx["raw"], rx["raw"]))
                if _same(high, level) and low < high:
                    return "gap_fill"
    for x, y in zip(lk, rk, strict=False):
        if x != y:
            return "intrahour_order" if x[1] == y[1] else "unexplained"
    return "unexplained"


def _row(left: dict, right: dict) -> Row:
    le, re_ = costs(left["legs"])["pnl"], costs(right["legs"])["pnl"]
    row = Row(left["symbol"], left["opened_ms"], left, right)
    row.execution_diff = re_ - le
    row.cost_diff = (right["pnl"] - re_) - (left["pnl"] - le)
    for name in ("stop", "target", "reason", "closed_ms"):
        row.fields[name] = (left[name], right[name])
    for i, (x, y) in enumerate(zip(left["legs"], right["legs"], strict=False)):
        for key in ("kind", "hour", "raw", "qty"):
            row.fields[f"leg{i}.{key}"] = (x[key], y[key])
    row.fields["execution_pnl"] = (le, re_)
    row.fields["reported_pnl"] = (left["pnl"], right["pnl"])
    row.category = classify(row)
    return row


def match(
    left: list[dict], right: list[dict], end_ms: int
) -> tuple[list[Row], list[dict], list[dict], list[dict]]:
    """Pair trades by coin and entry bar; trades still open at end_ms are set aside."""
    def closed(t: dict) -> bool:
        return t.get("closed_ms") is not None and t["closed_ms"] <= end_ms

    still_open = [t for t in left + right if not closed(t)]
    lmap = {(t["symbol"], t["opened_ms"]): t for t in left if closed(t)}
    rmap = {(t["symbol"], t["opened_ms"]): t for t in right if closed(t)}
    rows = [_row(lmap[k], rmap[k]) for k in sorted(lmap.keys() & rmap.keys(), key=lambda k: k[1])]
    left_only = [lmap[k] for k in sorted(lmap.keys() - rmap.keys(), key=lambda k: k[1])]
    right_only = [rmap[k] for k in sorted(rmap.keys() - lmap.keys(), key=lambda k: k[1])]
    return rows, left_only, right_only, still_open


def summary(rows: list[Row]) -> dict:
    counts: dict[str, int] = defaultdict(int)
    execution: dict[str, float] = defaultdict(float)
    for row in rows:
        counts[row.category] += 1
        execution[row.category] += row.execution_diff
    return {"counts": dict(counts), "unexplained": counts.get("unexplained", 0),
            "execution_pnl_diff": dict(execution),
            "cost_diff_total": sum(row.cost_diff for row in rows),
            "left_execution_pnl": sum(row.fields["execution_pnl"][0] for row in rows),
            "right_execution_pnl": sum(row.fields["execution_pnl"][1] for row in rows)}
