"""Classical Strategy Lab runs.

python -m sonic_research.classical.run train      walk-forward on 2021-06..2023-12 + frozen choices
python -m sonic_research.classical.run holdout    one-time 2024-01..2026-10 check (frozen, locked)
"""

from __future__ import annotations

import hashlib
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from ..data import load_bars, month_range
from ..jesse_audit.filters import load_filters
from .core import Series, ema
from .engine import GRID, Combo, outcome, portfolio, unit_pnl
from .evaluate import FOLDS, HOLDOUT, OOS, TRAIN, choose, metrics, qualifies, qualifies_pooled
from .strategies import RULES, Ctx, signals

LAB = Path("D:/Sonic-Classical-Research")
REPORTS = Path(__file__).resolve().parents[2] / "reports"
COINS = ["SOLUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT", "TRXUSDT", "BNBUSDT", "AVAXUSDT"]
FOUR_H = 4 * 3_600_000
CHOICES = REPORTS / "classical_choices.json"
LOCK = REPORTS / "classical_holdout.lock.json"


def load(symbol: str, first: str, last: str) -> Series:
    b = load_bars(symbol, month_range(first, last), "4h")
    return Series(symbol, b.t, b.o, b.h, b.low, b.c)


def regimes(btc: Series) -> dict[int, str]:
    """Market state at each 4h close: bull / bear / range from BTC vs its EMA200 and slope."""
    e = ema(btc.c, 200)
    out = {}
    for i in range(230, len(btc)):
        slope = e[i] / e[i - 30] - 1
        state = "bull" if btc.c[i] > e[i] and slope > 0.01 else (
            "bear" if btc.c[i] < e[i] and slope < -0.01 else "range")
        out[int(btc.t[i]) + FOUR_H] = state
    return out


def candidates(series: dict[str, Series], window: tuple[int, int]) -> dict:
    """{(strategy, side, coin, combo): [candidate trades]} for entries in window, plus delay
    and cost-x2 variants keyed with 'delay' / per-unit pnl at cost 2."""
    out: dict = {}
    for coin, s0 in series.items():
        for s in (s0, s0.inverted()):
            ctx = Ctx(s)
            for name in RULES:
                sigs = [x for x in signals(s, name, ctx)
                        if window[0] <= int(s.t[x.i]) + FOUR_H < window[1]]
                for combo in GRID:
                    for delay in (False, True):
                        rows = []
                        for sig in sigs:
                            o = outcome(s, sig, combo, delay=delay)
                            if o is None:
                                continue
                            entry_t = int(s.t[o["entry_j"]]) + (0 if delay else FOUR_H)
                            rows.append({"coin": coin, "strategy": name, "side": s.side,
                                         "entry_t": entry_t,
                                         "exit_t": int(s.t[o["exit_j"]]) + FOUR_H,
                                         "entry_raw": o["entry_raw"], "stop": o["stop"],
                                         "pnl_unit": unit_pnl(o["entry_raw"], o["legs"]),
                                         "pnl_unit_cost2": unit_pnl(o["entry_raw"], o["legs"], 2),
                                         "pnl_unit_cost3": unit_pnl(o["entry_raw"], o["legs"], 3),
                                         "reason": o["reason"], "why": sig.reason})
                        out[(name, s.side, coin, combo, delay)] = rows
    return out


def _within(rows: list[dict], w: tuple[int, int]) -> list[dict]:
    return [r for r in rows if w[0] <= r["entry_t"] < w[1]]


def _run(rows: list[dict], filters: dict, cost: str = "pnl_unit", risk: float = 0.5) -> dict:
    res = portfolio([dict(r, pnl_unit=r[cost]) for r in rows], filters, risk_pct=risk)
    return {"trades": res["trades"], "skipped": res["skipped"]}


def _combo_key(c: Combo) -> str:
    return f"rr{c.rr}-{c.exit}-{'htf' if c.htf else 'nohtf'}"


def train() -> dict:
    t0 = time.time()
    filters = load_filters(REPORTS / "jesse_filters_full.json")
    series = {c: load(c, "2020-12", "2023-12") for c in COINS}
    reg = regimes(load("BTCUSDT", "2020-12", "2023-12"))
    cands = candidates(series, TRAIN)
    pairs = sorted({(n, side, coin) for n, side, coin, _, _ in cands})
    combos = {_combo_key(c): c for c in GRID}
    out = {"pairs": {}, "pooled": {}, "sensitivity": {}}
    oos_rows, final_combo = {}, {}
    for pair in pairs:
        rows_oos, folds = [], []
        for sel, test in FOLDS:
            res = {k: metrics(_run(_within(cands[(*pair, c, False)], sel), filters)["trades"])
                   for k, c in combos.items()}
            k = choose(res)
            if k is None:
                folds.append({"combo": None, "net": 0.0})
                continue
            test_rows = _within(cands[(*pair, combos[k], False)], test)
            folds.append({"combo": k, "net": metrics(_run(test_rows, filters)["trades"])["net"]})
            rows_oos += [dict(r, combo=k) for r in test_rows]
        oos_rows[pair] = rows_oos
        m = metrics(_run(rows_oos, filters)["trades"])
        m["net_cost2"] = metrics(_run(rows_oos, filters, "pnl_unit_cost2")["trades"])["net"]
        m["net_cost3"] = metrics(_run(rows_oos, filters, "pnl_unit_cost3")["trades"])["net"]
        delay_rows = [r for f, (sel, test) in zip(folds, FOLDS, strict=True) if f["combo"]
                      for r in _within(cands[(*pair, combos[f["combo"]], True)], test)]
        m["net_delay"] = metrics(_run(delay_rows, filters)["trades"])["net"]
        m["folds_positive"] = sum(1 for f in folds if f["net"] > 0)
        m["signals_oos"] = len(rows_oos)
        m["not_representable"] = sum(1 for x in _run(rows_oos, filters)["skipped"]
                                     if x["why"] == "not_representable")
        full = {k: metrics(_run(cands[(*pair, c, False)], filters)["trades"])
                for k, c in combos.items()}
        final_combo[pair] = choose(full)
        out["pairs"]["|".join(pair)] = {"oos": m, "folds": folds, "qualify": qualifies(m),
                                        "final_combo": final_combo[pair]}
    # pooled per strategy and side (all coins in one $30 account), walk-forward combos
    for name in sorted({p[0] for p in pairs}):
        for side in ("long", "short"):
            rows = [r for p, rs in oos_rows.items() if p[0] == name and p[1] == side for r in rs]
            run = _run(rows, filters)
            m = metrics(run["trades"])
            m["net_cost2"] = metrics(_run(rows, filters, "pnl_unit_cost2")["trades"])["net"]
            m["net_cost3"] = metrics(_run(rows, filters, "pnl_unit_cost3")["trades"])["net"]
            m["net_risk1pct"] = metrics(_run(rows, filters, risk=1.0)["trades"])["net"]
            m["skipped"] = _why(run["skipped"])
            m["by_regime"] = _regime(run["trades"], reg)
            out["pooled"][f"{name}|{side}"] = {"oos": m, "qualify": qualifies_pooled(m)}
            # sensitivity: every combo on the whole training window (in-sample, information)
            out["sensitivity"][f"{name}|{side}"] = {
                k: round(metrics(_run([r for coin in COINS
                                       for r in cands[(name, side, coin, c, False)]],
                                      filters)["trades"])["net"], 3) for k, c in combos.items()}
    qualified = [k for k, v in out["pairs"].items() if v["qualify"]["ok"]]
    out["qualified_pairs"] = qualified
    out["qualified_strategies"] = [k for k, v in out["pooled"].items() if v["qualify"]["ok"]]
    out["candle_value"] = {side: {n: out["pooled"][f"{n}|{side}"]["oos"]["net"]
                                  for n in ("sr_bounce", "sr_bounce_nocandle", "candle_trend")}
                           for side in ("long", "short")}
    # C: best qualified pair per coin (by OOS net); D: all qualified pairs together
    best = {}
    for k in qualified:
        name, side, coin = k.split("|")
        net = out["pairs"][k]["oos"]["net"]
        if coin not in best or net > out["pairs"][best[coin]]["oos"]["net"]:
            best[coin] = k
    for label, keys in (("C", list(best.values())), ("D", qualified),
                        ("C_long", [k for k in best.values() if "|long|" in k]),
                        ("D_long", [k for k in qualified if "|long|" in k])):
        rows = [r for k in keys for r in oos_rows[tuple(k.split("|"))]]
        run = _run(rows, filters)
        m = metrics(run["trades"])
        m["by_regime"] = _regime(run["trades"], reg)
        m["skipped"] = _why(run["skipped"])
        out[label] = {"pairs": keys, "oos": m,
                      "note": "selection used this window: not an out-of-sample result"}
    out["sonic_A_oos"] = _sonic("jesse_full.json", OOS)
    out["oos_window"] = [_iso(OOS[0]), _iso(OOS[1])]
    out["seconds"] = round(time.time() - t0)
    choices = {"qualified_pairs": qualified,
               "final_combo": {k: final_combo[tuple(k.split("|"))] for k in qualified},
               "C": best,
               "strategies_all": {"|".join(p): final_combo[p] for p in pairs}}
    if not CHOICES.exists():
        CHOICES.write_text(json.dumps(choices, indent=1, sort_keys=True), encoding="utf-8")
    out["choices_sha256"] = hashlib.sha256(CHOICES.read_bytes()).hexdigest()
    _save("classical_train", out)
    return out


def _why(skipped: list[dict]) -> dict:
    counts: dict[str, int] = {}
    for x in skipped:
        counts[x["why"]] = counts.get(x["why"], 0) + 1
    return counts


def _regime(trades: list[dict], reg: dict[int, str]) -> dict:
    out: dict[str, float] = {}
    for t in trades:
        k = reg.get(t["entry_t"] if t["entry_t"] in reg else t["entry_t"] + FOUR_H, "unknown")
        out[k] = out.get(k, 0.0) + t["pnl"]
    return out


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%d")


def _sonic(report: str, window: tuple[int, int]) -> dict:
    r = json.loads((REPORTS / report).read_text(encoding="utf-8"))
    trades = [x["right"] for x in r["comparisons"]["R1_vs_R2"]["rows"]]
    rows = [{"coin": t["symbol"], "strategy": "sonic", "entry_t": t["opened_ms"],
             "exit_t": t["closed_ms"], "pnl": t["pnl"], "qty": t["legs"][0]["qty"],
             "entry_raw": t["legs"][0]["raw"], "stop": t["stop"]} for t in trades
            if window[0] <= t["opened_ms"] < window[1]]
    m = metrics(rows)
    m["note"] = "Sonic's own $30 account (1% risk) from the verified Jesse run, trades in window"
    return m


def _save(name: str, out: dict) -> None:
    text = json.dumps(out, indent=1, default=lambda x: None if x != x else float(x),
                      sort_keys=True)
    LAB.joinpath("reports").mkdir(parents=True, exist_ok=True)
    (LAB / "reports" / f"{name}.json").write_text(text, encoding="utf-8")
    (REPORTS / f"{name}.json").write_text(text, encoding="utf-8")


def holdout() -> dict:
    if LOCK.exists():
        raise SystemExit(f"hold-out already run ({LOCK}); results stay as they are")
    from .freeze import check_frozen
    check_frozen()
    t0 = time.time()
    choices = json.loads(CHOICES.read_text(encoding="utf-8"))
    filters = load_filters(REPORTS / "jesse_filters_full.json")
    series = {c: load(c, "2023-06", "2026-10") for c in COINS}
    reg = regimes(load("BTCUSDT", "2023-01", "2026-10"))
    cands = candidates(series, HOLDOUT)
    combos = {_combo_key(c): c for c in GRID}
    out = {"strategies": {}, "pairs": {}}

    def rows_for(keys: list[str], table: dict) -> list[dict]:
        rows = []
        for k in keys:
            name, side, coin = k.split("|")
            if table.get(k):
                rows += cands[(name, side, coin, combos[table[k]], False)]
        return rows

    for name in RULES:
        for side in ("long", "short"):
            keys = [f"{name}|{side}|{c}" for c in COINS]
            run = _run(rows_for(keys, choices["strategies_all"]), filters)
            m = metrics(run["trades"])
            m["skipped"] = _why(run["skipped"])
            out["strategies"][f"{name}|{side}"] = m
    for label, keys in (("C", list(choices["C"].values())), ("D", choices["qualified_pairs"]),
                        ("C_long", [k for k in choices["C"].values() if "|long|" in k]),
                        ("D_long", [k for k in choices["qualified_pairs"] if "|long|" in k])):
        run = _run(rows_for(keys, choices["final_combo"]), filters)
        m = metrics(run["trades"])
        m["by_regime"] = _regime(run["trades"], reg)
        m["skipped"] = _why(run["skipped"])
        out[label] = {"pairs": keys, "metrics": m}
    for k in choices["qualified_pairs"]:
        out["pairs"][k] = metrics(_run(rows_for([k], choices["final_combo"]), filters)["trades"])
    out["sonic_A"] = _sonic("jesse_holdout.json", HOLDOUT)
    out["seconds"] = round(time.time() - t0)
    _save("classical_holdout", out)
    digest = hashlib.sha256((REPORTS / "classical_holdout.json").read_bytes()).hexdigest()
    LOCK.write_text(json.dumps({"at": datetime.now(UTC).isoformat(timespec="seconds"),
                                "result_sha256": digest}, indent=1), encoding="utf-8")
    return out


if __name__ == "__main__":
    res = train() if sys.argv[1] == "train" else holdout()
    print(json.dumps({k: v for k, v in res.items() if k in ("qualified_pairs",
                                                              "qualified_strategies", "seconds")},
                     indent=1))
    _ = np
