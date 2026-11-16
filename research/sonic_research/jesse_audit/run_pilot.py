"""Jesse audit runs: R0 (our replay) vs R1 (Jesse on minutes made from our hours: engine parity
only) vs R2 (Jesse on real Binance minutes). Training data only.

python -m sonic_research.jesse_audit.run_pilot                 small pilot (frozen choice)
python -m sonic_research.jesse_audit.run_pilot --estimate      full run: size check, HEAD only
python -m sonic_research.jesse_audit.run_pilot --full --data-dir E:/sonic_1m
(run with the Jesse environment's python)
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import platform
import time
from dataclasses import asdict
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path

import numpy as np

from ..data import Bars, load_bars, month_range
from ..engine_replay import Limits, live_filters, replay
from . import DISK_BUDGET_BYTES, full
from .bridge import BTC, jesse_inputs, reset_ledger, run_bridge
from .compare import match, normalize_engine, normalize_jesse, summary
from .costs import costs
from .data_1m import CACHE, compare_to_bars, load_minutes
from .explain import explain_rows, prove_minute_path
from .filters import load_filters, save_filters
from .offline import block_network, disk_free_bytes
from .synth import minutes_from_hours

REPORTS = Path(__file__).resolve().parents[2] / "reports"
CHOICE = REPORTS / "jesse_pilot_choice.json"
ESTIMATE = REPORTS / "jesse_full_estimate.json"
VENV = Path.home() / "tools" / "jesse-venv"
FOUR_H = 4 * 3_600_000
WARMUP_MONTHS = 6


def _ms(month: str) -> int:
    return int(datetime.strptime(month, "%Y-%m").replace(tzinfo=UTC).timestamp() * 1000)


def _shift(month: str, months: int) -> str:
    year, m = map(int, month.split("-"))
    index = year * 12 + m - 1 + months
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def load_choice() -> dict:
    data = json.loads(CHOICE.read_text(encoding="utf-8"))
    body = {k: v for k, v in data.items() if k != "sha256"}
    text = json.dumps(body, indent=1, sort_keys=True)
    if hashlib.sha256(text.encode()).hexdigest() != data["sha256"]:
        raise SystemExit("jesse_pilot_choice.json changed after it was frozen")
    return data


def _bytes(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file()) if path.exists() else 0


def disk(log: list, step: str, data_dir: Path) -> dict:
    """C: budget = Jesse environment + 1m data when that data sits on C:."""
    data = _bytes(data_dir)
    entry = {"at": datetime.now(UTC).isoformat(timespec="seconds"), "step": step,
             "free_c": disk_free_bytes(), "venv": _bytes(VENV), "data_1m": data,
             "data_dir": str(data_dir)}
    entry["extra_on_c"] = entry["venv"] + (data if full.on_c_drive(data_dir) else 0)
    log.append(entry)
    if entry["extra_on_c"] > DISK_BUDGET_BYTES:
        raise SystemExit(f"disk budget exceeded at {step}: {entry['extra_on_c']} bytes on C:")
    return entry


def _cut(bars: Bars, lo: int, hi: int) -> Bars:
    keep = (bars.t >= lo) & (bars.t < hi)
    return Bars(bars.symbol, bars.t[keep], bars.o[keep], bars.h[keep], bars.low[keep],
                bars.c[keep])


def _hour_gaps(bars: Bars) -> list[int]:
    return [int(t) for t, d in zip(bars.t[:-1], np.diff(bars.t), strict=True) if d != 3_600_000]


def monte_carlo_smoke(minutes: dict, coins: list[str], warmup_bars: int, filters: dict,
                      limits: Limits) -> dict:
    """Run-check only (owner: its numbers decide nothing): trade-order shuffling, free, no DB."""
    os.environ["RAY_USAGE_STATS_ENABLED"] = "0"
    import ray
    from jesse.research import monte_carlo_trades

    reset_ledger(limits, filters, coins)
    started = time.time()
    with block_network(allow_loopback=True):
        ray.init(num_cpus=2, include_dashboard=False, _node_ip_address="127.0.0.1",
                 ignore_reinit_error=True, log_to_driver=False)
        try:
            result = monte_carlo_trades(*jesse_inputs(minutes, coins, warmup_bars),
                                        num_scenarios=200, progress_bar=False, cpu_cores=2)
        finally:
            ray.shutdown()
    return {"scenarios": len(result["scenarios"]), "seconds": round(time.time() - started),
            "note": "run-check only; Jesse here has no fees, numbers are not used for anything"}


def run_estimate() -> None:
    """Which coins have 1m data for every full-run month, and how big it is (HEAD only)."""
    months = full.months_needed()
    symbols = list(Limits().symbols)
    sizes = {s: [full.zip_size(s, m) for m in months] for s in symbols + [BTC]}
    coins = full.usable_coins(sizes, symbols, months)
    est = full.estimate({s: sizes[s] for s in coins + [BTC]}, months)
    out = {"months": [months[0], months[-1]], "coins": coins,
           "left_out": {s: [m for m, x in zip(months, sizes[s], strict=True) if x is None]
                        for s in symbols if s not in coins}, **est}
    ESTIMATE.write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(json.dumps(out, indent=1))


def _filters_for(symbols: list[str], path: Path) -> dict:
    if not path.exists():  # public exchangeInfo, fetched once and then frozen
        save_filters(path, live_filters(symbols))
    filters = load_filters(path)
    missing = [s for s in symbols if s not in filters]
    if missing:
        raise SystemExit(f"{path.name} lacks {missing}")
    return filters


def run(is_full: bool, data_dir: Path) -> None:
    if is_full:
        coins = json.loads(ESTIMATE.read_text(encoding="utf-8"))["coins"]
        first, start, end = full.FULL_FIRST, full.FULL_START, full.FULL_END
        filters_path, out_path = REPORTS / "jesse_filters_full.json", REPORTS / "jesse_full.json"
    else:
        choice = load_choice()["choice"]
        coins, start, end = choice["coins"], choice["start"], choice["end"]
        first = _shift(start, -WARMUP_MONTHS)
        filters_path, out_path = REPORTS / "jesse_filters.json", REPORTS / "jesse_pilot.json"
    symbols = coins + [BTC]
    months = month_range(first, end)
    first_ms, start_ms, end_ms = _ms(first), _ms(start), _ms(_shift(end, 1))
    warmup_bars = (start_ms - first_ms) // FOUR_H
    limits = Limits(symbols=coins)
    filters = _filters_for(symbols, filters_path)
    log: list = []
    seconds: dict = {}
    disk(log, "start", data_dir)

    t0 = time.time()
    r0 = asyncio.run(replay(start, end, limits, filters=filters))
    seconds["R0"] = round(time.time() - t0)

    hours = {s: _cut(load_bars(s, months, "1h"), first_ms, end_ms) for s in symbols}
    fours = {s: _cut(load_bars(s, months, "4h"), first_ms, end_ms) for s in symbols}
    gaps = {s: _hour_gaps(hours[s]) for s in symbols}

    synth = {s: minutes_from_hours(h.t, h.o, h.h, h.low, h.c) for s, h in hours.items()}
    synth_check = {s: compare_to_bars(synth[s], fours[s], 240) for s in symbols}
    t0 = time.time()
    r1 = run_bridge(synth, coins, filters, warmup_bars, limits=limits)
    seconds["R1"] = round(time.time() - t0)
    disk(log, "after R1", data_dir)

    real = {}
    for s in symbols:
        real[s] = load_minutes(s, months, cache_dir=data_dir)
        disk(log, f"after 1m {s}", data_dir)
    real_check = {s: {"1h": compare_to_bars(real[s], hours[s], 60),
                      "4h": compare_to_bars(real[s], fours[s], 240)} for s in symbols}
    t0 = time.time()
    r2 = run_bridge(real, coins, filters, warmup_bars, limits=limits)
    seconds["R2"] = round(time.time() - t0)

    def eng(t: dict) -> dict:
        f = filters[t["symbol"]]
        return normalize_engine(t, r0["scale_events"], hours[t["symbol"]], f.step_size,
                                f.tick_size)

    def jes(t: dict, minutes: dict) -> dict:
        f = filters[t["symbol"]]
        return normalize_jesse(t, f.step_size, f.tick_size, minutes=minutes[t["symbol"]])

    n0 = [eng(t) for t in r0["closed"]]
    n1, n2 = [jes(t, synth) for t in r1.trades], [jes(t, real) for t in r2.trades]
    cut_ms, cut_reason = full.comparison_end(end_ms, r0["halts"])
    opens = {"R0": r0["open"], "R1": r1.open_at_end, "R2": r2.open_at_end}
    comparisons = {}
    for name, (a, b, la, lb) in {"R0_vs_R1": (n0, n1, "R0", "R1"),
                                 "R1_vs_R2": (n1, n2, "R1", "R2")}.items():
        rows, left_only, right_only, still_open = match(
            a, b, cut_ms, parity=name == "R0_vs_R1", left_open=opens[la], right_open=opens[lb])
        explain_rows(rows, a, b, fours, limits, filters, parity=name == "R0_vs_R1")
        if name == "R1_vs_R2":
            for row in rows:
                prove_minute_path(row, synth, real)
        comparisons[name] = {"summary": summary(rows), "rows": [asdict(r) for r in rows],
                             "left_only": left_only, "right_only": right_only,
                             "open_at_end": still_open}

    def total(trades: list[dict]) -> float:
        return sum(costs(t["legs"])["pnl"] for t in trades)

    report = {
        "mode": "full" if is_full else "pilot", "coins": coins, "window": [start, end],
        "warmup_months": months[:WARMUP_MONTHS], "warmup_bars": warmup_bars,
        "R0": {"trades": len(n0), "open_at_end": r0["open_at_end"], "halts": r0["halts"],
               "errors": r0["errors"], "skipped": r0["skipped"]},
        "R1": {"trades": len(n1), "open_at_end": len(r1.open_at_end), "skipped": r1.skipped,
               "plan_mismatch": r1.plan_mismatch,
               "note": "minutes made from our hourly bars: engine parity only, "
                       "not real movement inside the hour"},
        "R2": {"trades": len(n2), "open_at_end": len(r2.open_at_end), "skipped": r2.skipped,
               "plan_mismatch": r2.plan_mismatch},
        "comparison_end": {"at_ms": cut_ms, "reason": cut_reason or "window end",
                           "note": "comparison stops at our replay's first safety halt; trades "
                                   "open at that point are set aside as open"},
        "comparisons": comparisons,
        "minute_effect": {"R1_pnl_all_closed": total(n1), "R2_pnl_all_closed": total(n2),
                          "difference": total(n2) - total(n1)},
        "hour_gaps_in_our_data": gaps,
        "synthetic_vs_our_4h": synth_check,
        "real_minutes_vs_our_bars": real_check,
        "seconds": seconds,
        "disk": log,
        "versions": {p: version(p) for p in ("jesse", "jesse-rust", "ray", "numpy")}
        | {"python": platform.python_version()},
    }
    if not is_full:
        t0 = time.time()
        report["monte_carlo_smoke"] = monte_carlo_smoke(real, coins, warmup_bars, filters, limits)
        seconds["monte_carlo"] = round(time.time() - t0)
    disk(log, "end", data_dir)
    out_path.write_text(json.dumps(report, indent=1, default=float), encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("R0", "R1", "R2", "minute_effect", "seconds")},
                     indent=1, default=float))
    for name, part in comparisons.items():
        print(name, json.dumps(part["summary"], default=float), "left_only",
              len(part["left_only"]), "right_only", len(part["right_only"]), "open_at_end",
              len(part["open_at_end"]))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--full", action="store_true")
    parser.add_argument("--estimate", action="store_true")
    parser.add_argument("--data-dir", type=Path)
    args = parser.parse_args(argv)
    if args.estimate:
        run_estimate()
    elif args.full:
        run(True, full.check_data_dir(args.data_dir))
    else:
        run(False, args.data_dir or CACHE)


if __name__ == "__main__":
    main()
