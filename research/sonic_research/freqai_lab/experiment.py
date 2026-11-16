"""One FreqAI experiment: cases A/B/C on real Binance minutes (Jesse data on D:), with metrics,
trade-level changes, stability checks and the pre-registered verdict.

python -m sonic_research.freqai_lab.experiment <predictions-name> pilot|train
(Jesse environment). Results: D:/Sonic-FreqAI/reports/<name>.json and
research/reports/freqai_<name>.json
"""

from __future__ import annotations

import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from ..data import month_range
from ..engine_replay import Limits
from ..jesse_audit.costs import costs
from ..jesse_audit.data_1m import load_minutes
from ..jesse_audit.filters import load_filters
from . import BTC, COINS, LAB
from .evaluate import load_predictions, metrics, run_case, verdict

REPORTS = Path(__file__).resolve().parents[2] / "reports"
JESSE_DATA = Path("D:/Sonic-Jesse-Research")
FOUR_H = 4 * 3_600_000
WINDOWS = {  # name: (coins, warm-up first month, start, end)
    "pilot": (["SOLUSDT", "TRXUSDT"], "2022-07", "2023-01", "2023-06"),
    "train": (COINS, "2020-12", "2021-06", "2023-12"),
}


def _ms(month: str) -> int:
    return int(datetime.strptime(month, "%Y-%m").replace(tzinfo=UTC).timestamp() * 1000)


def _key(t: dict) -> tuple[str, int]:
    return t["symbol"], int(t["opened_ms"])


def changes(base: list[dict], case: list[dict]) -> dict:
    """Trade-level difference of a case against A: dropped, added, and changed common trades."""
    pb = {_key(t): costs(t["legs"])["pnl"] for t in base}
    pc = {_key(t): costs(t["legs"])["pnl"] for t in case}
    items = [(k, -pb[k], "dropped") for k in pb.keys() - pc.keys()]
    items += [(k, pc[k], "added") for k in pc.keys() - pb.keys()]
    items += [(k, pc[k] - pb[k], "changed") for k in pb.keys() & pc.keys()
              if abs(pc[k] - pb[k]) > 1e-12]
    improved = sum(1 for _, d, _ in items if d > 0)
    worsened = sum(1 for _, d, _ in items if d < 0)
    return {"items": [{"symbol": k[0], "opened_ms": k[1], "effect": d, "kind": kind}
                      for k, d, kind in sorted(items, key=lambda x: x[1])],
            "improved": improved, "worsened": worsened,
            "dropped_losers": sum(1 for k, d, kind in items if kind == "dropped" and d > 0),
            "dropped_winners": sum(1 for k, d, kind in items if kind == "dropped" and d < 0)}


def stability(base_m: dict, case_m: dict, change: dict) -> dict:
    diff = case_m["net"] - base_m["net"]
    years = sorted(set(base_m["by_year"]) | set(case_m["by_year"]))
    by_year = {y: case_m["by_year"].get(y, 0.0) - base_m["by_year"].get(y, 0.0) for y in years}
    coins = sorted(set(base_m["by_coin"]) | set(case_m["by_coin"]))
    by_coin = {c: case_m["by_coin"].get(c, 0.0) - base_m["by_coin"].get(c, 0.0) for c in coins}
    best = max((i["effect"] for i in change["items"]), default=0.0)
    return {"diff": diff, "by_year": by_year, "by_coin": by_coin,
            "years_better": sum(1 for v in by_year.values() if v > 1e-12),
            "after_drop_best_trade": diff - max(best, 0.0),
            "after_drop_each_coin_min": min((diff - v for v in by_coin.values()), default=diff)}


def main(name: str, window: str, reproducible: bool, leak_tests: bool) -> dict:
    coins, first, start, end = WINDOWS[window]
    months = month_range(first, end)
    warmup_bars = (_ms(start) - _ms(first)) // FOUR_H
    limits = Limits(symbols=coins)
    filters = load_filters(REPORTS / "jesse_filters_full.json")
    started = time.time()
    minutes = {s: load_minutes(s, months, cache_dir=JESSE_DATA) for s in coins + [BTC]}
    preds = load_predictions(LAB / "predictions" / name, coins)
    out = {"name": name, "window": window, "coins": coins, "start": start, "end": end,
           "predictions": len(preds), "cases": {}}
    trades = {}
    for case in ("A", "B", "C"):
        ledger, log = run_case(case, minutes, coins, filters, warmup_bars, limits, preds)
        trades[case] = ledger.trades
        out["cases"][case] = {"metrics": metrics(ledger.trades), "rejected": log.rejected,
                              "missing_predictions": log.missing, "reordered_bars": log.reordered,
                              "open_at_end": len(ledger.open_at_end),
                              "plan_mismatch": ledger.plan_mismatch}
    base = out["cases"]["A"]["metrics"]
    for case in ("B", "C"):
        change = changes(trades["A"], trades[case])
        stab = stability(base, out["cases"][case]["metrics"], change)
        out["cases"][case].update(changes=change, stability=stab, verdict=verdict(
            base, out["cases"][case]["metrics"],
            dict(stab, reproducible=reproducible, leak_tests=leak_tests)))
    out["seconds"] = round(time.time() - started)
    text = json.dumps(out, indent=1, default=float)
    (LAB / "reports").mkdir(parents=True, exist_ok=True)
    (LAB / "reports" / f"{name}.json").write_text(text, encoding="utf-8")
    (REPORTS / f"freqai_{name}.json").write_text(text, encoding="utf-8")
    return out


if __name__ == "__main__":
    result = main(sys.argv[1], sys.argv[2], reproducible=sys.argv[3] == "yes",
                  leak_tests=sys.argv[4] == "yes")
    for case, part in result["cases"].items():
        m = part["metrics"]
        print(case, {k: (round(v, 3) if isinstance(v, float) else v) for k, v in m.items()
                     if k in ("trades", "win_rate_pct", "net", "profit_factor", "max_dd_pct")},
              "rejected", len(part["rejected"]), "missing", len(part["missing_predictions"]),
              part.get("verdict", {}).get("promising"))
