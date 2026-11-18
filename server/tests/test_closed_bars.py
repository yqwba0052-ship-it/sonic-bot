"""Seen on the server 2026-11-18 00:00:03 UTC: the scan used the 16:00 bar for BTC because
Binance had not opened the 00:00 bar yet and the code always dropped "the last row" (which was
the 20:00 bar that had just closed). Closed bars must be chosen by their close time."""

from __future__ import annotations

import httpx

from sonic.exchange import PUBLIC_URL, PublicMarket
from sonic.strategy import BAR_MS, HISTORY_BARS

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


async def test_gaps_in_the_feed_do_not_change_which_bar_is_last() -> None:
    rows = [kline(T - (k + 1) * BAR_MS, 100.0 + k) for k in reversed(range(8))]
    del rows[3]  # a missing bar in the middle
    rows.append(kline(T, 7.0))  # forming
    bars = await market(rows, T + 1_000).bars("SOLUSDT", "4h", 5)
    assert bars[-1].t == T - BAR_MS and len(bars) == 5
    assert all(b.t + BAR_MS <= T for b in bars)


async def test_a_slow_answer_after_the_next_close_keeps_only_closed_bars() -> None:
    rows = [kline(T - (k + 1) * BAR_MS, 100.0 + k) for k in reversed(range(4))]
    rows.append(kline(T, 7.0))  # was forming when Binance answered, closed by the time we read
    bars = await market(rows, T + BAR_MS + 500).bars("SOLUSDT", "4h", 3)
    assert bars[-1].t == T  # judged by the clock at reading time: it has closed


# ---------- the engine waits for the same closed bar on BTC and every coin

def _engine_with_lagging(symbol: str):
    from test_system import build

    engine, exchange, market, clock = build(("SOLUSDT", "XRPUSDT"))
    plain = market.bars
    calls: dict[str, int] = {}
    lag = {"on": True}

    async def bars(sym: str, interval: str, limit: int):
        if limit == HISTORY_BARS:  # the scan's own fetches (the lab asks for 600)
            calls[sym] = calls.get(sym, 0) + 1
        out = await plain(sym, interval, limit)
        if sym == symbol and lag["on"] and interval == "4h":
            return out[:-1]  # Binance has not served the just-closed bar for this one yet
        return out

    market.bars = bars
    return engine, clock, calls, lag


async def test_a_coin_behind_btc_delays_the_scan_until_both_have_the_bar() -> None:
    from sonic import monitor

    engine, clock, calls, lag = _engine_with_lagging("XRPUSDT")
    await engine.start()
    await engine.tick()
    assert monitor.scans(engine.store.db) == []  # waiting, nothing judged on mixed bars
    assert int(engine.store.get("last_bar_ms", 0)) < engine.last_closed_bar()
    lag["on"] = False
    clock.now += 15
    await engine.tick()
    row = monitor.scans(engine.store.db)[0]
    assert row["bar_ms"] == engine.last_closed_bar() and row["data_ok"] == 1
    assert all(c.get("fresh", True) for c in row["coins"].values())
    assert not any(e["code"] == "stale_bars" for e in engine.store.events(50))


async def test_after_five_minutes_the_scan_goes_ahead_and_says_what_was_stale() -> None:
    from sonic import monitor
    from sonic.engine import FRESH_WAIT_MS

    engine, clock, _, _ = _engine_with_lagging("XRPUSDT")
    await engine.start()
    await engine.tick()
    assert monitor.scans(engine.store.db) == []
    clock.now += FRESH_WAIT_MS / 1000 + 1
    await engine.tick()
    row = monitor.scans(engine.store.db)[0]
    assert row["coins"]["XRPUSDT"]["fresh"] is False and row["coins"]["SOLUSDT"]["fresh"] is True
    assert any(e["code"] == "stale_bars" and "XRPUSDT" in e["detail"]
               for e in engine.store.events(50))


async def test_one_fetch_per_symbol_per_scan() -> None:
    engine, _, calls, lag = _engine_with_lagging("NONE")
    await engine.start()
    calls.clear()
    await engine.tick()
    assert calls["BTCUSDT"] == 1 and calls["SOLUSDT"] == 1 and calls["XRPUSDT"] == 1


async def test_a_restart_while_waiting_scans_the_bar_once_and_opens_once(tmp_path) -> None:
    from test_system import Clock, FakeMarket, settings

    from sonic import monitor
    from sonic.engine import Engine
    from sonic.exchange import PaperExchange
    from sonic.risk import Limits
    from sonic.store import Store

    path = str(tmp_path / "sonic.db")
    clock = Clock()
    market = FakeMarket()
    market.clock = clock
    plain = market.bars
    lag = {"on": True}

    async def bars(sym: str, interval: str, limit: int):
        out = await plain(sym, interval, limit)
        return out[:-1] if lag["on"] and sym == "BTCUSDT" and interval == "4h" else out

    market.bars = bars

    def engine_on(store: Store) -> Engine:
        e = Engine(store, PaperExchange(market, store, 10000.0), settings(), clock=clock)
        e.limits = Limits(symbols=["SOLUSDT"])
        return e

    store = Store(path)
    store.put("auto_resume", True)
    first = engine_on(store)
    await first.start()
    await first.tick()  # waiting for BTC
    store.db.close()
    lag["on"] = False
    store2 = Store(path)
    second = engine_on(store2)
    clock.now += 15
    await second.tick()
    clock.now += 15
    await second.tick()
    assert len(store2.trades()) == 1 and len(monitor.scans(store2.db)) == 1


async def test_a_coin_without_recent_data_is_noted_but_never_delays_the_scan() -> None:
    from test_system import build

    from sonic import monitor

    engine, _, market, _ = build(("SOLUSDT", "XRPUSDT"))
    plain = market.bars

    async def bars(sym: str, interval: str, limit: int):
        out = await plain(sym, interval, limit)
        return out[:-10] if sym == "XRPUSDT" and interval == "4h" else out  # 40 h old

    market.bars = bars
    await engine.start()
    await engine.tick()
    row = monitor.scans(engine.store.db)[0]
    assert row["bar_ms"] == engine.last_closed_bar()
    assert row["coins"]["XRPUSDT"]["fresh"] is False
    assert any(e["code"] == "stale_bars" for e in engine.store.events(50))
