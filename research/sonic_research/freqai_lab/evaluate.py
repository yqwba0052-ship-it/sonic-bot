"""Cases A/B/C on Sonic's verified Jesse bridge, metrics, and the pre-registered verdict.

A = Sonic as is. B = a Sonic entry is taken only if FreqAI predicts > 0. C = all entries kept, but
when they compete in one bar they are ordered by the prediction (highest first) instead of the
fixed symbol order. The bridge and planner are not edited: the choice is injected around them.
Run with the Jesse environment: C:/Users/sa/tools/jesse-venv/Scripts/python.
"""

from __future__ import annotations

import csv
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from ..jesse_audit import bridge
from ..jesse_audit.costs import costs

FOUR_H = 4 * 3_600_000
THRESHOLD = 0.0  # fixed before any result (design)


@dataclass
class Decisions:
    rejected: list[dict] = field(default_factory=list)
    missing: list[dict] = field(default_factory=list)
    reordered: int = 0


def load_predictions(folder: Path, symbols: list[str]) -> dict[tuple[str, int], float]:
    """(symbol, close_ms of the 4h bar) -> prediction; only rows FreqAI marked do_predict == 1."""
    out = {}
    for symbol in symbols:
        path = Path(folder) / f"{symbol[:-4]}_USDT.csv"
        with path.open(encoding="utf-8") as f:
            for row in csv.DictReader(f):
                pred = row["&-fwd_ret"]
                if pred in ("", "nan") or float(row["do_predict"]) != 1:
                    continue
                opened = int(datetime.fromisoformat(row["date"]).timestamp() * 1000)
                out[(symbol, opened + FOUR_H)] = float(pred)
    return out


def choose(case: str, candidates: list, preds: dict, close_ms: int, log: Decisions) -> list:
    if case == "A":
        return candidates
    if case == "B":
        kept = []
        for c in candidates:
            p = preds.get((c[0], close_ms))
            if p is None:
                log.missing.append({"symbol": c[0], "at_ms": close_ms})
                kept.append(c)  # no prediction: Sonic's own decision stands (counted)
            elif p > THRESHOLD:
                kept.append(c)
            else:
                log.rejected.append({"symbol": c[0], "at_ms": close_ms, "pred": p})
        return kept
    if case == "C":
        order = sorted(range(len(candidates)), key=lambda i: (
            preds.get((candidates[i][0], close_ms)) is None,
            -(preds.get((candidates[i][0], close_ms)) or 0.0), i))
        ranked = [candidates[i] for i in order]
        if ranked != candidates:
            log.reordered += 1
        return ranked
    raise ValueError(case)


def run_case(case: str, minutes: dict, coins: list[str], filters: dict, warmup_bars: int,
             limits, preds: dict) -> tuple[bridge.Ledger, Decisions]:
    """Sonic in the Jesse bridge with the case's choice injected; the bridge code is unchanged."""
    log, now = Decisions(), {"ms": 0}
    plan_bar, plan_whole_bar = bridge.plan_bar, bridge._plan_whole_bar

    def timed(strategy, close_ms):
        now["ms"] = close_ms
        return plan_whole_bar(strategy, close_ms)

    def chosen(limits_, filters_, realized, open_trades, exits, candidates):
        return plan_bar(limits_, filters_, realized, open_trades, exits,
                        choose(case, candidates, preds, now["ms"], log))

    bridge.plan_bar, bridge._plan_whole_bar = chosen, timed
    try:
        ledger = bridge.run_bridge(minutes, coins, filters, warmup_bars, limits=limits)
    finally:
        bridge.plan_bar, bridge._plan_whole_bar = plan_bar, plan_whole_bar
    return ledger, log


def _day(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%d")


def metrics(trades: list[dict], capital: float = 30.0) -> dict:
    ordered = sorted(trades, key=lambda t: t["closed_ms"])
    pnls = [costs(t["legs"])["pnl"] for t in ordered]
    equity = peak = capital
    worst = 0.0
    for p in pnls:
        equity += p
        peak = max(peak, equity)
        worst = max(worst, 1 - equity / peak)
    wins, losses = [p for p in pnls if p > 0], [p for p in pnls if p <= 0]
    by_year, by_month, by_coin = defaultdict(float), defaultdict(float), defaultdict(float)
    for t, p in zip(ordered, pnls, strict=True):
        by_year[_day(t["closed_ms"])[:4]] += p
        by_month[_day(t["closed_ms"])[:7]] += p
        by_coin[t["symbol"]] += p
    return {
        "trades": len(pnls), "win_rate_pct": round(100 * len(wins) / len(pnls), 1) if pnls else 0,
        "net": sum(pnls), "profit_factor": (sum(wins) / -sum(losses)) if sum(losses) else None,
        "max_dd_pct": round(100 * worst, 2),
        "avg_win": sum(wins) / len(wins) if wins else 0.0,
        "avg_loss": sum(losses) / len(losses) if losses else 0.0,
        "trading_days": len({_day(t["opened_ms"]) for t in ordered}),
        "by_year": dict(by_year), "by_month": dict(by_month), "by_coin": dict(by_coin),
    }


def verdict(base: dict, case: dict, stable: dict) -> dict:
    """The design's success rules, unchanged: all must hold."""
    better_net = case["net"] > base["net"]
    safer = (case["max_dd_pct"] <= 0.8 * base["max_dd_pct"]
             and case["net"] >= 0.9 * base["net"])
    checks = {
        "1_net_or_risk": better_net or safer,
        "2_two_of_three_years": stable["years_better"] >= 2,
        "3_not_one_trade_or_coin": stable["after_drop_best_trade"] > 0
        and stable["after_drop_each_coin_min"] > 0,
        "4_reproducible": bool(stable["reproducible"]),
        "5_no_leak": bool(stable["leak_tests"]),
    }
    return {"promising": all(checks.values()), "checks": checks}
