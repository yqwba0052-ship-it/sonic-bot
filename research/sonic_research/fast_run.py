"""Sonic Fast: choose settings on 2021-2023, freeze them, judge them once on 2024-2026.

python -m sonic_research.fast_run train      # grid on the training years, writes the frozen file
python -m sonic_research.fast_run holdout    # one run of the frozen settings (refuses a second)

Design: docs/superpowers/specs/2026-11-14-sonic-fast-design.md. Research only.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import itertools
import json
import sys
from dataclasses import asdict, replace
from pathlib import Path

from . import fast
from .data import load_bars, month_range

COINS = ("SOLUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT", "SUIUSDT", "TRXUSDT", "BNBUSDT", "AVAXUSDT")
TIMEFRAMES = {"15m": (900_000, 96), "30m": (1_800_000, 48), "1h": (3_600_000, 24)}
MONTHS = month_range("2020-06", "2026-10")
TRAIN = (dt.date(2021, 1, 1), dt.date(2023, 12, 31))
HOLDOUT = (dt.date(2024, 1, 1), dt.date(2026, 10, 31))
COSTS = (0.0014, 0.0020, 0.0030)
GRID = [fast.Params(L, k, ks, kt, guard) for L, k, ks, kt, guard in itertools.product(
    (30, 35, 40), (3, 6), (1.0, 1.5, 2.0), (1.0, 1.5, 2.0, 3.0), ("fixed", "vol"))]
LIMITS = {"profit_factor": 1.2, "max_drawdown": 0.10, "per_suitable_day": 1.5}
CAPITAL = 30.0
REPORTS = Path(__file__).resolve().parent.parent / "reports"
FROZEN = REPORTS / "sonic_fast_frozen.json"
OUT = REPORTS / "sonic_fast.json"
SONIC_REPLAY = Path(__file__).resolve().parents[2] / "server" / "replay" / "trades.json"


def ms(day: dt.date) -> int:
    return int(dt.datetime(day.year, day.month, day.day, tzinfo=dt.UTC).timestamp() * 1000)


def select(rows: list[dict]) -> dict | None:
    """The highest net profit after costs among settings that pass every training limit."""
    ok = [r for r in rows
          if (r["metrics"]["profit_factor"] or 0) >= LIMITS["profit_factor"]
          and r["metrics"]["max_drawdown"] <= LIMITS["max_drawdown"]
          and r["metrics"]["per_suitable_day"] >= LIMITS["per_suitable_day"]]
    return max(ok, key=lambda r: r["metrics"]["net_pnl"]) if ok else None


def freeze(choice: dict, path: Path) -> str:
    path.write_text(json.dumps(choice, indent=1, default=str), encoding="utf-8")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verdict(m: dict, combined_worst_pct: float, sonic_worst_pct: float) -> tuple[bool, list[str]]:
    """Acceptance on the hold-out, fixed in advance (never lowered)."""
    why = []
    if m["net_pnl"] <= 0:
        why.append("net profit after costs is not positive")
    if (m["profit_factor"] or 0) < 1.2:
        why.append("profit factor under 1.2")
    if m["max_drawdown"] > 0.10:
        why.append("drawdown over 10%")
    if m["per_suitable_day"] < 1.5:
        why.append("fewer than 1.5 trades per suitable day")
    if combined_worst_pct < sonic_worst_pct - 2.0:
        why.append("worst month with Sonic is more than 2 points worse than Sonic alone")
    return not why, why


def worst_month_pct(closed: list[tuple[int, float]], capital: float) -> float:
    """Worst month's result as % of the equity at that month's start (closed trades only)."""
    equity, months, start = capital, {}, {}
    for closed_ms, pnl in sorted(closed):
        key = dt.datetime.fromtimestamp(closed_ms / 1000, dt.UTC).strftime("%Y-%m")
        start.setdefault(key, equity)
        months[key] = months.get(key, 0.0) + pnl
        equity += pnl
    return min((v / start[k] * 100 for k, v in months.items()), default=0.0)


def load(tf: str) -> dict:
    """Bars, regime, indicators and fast-bar times per coin, from Binance public data."""
    bar_ms, _ = TIMEFRAMES[tf]
    btc4 = load_bars("BTCUSDT", MONTHS, "4h")
    data: dict = {"bars": {}, "reg": {}, "ind": {}, "t": {}}
    for coin in COINS:
        bars = load_bars(coin, MONTHS, tf)
        if len(bars) < 200:
            continue
        data["bars"][coin] = bars
        data["reg"][coin] = fast.regime(bars.t, bar_ms, load_bars(coin, MONTHS, "4h"), btc4)
        data["ind"][coin] = fast.indicators(bars)
        data["t"][coin] = bars.t
    data["suitable"] = fast.suitable_days(data["reg"], data["t"])
    return data


def sonic_trades() -> list[dict]:
    return json.loads(SONIC_REPLAY.read_text(encoding="utf-8"))["trades"]


def evaluate(data: dict, tf: str, p: fast.Params, period: tuple, cost: float,
             cache: dict, sonic: list[dict]) -> tuple[dict, list]:
    bar_ms, hold = TIMEFRAMES[tf]
    key = (p.L, p.k, p.guard, p.kt)
    if key not in cache:
        cache[key] = [c for coin, b in data["bars"].items()
                      for c in fast.candidates(coin, b, bar_ms, data["reg"][coin], p,
                                               ind=data["ind"][coin])]
    cands = [replace(c, stop=c.entry - p.ks * c.atr) for c in cache[key]]
    trades = fast.run(cands, data["bars"], hold, cost, sonic, ms(period[0]),
                      ms(period[1] + dt.timedelta(days=1)), CAPITAL)
    suitable = {d for d in data["suitable"] if period[0] <= d <= period[1]}
    return fast.metrics(trades, suitable, period[0], period[1], CAPITAL, cost), trades


def train() -> dict:
    sonic = sonic_trades()
    frozen: dict = {"made": dt.datetime.now(dt.UTC).isoformat(), "period": [str(d) for d in TRAIN],
                    "cost": COSTS[0], "limits": LIMITS, "timeframes": {}}
    for tf in TIMEFRAMES:
        data, cache, rows = load(tf), {}, []
        for p in GRID:
            m, _ = evaluate(data, tf, p, TRAIN, COSTS[0], cache, sonic)
            rows.append({"params": asdict(p), "metrics": m})
        choice = select(rows)
        best = sorted(rows, key=lambda r: -r["metrics"]["net_pnl"])[:5]
        frozen["timeframes"][tf] = {"choice": choice, "top5_by_net": best}
        print(tf, "chosen:", choice["params"] if choice else None, flush=True)
    digest = freeze(frozen, FROZEN)
    print("frozen", FROZEN.name, digest)
    return frozen


def holdout(frozen: Path = FROZEN, out: Path = OUT, force: bool = False) -> dict:
    if not frozen.exists():
        raise SystemExit("frozen settings missing: run `train` first")
    if out.exists() and not force:
        raise SystemExit("the hold-out was already run once; its result stands")
    digest = hashlib.sha256(frozen.read_bytes()).hexdigest()
    settings = json.loads(frozen.read_text(encoding="utf-8"))
    sonic = sonic_trades()
    lo, hi = ms(HOLDOUT[0]), ms(HOLDOUT[1] + dt.timedelta(days=1))
    sonic_hold = [(s["closed_ms"], s["pnl"]) for s in sonic if lo <= s["closed_ms"] < hi]
    sonic_worst = worst_month_pct(sonic_hold, CAPITAL)
    result: dict = {"frozen_sha256": digest, "period": [str(d) for d in HOLDOUT],
                    "sonic_alone_worst_month_pct": round(sonic_worst, 2), "timeframes": {}}
    for tf, entry in settings["timeframes"].items():
        if entry["choice"] is None:
            result["timeframes"][tf] = {"accepted": False,
                                        "why": ["no setting passed the training limits"]}
            continue
        p = fast.Params(**entry["choice"]["params"])
        data, cache, by_cost = load(tf), {}, {}
        for cost in COSTS:
            m, trades = evaluate(data, tf, p, HOLDOUT, cost, cache, sonic)
            by_cost[str(cost)] = m
            if cost == COSTS[0]:
                both = sonic_hold + [(t.closed_ms, t.pnl) for t in trades]
                combined_worst = worst_month_pct(both, 2 * CAPITAL)
                ok, why = verdict(m, combined_worst, sonic_worst)
        result["timeframes"][tf] = {"params": entry["choice"]["params"], "by_cost": by_cost,
                                    "combined_worst_month_pct": round(combined_worst, 2),
                                    "accepted": ok, "why": why}
        print(tf, "accepted" if ok else "rejected", why, flush=True)
    out.write_text(json.dumps(result, indent=1, default=str), encoding="utf-8")
    return result


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command == "train":
        train()
    elif command == "holdout":
        holdout(force="--force" in sys.argv)
    else:
        raise SystemExit("usage: python -m sonic_research.fast_run train|holdout")
