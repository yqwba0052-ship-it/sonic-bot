"""Compare be_delay runs against the base run with the pre-registered criteria.

    python -I be_delay_compare.py BASE.json CAND1.json [CAND2.json ...]
"""

from __future__ import annotations

import json
import statistics as st
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path


def stats(run: dict) -> dict:
    trades = run["trades"]
    pnl = [t["pnl"] for t in trades]
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    top5 = sorted(pnl, reverse=True)[:5]
    years: dict[str, float] = {}
    for t in trades:
        year = datetime.fromtimestamp(t["closed_ms"] / 1000, UTC).strftime("%Y")
        years[year] = round(years.get(year, 0.0) + t["pnl"], 2)
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = -sum(t["pnl"] for t in losses)
    return {
        "trades": len(trades), "open_at_end": run["summary"]["open_at_end"],
        "win_rate": round(len(wins) / len(trades), 4) if trades else None,
        "net": round(sum(pnl), 2),
        "max_drawdown": round(run["summary"]["max_drop"], 4),
        "net_without_top5": round(sum(pnl) - sum(top5), 2),
        "avg_win_usd": round(st.mean([t["pnl"] for t in wins]), 3) if wins else None,
        "avg_loss_usd": round(st.mean([t["pnl"] for t in losses]), 3) if losses else None,
        "avg_win_r": round(st.mean([t["r"] for t in wins if t["r"] is not None]), 3)
        if wins else None,
        "avg_loss_r": round(st.mean([t["r"] for t in losses if t["r"] is not None]), 3)
        if losses else None,
        "avg_r": round(st.mean([t["r"] for t in trades if t["r"] is not None]), 3),
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else None,
        "fees": round(sum(t["fees"] for t in trades), 2),
        "by_year": years,
        "kinds": dict(Counter(t["kind"] for t in trades)),
        "errors": run["summary"]["errors"],
    }


def eligible(base: dict, cand: dict) -> dict:
    checks = {
        "net_above_base": cand["net"] > base["net"],
        "drawdown_within_1.25x": cand["max_drawdown"] <= 1.25 * base["max_drawdown"],
        "net_without_top5_above_base": cand["net_without_top5"] > base["net_without_top5"],
    }
    years_better = sum(1 for y, v in cand["by_year"].items() if v > base["by_year"].get(y, 0.0))
    return {**checks, "eligible": all(checks.values()), "years_better": years_better}


def main() -> None:
    runs = [json.loads(Path(p).read_text()) for p in sys.argv[1:]]
    base = stats(runs[0])
    out = {"base": {"trigger": runs[0]["trigger"], **base}, "candidates": []}
    for run in runs[1:]:
        s = stats(run)
        out["candidates"].append({"trigger": run["trigger"], **s, "checks": eligible(base, s)})
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
