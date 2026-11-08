"""Paper-trade the REAL bot engine on past Binance prices, hour by hour.

python -m sonic_research.engine_replay [start-month] [end-month]

The engine, paper exchange, sizing, safety limits and strategy are the server's own code;
only the market feed and the clock are replaced: the feed shows the bot only bars that had
closed at the simulated hour, and the price is the last closed 1-hour close. This finds
bugs that unit tests miss and shows what months of paper trading would have looked like.
"""

from __future__ import annotations

import asyncio
import bisect
import json
import sys
import time
import urllib.request
from collections import Counter
from pathlib import Path

import numpy as np

from .data import load_bars, month_range

SERVER = Path(__file__).resolve().parents[2] / "server"
sys.path.insert(0, str(SERVER))

from sonic.config import Settings  # noqa: E402
from sonic.engine import Engine  # noqa: E402
from sonic.exchange import PaperExchange, parse_filters  # noqa: E402
from sonic.risk import Filters, Limits  # noqa: E402
from sonic.store import Store  # noqa: E402
from sonic.strategy import Bar  # noqa: E402

HOUR_MS = 3_600_000
FOUR_H_MS = 4 * HOUR_MS
REPORT = Path(__file__).resolve().parent.parent / "reports" / "engine_replay.json"


class ReplayMarket:
    """Shows the engine only what was known at `now_ms`."""

    def __init__(self, symbols: list[str], months: list[str], filters: dict[str, Filters]) -> None:
        self.now_ms = 0
        self.filters_by_symbol = filters
        self.series: dict[tuple[str, str], tuple[list[int], list[Bar]]] = {}
        for symbol in symbols:
            for interval in ("4h", "1h"):
                raw = load_bars(symbol, months, interval)
                bars = [Bar(int(t), o, h, low, c) for t, o, h, low, c in
                        zip(raw.t, raw.o, raw.h, raw.low, raw.c, strict=True)]
                self.series[(symbol, interval)] = ([b.t for b in bars], bars)

    def _closed(self, symbol: str, interval: str) -> list[Bar]:
        times, bars = self.series[(symbol, interval)]
        length = FOUR_H_MS if interval == "4h" else HOUR_MS
        return bars[: bisect.bisect_right(times, self.now_ms - length)]

    async def bars(self, symbol: str, interval: str, limit: int) -> list[Bar]:
        return self._closed(symbol, interval)[-limit:]

    async def price(self, symbol: str) -> float:
        return self._closed(symbol, "1h")[-1].c

    async def prices(self) -> dict[str, float]:
        return {s: await self.price(s) for s, interval in self.series if interval == "1h"}

    async def filters(self, symbol: str) -> Filters:
        return self.filters_by_symbol[symbol]


def live_filters(symbols: list[str]) -> dict[str, Filters]:
    url = "https://fapi.binance.com/fapi/v1/exchangeInfo"
    with urllib.request.urlopen(url, timeout=30) as response:
        info = json.loads(response.read())
    return {symbol: parse_filters(info, symbol) for symbol in symbols}


async def replay(start: str, end: str, limits: Limits) -> dict:
    symbols = sorted(set(limits.symbols) | {"BTCUSDT"})
    year, month = map(int, start.split("-"))
    year, month = (year, month - 7) if month > 7 else (year - 1, month + 5)
    history = month_range(f"{year:04d}-{month:02d}", end)  # 1000 4h bars of warm-up
    market = ReplayMarket(symbols, history, live_filters(symbols))
    store = Store(":memory:")
    clock = {"now": 0.0}
    exchange = PaperExchange(market, store, 10_000.0, clock=lambda: clock["now"])
    settings = Settings(mode="paper", token="replay", db_path=":memory:")
    engine = Engine(store, exchange, settings, clock=lambda: clock["now"])

    from datetime import UTC, datetime

    start_ms = int(datetime.strptime(start, "%Y-%m").replace(tzinfo=UTC).timestamp() * 1000)
    end_times = market.series[("BTCUSDT", "1h")][0]
    end_ms = end_times[-1] + HOUR_MS
    clock["now"] = start_ms / 1000
    market.now_ms = start_ms
    await engine.set_limits(limits)
    await engine.start()

    budget_curve = []
    started = time.time()
    now = start_ms
    while now <= end_ms:
        clock["now"] = now / 1000
        market.now_ms = now
        await engine.tick()
        if engine.halted:
            # A real owner would look and decide; the replay records it and carries on.
            await engine.clear_halt() if engine.halted not in ("budget_used",) else None
            if not engine.halted:
                await engine.start()
        budget_curve.append(engine.budget_left())
        now += HOUR_MS
    trades = [t for t in store.trades(100_000) if t["closed_ms"]]
    events = Counter(e["code"] for e in store.events(1_000_000))
    skipped = Counter(e["detail"].split()[-1] for e in store.events(1_000_000)
                      if e["code"] == "trade_skipped")
    errors = [e["detail"] for e in store.events(1_000_000) if e["code"] == "tick_failed"]
    curve = np.array(budget_curve)
    peak = np.maximum.accumulate(curve)
    r = np.array([t["r"] for t in trades if t["r"] is not None])
    return {
        "period": f"{start}..{end}",
        "seconds": round(time.time() - started),
        "trades": len(trades),
        "open_at_end": len(store.open_trades()),
        "win_rate": float((r > 0).mean()) if len(r) else None,
        "avg_r": float(r.mean()) if len(r) else None,
        "pnl_usdt": round(sum(t["pnl"] for t in trades), 2),
        "start_amount": limits.capital_usdt,
        "end_amount": round(float(curve[-1]), 2),
        "max_drop": float((1 - curve / peak).max()),
        "scaled": sum(1 for t in trades if t["scaled"]),
        "events": dict(events),
        "skipped": dict(skipped),
        "errors": errors[:20],
        "by_coin": {s: [round(float(np.mean([t["r"] > 0 for t in trades if t["symbol"] == s])), 2),
                        sum(t["symbol"] == s for t in trades)]
                    for s in limits.symbols if any(t["symbol"] == s for t in trades)},
    }


def main() -> None:
    start = sys.argv[1] if len(sys.argv) > 1 else "2025-07"
    end = sys.argv[2] if len(sys.argv) > 2 else "2026-10"
    report = asyncio.run(replay(start, end, Limits()))
    REPORT.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=1, ensure_ascii=False))



if __name__ == "__main__":
    main()
