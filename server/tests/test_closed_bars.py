"""Seen on the server 2026-11-18 00:00:03 UTC: the scan used the 16:00 bar for BTC because
Binance had not opened the 00:00 bar yet and the code always dropped "the last row" (which was
the 20:00 bar that had just closed). Closed bars must be chosen by their close time."""

from __future__ import annotations

import httpx

from sonic.exchange import PUBLIC_URL, PublicMarket
from sonic.strategy import BAR_MS

T = 1_794_960_000_000  # 2026-11-18 00:00:00 UTC, a 4h close


def kline(open_ms: int, close: float) -> list:
    return [open_ms, "1", "1", "1", str(close), "0", open_ms + BAR_MS - 1, "0", 0, "0", "0", "0"]


def market(rows: list[list], now_ms: int) -> PublicMarket:
    def handler(request: httpx.Request) -> httpx.Response:
        limit = int(request.url.params["limit"])
        return httpx.Response(200, json=rows[-limit:])

    client = httpx.AsyncClient(base_url=PUBLIC_URL, transport=httpx.MockTransport(handler))
    return PublicMarket(client, clock=lambda: now_ms / 1000)


async def test_just_after_a_close_without_the_next_bar_the_closed_bar_is_kept() -> None:
    rows = [kline(T - (k + 1) * BAR_MS, 100.0 + k) for k in reversed(range(5))]  # ..20:00
    bars = await market(rows, T + 3_000).bars("BTCUSDT", "4h", 3)
    assert bars[-1].t == T - BAR_MS  # the 20:00 bar that closed 3 s ago
    assert len(bars) == 3


async def test_the_forming_bar_is_still_dropped() -> None:
    rows = [kline(T - (k + 1) * BAR_MS, 100.0 + k) for k in reversed(range(5))]
    rows.append(kline(T, 999.0))  # the 00:00 bar, still forming
    bars = await market(rows, T + 60_000).bars("BTCUSDT", "4h", 3)
    assert bars[-1].t == T - BAR_MS and all(b.c != 999.0 for b in bars)
    assert len(bars) == 3


async def test_same_answer_whether_or_not_binance_opened_the_next_bar() -> None:
    rows = [kline(T - (k + 1) * BAR_MS, 100.0 + k) for k in reversed(range(6))]
    without = await market(rows, T + 2_000).bars("SOLUSDT", "4h", 4)
    with_forming = await market(rows + [kline(T, 5.0)], T + 2_000).bars("SOLUSDT", "4h", 4)
    assert without == with_forming
