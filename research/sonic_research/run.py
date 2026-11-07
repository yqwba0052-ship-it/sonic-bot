"""Phase 1 research run: python -m sonic_research.run

1. Download history for 12 coins (cached in research/data).
2. Run every parameter set of three strategy families on every coin.
3. Pick the best sets on the training period only, then show how they did on later,
   unseen months (the honest number).
4. Learning test: each month, re-pick the set from the last 6 months and drop coins that
   kept losing, then trade the next month. Compare with the fixed choice.
"""

from __future__ import annotations

import itertools
import json
import time
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from .backtest import Trade, simulate
from .data import SYMBOLS, load_bars, load_funding, month_range
from .metrics import summarize
from .strategies import Params, btc_trend, build

START, TRAIN_END, END = "2023-01", "2025-04", "2026-10"
MIN_TRADES = 100
REPORT_DIR = Path(__file__).resolve().parent.parent / "reports"


def month_ms(month: str) -> int:
    return int(datetime.strptime(month, "%Y-%m").replace(tzinfo=UTC).timestamp() * 1000)


def grid() -> list[Params]:
    sets = []
    for fast, slow, k, target, regime in itertools.product(
        (10, 20), (50, 100), (2.0, 3.0), (2.0, 3.0, None), (False, True)
    ):
        sets.append(Params("trend", fast, slow, k, target, 120, regime))
    for channel, k, target, regime in itertools.product(
        (20, 55), (2.0, 3.0), (2.0, 3.0, None), (False, True)
    ):
        sets.append(Params("breakout", channel, 0, k, target, 120, regime))
    for period, level, k, hold, regime in itertools.product(
        (2, 14), (10, 25), (2.0, 3.0), (6, 12), (False, True)
    ):
        sets.append(Params("reversion", period, level, k, None, hold, regime))
    return sets


def within(trades: list[Trade], start: int, end: int) -> list[Trade]:
    return [trade for trade in trades if start <= trade.entry_t < end]


def learning_walk(
    results: dict[str, list[Trade]],
    test_months: list[str],
    lookback: int = 6,
    all_months: list[str] | None = None,
) -> tuple[list[Trade], list[dict]]:
    """Each month: pick the best set on the previous `lookback` months, skip coins whose
    trades with that set lost on average (10+ trades), trade the month, then move on."""
    chosen: list[Trade] = []
    log = []
    all_months = all_months or month_range(START, END)
    for month in test_months:
        index = all_months.index(month)
        start = month_ms(all_months[max(0, index - lookback)])
        now = month_ms(month)
        nxt = month_ms(all_months[index + 1]) if index + 1 < len(all_months) else 10**15
        best, best_score = None, -np.inf
        for name, trades in results.items():
            recent = [t for t in trades if start <= t.entry_t and t.exit_t <= now]
            if len(recent) >= 30:
                score = float(np.mean([t.r for t in recent]))
                if score > best_score:
                    best, best_score = name, score
        if best is None or best_score <= 0:
            log.append({"month": month, "set": None, "reason": "nothing earned recently"})
            continue
        recent = [t for t in results[best] if start <= t.entry_t and t.exit_t <= now]
        by_coin = defaultdict(list)
        for trade in recent:
            by_coin[trade.symbol].append(trade.r)
        dropped = sorted(s for s, rs in by_coin.items() if len(rs) >= 10 and np.mean(rs) < 0)
        month_trades = [
            t for t in within(results[best], now, nxt) if t.symbol not in dropped
        ]
        chosen.extend(month_trades)
        log.append({"month": month, "set": best, "recent_avg_r": round(best_score, 3),
                    "dropped": dropped, "trades": len(month_trades)})
    return chosen, log


def main() -> None:
    started = time.time()
    months = month_range(START, END)
    train_end_ms = month_ms("2025-05")
    print(f"loading {len(SYMBOLS)} coins, {months[0]}..{months[-1]}")
    bars = {symbol: load_bars(symbol, months) for symbol in SYMBOLS}
    funding = {symbol: load_funding(symbol, months) for symbol in SYMBOLS}
    btc = bars["BTCUSDT"]
    regime = {symbol: btc_trend(btc, bars[symbol]) for symbol in SYMBOLS}
    print("bars per coin:", {s: len(b) for s, b in bars.items()})

    results: dict[str, list[Trade]] = {}
    params_by_name = {}
    for params in grid():
        trades: list[Trade] = []
        for symbol in SYMBOLS:
            signals = build(bars[symbol], params, regime[symbol])
            trades.extend(
                simulate(bars[symbol], signals, params.target_r, params.max_hold, funding[symbol])
            )
        results[params.name] = trades
        params_by_name[params.name] = params
    print(f"simulated {len(results)} sets in {time.time() - started:.0f}s")

    table = []
    for name, trades in results.items():
        train = [t for t in trades if t.exit_t <= train_end_ms]
        test = [t for t in trades if t.entry_t >= train_end_ms]
        table.append({"set": name, "train": summarize(train), "test": summarize(test)})
    eligible = [row for row in table if row["train"]["trades"] >= MIN_TRADES]
    eligible.sort(key=lambda row: row["train"]["avg_r"], reverse=True)

    best_by_family = {}
    for row in eligible:
        family = row["set"].split("-")[0]
        best_by_family.setdefault(family, row)

    test_months = [m for m in months if month_ms(m) >= train_end_ms]
    learned, learn_log = learning_walk(results, test_months)
    fixed = eligible[0]

    btc_test = btc.c[btc.t >= train_end_ms]
    report = {
        "period": {"train": f"{START}..2025-04", "test": f"2025-05..{END}"},
        "costs": "taker fee 0.05% each side, slippage 0.02% each side, real funding",
        "sets_tested": len(results),
        "sets_positive_train": sum(r["train"]["avg_r"] > 0 for r in eligible),
        "sets_positive_test": sum(r["test"]["avg_r"] > 0 for r in eligible),
        "top_train": eligible[:10],
        "best_by_family": best_by_family,
        "fixed_choice_test": fixed["test"],
        "learning_test": summarize(learned),
        "learning_log": learn_log,
        "btc_buy_hold_test": float(btc_test[-1] / btc_test[0] - 1),
        "seconds": round(time.time() - started),
    }
    REPORT_DIR.mkdir(exist_ok=True)
    path = REPORT_DIR / "phase1.json"
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k not in ("top_train", "learning_log")},
                     indent=1)[:4000])
    print("top 10 on training, and the same sets on the unseen test period:")
    for row in eligible[:10]:
        tr, te = row["train"], row["test"]
        print(f"{row['set']:45} train n={tr['trades']:4} avgR={tr['avg_r']:+.3f} "
              f"PF={tr['profit_factor']:.2f} | test n={te['trades']:4} avgR={te['avg_r']:+.3f} "
              f"PF={te['profit_factor']:.2f} ret={te['total_return']:+.1%} "
              f"DD={te['max_drawdown']:.1%}")
    print(f"report: {path}")


if __name__ == "__main__":
    main()
