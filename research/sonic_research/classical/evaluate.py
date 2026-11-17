"""Metrics, walk-forward choice and the design's qualification rules (applied as written)."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime


def _ms(day: str) -> int:
    return int(datetime.fromisoformat(day).replace(tzinfo=UTC).timestamp() * 1000)


TRAIN = (_ms("2021-06-01"), _ms("2024-01-01"))
FOLDS = [  # (selection window, test window), entry times
    ((_ms("2021-06-01"), _ms("2022-07-01")), (_ms("2022-07-01"), _ms("2023-01-01"))),
    ((_ms("2021-06-01"), _ms("2023-01-01")), (_ms("2023-01-01"), _ms("2023-07-01"))),
    ((_ms("2021-06-01"), _ms("2023-07-01")), (_ms("2023-07-01"), _ms("2024-01-01"))),
]
OOS = (FOLDS[0][1][0], FOLDS[-1][1][1])
HOLDOUT = (_ms("2024-01-01"), _ms("2026-11-01"))


def _day(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%d")


def metrics(trades: list[dict], capital: float = 30.0) -> dict:
    ordered = sorted(trades, key=lambda t: t["exit_t"])
    pnls = [t["pnl"] for t in ordered]
    equity = peak = capital
    worst = 0.0
    for p in pnls:
        equity += p
        peak = max(peak, equity)
        worst = max(worst, 1 - equity / peak)
    wins, losses = [p for p in pnls if p > 0], [p for p in pnls if p <= 0]
    by_year, by_month, by_coin = defaultdict(float), defaultdict(float), defaultdict(float)
    for t in ordered:
        by_year[_day(t["exit_t"])[:4]] += t["pnl"]
        by_month[_day(t["exit_t"])[:7]] += t["pnl"]
        by_coin[t["coin"]] += t["pnl"]
    r = [t["pnl"] / (t["qty"] * abs(t["entry_raw"] - t["stop"])) for t in ordered
         if t["qty"] and t["entry_raw"] != t["stop"]]
    net = sum(pnls)
    return {
        "trades": len(pnls), "net": net, "win_rate_pct": 100 * len(wins) / len(pnls) if pnls else 0,
        "profit_factor": (sum(wins) / -sum(losses)) if sum(losses) < 0 else (
            float("inf") if wins else 0.0),
        "max_dd_pct": 100 * worst, "avg_r": sum(r) / len(r) if r else 0.0,
        "net_without_best": net - max(pnls) if pnls and max(pnls) > 0 else net,
        "trading_days": len({_day(t["entry_t"]) for t in ordered}),
        "months_positive": sum(1 for v in by_month.values() if v > 0), "months": len(by_month),
        "by_year": dict(by_year), "by_month": dict(by_month), "by_coin": dict(by_coin),
    }


def choose(results: dict) -> str | None:
    """Best net among combos with at least 5 trades in the selection window."""
    ok = {k: v for k, v in results.items() if v["trades"] >= 5}
    return max(ok, key=lambda k: (ok[k]["net"], k)) if ok else None


def qualifies(m: dict) -> dict:
    checks = {"trades>=10": m["trades"] >= 10, "net>0": m["net"] > 0,
              "pf>=1.3": m["profit_factor"] >= 1.3, "net_cost2>0": m["net_cost2"] > 0,
              "net_without_best>0": m["net_without_best"] > 0, "dd<=20%": m["max_dd_pct"] <= 20,
              "folds_positive>=2": m["folds_positive"] >= 2}
    return {"ok": all(checks.values()), "checks": checks}


def qualifies_pooled(m: dict) -> dict:
    checks = {"trades>=30": m["trades"] >= 30, "net>0": m["net"] > 0,
              "pf>=1.2": m["profit_factor"] >= 1.2, "net_cost2>0": m["net_cost2"] > 0,
              "net_without_best>0": m["net_without_best"] > 0}
    return {"ok": all(checks.values()), "checks": checks}
