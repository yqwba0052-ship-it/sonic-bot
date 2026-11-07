"""Smallest starting amount that still trades the strategy and profits:
python -m sonic_research.capital

Replays the strategy's trades in time order on one account with Binance's rules:
- each order must be at least the coin's minimum order value;
- all open positions together must fit inside the account's margin (leverage cap);
- at most `max_open` positions at once;
- a trade too small for the minimum may be raised to it only if the stop then risks no more
  than `max_risk`; otherwise it is skipped (the bot's real behaviour).
Results are from the account's own money, with fees, slippage and funding inside each trade.
"""

from __future__ import annotations

import itertools
import json
from dataclasses import dataclass
from pathlib import Path

from .backtest import Trade
from .data import SYMBOLS, load_bars, load_funding, month_range
from .phase2 import END, START, run_set
from .run import grid, month_ms
from .strategies import btc_trend

BEST = "trend-20-100-k3-tnone-h120-btc"
# Binance smallest order value per coin (USDT), read from exchangeInfo on 2026-11-06/08.
MIN_ORDER = {
    "BTCUSDT": 76.0, "ETHUSDT": 20.0, "LINKUSDT": 20.0, "LTCUSDT": 20.0, "AVAXUSDT": 8.2,
    "BNBUSDT": 7.0, "SOLUSDT": 5.0, "XRPUSDT": 5.0, "DOGEUSDT": 5.0, "ADAUSDT": 5.0,
    "SUIUSDT": 5.0, "TRXUSDT": 5.0,
}
LEVERAGE = 3
REPORT = Path(__file__).resolve().parent.parent / "reports" / "capital.json"


@dataclass
class Outcome:
    final: float
    max_drawdown: float
    taken: int
    skipped: int


def replay(trades: list[Trade], start: float, risk: float, max_open: int,
           max_risk: float) -> Outcome:
    equity = start
    peak = start
    worst = 0.0
    open_positions: list[tuple[int, float, float]] = []  # exit_t, notional, pnl
    taken = skipped = 0
    for trade in sorted(trades, key=lambda t: t.entry_t):
        # settle positions that closed before this entry
        still_open = []
        for exit_t, notional, pnl in sorted(open_positions):
            if exit_t <= trade.entry_t:
                equity += pnl
                peak = max(peak, equity)
                worst = max(worst, 1 - equity / peak)
            else:
                still_open.append((exit_t, notional, pnl))
        open_positions = still_open
        if equity <= 0:
            break
        stop_pct = abs(trade.ret / trade.r) if trade.r else 0.0
        if stop_pct <= 0 or len(open_positions) >= max_open:
            skipped += 1
            continue
        notional = equity * risk / stop_pct
        minimum = MIN_ORDER[trade.symbol]
        if notional < minimum:
            if minimum * stop_pct <= equity * max_risk:
                notional = minimum
            else:
                skipped += 1
                continue
        margin_used = sum(n for _, n, _ in open_positions) / LEVERAGE
        if margin_used + notional / LEVERAGE > equity:
            skipped += 1
            continue
        taken += 1
        open_positions.append((trade.exit_t, notional, notional * trade.ret))
    for _, _, pnl in sorted(open_positions):
        equity += pnl
        peak = max(peak, equity)
        worst = max(worst, 1 - equity / peak)
    return Outcome(equity, worst, taken, skipped)


def main() -> None:
    months = month_range(START, END)
    bars = {s: load_bars(s, months) for s in SYMBOLS}
    funding = {s: load_funding(s, months) for s in SYMBOLS}
    regime = {s: btc_trend(bars["BTCUSDT"], bars[s]) for s in SYMBOLS}
    params = {p.name: p for p in grid()}[BEST]
    trades = run_set(params, bars, funding, regime)
    test = [t for t in trades if t.entry_t >= month_ms("2024-07")]
    rows = []
    print("period 2024-07..2026-10 (unseen in training); final = what the start became")
    print(f"{'start':>6} {'risk':>5} {'open':>4} {'final':>9} {'gain':>7} {'DD':>5} {'taken':>6}")
    for start, risk, max_open in itertools.product(
        (20, 30, 50, 75, 100, 150, 200, 300, 500), (0.005, 0.01), (3, 6)
    ):
        out = replay(test, start, risk, max_open, max_risk=max(2 * risk, 0.02))
        share = out.taken / (out.taken + out.skipped)
        rows.append({"start": start, "risk": risk, "max_open": max_open,
                     "final": round(out.final, 2), "gain": out.final / start - 1,
                     "max_drawdown": out.max_drawdown, "taken": out.taken,
                     "skipped": out.skipped})
        print(f"{start:>6} {risk:>5.1%} {max_open:>4} {out.final:>9.2f} "
              f"{out.final / start - 1:>+7.0%} {out.max_drawdown:>5.0%} {share:>6.0%}")
    print("whole history 2021-01..2026-10 (includes the 2022 bear market):")
    for start, risk, max_open in ((20, 0.01, 3), (30, 0.01, 3), (50, 0.01, 3), (50, 0.005, 6),
                                  (100, 0.005, 6), (100, 0.01, 3)):
        out = replay(trades, start, risk, max_open, max_risk=max(2 * risk, 0.02))
        share = out.taken / (out.taken + out.skipped)
        print(f"{start:>6} {risk:>5.1%} {max_open:>4} {out.final:>9.2f} "
              f"{out.final / start - 1:>+7.0%} {out.max_drawdown:>5.0%} {share:>6.0%}")
    REPORT.write_text(json.dumps(rows, indent=1), encoding="utf-8")
    print("months in test:", round((test[-1].exit_t - test[0].entry_t) / 2.63e9, 1),
          "| trades available:", len(test), "| report:", REPORT)


if __name__ == "__main__":
    main()
