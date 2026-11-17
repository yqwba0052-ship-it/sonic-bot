"""Execution study: Sonic's own trades with four entry styles, on real Binance trade prints.

A      = Sonic's assumption (close + 0.02%, taker 0.05%); must reproduce the Jesse trades.
A_real = market buy on real prints (taker buys after T + latency), taker 0.05%  <- fair baseline.
B      = maker limit at the decision close, strict trade-through fills, 60 min, unfilled = missed.
C      = limit like B when ATR% <= theta (training median), rest at market after 60 min;
         otherwise market at once. Never misses.
Exits: Sonic's rules on 1-minute candles, same for every case.

python -m sonic_research.execution_lab.run <name> pilot|train|holdout
"""

from __future__ import annotations

import hashlib
import json
import statistics
import sys
import time
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from ..data import load_bars, month_range
from ..jesse_audit.data_1m import load_minutes
from ..jesse_audit.explain import stop_distance_at
from ..jesse_audit.filters import load_filters
from . import LAB
from .agg_data import file_for, trades_after
from .fills import limit_buy, market_buy
from .sim import FOUR_H, MIN, exit_path, market_exit_time, pnl

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "server"))
from sonic import strategy as sonic  # noqa: E402

REPORTS = Path(__file__).resolve().parents[2] / "reports"
JESSE_DATA = Path("D:/Sonic-Jesse-Research")
LATENCY_MS = 2_000
WAIT_MIN = 60
TAKER, MAKER, SLIP = 0.0005, 0.0002, 0.0002
SOURCES = {"pilot": "jesse_full.json", "train": "jesse_full.json",
           "holdout": "jesse_holdout.json"}


def _day(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%d")


def sonic_trades(window: str) -> list[dict]:
    r = json.loads((REPORTS / SOURCES[window]).read_text(encoding="utf-8"))
    rows = r["comparisons"]["R1_vs_R2"]["rows"]
    trades = [x["right"] for x in rows]  # R2: Jesse on real minutes, verified against Sonic
    if window == "pilot":
        lo, hi = 1_672_531_200_000, 1_688_169_600_000  # 2023-01-01 .. 2023-07-01
        trades = [t for t in trades if t["symbol"] in ("SOLUSDT", "TRXUSDT")
                  and lo <= t["opened_ms"] < hi]
    return sorted(trades, key=lambda t: t["opened_ms"])


def atr_pct(fours, signal_ms: int) -> float:
    idx = np.nonzero(fours.t + FOUR_H <= signal_ms)[0][-sonic.HISTORY_BARS:]
    bars = [sonic.Bar(int(fours.t[j]), float(fours.o[j]), float(fours.h[j]),
                      float(fours.low[j]), float(fours.c[j])) for j in idx]
    return sonic.atr(bars) / bars[-1].c


def _paths(symbol: str, signal_ms: int) -> list[Path]:
    days = sorted({_day(signal_ms), _day(signal_ms + (WAIT_MIN + 1) * MIN)})
    return [file_for(symbol, d) for d in days]


def simulate(trade: dict, case: str, ctx: dict) -> dict:
    s, signal = trade["symbol"], int(trade["legs"][0]["ms"])
    close, qty = float(trade["legs"][0]["raw"]), float(trade["legs"][0]["qty"])
    f, minutes, fours = ctx["filters"][s], ctx["minutes"][s], ctx["fours"][s]
    dist = stop_distance_at(fours, signal)
    exit_ms, exit_reason = ctx["exits"][(s, signal)]
    start = signal + LATENCY_MS
    prints = ctx["prints"].get((s, signal))
    out = {"symbol": s, "signal_ms": signal, "close": close, "qty_signal": qty, "case": case,
           "atr_pct": ctx["atr"][(s, signal)]}
    parts = []  # (qty, price, fee_rate, done_ms)
    if case == "A":
        parts.append((qty, close * (1 + SLIP), TAKER, signal))
    elif case == "A_real" or (case == "C" and out["atr_pct"] > ctx["theta"]):
        m = market_buy(prints, qty, start)
        if m.filled:
            parts.append((m.filled, m.price, TAKER, m.last_ms))
    else:  # B, or C in calm markets: maker limit first
        lim = limit_buy(prints, qty, close, start, start + WAIT_MIN * MIN)
        if lim.filled:
            parts.append((lim.filled, lim.price, MAKER, lim.last_ms))
        rest = qty - (lim.filled if lim.filled else 0.0)
        if case == "C" and rest > 1e-12:
            m = market_buy(prints, rest, start + WAIT_MIN * MIN)
            if m.filled:
                parts.append((m.filled, m.price, TAKER, m.last_ms))
    filled = sum(p[0] for p in parts)
    out.update(filled=filled, partial=0 < filled < qty - 1e-12, missed=filled <= 0)
    if filled <= 0:
        out.update(pnl=0.0, legs=[], reason="not_filled", wait_ms=None, entry_eff=None,
                   fee=0.0, slippage_bps=None)
        return out
    eff = sum(q * p for q, p, _, _ in parts) / filled
    fee = sum(q * p * r for q, p, r, _ in parts)
    done = max(p[3] for p in parts)
    begin = signal if case == "A" else done
    legs, reason = exit_path(minutes, begin, eff, filled, dist, f, exit_ms, exit_reason,
                             no_target_before=done if case != "A" else None)
    out.update(entry_eff=eff, fee=fee, legs=legs, reason=reason, wait_ms=done - signal,
               slippage_bps=1e4 * (eff - close) / close, pnl=pnl(eff, fee, legs),
               exit_ms=legs[-1]["ms"] if legs else None,
               maker_share=sum(q for q, _, r, _ in parts if r == MAKER) / filled)
    return out


def metrics(rows: list[dict], base: dict[tuple, dict] | None = None) -> dict:
    done = [r for r in rows if not r["missed"] and r["legs"]]
    ordered = sorted(done, key=lambda r: r["exit_ms"])
    equity = peak = 30.0
    worst = 0.0
    for r in ordered:
        equity += r["pnl"]
        peak = max(peak, equity)
        worst = max(worst, 1 - equity / peak)
    wins = [r["pnl"] for r in done if r["pnl"] > 0]
    losses = [r["pnl"] for r in done if r["pnl"] <= 0]
    by_year = defaultdict(float)
    for r in done:
        by_year[_day(r["exit_ms"])[:4]] += r["pnl"]
    out = {
        "signals": len(rows), "filled": sum(1 for r in rows if not r["missed"]),
        "not_filled": sum(1 for r in rows if r["missed"]),
        "partial": sum(1 for r in rows if r["partial"]),
        "net": sum(r["pnl"] for r in done), "max_dd_pct": round(100 * worst, 2),
        "profit_factor": (sum(wins) / -sum(losses)) if sum(losses) else None,
        "win_rate_pct": round(100 * len(wins) / len(done), 1) if done else 0.0,
        "avg_slippage_bps": statistics.fmean(r["slippage_bps"] for r in done) if done else None,
        "avg_fee": statistics.fmean(r["fee"] for r in done) if done else None,
        "total_fee": sum(r["fee"] for r in done),
        "avg_wait_s": statistics.fmean(r["wait_ms"] / 1000 for r in done) if done else None,
        "stop_exits": sum(1 for r in done if r["reason"] in ("stop_loss", "breakeven")),
        "open_at_end": sum(1 for r in done if r["reason"] == "open_at_end"),
        "by_year": dict(by_year),
    }
    if base is not None:
        missed = [r for r in rows if r["missed"]]
        out["missed_winners"] = sum(1 for r in missed if base[(r["symbol"], r["signal_ms"])]
                                    ["pnl"] > 0)
        out["avoided_losers"] = sum(1 for r in missed if base[(r["symbol"], r["signal_ms"])]
                                    ["pnl"] <= 0)
        same_stop = [r for r in done if r["reason"] in ("stop_loss", "breakeven")]
        out["stop_exits_same_minute_as_base"] = sum(
            1 for r in same_stop if base[(r["symbol"], r["signal_ms"])].get("exit_ms")
            == r["exit_ms"])
    return out


def main(name: str, window: str) -> dict:
    started = time.time()
    trades = sonic_trades(window)
    symbols = sorted({t["symbol"] for t in trades})
    first = datetime.fromtimestamp(trades[0]["opened_ms"] / 1000, UTC)
    last = datetime.fromtimestamp(trades[-1]["closed_ms"] / 1000, UTC)
    warm = f"{first.year - 1 if first.month <= 7 else first.year:04d}-" \
           f"{(first.month - 7) % 12 + 1:02d}"
    months = month_range(warm, last.strftime("%Y-%m"))
    end_month = "2026-10" if window == "holdout" else "2023-12"
    months = [m for m in months if m <= end_month]
    ctx = {"filters": load_filters(REPORTS / "jesse_filters_full.json"),
           "fours": {s: load_bars(s, months, "4h") for s in symbols},
           "minutes": {s: load_minutes(s, [m for m in months if m >= first.strftime("%Y-%m")],
                                       cache_dir=JESSE_DATA) for s in symbols}}
    ctx["exits"] = {(t["symbol"], t["legs"][0]["ms"]): market_exit_time(
        ctx["fours"][t["symbol"]], t["legs"][0]["ms"]) for t in trades}
    ctx["atr"] = {(t["symbol"], t["legs"][0]["ms"]): atr_pct(ctx["fours"][t["symbol"]],
                                                             t["legs"][0]["ms"]) for t in trades}
    theta_path = LAB / "reports" / "theta.json"
    if window == "train" and not theta_path.exists():  # fixed once, from prices only
        theta_path.write_text(json.dumps({"theta_atr_pct": statistics.median(
            ctx["atr"].values()), "from": "median ATR% of the 82 training signals"}),
            encoding="utf-8")
    ctx["theta"] = json.loads(theta_path.read_text(encoding="utf-8"))["theta_atr_pct"] \
        if theta_path.exists() else statistics.median(ctx["atr"].values())
    ctx["prints"] = {}
    for t in trades:
        s, sig = t["symbol"], int(t["legs"][0]["ms"])
        ctx["prints"][(s, sig)] = trades_after(_paths(s, sig), sig,
                                               sig + (WAIT_MIN + 30) * MIN)
    cases = {c: [simulate(t, c, ctx) for t in trades] for c in ("A", "A_real", "B", "C")}
    jesse = {(t["symbol"], t["legs"][0]["ms"]): t for t in trades}
    check = []
    for r in cases["A"]:
        j = jesse[(r["symbol"], r["signal_ms"])]
        jl = [(leg["kind"], round(leg["raw"], 10)) for leg in j["legs"][1:]]
        rl = [(leg["kind"], round(leg["raw"], 10)) for leg in r["legs"]]
        check.append({"symbol": r["symbol"], "signal_ms": r["signal_ms"],
                      "same_legs": jl == rl, "same_reason": j["reason"] == r["reason"],
                      "pnl_diff": r["pnl"] - j["pnl"], "jesse": jl, "sim": rl,
                      "jesse_reason": j["reason"], "sim_reason": r["reason"]})
    base = {(r["symbol"], r["signal_ms"]): r for r in cases["A_real"]}
    out = {"name": name, "window": window, "trades": len(trades), "theta_atr_pct": ctx["theta"],
           "latency_ms": LATENCY_MS, "wait_min": WAIT_MIN,
           "validation_A_vs_jesse": {"same": sum(c["same_legs"] and c["same_reason"]
                                                 for c in check),
                                     "total": len(check),
                                     "max_abs_pnl_diff": max(abs(c["pnl_diff"]) for c in check),
                                     "mismatches": [c for c in check
                                                    if not (c["same_legs"] and c["same_reason"])]},
           "metrics": {c: metrics(rows, base if c in ("B", "C") else None)
                       for c, rows in cases.items()},
           "rows": cases, "seconds": round(time.time() - started)}
    text = json.dumps(out, indent=1, default=float, sort_keys=True)
    out["result_sha256"] = hashlib.sha256(json.dumps(
        {k: v for k, v in out.items() if k != "seconds"}, default=float,
        sort_keys=True).encode()).hexdigest()
    text = json.dumps(out, indent=1, default=float, sort_keys=True)
    (LAB / "reports").mkdir(parents=True, exist_ok=True)
    (LAB / "reports" / f"{name}.json").write_text(text, encoding="utf-8")
    (REPORTS / f"execution_{name}.json").write_text(text, encoding="utf-8")
    return out


if __name__ == "__main__":
    res = main(sys.argv[1], sys.argv[2])
    print("validation A vs Jesse:", {k: v for k, v in res["validation_A_vs_jesse"].items()
                                     if k != "mismatches"})
    for c, m in res["metrics"].items():
        print(c, {k: (round(v, 3) if isinstance(v, float) else v) for k, v in m.items()
                  if k != "by_year"}, {k: round(v, 2) for k, v in m["by_year"].items()})
    print("sha", res["result_sha256"][:16], "seconds", res["seconds"])
