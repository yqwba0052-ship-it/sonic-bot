"""Experiment: move the stop to the entry later (at +0.5R or +1R) instead of right after the
half profit at +0.20R. Research only: Sonic's own files are imported, never changed.

Everything else is Sonic as deployed: the half closes at +0.20R, the first stop is 2.5 ATR,
the rest exits on the EMA cross back or after 240 bars, same sizing, fees and slippage.
Only the moment the rest's stop moves to the entry changes:
  - "base": the deployed engine and paper exchange, untouched (stop -> entry at the half);
  - trigger X: after the half, the rest keeps its ORIGINAL stop until price reaches
    entry + X x (first stop distance); from the next check on, the stop is the entry.
    X = 0.20 must reproduce "base" exactly (checked).

Ordering inside one 1-hour bar (replay), as in the deployed exchange: stop first, then the
half target, then the trigger. A stop moved by a bar applies from the next bar (never inside
the bar that triggered it).

    python -I -m ... be_delay.py --trigger base|0.2|0.5|1.0 --start 2021-06 --end 2023-12
        --cutoff-ms N --out result.json [--fee-mult 2]
"""

from __future__ import annotations

import argparse
import asyncio
import calendar
import csv
import io
import json
import sys
import urllib.request
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "research"))
sys.path.insert(0, str(ROOT / "server"))

from sonic import exchange as exchange_mod  # noqa: E402
from sonic.exchange import HOUR_MS, PaperExchange, Position  # noqa: E402
from sonic.strategy import HALF_AT_R  # noqa: E402

from sonic_research import data as data_mod  # noqa: E402
from sonic_research import engine_replay  # noqa: E402

# Months whose official archive file has missing days (found 2026-11-20) and the month in
# progress: read from Binance's public API instead, in memory only (it matched the archive on
# every shared bar). Nothing is written to the project's data cache.
FILL_MONTHS = {"2022-02", "2022-04", "2026-10", "2026-11"}
_original_fetch = data_mod._fetch
CUTOFF_MS = 0


def _api_month(symbol: str, interval: str, month: str) -> bytes | None:
    year, mon = map(int, month.split("-"))
    start = int(datetime(year, mon, 1, tzinfo=UTC).timestamp() * 1000)
    end = int(datetime(year, mon, calendar.monthrange(year, mon)[1], 23, 59,
                       tzinfo=UTC).timestamp() * 1000)
    url = (f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}&interval={interval}"
           f"&startTime={start}&endTime={end}&limit=1500")
    with urllib.request.urlopen(url, timeout=30) as response:  # noqa: S310 - public URL
        rows = [r for r in json.loads(response.read()) if int(r[6]) < CUTOFF_MS]
    if not rows:
        return None
    text = io.StringIO()
    csv.writer(text).writerows(rows)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(f"{symbol}-{interval}-{month}.csv", text.getvalue())
    return buffer.getvalue()


def _fetch(url: str, path: Path) -> bytes | None:
    month = path.name[-11:-4]
    if month in FILL_MONTHS and "/klines/" in url:
        symbol, interval = path.name.split("-")[0], path.name.split("-")[1]
        return _api_month(symbol, interval, month)
    return _original_fetch(url, path)


class DelayedBreakevenExchange(PaperExchange):
    """The deployed paper exchange; only the rest's move to break-even is delayed."""

    trigger_r = 0.5

    async def protect(self, symbol: str, side: str, stop: float, target: float | None,
                      target_qty: float = 0.0) -> None:
        await super().protect(symbol, side, stop, target, target_qty)
        pos = self.state["open"][symbol]
        pos["risk_dist"] = abs(pos["entry"] - stop)
        self._save()

    def _scale(self, symbol: str, raw: float) -> None:
        keep = self.state["open"][symbol]["stop"]
        super()._scale(symbol, raw)  # half closed; the parent moves the stop to the entry
        pos = self.state["open"][symbol]
        if self.trigger_r <= HALF_AT_R + 1e-12:
            return  # same as deployed: break-even right at the half
        direction = 1 if pos["qty"] > 0 else -1
        pos["stop"] = keep  # the rest keeps its first stop ...
        pos["be_trigger"] = pos["entry"] + direction * self.trigger_r * pos["risk_dist"]
        self._save()  # ... until price reaches the trigger

    def _check_trigger(self, pos: dict[str, Any], high: float, low: float) -> None:
        trigger = pos.get("be_trigger")
        if trigger is None:
            return
        if (high >= trigger) if pos["qty"] > 0 else (low <= trigger):
            pos["stop"] = pos["entry"]
            pos["be_trigger"] = None
            self._save()

    async def move_stop(self, symbol: str, side: str, stop: float) -> None:
        # The engine asks for break-even as soon as it sees the half filled; here the
        # exchange decides when (trigger). Any other request passes through unchanged.
        pos = self.state["open"].get(symbol)
        if pos is not None and pos.get("be_trigger") is not None and stop == pos["entry"]:
            return
        await super().move_stop(symbol, side, stop)

    async def _replay_missed(self, symbol: str, pos: dict[str, Any]) -> bool:
        if pos.get("stop") is None:
            return False
        since = int(pos.get("checked_ms", 0))
        for bar in await self.market.bars(symbol, "1h", 500):
            if bar.t + HOUR_MS <= since:
                continue
            long = pos["qty"] > 0
            stop, target = pos["stop"], pos.get("target")
            if long and bar.low <= stop:
                self._settle(symbol, min(bar.o, stop))
                return True
            if not long and bar.h >= stop:
                self._settle(symbol, max(bar.o, stop))
                return True
            if target is not None and (bar.h >= target if long else bar.low <= target):
                self._scale(symbol, max(bar.o, target) if long else min(bar.o, target))
                pos = self.state["open"][symbol]
            self._check_trigger(pos, bar.h, bar.low)  # applies from the next bar
        return False

    async def positions(self) -> dict[str, Position]:
        result: dict[str, Position] = {}
        for symbol in list(self.state["open"]):
            pos = self.state["open"][symbol]
            if await self._replay_missed(symbol, pos):
                continue
            pos = self.state["open"][symbol]
            pos["checked_ms"] = int(self.clock() * 1000)
            self._save()
            last = await self.market.price(symbol)
            long = pos["qty"] > 0
            stop, target = pos.get("stop"), pos.get("target")
            if stop is not None and (last <= stop if long else last >= stop):
                self._settle(symbol, last)
                continue
            if target is not None and (last >= target if long else last <= target):
                self._scale(symbol, last)
                pos = self.state["open"][symbol]
            self._check_trigger(pos, last, last)
            unrealized = pos["qty"] * (last - pos["entry"])
            result[symbol] = Position(symbol, pos["qty"], pos["entry"], last, unrealized)
        return result


def classify(trade: dict[str, Any]) -> str:
    """Exit kind from prices, independent of the engine's label."""
    reason = trade["exit_reason"]
    if reason in ("trade_exit_signal", "trade_exit_time"):
        return reason
    if trade["scaled"]:
        near = trade["exit_price"] >= trade["entry_price"] * (1 - 0.005)
        return "breakeven" if near else "stop_after_half"
    return "stop_loss"


async def run(trigger: str, start: str, end: str, fee_mult: float) -> dict[str, Any]:
    from sonic.risk import Limits

    if fee_mult != 1.0:
        exchange_mod.FEE_RATE *= fee_mult
        exchange_mod.SLIPPAGE *= fee_mult
    if trigger != "base":
        DelayedBreakevenExchange.trigger_r = float(trigger)
        engine_replay.PaperExchange = DelayedBreakevenExchange
    report = await engine_replay.replay(start, end, Limits(), save_trades=False)
    closed = sorted(report.pop("closed"), key=lambda t: t["opened_ms"])
    keep = ("symbol", "side", "opened_ms", "closed_ms", "entry_price", "exit_price", "qty",
            "pnl", "fees", "r", "exit_reason", "scaled")
    return {"trigger": trigger, "start": start, "end": end, "fee_mult": fee_mult,
            "cutoff_ms": CUTOFF_MS,
            "summary": {k: report[k] for k in ("trades", "open_at_end", "win_rate", "avg_r",
                                                "pnl_usdt", "end_amount", "max_drop",
                                                "errors", "skipped")},
            "trades": [{**{k: t[k] for k in keep}, "kind": classify(t)} for t in closed]}


def main() -> None:
    global CUTOFF_MS
    parser = argparse.ArgumentParser()
    parser.add_argument("--trigger", required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--cutoff-ms", type=int, required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--fee-mult", type=float, default=1.0)
    args = parser.parse_args()
    CUTOFF_MS = args.cutoff_ms
    data_mod._fetch = _fetch
    result = asyncio.run(run(args.trigger, args.start, args.end, args.fee_mult))
    Path(args.out).write_text(json.dumps(result, indent=1), encoding="utf-8")
    print(json.dumps(result["summary"], indent=1))


if __name__ == "__main__":
    main()
