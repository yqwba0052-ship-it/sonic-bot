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
    exit_hour = int(trade["closed_ms"]) - H_MS
    exit_raw = float(trade["exit_price"]) / (1 - SLIPPAGE)
    where = (hours.t == exit_hour).nonzero()[0]
    anchor = ({"open": float(hours.o[where[0]]), "close": float(hours.c[where[0]])}
              if len(where) else {})  # the exit hour's own open/close, to check fill causes
    legs.append({"kind": "exit", "hour": exit_hour, "raw": exit_raw, "qty": qty - sold, **anchor})
    reason = trade["exit_reason"]
    if (reason == "stop_loss" and not trade.get("scaled") and target is not None
            and abs(costs(legs)["pnl"] - float(trade["pnl"])) > 1e-9):
        # Half target and breakeven stop in ONE hour: the paper exchange takes the half, then
        # closes the rest at the hour close in the same sync, before the engine marks the trade
        # scaled, so the row says stop_loss / unscaled. Rebuild it; keep it only if its cost
        # matches the engine's own pnl, and keep the engine's label for the report.
        half = _floor_to(qty / 2, step)
        rebuilt = [legs[0], {"kind": "half", "hour": exit_hour,
                             "raw": max(anchor["open"], target), "qty": half},
                   {"kind": "exit", "hour": exit_hour, "raw": exit_raw, "qty": qty - half,
                    **anchor}]
        if abs(costs(rebuilt)["pnl"] - float(trade["pnl"])) <= 1e-9:
            legs, reason = rebuilt, "breakeven"
    # The engine overwrites stop_price with the entry when it scales; risk_usdt keeps the
    # original distance (qty * |entry - stop|), and the original stop sat on the tick grid.
    unit = float(tick)
    stop = round(round((float(trade["entry_price"]) - float(trade["risk_usdt"]) / qty) / unit)
                 * unit, 12)
    return {"symbol": trade["symbol"], "opened_ms": int(trade["opened_ms"]),
            "closed_ms": int(trade["closed_ms"]), "stop": stop,
            "target": target, "reason": reason, "engine_label": trade["exit_reason"],
            "pnl": float(trade["pnl"]), "legs": legs, "step": float(step), "tick": float(tick)}


def normalize_jesse(trade: dict, step: str, tick: str) -> dict:
    """The bridge's trade -> the same shape. Stop and half fills carry Jesse's minute time."""
    legs = []
    for leg in trade["legs"]:
        if leg["kind"] == "entry":
            hour = leg["ms"]
        elif leg["kind"] == "exit" and trade["reason"] in MARKET_EXITS:
            hour = leg["ms"] - H_MS
        else:
            hour = (leg["ms"] - 1) // H_MS * H_MS  # Jesse stamps a fill at its minute's end
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


def _same(a: float | None, b: float | None, tick: float = 0.0) -> bool:
    """Equal within PRICE_TOL, and never as loose as a quarter tick (one tick is a difference)."""
    if a is None or b is None:
        return a is b
    tol = PRICE_TOL * max(abs(a), abs(b), 1e-12)
    return abs(a - b) <= (min(tol, tick / 4) if tick else tol)


def _legs_same(a: list[dict], b: list[dict], qty: bool = True, tick: float = 0.0) -> bool:
    return len(a) == len(b) and all(
        x["kind"] == y["kind"] and x["hour"] == y["hour"] and _same(x["raw"], y["raw"], tick)
        and (not qty or abs(x["qty"] - y["qty"]) < 1e-9) for x, y in zip(a, b, strict=True))


def classify(row: Row, parity: bool = False) -> str:
    """parity=True (R0 vs R1, same hours on both sides): a different order inside an hour cannot
    come from the data, so it counts as unexplained."""
    left, right = row.left, row.right
    tick = left["tick"]
    lq, rq = left["legs"][0]["qty"], right["legs"][0]["qty"]
    if not _same(left["legs"][0]["raw"], right["legs"][0]["raw"], tick):
        return "unexplained"
    rounding = abs(lq - rq) > 1e-9
    if rounding and abs(lq - rq) > left["step"] + 1e-9:
        return "unexplained"
    for name in ("stop", "target"):
        if not _same(left[name], right[name], tick):
            if abs((left[name] or 0) - (right[name] or 0)) > left["tick"] + 1e-9:
                return "unexplained"
            rounding = True
    if rounding:  # only rounding may differ; anything else as well needs a human look
        same = _legs_same(left["legs"], right["legs"], qty=False, tick=tick)
        return "rounding" if same and left["reason"] == right["reason"] else "unexplained"
    if _legs_same(left["legs"], right["legs"], tick=tick) and left["reason"] == right["reason"]:
        return "identical" if abs(row.cost_diff) < 1e-9 else "slippage"
    lk = [(x["kind"], x["hour"]) for x in left["legs"]]
    rk = [(x["kind"], x["hour"]) for x in right["legs"]]
    if lk == rk:
        lx, rx = left["legs"][-1], right["legs"][-1]
        if _legs_same(left["legs"][:-1], right["legs"][:-1], tick=tick):
            level = left["legs"][0]["raw"] * (1 + SLIPPAGE) if left["reason"] == "breakeven" \
                else left["stop"]
            half_same_hour = len(left["legs"]) == 3 and left["legs"][1]["hour"] == lx["hour"]
            at_level, other = (rx, lx) if _same(rx["raw"], level, tick) else (lx, rx)
            hit = _same(at_level["raw"], level, tick)
            same_reason = left["reason"] == right["reason"]
            # The other side must have filled exactly where its own engine fills: the hour close
            # (breakeven after the half in one hour) or the hour open (a gap through the stop).
            if same_reason and left["reason"] == "breakeven" and half_same_hour and hit \
                    and "close" in other and _same(other["raw"], other["close"], tick):
                return "breakeven_same_hour"
            if same_reason and left["reason"] in ("stop_loss", "breakeven") and hit \
                    and other["raw"] < at_level["raw"] \
                    and ("open" not in other or _same(other["raw"], other["open"], tick)):
                return "gap_fill"
    for x, y in zip(lk, rk, strict=False):
        if x != y:
            return "intrahour_order" if x[1] == y[1] and not parity else "unexplained"
    return "unexplained"


def _row(left: dict, right: dict, parity: bool = False) -> Row:
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
    row.category = classify(row, parity)
    return row


def match(
    left: list[dict], right: list[dict], end_ms: int, parity: bool = False,
    left_open: list[dict] | None = None, right_open: list[dict] | None = None,
) -> tuple[list[Row], list[dict], list[dict], list[dict]]:
    """Pair trades by coin and entry bar. A trade still open at the end on EITHER side (passed in
    left_open/right_open, or closing after end_ms) is set aside as open-at-end, not 'missing'."""
    def key(t: dict) -> tuple[str, int]:
        return t["symbol"], int(t["opened_ms"])

    opened_at_end = [*(left_open or []), *(right_open or [])]
    open_keys = {key(t) for t in opened_at_end}

    def closed(t: dict) -> bool:
        return (t.get("closed_ms") is not None and t["closed_ms"] <= end_ms
                and key(t) not in open_keys)

    still_open = opened_at_end + [t for t in left + right if not closed(t)]
    lmap = {key(t): t for t in left if closed(t)}
    rmap = {key(t): t for t in right if closed(t)}
    both = sorted(lmap.keys() & rmap.keys(), key=lambda k: k[1])
    rows = [_row(lmap[k], rmap[k], parity) for k in both]
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
