"""Phase 2: longer history and stress tests. python -m sonic_research.phase2

- History from 2021-01 (includes the 2022 bear market); train to 2024-06, test after.
- Top sets on training are re-run with double slippage and a higher fee.
- Each top set is checked year by year and against its neighbours (one setting changed):
  a real edge sits on a plateau, a lucky one on a narrow peak.
- The learning walk runs with 6- and 12-month memories.
"""

from __future__ import annotations

import json
import time
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from .backtest import FEE, SLIPPAGE, Trade, simulate
from .data import SYMBOLS, load_bars, load_funding, month_range
from .metrics import summarize
from .run import MIN_TRADES, grid, learning_walk, month_ms
from .strategies import Params, btc_trend, build

START, TRAIN_LAST, END = "2021-01", "2024-06", "2026-10"
REPORT = Path(__file__).resolve().parent.parent / "reports" / "phase2.json"


def run_set(params, bars, funding, regime, slippage=SLIPPAGE, fee=FEE) -> list[Trade]:
    trades: list[Trade] = []
    for symbol in SYMBOLS:
        signals = build(bars[symbol], params, regime[symbol])
        trades.extend(
            simulate(bars[symbol], signals, params.target_r, params.max_hold, funding[symbol],
                     slippage=slippage, fee=fee)
        )
    return trades


def by_year(trades: list[Trade]) -> dict[str, dict[str, float]]:
    groups: dict[str, list[Trade]] = defaultdict(list)
    for trade in trades:
        year = datetime.fromtimestamp(trade.entry_t / 1000, UTC).year
        groups[str(year)].append(trade)
    return {
        year: {"trades": len(items), "avg_r": round(float(np.mean([t.r for t in items])), 3)}
        for year, items in sorted(groups.items())
    }


def neighbours(params: Params, all_params: list[Params]) -> list[Params]:
    fields = ("a", "b", "atr_k", "target_r", "max_hold", "regime")
    result = []
    for other in all_params:
        if other.family != params.family or other == params:
            continue
        changed = sum(getattr(other, f) != getattr(params, f) for f in fields)
        if changed == 1:
            result.append(other)
    return result


def main() -> None:
    started = time.time()
    months = month_range(START, END)
    split = month_ms("2024-07")
    bars = {s: load_bars(s, months) for s in SYMBOLS}
    funding = {s: load_funding(s, months) for s in SYMBOLS}
    regime = {s: btc_trend(bars["BTCUSDT"], bars[s]) for s in SYMBOLS}
    print("first bar per coin:", {s: datetime.fromtimestamp(b.t[0] / 1000, UTC).date().isoformat()
                                  for s, b in bars.items()})
    all_params = grid()
    results = {p.name: run_set(p, bars, funding, regime) for p in all_params}
    by_name = {p.name: p for p in all_params}
    print(f"simulated {len(results)} sets in {time.time() - started:.0f}s")

    rows = []
    for name, trades in results.items():
        train = summarize([t for t in trades if t.exit_t <= split])
        test = summarize([t for t in trades if t.entry_t >= split])
        rows.append({"set": name, "train": train, "test": test})
    eligible = sorted(
        (r for r in rows if r["train"]["trades"] >= MIN_TRADES),
        key=lambda r: r["train"]["avg_r"],
        reverse=True,
    )

    top = []
    for row in eligible[:8]:
        params = by_name[row["set"]]
        harsh = run_set(params, bars, funding, regime, slippage=2 * SLIPPAGE, fee=0.0006)
        near = neighbours(params, all_params)
        near_test = [
            summarize([t for t in results[n.name] if t.entry_t >= split])["avg_r"] for n in near
        ]
        top.append({
            "set": row["set"],
            "train": row["train"],
            "test": row["test"],
            "test_harsh_costs": summarize([t for t in harsh if t.entry_t >= split]),
            "years": by_year(results[row["set"]]),
            "neighbours_test_avg_r": [round(v, 3) for v in near_test],
            "neighbours_positive": f"{sum(v > 0 for v in near_test)}/{len(near_test)}",
        })

    test_months = [m for m in months if month_ms(m) >= split]
    walks = {}
    for memory in (6, 12):
        trades, log = learning_walk(results, test_months, memory, months)
        walks[f"memory_{memory}m"] = {"test": summarize(trades), "years": by_year(trades),
                                      "log": log}

    btc = bars["BTCUSDT"]
    btc_test = btc.c[btc.t >= split]
    report = {
        "period": {"train": f"{START}..{TRAIN_LAST}", "test": f"2024-07..{END}"},
        "sets_tested": len(results),
        "sets_positive_train": sum(r["train"]["avg_r"] > 0 for r in eligible),
        "sets_positive_test": sum(r["test"]["avg_r"] > 0 for r in eligible),
        "eligible": len(eligible),
        "top": top,
        "learning": walks,
        "btc_buy_hold_test": float(btc_test[-1] / btc_test[0] - 1),
        "seconds": round(time.time() - started),
    }
    REPORT.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("period", "sets_tested", "eligible",
                                             "sets_positive_train", "sets_positive_test",
                                             "btc_buy_hold_test")}))
    for item in top:
        tr, te, hc = item["train"], item["test"], item["test_harsh_costs"]
        print(f"{item['set']:44} train avgR={tr['avg_r']:+.3f} n={tr['trades']} | test "
              f"avgR={te['avg_r']:+.3f} n={te['trades']} ret={te['total_return']:+.1%} "
              f"DD={te['max_drawdown']:.1%} | harsh avgR={hc['avg_r']:+.3f} | "
              f"neighbours+ {item['neighbours_positive']}")
        print("    years:", {y: v["avg_r"] for y, v in item["years"].items()})
    for name, walk in walks.items():
        w = walk["test"]
        print(f"learning {name}: n={w['trades']} avgR={w['avg_r']:+.3f} "
              f"ret={w['total_return']:+.1%} DD={w['max_drawdown']:.1%} "
              f"years={ {y: v['avg_r'] for y, v in walk['years'].items()} }")
    print(f"report: {REPORT}")


if __name__ == "__main__":
    main()
