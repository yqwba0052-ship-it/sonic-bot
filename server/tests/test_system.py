from __future__ import annotations

import hashlib
import hmac
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from sonic import strategy
from sonic.api import create_app
from sonic.config import Settings
from sonic.engine import Engine
from sonic.exchange import DEMO_URL, PUBLIC_URL, BinanceDemo, PaperExchange
from sonic.risk import (
    ALLOWED_SYMBOLS,
    Filters,
    Limits,
    min_amount_for,
    size_position,
    stop_price,
)
from sonic.store import Store
from sonic.strategy import BAR_MS, Bar

FILTERS = Filters("0.001", "0.01", 0.001, 5.0)
T0 = 1_800_000_000.0


def make_bars(closes: list[float]) -> list[Bar]:
    return [Bar(i * BAR_MS, c, c * 1.01, c * 0.99, c) for i, c in enumerate(closes)]


def series_until(signal: str, btc_up: bool) -> list[float]:
    """A falling (or rising) market that turns until the EMAs cross on the last bar."""
    step = -0.05 if signal == "BUY" else 0.05
    closes = [100 + i * step for i in range(400)]
    turn = 1.01 if signal == "BUY" else 0.99
    while strategy.entry_signal(make_bars(closes), btc_up) != signal:
        closes.append(closes[-1] * turn)
    return closes


UP_CROSS = series_until("BUY", True)
DOWN_CROSS = series_until("SELL", False)
BTC_UP = [50_000 * 1.001**i for i in range(400)]
BTC_DOWN = [50_000 * 0.999**i for i in range(400)]


class FakeMarket:
    def __init__(self) -> None:
        self.coin = list(UP_CROSS)
        self.btc = list(BTC_UP)
        self.last = self.coin[-1]

    async def filters(self, symbol: str) -> Filters:
        return FILTERS

    async def price(self, symbol: str) -> float:
        return self.last

    async def prices(self) -> dict[str, float]:
        return {symbol: self.last for symbol in ALLOWED_SYMBOLS}

    async def bars(self, symbol: str, interval: str, limit: int) -> list[Bar]:
        return make_bars(self.btc if symbol == "BTCUSDT" else self.coin)

    async def bars_until(self, symbol: str, interval: str, end_ms: int, limit: int) -> list[Bar]:
        return [b for b in await self.bars(symbol, interval, limit) if b.t < end_ms][-limit:]


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> float:
        return self.now

    def next_bar(self, bars: int = 1) -> None:
        self.now += bars * BAR_MS / 1000


def settings() -> Settings:
    return Settings(mode="paper", token="secret", db_path=":memory:")


def build(
    symbols: tuple[str, ...] = ("SOLUSDT",), **limits: float
) -> tuple[Engine, PaperExchange, FakeMarket, Clock]:
    store = Store(":memory:")
    market = FakeMarket()
    exchange = PaperExchange(market, store, 10000.0)
    clock = Clock()
    engine = Engine(store, exchange, settings(), clock=clock)
    engine.limits = Limits(symbols=list(symbols), **limits)
    return engine, exchange, market, clock


# ---------- limits and sizing


def test_default_limits_are_the_tested_small_account() -> None:
    limits = Limits()
    assert limits.capital_usdt == 30
    assert limits.max_open == 3
    assert limits.leverage == 3
    assert limits.risk_per_trade_pct == 1
    assert not limits.allow_shorts
    assert "BTCUSDT" not in limits.symbols


def test_leverage_24_is_refused() -> None:
    with pytest.raises(ValueError):
        Limits(leverage=24)


def test_limits_reject_bad_relations() -> None:
    with pytest.raises(ValueError, match="daily_loss_above_capital"):
        Limits(capital_usdt=10, max_daily_loss_usdt=30)
    with pytest.raises(ValueError, match="unknown_symbol"):
        Limits(symbols=["SCAMUSDT"])
    with pytest.raises(ValueError):
        Limits(max_open=10)


def test_size_risks_the_set_share_of_the_amount() -> None:
    size = size_position(Limits(capital_usdt=1000, max_daily_loss_usdt=50), 100.0, 5.0, 0.0,
                         FILTERS, 1000.0)
    assert size.qty == 2.0  # 1% of 1000 = 10 USDT at a 5 USDT stop


def test_small_amount_is_raised_to_the_exchange_minimum_when_risk_allows() -> None:
    size = size_position(Limits(risk_per_trade_pct=1), 100.0, 7.0, 0.0, FILTERS, 30.0)
    assert size.qty == 0.05  # 5 USDT order, stop risks 0.35 USDT <= 2% of 30


def test_too_small_amount_skips_the_trade() -> None:
    limits = Limits(capital_usdt=10, max_daily_loss_usdt=3, risk_per_trade_pct=1)
    assert size_position(limits, 100.0, 7.0, 0.0, FILTERS, 10.0).reason == "amount_too_small"
    # At 2% risk the cap is 4%: a 0.35 USDT stop on 10 USDT fits.
    two = Limits(capital_usdt=10, max_daily_loss_usdt=3, risk_per_trade_pct=2)
    assert size_position(two, 100.0, 7.0, 0.0, FILTERS, 10.0).qty == 0.05


def test_margin_must_fit_in_the_amount() -> None:
    limits = Limits(capital_usdt=100, max_daily_loss_usdt=10, leverage=1)
    assert size_position(limits, 100.0, 5.0, 99.0, FILTERS, 100.0).reason == "capital_in_use"


def test_min_amount_matches_research_rule() -> None:
    assert min_amount_for(5.0, 0.063, 3, 1.0) == pytest.approx(15.75)
    assert min_amount_for(76.0, 0.063, 3, 1.0) == pytest.approx(239.4)
    assert min_amount_for(5.0, 0.063, 5, 2.0) == pytest.approx(7.875)


def test_stop_price_sits_on_the_losing_side() -> None:
    assert stop_price("BUY", 100.0, 6.0, "0.01") == 94.0
    assert stop_price("SELL", 100.0, 6.0, "0.01") == 106.0


# ---------- strategy


def test_entry_needs_a_cross_and_the_btc_trend() -> None:
    assert strategy.entry_signal(make_bars(UP_CROSS), btc_up=True) == "BUY"
    assert strategy.entry_signal(make_bars(UP_CROSS), btc_up=False) is None
    assert strategy.entry_signal(make_bars(DOWN_CROSS), btc_up=False) == "SELL"
    assert strategy.entry_signal(make_bars(UP_CROSS[:-1]), btc_up=True) is None


def test_exit_when_trend_turns() -> None:
    assert not strategy.exit_due(make_bars(UP_CROSS), "BUY")
    assert strategy.exit_due(make_bars(DOWN_CROSS), "BUY")
    assert strategy.exit_due(make_bars(UP_CROSS), "SELL")


def test_btc_regime() -> None:
    assert strategy.btc_uptrend(make_bars(BTC_UP))
    assert not strategy.btc_uptrend(make_bars(BTC_DOWN))


# ---------- engine


async def test_opens_once_per_bar_with_a_stop_and_a_half_target() -> None:
    engine, exchange, _, _ = build()
    await engine.start()
    await engine.tick()
    await engine.tick()
    trades = engine.store.open_trades()
    assert len(trades) == 1
    trade = trades[0]
    assert trade["side"] == "BUY"
    paper = exchange.state["open"]["SOLUSDT"]
    assert paper["stop"] == trade["stop_price"]
    distance = trade["entry_price"] - trade["stop_price"]
    half = trade["entry_price"] + distance * strategy.HALF_AT_R
    assert trade["target_price"] == pytest.approx(half, abs=0.02)
    assert paper["target_qty"] == pytest.approx(trade["qty"] / 2, abs=0.001)
    assert trade["risk_usdt"] > 0


async def test_no_entry_while_stopped() -> None:
    engine, _, _, _ = build()
    await engine.tick()
    assert engine.store.open_trades() == []


async def test_no_long_against_a_falling_btc() -> None:
    engine, _, market, _ = build()
    market.btc = list(BTC_DOWN)
    await engine.start()
    await engine.tick()
    assert engine.store.open_trades() == []


async def test_stop_loss_closes_with_fees_and_r_near_minus_one() -> None:
    engine, _, market, _ = build()
    await engine.start()
    await engine.tick()
    stop = engine.store.open_trades()[0]["stop_price"]
    market.last = stop * 0.999
    await engine.tick()
    trade = engine.store.trades()[0]
    assert trade["closed_ms"] is not None
    assert trade["fees"] > 0
    assert -1.3 < trade["r"] < -0.9


async def test_exit_when_the_trend_turns_on_the_next_bar() -> None:
    engine, _, market, clock = build()
    await engine.start()
    await engine.tick()
    market.coin = list(DOWN_CROSS)
    clock.next_bar()
    await engine.tick()
    assert engine.store.open_trades() == []
    assert engine.store.events(5)[1]["code"] == "trade_exit_signal" or any(
        e["code"] == "trade_exit_signal" for e in engine.store.events(10)
    )


async def test_exit_after_twenty_days() -> None:
    engine, _, _, clock = build()
    await engine.start()
    await engine.tick()
    await engine.stop()
    clock.next_bar(strategy.MAX_HOLD_BARS)
    await engine.tick()
    assert engine.store.open_trades() == []
    assert any(e["code"] == "trade_exit_time" for e in engine.store.events(10))


async def test_never_more_open_trades_than_allowed() -> None:
    engine, _, _, _ = build(("SOLUSDT", "XRPUSDT", "ADAUSDT"), max_open=2)
    await engine.start()
    await engine.tick()
    assert len(engine.store.open_trades()) == 2


async def test_daily_loss_limit_closes_and_latches() -> None:
    engine, exchange, market, _ = build(capital_usdt=100, max_daily_loss_usdt=0.3)
    await engine.start()
    await engine.tick()
    market.last *= 0.97
    await engine.tick()
    assert engine.halted == "daily_loss_limit"
    assert exchange.state["open"] == {}
    with pytest.raises(ValueError):
        await engine.start()


async def test_losing_the_whole_amount_closes_and_halts() -> None:
    engine, exchange, market, _ = build(capital_usdt=30, max_daily_loss_usdt=30)
    await engine.start()
    await engine.tick()
    assert engine.store.open_trades()
    # Other losses already used 27 of the 30 USDT (closed yesterday: no daily-limit halt).
    old = engine.store.open_trade(
        symbol="XRPUSDT", side="BUY", qty=1, entry_price=1, stop_price=0.9, target_price=0,
        margin=1, opened_ms=0, risk_usdt=1,
    )
    engine.store.close_trade(old, int(T0 * 1000) - 86_400_000, 0.5, -27.0, 0.1)
    market.last *= 0.01  # crash far through the stop
    await engine.tick()
    assert engine.halted == "budget_used"
    assert exchange.state["open"] == {}
    with pytest.raises(ValueError, match="budget_still_used"):
        await engine.clear_halt()


async def test_drift_alarm_stops_new_entries_after_a_bad_run() -> None:
    engine, _, _, clock = build()
    for i in range(strategy.DRIFT_WINDOW):
        trade_id = engine.store.open_trade(
            symbol="XRPUSDT", side="BUY", qty=1, entry_price=1, stop_price=0.9,
            target_price=0, margin=1, opened_ms=i, risk_usdt=0.1,
        )
        engine.store.close_trade(trade_id, int(T0 * 1000) - 1000 + i, 0.9, -0.1, 0.0)
    await engine.start()
    await engine.tick()
    assert engine.halted == "strategy_drift"
    assert engine.store.open_trades() == []
    clock.next_bar()
    await engine.clear_halt()
    assert engine.recent_r() == []


async def test_panic_closes_everything_and_blocks_entries() -> None:
    engine, exchange, _, clock = build()
    await engine.start()
    await engine.tick()
    await engine.panic()
    clock.next_bar()
    await engine.tick()
    assert exchange.state["open"] == {}
    assert engine.store.open_trades() == []
    assert engine.halted == "panic_button"


async def test_failed_protection_closes_position_and_halts() -> None:
    engine, exchange, _, _ = build()

    async def broken(*_: object) -> None:
        raise RuntimeError("rejected")

    exchange.protect = broken  # type: ignore[method-assign]
    await engine.start()
    await engine.tick()
    assert engine.halted == "protection_failed"
    assert exchange.state["open"] == {}


async def test_order_that_timed_out_but_filled_halts_as_unknown_position() -> None:
    engine, exchange, _, _ = build()
    await exchange.open_market("SOLUSDT", "BUY", 1.0, "lost-response")
    await engine.start()
    await engine.tick()
    assert engine.halted == "unknown_position"


async def test_short_outages_do_not_halt_but_long_ones_do() -> None:
    engine, exchange, _, clock = build()
    real_positions = exchange.positions

    async def down() -> dict:
        raise RuntimeError("network down")

    exchange.positions = down  # type: ignore[method-assign]
    await engine.start()
    for _ in range(20):  # 20 failed ticks within 29 minutes, e.g. waking from sleep
        await engine.tick()
        clock.now += 85
    assert engine.halted == ""
    assert engine.status()["failing_since_ms"] is not None
    assert sum(e["code"] == "tick_failed" for e in engine.store.events(50)) == 1
    exchange.positions = real_positions  # type: ignore[method-assign]
    await engine.tick()
    assert engine.status()["failing_since_ms"] is None
    assert any(e["code"] == "connection_restored" for e in engine.store.events(5))
    exchange.positions = down  # type: ignore[method-assign]
    await engine.tick()
    clock.now += 31 * 60
    await engine.tick()
    assert engine.halted == "repeated_failures"


async def test_restart_never_resumes_and_keeps_halt_and_limits() -> None:
    engine, exchange, _, _ = build()
    await engine.set_limits(Limits(capital_usdt=50, max_daily_loss_usdt=10))
    engine.halt("panic_button")
    restarted = Engine(engine.store, exchange, settings())
    assert not restarted.running
    assert restarted.halted == "panic_button"
    assert restarted.limits.capital_usdt == 50


async def test_limits_cannot_change_while_running() -> None:
    engine, _, _, _ = build()
    await engine.start()
    with pytest.raises(ValueError):
        await engine.set_limits(Limits())


async def test_old_database_gets_new_columns(tmp_path) -> None:
    import sqlite3

    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute(
        "CREATE TABLE trades (id INTEGER PRIMARY KEY, symbol TEXT, side TEXT, qty REAL, "
        "entry_price REAL, stop_price REAL, target_price REAL, margin REAL, opened_ms INTEGER, "
        "closed_ms INTEGER, exit_price REAL, pnl REAL, fees REAL)"
    )
    old.commit()
    old.close()
    store = Store(str(path))
    columns = {row["name"] for row in store.db.execute("PRAGMA table_info(trades)")}
    assert {"risk_usdt", "r"} <= columns


# ---------- API


def test_api_requires_token_and_reports_status_markets_and_insights() -> None:
    engine, _, _, _ = build()
    with TestClient(create_app(engine, "secret", background=False)) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/status").status_code == 401
        headers = {"Authorization": "Bearer secret"}
        status = client.get("/status", headers=headers).json()
        assert status["strategy"] == strategy.NAME
        assert status["budget_left"] == 30
        markets = client.get("/markets", headers=headers).json()
        assert len(markets) == 12
        assert all(item["min_amount_usdt"] > 0 for item in markets)
        insights = client.get("/insights", headers=headers).json()
        assert insights["window"] == strategy.DRIFT_WINDOW


def test_api_explains_limit_errors() -> None:
    engine, _, _, _ = build()
    with TestClient(create_app(engine, "secret", background=False)) as client:
        headers = {"Authorization": "Bearer secret"}
        response = client.put("/limits", headers=headers,
                              json={"capital_usdt": 10, "max_daily_loss_usdt": 30})
        assert response.json()["detail"] == "daily_loss_above_capital"
        response = client.put("/limits", headers=headers, json={"symbols": ["SCAMUSDT"]})
        assert response.json()["detail"] == "unknown_symbol"
        response = client.put("/limits", headers=headers,
                              json={"capital_usdt": 6, "max_daily_loss_usdt": 2})
        assert response.json()["detail"] == "coin_below_minimum"
        response = client.put("/limits", headers=headers, json={"leverage": 50})
        assert response.json()["detail"] == "invalid_values"


# ---------- Binance demo client


def test_demo_client_refuses_any_other_url() -> None:
    with pytest.raises(ValueError):
        BinanceDemo(httpx.AsyncClient(base_url=PUBLIC_URL), "k", "s")


async def test_demo_client_signs_and_sends_only_a_stop() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"algoId": 1})

    client = httpx.AsyncClient(base_url=DEMO_URL, transport=httpx.MockTransport(handler))
    await BinanceDemo(client, "key", "secret").protect("BTCUSDT", "BUY", 74000.5, None)
    assert [item.url.path for item in seen] == ["/fapi/v1/algoOrder"]
    query = seen[0].url.query.decode()
    unsigned, signature = query.rsplit("&signature=", 1)
    assert signature == hmac.new(b"secret", unsigned.encode(), hashlib.sha256).hexdigest()
    fields = parse_qs(urlsplit(f"?{query}").query)
    assert fields["type"] == ["STOP_MARKET"]
    assert fields["side"] == ["SELL"]
    assert fields["closePosition"] == ["true"]


async def test_demo_close_still_sends_order_when_cancel_fails() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(f"{request.method} {request.url.path}")
        if request.method == "DELETE":
            return httpx.Response(400, json={"code": -2011, "msg": "nothing to cancel"})
        return httpx.Response(200, json={})

    client = httpx.AsyncClient(base_url=DEMO_URL, transport=httpx.MockTransport(handler))
    await BinanceDemo(client, "key", "secret").close_market("BTCUSDT", -0.5, "id1")
    assert paths == ["DELETE /fapi/v1/algoOpenOrders", "POST /fapi/v1/order"]


async def test_paper_catches_a_stop_touched_while_the_laptop_slept() -> None:
    engine, exchange, market, _ = build()
    await engine.start()
    await engine.tick()
    pos = exchange.state["open"]["SOLUSDT"]
    stop = pos["stop"]
    slept_from = pos["checked_ms"]
    dip = Bar(slept_from + 60_000, stop * 1.01, stop * 1.02, stop * 0.98, stop * 1.01)
    real_bars = market.bars

    async def bars_with_dip(symbol: str, interval: str, limit: int) -> list[Bar]:
        return [dip] if interval == "1h" else await real_bars(symbol, interval, limit)

    market.bars = bars_with_dip  # type: ignore[method-assign]
    await engine.tick()  # the price is back above the stop now, but the dip counts
    trade = engine.store.trades()[0]
    assert trade["closed_ms"] is not None
    assert trade["exit_price"] == pytest.approx(stop * (1 - 0.0002))


async def test_no_shorts_unless_the_owner_allows_them() -> None:
    engine, _, market, clock = build()
    market.coin = list(DOWN_CROSS)
    market.btc = list(BTC_DOWN)
    await engine.start()
    await engine.tick()
    assert engine.store.open_trades() == []
    await engine.stop()
    await engine.set_limits(engine.limits.model_copy(update={"allow_shorts": True}))
    await engine.start()
    clock.next_bar()
    await engine.tick()
    assert engine.store.open_trades()[0]["side"] == "SELL"


async def test_half_closes_at_the_target_and_the_rest_cannot_lose() -> None:
    engine, exchange, market, _ = build()
    await engine.start()
    await engine.tick()
    trade = engine.store.open_trades()[0]
    market.last = trade["target_price"] * 1.001
    await engine.tick()
    scaled = engine.store.open_trades()[0]
    assert scaled["scaled"] == 1
    assert scaled["stop_price"] == trade["entry_price"]
    assert exchange.state["open"]["SOLUSDT"]["qty"] == pytest.approx(trade["qty"] / 2, abs=0.001)
    assert any(e["code"] == "trade_scaled" for e in engine.store.events(5))
    market.last = trade["entry_price"] * 0.999  # back to the entry: the rest stops out
    await engine.tick()
    closed = engine.store.trades()[0]
    assert closed["closed_ms"] is not None
    assert closed["r"] > 0  # half the target earned, minus fees: still a winning trade


async def test_paper_replays_a_target_then_a_stop_missed_during_sleep() -> None:
    engine, exchange, market, _ = build()
    await engine.start()
    await engine.tick()
    trade = engine.store.open_trades()[0]
    since = exchange.state["open"]["SOLUSDT"]["checked_ms"]
    up = Bar(since + 60_000, trade["entry_price"], trade["target_price"] * 1.01,
             trade["entry_price"], trade["target_price"])
    down = Bar(since + 3_660_000, trade["target_price"], trade["target_price"],
               trade["entry_price"] * 0.99, trade["entry_price"] * 0.99)
    real_bars = market.bars

    async def slept(symbol: str, interval: str, limit: int) -> list[Bar]:
        return [up, down] if interval == "1h" else await real_bars(symbol, interval, limit)

    market.bars = slept  # type: ignore[method-assign]
    await engine.tick()
    closed = engine.store.trades()[0]
    assert closed["closed_ms"] is not None
    assert closed["pnl"] > 0


async def test_demo_places_half_take_profit_and_moves_the_stop() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"algoId": 1})

    client = httpx.AsyncClient(base_url=DEMO_URL, transport=httpx.MockTransport(handler))
    demo = BinanceDemo(client, "key", "secret")
    await demo.protect("SOLUSDT", "BUY", 90.0, 105.0, 0.05)
    first = parse_qs(urlsplit(f"?{seen[0].url.query.decode()}").query)
    second = parse_qs(urlsplit(f"?{seen[1].url.query.decode()}").query)
    assert first["type"] == ["TAKE_PROFIT_MARKET"]
    assert first["quantity"] == ["0.05"]
    assert first["reduceOnly"] == ["true"]
    assert "closePosition" not in first
    assert second["type"] == ["STOP_MARKET"]
    assert second["closePosition"] == ["true"]
    seen.clear()
    await demo.move_stop("SOLUSDT", "BUY", 100.0)
    assert [f"{r.method} {r.url.path}" for r in seen] == [
        "DELETE /fapi/v1/algoOpenOrders",
        "POST /fapi/v1/algoOrder",
    ]


async def test_keep_awake_is_on_by_default_and_can_be_switched_off(monkeypatch) -> None:
    from sonic import awake

    calls: list[bool] = []
    monkeypatch.setattr(awake, "set_keep_awake", lambda on: calls.append(on) or True)
    engine, _, _, _ = build()
    assert engine.keep_awake is True
    with TestClient(create_app(engine, "secret", background=False)) as client:
        headers = {"Authorization": "Bearer secret"}
        bad = client.put("/settings", headers=headers, json={"keep_awake": "yes"})
        assert bad.status_code == 422
        body = client.put("/settings", headers=headers, json={"keep_awake": False}).json()
        assert body["keep_awake"] is False
    assert calls == [False]
    restarted = Engine(engine.store, engine.exchange, settings())
    assert restarted.keep_awake is False


def test_host_comes_from_the_environment(tmp_path, monkeypatch) -> None:
    from sonic.config import load_settings

    monkeypatch.setenv("SONIC_HOST", "127.0.0.1")
    loaded = load_settings(tmp_path)
    assert loaded.host == "127.0.0.1"
    assert "SONIC_TOKEN=" in (tmp_path / ".env").read_text(encoding="utf-8")


async def test_resaving_limits_keeps_past_losses() -> None:
    engine, _, _, clock = build()
    await engine.set_limits(Limits())
    old = engine.store.open_trade(
        symbol="XRPUSDT", side="BUY", qty=1, entry_price=1, stop_price=0.9, target_price=0,
        margin=1, opened_ms=0, risk_usdt=1,
    )
    clock.next_bar()
    engine.store.close_trade(old, engine.now_ms(), 0.9, -5.0, 0.1)
    clock.next_bar()
    await engine.set_limits(Limits(max_open=2))  # same amount, other limit changed
    assert engine.budget_left() == pytest.approx(25.0)
    await engine.set_limits(Limits(capital_usdt=40, max_daily_loss_usdt=6))  # a new amount
    assert engine.budget_left() == pytest.approx(40.0)
    assert any(e["code"] == "budget_restarted" for e in engine.store.events(5))


async def test_status_tells_what_the_last_scan_found() -> None:
    engine, _, market, clock = build()
    await engine.tick()
    assert engine.status()["last_scan"]["market"] == "not_running"
    await engine.start()
    clock.next_bar()
    await engine.tick()
    scan = engine.status()["last_scan"]
    assert scan["market"] == "btc_up"
    assert scan["signals"] == 1 and scan["opened"] == 1


async def test_trades_record_why_they_opened_and_closed(tmp_path) -> None:
    import json as _json

    from sonic import journal

    engine, _, market, _ = build()
    engine.journal_dir = tmp_path
    await engine.start()
    await engine.tick()
    trade = engine.store.open_trades()[0]
    plan = _json.loads(trade["plan"])
    assert plan["btc_up"] is True and plan["fast"] > plan["slow"]
    market.last = trade["stop_price"] * 0.99
    await engine.tick()
    closed = engine.store.trades()[0]
    assert closed["exit_reason"] == "stop_loss"
    files = list(tmp_path.glob("*.md"))
    assert len(files) == 1
    text = files[0].read_text(encoding="utf-8")
    assert "SOLUSDT" in text and journal.REASONS["stop_loss"] in text and "BTC up" in text


async def test_panic_and_breakeven_reasons() -> None:
    engine, _, market, _ = build()
    await engine.start()
    await engine.tick()
    trade = engine.store.open_trades()[0]
    market.last = trade["target_price"] * 1.001
    await engine.tick()
    market.last = trade["entry_price"] * 0.999
    await engine.tick()
    assert engine.store.trades()[0]["exit_reason"] == "breakeven"
    engine2, _, _, _ = build()
    await engine2.start()
    await engine2.tick()
    await engine2.panic()
    assert engine2.store.trades()[0]["exit_reason"] == "panic_button"


async def test_month_report_measures_the_goal() -> None:
    engine, _, _, _ = build()
    for i, pnl in enumerate([1.0, 1.0, 1.0, 1.0, -1.0]):
        trade_id = engine.store.open_trade(
            symbol="XRPUSDT", side="BUY", qty=1, entry_price=1, stop_price=0.9,
            target_price=0, margin=1, opened_ms=0, risk_usdt=0.1,
        )
        engine.store.close_trade(trade_id, int(T0 * 1000) - i, 1, pnl, 0.0, "breakeven")
    report = engine.month_report()
    assert report["trades"] == 5 and report["win_rate"] == 0.8
    assert report["verdict"] == "in_goal"
    with TestClient(create_app(engine, "secret", background=False)) as client:
        headers = {"Authorization": "Bearer secret"}
        assert client.get("/report", headers=headers).json()["trades"] == 5
        assert client.get("/report?month=bad", headers=headers).status_code == 422


async def test_chart_shows_candles_emas_and_the_trade() -> None:
    engine, _, _, _ = build()
    await engine.start()
    await engine.tick()
    data = await engine.chart("SOLUSDT", "live", None, 60)
    assert len(data["bars"]) == 60 and len(data["fast"]) == 60 and len(data["slow"]) == 60
    assert data["trades"] and data["trades"][0]["entry_price"] > 0
    assert data["settings"]["atr_k"] == strategy.ATR_K


def test_chart_and_replay_endpoints(tmp_path, monkeypatch) -> None:
    import json as _json

    from sonic import chart

    saved = {"period": "2025-07..2026-10", "trades": [{
        "id": 7, "symbol": "SOLUSDT", "side": "BUY", "qty": 0.1, "entry_price": 100.0,
        "stop_price": 95.0, "target_price": 101.0, "opened_ms": BAR_MS * 300,
        "closed_ms": BAR_MS * 320, "exit_price": 100.0, "pnl": 0.05, "r": 0.1, "scaled": 1,
        "exit_reason": "breakeven",
    }]}
    path = tmp_path / "trades.json"
    path.write_text(_json.dumps(saved), encoding="utf-8")
    monkeypatch.setattr(chart, "REPLAY_FILE", path)
    engine, _, _, _ = build()
    with TestClient(create_app(engine, "secret", background=False)) as client:
        headers = {"Authorization": "Bearer secret"}
        assert client.get("/chart?symbol=SOLUSDT", headers=headers).status_code == 200
        assert client.get("/chart?symbol=SCAMUSDT", headers=headers).status_code == 422
        listed = client.get("/replay/trades?symbol=SOLUSDT", headers=headers).json()
        assert listed["period"] == "2025-07..2026-10" and listed["trades"][0]["id"] == 7
        focused = client.get("/chart?symbol=SOLUSDT&source=replay&trade_id=7", headers=headers)
        body = focused.json()
        assert body["focus"] == 7 and body["trades"][0]["exit_reason"] == "breakeven"
        assert all(bar[0] < BAR_MS * 320 + 21 * BAR_MS for bar in body["bars"])


async def test_plan_with_numpy_values_is_saved_before_the_order() -> None:
    np = pytest.importorskip("numpy")

    engine, exchange, _, _ = build()
    await engine.start()
    plan = {"btc_up": np.bool_(True), "fast": np.float64(1.5)}
    async with engine.lock:
        await engine._open("SOLUSDT", "BUY", 6.0, 0.0, plan)
    trade = engine.store.open_trades()[0]
    assert '"btc_up": "True"' in trade["plan"] or '"btc_up": true' in trade["plan"]
    assert exchange.state["open"]  # the order went through and the trade is known


async def test_simulator_gives_every_trade_event_and_the_forming_candle() -> None:
    engine, _, market, clock = build()
    await engine.start()
    await engine.tick()  # opens on the up-cross
    trade = engine.store.open_trades()[0]
    # The next candle reaches the half-profit target.
    market.coin.append(trade["target_price"] * 1.02)
    market.last = market.coin[-1]
    clock.next_bar()
    await engine.tick()
    data = await engine.simulate("SOLUSDT", "live", 1, None)
    assert data["lead"] == 90 and len(data["bars"]) == 180 + 90 and data["forming"] is None
    shown = data["trades"][0]
    assert shown["scaled"] and "half_ms" in shown


def test_half_profit_time_is_the_first_candle_that_reaches_the_target() -> None:
    from sonic import chart

    bars = [Bar(i * BAR_MS, 100, high, 99, 100) for i, high in enumerate([100, 101, 103, 104])]
    trade = {"side": "BUY", "opened_ms": BAR_MS + 5, "closed_ms": None,
             "target_price": 102.5, "scaled": 1}
    assert chart.half_time(trade, bars) == 2 * BAR_MS
    assert chart.half_time(dict(trade, scaled=0), bars) is None
    short = dict(trade, side="SELL", target_price=99.5)
    assert chart.half_time(short, bars) == BAR_MS + 5  # reached in the entry candle itself


async def test_simulator_splits_off_the_candle_still_forming() -> None:
    engine, _, market, clock = build()
    bars = make_bars(market.coin)
    clock.now = (bars[-1].t + BAR_MS // 2) / 1000  # half way through the last candle
    data = await engine.simulate("SOLUSDT", "live", 1, None)
    assert data["forming"][0] == bars[-1].t
    assert data["bars"][-1][0] == bars[-2].t
    assert data["forming"][4] == market.last


async def test_start_after_a_missed_bar_scans_it_at_once() -> None:
    engine, _, _, _ = build()
    await engine.tick()  # the bar closes while the bot is off
    assert engine.status()["last_scan"]["market"] == "not_running"
    assert not engine.store.open_trades()
    await engine.start()
    await engine.tick()  # same bar, but now scanned
    assert engine.status()["last_scan"]["market"] == "btc_up"
    assert engine.store.open_trades()


def test_simulate_endpoint_checks_its_input() -> None:
    engine, _, _, _ = build()
    with TestClient(create_app(engine, "secret", background=False)) as client:
        headers = {"Authorization": "Bearer secret"}
        ok = client.get("/simulate?symbol=SOLUSDT&months=3", headers=headers)
        assert ok.status_code == 200 and len(ok.json()["bars"]) > 0
        assert client.get("/simulate?symbol=SCAMUSDT", headers=headers).status_code == 422
        assert client.get("/simulate?symbol=SOLUSDT&months=12", headers=headers).status_code == 422
        replay = client.get("/simulate?symbol=SOLUSDT&source=replay", headers=headers)
        assert replay.status_code == 200


async def test_simulator_period_stops_before_its_end(tmp_path, monkeypatch) -> None:
    from sonic import chart

    monkeypatch.setattr(chart, "REPLAY_FILE", tmp_path / "none.json")
    engine, _, _, _ = build()
    end = 350 * BAR_MS
    data = await engine.simulate("SOLUSDT", "replay", 1, end)
    assert data["bars"][-1][0] == end - BAR_MS  # the candle opening at the end is left out


def test_no_buy_while_the_coin_is_below_its_long_average() -> None:
    # The EMAs cross up, BTC is up, but the coin is still under its 200-bar EMA.
    closes = [200 - i * 0.3 for i in range(400)]
    while strategy.entry_signal(make_bars(closes), True) is None and closes[-1] < 400:
        closes.append(closes[-1] * 1.01)
        bars = make_bars(closes)
        fast, slow = strategy.ema(closes, strategy.FAST), strategy.ema(closes, strategy.SLOW)
        if fast[-1] > slow[-1] and fast[-2] <= slow[-2]:
            long_avg = strategy.ema(closes, strategy.COIN_TREND_PERIOD)[-1]
            assert closes[-1] <= long_avg  # crossed, but under the long average: no signal
            assert strategy.entry_signal(bars, True) is None
            break
    else:
        pytest.fail("no EMA cross happened in the test series")


async def test_restart_resumes_only_when_the_owner_allowed_it() -> None:
    engine, exchange, _, clock = build()
    await engine.start()

    def restarted() -> Engine:
        return Engine(engine.store, exchange, settings(), clock=clock)

    assert not restarted().running  # default: never resume by itself
    await engine.set_auto_resume(True)
    assert restarted().running and restarted().status()["auto_resume"]
    await engine.stop()
    assert not restarted().running  # the owner stopped it: stays stopped
    await engine.start()
    engine.halt("panic_button")
    assert not restarted().running  # halted: stays stopped


def test_settings_endpoint_takes_both_switches() -> None:
    engine, _, _, _ = build()
    with TestClient(create_app(engine, "secret", background=False)) as client:
        headers = {"Authorization": "Bearer secret"}
        body = client.put("/settings", json={"auto_resume": True}, headers=headers).json()
        assert body["auto_resume"] is True
        for bad in ({"auto_resume": "yes"}, {"other": True}, {}):
            assert client.put("/settings", json=bad, headers=headers).status_code == 422
