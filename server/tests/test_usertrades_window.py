"""Binance's GET /fapi/v1/userTrades returns at most 7 days per request (startTime alone means
startTime..startTime+7d) and 500 rows by default (1000 max). A Sonic trade can stay open up to
240 bars (40 days), so the closing fills must be read window by window, page by page."""

from __future__ import annotations

import time
from urllib.parse import parse_qs, urlsplit

import httpx

from sonic.exchange import DEMO_URL, BinanceDemo

DAY = 24 * 3600 * 1000
WEEK = 7 * DAY


def fake_binance(fills: list[dict]) -> httpx.MockTransport:
    """userTrades with Binance's documented limits; every other call answers 200 {}."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path != "/fapi/v1/userTrades":
            return httpx.Response(200, json={})
        q = {k: v[0] for k, v in parse_qs(urlsplit(f"?{request.url.query.decode()}").query).items()}
        now = int(time.time() * 1000)
        start = int(q["startTime"]) if "startTime" in q else now - WEEK
        end = int(q["endTime"]) if "endTime" in q else start + WEEK
        if end - start > WEEK:
            return httpx.Response(400, json={"code": -1127, "msg": "More than 7 days"})
        limit = min(int(q.get("limit", 500)), 1000)
        rows = sorted((f for f in fills if start <= f["time"] <= end),
                      key=lambda f: (f["time"], f["id"]))
        return httpx.Response(200, json=rows[:limit])

    return httpx.MockTransport(handler)


def fill(n: int, at_ms: int, price: float, pnl: float, fee: float = 0.01,
         side: str = "BUY", order: int = 1) -> dict:
    return {"id": n, "orderId": order, "side": side, "time": at_ms, "price": str(price),
            "realizedPnl": str(pnl), "commission": str(fee)}


async def test_a_trade_closed_after_thirty_days_reads_its_closing_fill() -> None:
    now = int(time.time() * 1000)
    opened = now - 30 * DAY
    fills = [fill(1, opened, 100.0, 0.0), fill(2, now - DAY, 120.0, 20.0, side="SELL", order=2)]
    client = httpx.AsyncClient(base_url=DEMO_URL, transport=fake_binance(fills))
    result = await BinanceDemo(client, "k", "s").closed_result("SOLUSDT", opened)
    assert result.exit_price == 120.0
    assert abs(result.pnl - (20.0 - 0.02)) < 1e-9
    assert abs(result.fees - 0.02) < 1e-9


async def test_more_than_five_hundred_fills_in_one_week_are_all_counted() -> None:
    now = int(time.time() * 1000)
    opened = now - 3 * DAY
    fills = [fill(i, opened + i * 1000, 100.0, 0.0) for i in range(1, 1201)]
    fills.append(fill(5000, now - DAY, 90.0, -10.0, side="SELL", order=2))
    client = httpx.AsyncClient(base_url=DEMO_URL, transport=fake_binance(fills))
    result = await BinanceDemo(client, "k", "s").closed_result("SOLUSDT", opened)
    assert result.exit_price == 90.0
    assert abs(result.fees - 0.01 * 1201) < 1e-6


async def test_no_closing_fill_is_unproven_never_a_made_up_price() -> None:
    now = int(time.time() * 1000)
    opened = now - 10 * DAY
    only_entry = fake_binance([fill(1, opened, 100.0, 0)])
    client = httpx.AsyncClient(base_url=DEMO_URL, transport=only_entry)
    result = await BinanceDemo(client, "k", "s").closed_result("SOLUSDT", opened, "BUY")
    assert result.verified is False
    assert result.exit_price is None and result.pnl is None


async def test_an_earlier_trades_close_in_the_window_is_not_this_trade() -> None:
    now = int(time.time() * 1000)
    opened = now - 2 * DAY
    fills = [fill(1, opened, 50.0, 7.0, side="SELL", order=9),  # previous trade's close
             fill(2, opened + 1000, 100.0, 0.0),
             fill(3, now - DAY, 110.0, 10.0, side="SELL", order=2)]
    client = httpx.AsyncClient(base_url=DEMO_URL, transport=fake_binance(fills))
    result = await BinanceDemo(client, "k", "s").closed_result("SOLUSDT", opened, "BUY")
    assert result.exit_price == 110.0
    assert abs(result.pnl - (10.0 - 0.02)) < 1e-9
    assert result.scaled is False


async def test_a_close_in_several_fills_uses_their_average_price() -> None:
    now = int(time.time() * 1000)
    opened = now - DAY
    rows = [fill(1, opened, 100.0, 0.0),
            fill(2, now - 3000, 104.0, 1.0, side="SELL", order=2),
            fill(3, now - 2000, 106.0, 3.0, side="SELL", order=2)]
    rows[1]["qty"], rows[2]["qty"] = "1", "3"
    client = httpx.AsyncClient(base_url=DEMO_URL, transport=fake_binance(rows))
    result = await BinanceDemo(client, "k", "s").closed_result("SOLUSDT", opened, "BUY")
    assert abs(result.exit_price - 105.5) < 1e-9
    assert result.scaled is False  # one closing order in several fills is not a half


async def test_engine_records_an_unproven_close_without_inventing_a_result() -> None:
    from test_system import build

    from sonic.exchange import Closed

    engine, exchange, market, clock = build()
    await engine.start()
    await engine.tick()
    assert engine.store.open_trades()

    async def gone() -> dict:
        return {}

    async def unproven(symbol: str, since_ms: int, side: str | None = None) -> Closed:
        return Closed(None, None, 0.01, verified=False)

    exchange.positions, exchange.closed_result = gone, unproven
    clock.now += 60
    await engine.tick()
    closed = engine.store.trades()[0]
    assert closed["closed_ms"] is not None
    assert closed["exit_reason"] == "insufficient_data"
    assert closed["exit_price"] is None and closed["pnl"] is None and closed["r"] is None
    assert any(e["code"] == "trade_result_unproven" for e in engine.store.events(10))
    # an unproven loss would escape the loss guards: new entries stop until the owner looks
    assert engine.halted == "result_unproven" and not engine.running
    report = engine.month_report()
    assert report["unproven"] == 1 and report["trades"] == 0


async def test_an_opening_fill_slightly_before_the_recorded_time_still_counts() -> None:
    now = int(time.time() * 1000)
    opened = now - 2 * DAY
    fills = [fill(1, opened - 1500, 100.0, 0.0),  # Binance time 1.5 s before the local time
             fill(2, now - DAY, 108.0, 8.0, side="SELL", order=2)]
    client = httpx.AsyncClient(base_url=DEMO_URL, transport=fake_binance(fills))
    result = await BinanceDemo(client, "k", "s").closed_result("SOLUSDT", opened, "BUY")
    assert result.verified and result.exit_price == 108.0


async def test_no_fills_at_all_is_unproven_not_an_error() -> None:
    now = int(time.time() * 1000)
    client = httpx.AsyncClient(base_url=DEMO_URL, transport=fake_binance([]))
    result = await BinanceDemo(client, "k", "s").closed_result("SOLUSDT", now - DAY, "BUY")
    assert result.verified is False and result.exit_price is None


async def test_half_then_original_stop_on_the_exchange_is_a_stop_loss_with_the_half_taken() -> None:
    from test_system import build

    from sonic.exchange import Closed

    engine, exchange, _, clock = build()
    await engine.start()
    await engine.tick()
    trade = engine.store.open_trades()[0]

    async def gone() -> dict:
        return {}

    async def both(symbol: str, since_ms: int, side: str | None = None) -> Closed:
        # half at the target, then the rest at the ORIGINAL stop (the exchange never moved it)
        return Closed(trade["stop_price"], -0.3, 0.02, scaled=True)

    exchange.positions, exchange.closed_result = gone, both
    clock.now += 60
    await engine.tick()
    closed = engine.store.trades()[0]
    assert closed["scaled"] == 1 and closed["exit_reason"] == "stop_loss"
    assert closed["stop_price"] == trade["stop_price"]  # the stored stop is not rewritten
    assert closed["pnl"] == -0.3
