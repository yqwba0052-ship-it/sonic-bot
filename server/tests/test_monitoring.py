"""Scan log and daily balance: they record, never decide, and never stop trading."""

from __future__ import annotations

import random

from fastapi.testclient import TestClient
from test_system import BTC_DOWN, build, make_bars

from sonic import monitor, strategy
from sonic.api import create_app
from sonic.engine import Engine
from sonic.exchange import PaperExchange
from sonic.store import Store
from sonic.strategy import BAR_MS


def test_entry_reason_agrees_with_entry_signal_everywhere() -> None:
    rng = random.Random(7)
    checked = {"buy": 0, "sell": 0}
    for _ in range(400):
        closes = [100.0]
        drift = rng.uniform(-0.004, 0.004)
        for _ in range(rng.randint(250, 420)):
            closes.append(closes[-1] * (1 + drift + rng.gauss(0, 0.02)))
        bars = make_bars(closes)
        for btc_up in (True, False):
            side = strategy.entry_signal(bars, btc_up)
            reason = strategy.entry_reason(bars, btc_up)
            assert (reason == "buy") == (side == "BUY")
            assert (reason == "sell") == (side == "SELL")
            if reason in checked:
                checked[reason] += 1
    assert checked["buy"] > 0 and checked["sell"] > 0  # both signal paths were exercised


async def test_every_scan_is_logged_with_its_reasons() -> None:
    engine, _, _, _ = build(("SOLUSDT", "XRPUSDT"))
    await engine.start()
    await engine.tick()
    row = monitor.scans(engine.store.db)[0]
    assert row["bar_ms"] == engine.last_closed_bar()
    assert row["status"] == "scanned" and row["strategy"] == strategy.NAME
    assert row["market"] == "btc_up" and row["signals"] == 2 and row["opened"] >= 1
    assert row["btc_ema300"] and row["btc_gap_pct"] > 0
    assert row["coins"]["SOLUSDT"]["result"] == "opened"
    assert set(row["coins"]) == {"SOLUSDT", "XRPUSDT"}


async def test_btc_below_its_average_is_the_logged_reason() -> None:
    engine, _, market, _ = build()
    market.btc = list(BTC_DOWN)
    await engine.start()
    await engine.tick()
    row = monitor.scans(engine.store.db)[0]
    assert row["market"] == "btc_down" and row["signals"] == 0 and row["btc_gap_pct"] < 0
    assert row["coins"]["SOLUSDT"]["result"] == "cross_up_btc_down"
    assert engine.store.open_trades() == []


async def test_a_stopped_bot_logs_not_running_and_opens_nothing() -> None:
    engine, _, _, _ = build()
    await engine.tick()
    row = monitor.scans(engine.store.db)[0]
    assert row["status"] == "not_running" and engine.store.open_trades() == []


async def test_a_missed_bar_is_a_gap_and_a_quiet_bar_is_not() -> None:
    engine, _, market, clock = build()
    market.btc = list(BTC_DOWN)  # no signal at all: quiet scans
    await engine.start()
    await engine.tick()
    first = engine.last_closed_bar()
    clock.now += BAR_MS // 1000
    await engine.tick()  # quiet scan of the next bar
    clock.now += 2 * BAR_MS // 1000  # the server misses one whole bar
    await engine.tick()
    gaps = monitor.scan_gaps(engine.store.db, engine.last_closed_bar())
    assert gaps["since_bar_ms"] == first
    assert gaps["missed"] == [first + 2 * BAR_MS]
    assert gaps["errors"] == []


async def test_a_failed_scan_is_logged_then_retried() -> None:
    engine, _, market, clock = build()
    await engine.start()
    plain = market.bars
    calls = {"n": 0}

    async def broken(symbol: str, interval: str, limit: int):
        if symbol == "BTCUSDT" and calls["n"] == 0:
            calls["n"] += 1
            raise RuntimeError("feed down")
        return await plain(symbol, interval, limit)

    market.bars = broken
    await engine.tick()
    row = monitor.scans(engine.store.db)[0]
    assert row["status"] == "error" and "feed down" in row["error"]
    clock.now += 15
    await engine.tick()
    row = monitor.scans(engine.store.db)[0]
    assert row["status"] == "scanned" and row["attempts"] == 2 and row["error"] is None
    assert len(engine.store.open_trades()) == 1


async def test_a_broken_scan_log_never_stops_trading(monkeypatch) -> None:
    engine, _, _, _ = build()

    def boom(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(monitor, "record_scan", boom)
    await engine.start()
    await engine.tick()
    assert len(engine.store.open_trades()) == 1
    assert engine.running and not engine.halted and engine.failures == 0
    assert any(e["code"] == "scan_log_failed" for e in engine.store.events(20))


async def test_a_broken_balance_snapshot_never_stops_trading(monkeypatch) -> None:
    engine, _, _, _ = build()

    def boom(*args, **kwargs):
        raise RuntimeError("locked")

    monkeypatch.setattr(monitor, "write_snapshot", boom)
    await engine.start()
    await engine.tick()
    assert len(engine.store.open_trades()) == 1 and engine.failures == 0
    assert any(e["code"] == "equity_snapshot_failed" for e in engine.store.events(20))


async def test_old_scan_rows_are_pruned() -> None:
    engine, _, _, _ = build()
    db = engine.store.db
    old = engine.last_closed_bar() - (monitor.SCAN_KEEP_DAYS + 5) * monitor.DAY_MS
    monitor.record_scan(db, old, 0, {"status": "scanned"})
    monitor.record_scan(db, engine.last_closed_bar(), 0, {"status": "scanned"})
    assert [r["bar_ms"] for r in monitor.scans(db)] == [engine.last_closed_bar()]


async def test_one_snapshot_per_day_and_drawdown_counts_open_trades() -> None:
    engine, _, market, clock = build()
    await engine.start()
    await engine.tick()
    trade = engine.store.open_trades()[0]
    days = monitor.snapshots(engine.store.db)
    # the day's snapshot is the state seen at its first tick: before this bar's entry
    assert len(days) == 1 and days[0]["open_count"] == 0 and days[0]["closed_count"] == 0
    await engine.tick()
    assert len(monitor.snapshots(engine.store.db)) == 1  # same day: no second row
    # price falls but stays above the stop: no cash change, still a real drawdown
    market.last = (trade["entry_price"] + trade["stop_price"]) / 2
    clock.now += 60
    await engine.tick()
    peak = engine.store.get("equity_peak")
    assert peak["max_dd_pct"] > 0
    clock.now += 86_400
    await engine.tick()
    days = monitor.snapshots(engine.store.db)
    assert len(days) == 2
    today = days[0]
    assert today["open_count"] == 1
    assert today["unrealized"] < 0 and today["drawdown_pct"] > 0
    assert today["equity"] < today["peak"] and today["max_drawdown_pct"] >= today["drawdown_pct"]
    parts = today["capital"] + today["realized"] + today["unrealized"]
    assert abs(today["equity"] - parts) < 1e-6


async def test_restart_keeps_trades_and_never_opens_the_same_bar_twice(tmp_path) -> None:
    engine, exchange, market, clock = build()
    path = str(tmp_path / "sonic.db")
    store = Store(path)
    exchange = PaperExchange(market, store, 10000.0)
    engine = Engine(store, exchange, engine.settings, clock=clock)
    engine.limits = engine.limits.model_copy(update={"symbols": ["SOLUSDT"]})
    store.put("auto_resume", True)
    await engine.start()
    await engine.tick()
    assert len(store.open_trades()) == 1
    store.db.close()
    store2 = Store(path)
    engine2 = Engine(store2, PaperExchange(market, store2, 10000.0), engine.settings, clock=clock)
    engine2.limits = engine.limits
    clock.now += 15
    await engine2.tick()
    assert len(store2.open_trades()) == 1 and len(store2.trades()) == 1
    assert len(monitor.scans(store2.db)) == 1


async def test_scan_and_equity_routes_need_the_token() -> None:
    engine, _, _, _ = build()
    await engine.start()
    await engine.tick()
    client = TestClient(create_app(engine, "secret", background=False))
    assert client.get("/scans").status_code == 401
    assert client.get("/equity").status_code == 401
    auth = {"Authorization": "Bearer secret"}
    body = client.get("/scans", headers=auth).json()
    assert body["scans"][0]["coins"]["SOLUSDT"]["result"] == "opened"
    assert body["gaps"]["missed"] == []
    eq = client.get("/equity", headers=auth).json()
    assert len(eq["days"]) == 1 and eq["peak"]["peak"] > 0


def test_paper_mode_never_builds_an_order_client() -> None:
    import inspect

    from sonic import __main__ as entry
    from sonic import capital

    source = inspect.getsource(entry.main)
    assert 'if settings.mode == "demo"' in source and "PaperExchange(" in source
    assert "SONIC_LIVE_TRADING" not in source
    assert capital.live_allowed() is False  # the live switch is off in this environment


async def test_an_error_before_the_scan_does_not_reuse_the_previous_bars_data() -> None:
    engine, exchange, _, clock = build()
    await engine.start()
    await engine.tick()  # bar N scanned, coins noted
    plain = exchange.positions

    async def broken():
        raise RuntimeError("sync down")

    exchange.positions = broken
    clock.now += BAR_MS // 1000
    await engine.tick()
    row = monitor.scans(engine.store.db)[0]
    assert row["bar_ms"] == engine.last_closed_bar() and row["status"] == "error"
    assert row["coins"] == {} and row["btc_close"] is None
    exchange.positions = plain


async def test_a_new_amount_restarts_the_peak() -> None:
    engine, _, _, _ = build()
    await engine.tick()  # tracking runs while the bot is stopped too
    first = engine.store.get("equity_peak")
    await engine.set_limits(engine.limits.model_copy(update={"capital_usdt": 20.0}))
    await engine.tick()
    second = engine.store.get("equity_peak")
    assert second["basis"][0] == 20.0 and second["max_dd_pct"] == 0.0
    assert second["since_ms"] >= first["since_ms"]
