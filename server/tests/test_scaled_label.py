"""Half target and breakeven stop inside ONE hour must be recorded as a scaled breakeven."""

from __future__ import annotations

from test_system import DOWN_CROSS, build

from sonic.strategy import BAR_MS, Bar


async def test_half_then_breakeven_in_one_hour_is_labelled_breakeven() -> None:
    engine, _, market, clock = build()
    await engine.start()
    await engine.tick()
    trade = engine.store.open_trades()[0]
    entry, target = trade["entry_price"], trade["target_price"]
    hour = Bar(trade["opened_ms"], entry, target * 1.001, entry * 0.999, entry * 0.998)
    plain = market.bars

    async def bars(symbol: str, interval: str, limit: int) -> list[Bar]:
        if interval == "1h" and symbol != "BTCUSDT":
            return [hour]
        return await plain(symbol, interval, limit)

    market.bars = bars
    market.last = entry * 0.998  # the hour closed below the entry, after reaching the target
    clock.now += 3600
    await engine.tick()
    closed = engine.store.trades()[0]
    assert closed["closed_ms"] is not None
    assert closed["exit_reason"] == "breakeven"
    assert closed["scaled"] == 1
    assert any(e["code"] == "trade_scaled" for e in engine.store.events(10))


async def test_live_closed_result_sees_the_earlier_half() -> None:
    import httpx

    from sonic.exchange import DEMO_URL, BinanceDemo

    client = BinanceDemo(httpx.AsyncClient(base_url=DEMO_URL), "k", "s")
    fills = [
        {"orderId": 1, "side": "BUY", "price": "100", "commission": "0.01", "realizedPnl": "0"},
        {"orderId": 2, "side": "SELL", "price": "101", "commission": "0.01", "realizedPnl": "0.5"},
        {"orderId": 3, "side": "SELL", "price": "100", "commission": "0.01", "realizedPnl": "0"},
    ]

    async def signed(method, path, params):
        return fills

    async def nothing(symbol):
        return None

    client.signed, client.cancel_protection = signed, nothing
    assert (await client.closed_result("SOLUSDT", 0)).scaled is True
    fills[:] = [fills[0], fills[2]]  # one closing order only: no half before
    assert (await client.closed_result("SOLUSDT", 0)).scaled is False


async def test_plain_stop_without_half_stays_stop_loss_and_pnl_is_the_wallet_change() -> None:
    engine, exchange, market, clock = build()
    await engine.start()
    await engine.tick()
    trade = engine.store.open_trades()[0]
    before = await exchange.balance()
    market.last = trade["stop_price"] * 0.999
    clock.now += 60
    await engine.tick()
    closed = engine.store.trades()[0]
    assert closed["exit_reason"] == "stop_loss" and closed["scaled"] == 0
    entry_fee = closed["fees"] - abs(closed["qty"]) * closed["exit_price"] * 0.0005
    # the label never changes the money: pnl is exactly what the paper wallet gained or lost
    assert abs((await exchange.balance()) - before - (closed["pnl"] + entry_fee)) < 1e-9


async def test_label_fix_keeps_the_same_profit_as_the_wallet() -> None:
    engine, exchange, market, clock = build()
    await engine.start()
    start = await exchange.balance()
    await engine.tick()
    trade = engine.store.open_trades()[0]
    entry, target = trade["entry_price"], trade["target_price"]
    hour = Bar(trade["opened_ms"], entry, target * 1.001, entry * 0.999, entry * 0.998)
    plain = market.bars

    async def bars(symbol: str, interval: str, limit: int) -> list[Bar]:
        if interval == "1h" and symbol != "BTCUSDT":
            return [hour]
        return await plain(symbol, interval, limit)

    market.bars = bars
    market.last = entry * 0.998
    clock.now += 3600
    await engine.tick()
    closed = engine.store.trades()[0]
    assert closed["exit_reason"] == "breakeven"
    assert abs((await exchange.balance()) - start - closed["pnl"]) < 1e-9


async def test_trend_exit_after_the_half_keeps_its_own_reason() -> None:
    engine, _, market, clock = build()
    await engine.start()
    await engine.tick()
    trade = engine.store.open_trades()[0]
    market.last = trade["target_price"] * 1.001  # the half fills on the next check
    clock.now += 60
    await engine.tick()
    assert engine.store.open_trades()[0]["scaled"] == 1
    market.last = trade["entry_price"] * 1.0005  # above the moved stop
    market.coin = list(DOWN_CROSS)  # the EMAs turn on the next bar
    clock.now += BAR_MS // 1000
    await engine.tick()
    closed = engine.store.trades()[0]
    assert closed["exit_reason"] == "trade_exit_signal" and closed["scaled"] == 1
