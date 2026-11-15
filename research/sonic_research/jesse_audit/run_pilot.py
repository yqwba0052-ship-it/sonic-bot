"""Jesse pilot: R0 (our replay) vs R1 (Jesse on minutes made from our hours: engine parity only)
vs R2 (Jesse on real Binance minutes). Training data only; the frozen choice decides coins/window.

python -m sonic_research.jesse_audit.run_pilot   (run with the Jesse environment's python)
"""

from __future__ import annotations

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
from . import DISK_BUDGET_BYTES
from .bridge import BTC, EXCHANGE, jesse_symbol, run_bridge
from .compare import match, normalize_engine, normalize_jesse, summary
from .data_1m import CACHE, compare_to_bars, load_minutes
from .offline import block_network, disk_free_bytes
from .synth import minutes_from_hours

REPORTS = Path(__file__).resolve().parents[2] / "reports"
CHOICE = REPORTS / "jesse_pilot_choice.json"
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


def disk(log: list, step: str) -> dict:
    entry = {"at": datetime.now(UTC).isoformat(timespec="seconds"), "step": step,
             "free": disk_free_bytes(), "venv": _bytes(VENV), "data_1m": _bytes(CACHE)}
    entry["extra"] = entry["venv"] + entry["data_1m"]
    log.append(entry)
    if entry["extra"] > DISK_BUDGET_BYTES:
        raise SystemExit(f"disk budget exceeded at {step}: {entry['extra']} bytes")
    return entry


def _cut(bars: Bars, lo: int, hi: int) -> Bars:
    keep = (bars.t >= lo) & (bars.t < hi)
    return Bars(bars.symbol, bars.t[keep], bars.o[keep], bars.h[keep], bars.low[keep],
                bars.c[keep])


def _hour_gaps(bars: Bars) -> list[int]:
    return [int(t) for t, d in zip(bars.t[:-1], np.diff(bars.t), strict=True) if d != 3_600_000]


def monte_carlo_smoke(minutes: dict, coins: list[str], warmup_bars: int) -> dict:
    """Run-check only (owner: its numbers decide nothing): trade-order shuffling, free, no DB."""
    os.environ["RAY_USAGE_STATS_ENABLED"] = "0"
    import ray
    from jesse import helpers as jh
    from jesse.research import monte_carlo_trades

    from .bridge import SonicBridge
    data, warm = {}, {}
    for symbol, m in minutes.items():
        split = int(np.searchsorted(m[:, 0], m[0, 0] + warmup_bars * 240 * 60_000))
        key = jh.key(EXCHANGE, jesse_symbol(symbol))
        base = {"exchange": EXCHANGE, "symbol": jesse_symbol(symbol)}
        warm[key] = {**base, "candles": m[:split]}
        data[key] = {**base, "candles": m[split:]}
    config = {"starting_balance": 10_000, "fee": 0, "type": "futures", "futures_leverage": 3,
              "futures_leverage_mode": "cross", "exchange": EXCHANGE,
              "warm_up_candles": warmup_bars}
    routes = [{"exchange": EXCHANGE, "strategy": SonicBridge, "symbol": jesse_symbol(s),
               "timeframe": "4h"} for s in coins]
    data_routes = [{"exchange": EXCHANGE, "symbol": jesse_symbol(BTC), "timeframe": "4h"}]
    started = time.time()
    with block_network(allow_loopback=True):
        ray.init(num_cpus=2, include_dashboard=False, _node_ip_address="127.0.0.1",
                 ignore_reinit_error=True, log_to_driver=False)
        try:
            result = monte_carlo_trades(config, routes, data_routes, data, warm,
                                        num_scenarios=200, progress_bar=False, cpu_cores=2)
        finally:
            ray.shutdown()
    return {"scenarios": len(result["scenarios"]), "seconds": round(time.time() - started),
            "note": "run-check only; Jesse here has no fees, numbers are not used for anything"}


def main() -> None:
    choice = load_choice()["choice"]
    coins, start, end = choice["coins"], choice["start"], choice["end"]
    symbols = coins + [BTC]
    first = _shift(start, -WARMUP_MONTHS)
    months = month_range(first, end)
    first_ms, start_ms, end_ms = _ms(first), _ms(start), _ms(_shift(end, 1))
    warmup_bars = (start_ms - first_ms) // FOUR_H
    log: list = []
    seconds: dict = {}
    disk(log, "start")

    t0 = time.time()
    r0 = asyncio.run(replay(start, end, Limits(symbols=coins)))
    seconds["R0"] = round(time.time() - t0)
    raw_filters = live_filters(symbols)
    (REPORTS / "jesse_filters.json").write_text(
        json.dumps({s: asdict(f) for s, f in raw_filters.items()}, indent=1), encoding="utf-8")

    hours = {s: _cut(load_bars(s, months, "1h"), first_ms, end_ms) for s in symbols}
    fours = {s: _cut(load_bars(s, months, "4h"), first_ms, end_ms) for s in symbols}
    gaps = {s: _hour_gaps(hours[s]) for s in symbols}

    synth = {s: minutes_from_hours(h.t, h.o, h.h, h.low, h.c) for s, h in hours.items()}
    synth_check = {s: compare_to_bars(synth[s], fours[s], 240) for s in symbols}
    t0 = time.time()
    r1 = run_bridge(synth, coins, raw_filters, warmup_bars)
    seconds["R1"] = round(time.time() - t0)
    disk(log, "after R1")

    real = {s: load_minutes(s, months) for s in symbols}
    disk(log, "after 1m download")
    real_check = {s: {"1h": compare_to_bars(real[s], hours[s], 60),
                      "4h": compare_to_bars(real[s], fours[s], 240)} for s in symbols}
    t0 = time.time()
    r2 = run_bridge(real, coins, raw_filters, warmup_bars)
    seconds["R2"] = round(time.time() - t0)

    def eng(t: dict) -> dict:
        f = raw_filters[t["symbol"]]
        return normalize_engine(t, r0["scale_events"], hours[t["symbol"]], f.step_size,
                                f.tick_size)

    def jes(t: dict) -> dict:
        f = raw_filters[t["symbol"]]
        return normalize_jesse(t, f.step_size, f.tick_size)

    n0 = [eng(t) for t in r0["closed"]]
    n1, n2 = [jes(t) for t in r1.trades], [jes(t) for t in r2.trades]
    out_rows = {}
    for name, (a, b) in {"R0_vs_R1": (n0, n1), "R1_vs_R2": (n1, n2)}.items():
        rows, left_only, right_only, still_open = match(a, b, end_ms)
        out_rows[name] = {"summary": summary(rows), "rows": [asdict(r) for r in rows],
                          "left_only": left_only, "right_only": right_only,
                          "open_at_end": still_open}
    t0 = time.time()
    mc = monte_carlo_smoke(real, coins, warmup_bars)
    seconds["monte_carlo"] = round(time.time() - t0)
    disk(log, "end")

    report = {
        "choice": choice, "warmup_months": months[:WARMUP_MONTHS], "warmup_bars": warmup_bars,
        "R0": {"trades": len(n0), "open_at_end": r0["open_at_end"], "halts": r0["halts"],
               "errors": r0["errors"]},
        "R1": {"trades": len(n1), "open_at_end": len(r1.open_at_end), "skipped": r1.skipped,
               "note": "minutes made from our hourly bars: engine parity only, "
                       "not real movement inside the hour"},
        "R2": {"trades": len(n2), "open_at_end": len(r2.open_at_end), "skipped": r2.skipped},
        "comparisons": out_rows,
        "hour_gaps_in_our_data": gaps,
        "synthetic_vs_our_4h": synth_check,
        "real_minutes_vs_our_bars": real_check,
        "monte_carlo_smoke": mc,
        "seconds": seconds,
        "disk": log,
        "versions": {p: version(p) for p in ("jesse", "jesse-rust", "ray", "numpy")}
        | {"python": platform.python_version()},
    }
    (REPORTS / "jesse_pilot.json").write_text(json.dumps(report, indent=1, default=float),
                                              encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("R0", "R1", "R2", "seconds")}, indent=1,
                     default=float))
    for name, part in out_rows.items():
        print(name, json.dumps(part["summary"], default=float), "left_only",
              len(part["left_only"]), "right_only", len(part["right_only"]))


if __name__ == "__main__":
    main()
