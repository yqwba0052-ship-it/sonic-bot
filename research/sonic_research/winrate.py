"""High win rate versions: python -m sonic_research.winrate

Same trend entries, longs only, but take profit early at a close target (in units of the
stop distance). A close target wins more often; the question is whether it still makes
money after fees. Also lists the mean-reversion family, which is the classic high win rate
style. Results on the training and the unseen test period, plus a $30 account replay.
"""

from __future__ import annotations

from dataclasses import replace

import numpy as np

from .backtest import Trade
from .capital import replay
from .data import SYMBOLS, load_bars, load_funding, month_range
from .phase2 import END, START, run_set
from .run import grid, month_ms
from .strategies import Params, btc_trend

SMALL = {"SOLUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT", "SUIUSDT", "TRXUSDT", "BNBUSDT", "AVAXUSDT"}
BASE = Params("trend", 20, 100, 3.0, None, 120, True)


def line(trades: list[Trade]) -> str:
    if not trades:
        return "n=0"
    r = np.array([t.r for t in trades])
    return f"n={len(r):4} win={np.mean(r > 0):5.1%} avgR={r.mean():+.3f} totalR={r.sum():+7.1f}"


def account(trades: list[Trade]) -> str:
    chosen = [t for t in trades if t.symbol in SMALL]
    out = replay(chosen, 30, 0.01, 3, 0.02)
    return f"$30 -> {out.final:7.2f}  max drop {out.max_drawdown:4.0%}"


def main() -> None:
    months = month_range(START, END)
    bars = {s: load_bars(s, months) for s in SYMBOLS}
    funding = {s: load_funding(s, months) for s in SYMBOLS}
    regime = {s: btc_trend(bars["BTCUSDT"], bars[s]) for s in SYMBOLS}
    split = month_ms("2024-07")
    variants = [("trend, no target (current)", BASE)]
    variants += [(f"trend, take profit at {t}R", replace(BASE, target_r=t))
                 for t in (0.5, 0.75, 1.0, 1.5)]
    variants += [(f"trend, stop 2 ATR, target {t}R", replace(BASE, atr_k=2.0, target_r=t))
                 for t in (0.5, 1.0)]
    reversion = [p for p in grid() if p.family == "reversion"]
    print(f"{'':34} {'TRAIN 2021-01..2024-06':44} | TEST 2024-07..2026-10")
    for name, params in variants:
        trades = [t for t in run_set(params, bars, funding, regime) if t.side == 1]
        train = [t for t in trades if t.exit_t <= split]
        test = [t for t in trades if t.entry_t >= split]
        print(f"{name:34} {line(train):44} | {line(test)} | whole {account(trades)}")
    print("mean-reversion family, best 4 by training win rate (both sides):")
    rows = []
    for params in reversion:
        trades = run_set(params, bars, funding, regime)
        train = [t for t in trades if t.exit_t <= split]
        if len(train) >= 100:
            rows.append((np.mean([t.r > 0 for t in train]), params, trades))
    for _, params, trades in sorted(rows, key=lambda row: row[0], reverse=True)[:4]:
        train = [t for t in trades if t.exit_t <= split]
        test = [t for t in trades if t.entry_t >= split]
        print(f"{params.name:34} {line(train):44} | {line(test)} | whole {account(trades)}")


if __name__ == "__main__":
    main()
