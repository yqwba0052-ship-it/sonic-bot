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
from sonic.risk import Filters, Limits, max_order_usdt, protection_prices, size_position
from sonic.store import Store

FILTERS = Filters("0.001", "0.1", 0.001, 100.0)
RISING = [100.0] * 80 + [99.0, 98.0, 97.0] + [100.0, 104.0]


class FakeMarket:
    def __init__(self, price: float = 100.0) -> None:
        self.last = price
        self.closes = list(RISING)
        self.bar = 1

    async def filters(self, symbol: str) -> Filters:
        return FILTERS

    async def price(self, symbol: str) -> float:
        return self.last

    async def prices(self) -> dict[str, float]:
        from sonic.risk import ALLOWED_SYMBOLS

        return {symbol: self.last for symbol in ALLOWED_SYMBOLS}

    async def bars(self, symbol: str, interval: str, limit: int) -> list[tuple[int, float]]:
        return [(self.bar + index, close) for index, close in enumerate(self.closes)]


def settings(symbols: tuple[str, ...] = ("BTCUSDT",)) -> Settings:
    return Settings(mode="paper", token="secret", db_path=":memory:", symbols=symbols)


def build(symbols: tuple[str, ...] = ("BTCUSDT",)) -> tuple[Engine, PaperExchange, FakeMarket]:
    store = Store(":memory:")
    market = FakeMarket()
    exchange = PaperExchange(market, store, 10000.0)
    engine = Engine(store, exchange, settings(symbols), clock=lambda: 1_800_000_000.0)
    engine.limits = Limits(symbols=list(symbols))
    return engine, exchange, market


def test_limits_reject_daily_loss_above_capital() -> None:
    with pytest.raises(ValueError):
        Limits(capital_usdt=100, max_daily_loss_usdt=200)


def test_limits_reject_leverage_above_five() -> None:
    with pytest.raises(ValueError):
        Limits(leverage=6)


def test_size_uses_risk_amount() -> None:
    size = size_position(Limits(), 100.0, 0.0, FILTERS)
    assert size.qty == 10.0  # 1% of 1000 at a 1% stop is a 1000 notional


def test_size_never_exceeds_free_capital() -> None:
    limits = Limits(capital_usdt=1000, leverage=1, risk_per_trade_pct=5, stop_loss_pct=1)
    assert size_position(limits, 100.0, 900.0, FILTERS).qty == 1.0
    assert size_position(limits, 100.0, 1000.0, FILTERS).reason == "capital_in_use"


def test_small_capital_is_refused_below_exchange_minimum() -> None:
    limits = Limits(capital_usdt=50, max_daily_loss_usdt=5)
    assert size_position(limits, 75000.0, 0.0, FILTERS).reason == "below_exchange_minimum"


def test_protection_prices_follow_side() -> None:
    assert protection_prices("BUY", 100.0, Limits(), "0.1") == (99.0, 102.0)
    assert protection_prices("SELL", 100.0, Limits(), "0.1") == (101.0, 98.0)


def test_strategy_needs_history_and_detects_cross() -> None:
    assert strategy.signal([100.0] * 10) is None
    assert strategy.signal(RISING) == "BUY"


async def test_opens_once_per_bar_with_protection() -> None:
    engine, exchange, _ = build()
    await engine.start()
    await engine.tick()
    await engine.tick()
    trades = engine.store.open_trades()
    assert len(trades) == 1
    assert exchange.state["open"]["BTCUSDT"]["stop"] == trades[0]["stop_price"]


async def test_does_not_trade_while_stopped() -> None:
    engine, _, _ = build()
    await engine.tick()
    assert engine.store.open_trades() == []


async def test_stop_loss_closes_trade_with_fees_counted() -> None:
    engine, _, market = build()
    await engine.start()
    await engine.tick()
    market.last = 98.5
    await engine.tick()
    trade = engine.store.trades()[0]
    assert trade["closed_ms"] is not None
    assert trade["fees"] > 0
    assert trade["pnl"] < -10


async def test_daily_loss_limit_closes_and_latches() -> None:
    engine, exchange, market = build()
    await engine.set_limits(Limits(max_daily_loss_usdt=5, stop_loss_pct=5, risk_per_trade_pct=5))
    await engine.start()
    await engine.tick()
    market.last = 99.0
    await engine.tick()
    assert engine.halted == "daily_loss_limit"
    assert exchange.state["open"] == {}
    with pytest.raises(ValueError):
        await engine.start()
    with pytest.raises(ValueError):
        await engine.clear_halt()


async def test_panic_closes_everything_and_blocks_entries() -> None:
    engine, exchange, market = build()
    await engine.start()
    await engine.tick()
    await engine.panic()
    market.bar += 1
    await engine.tick()
    assert exchange.state["open"] == {}
    assert engine.store.open_trades() == []
    assert engine.halted == "panic_button"
    assert not engine.running


async def test_failed_protection_closes_position_and_halts() -> None:
    engine, exchange, _ = build()

    async def broken(*_: object) -> None:
        raise RuntimeError("rejected")

    exchange.protect = broken  # type: ignore[method-assign]
    await engine.start()
    await engine.tick()
    assert engine.halted == "protection_failed"
    assert exchange.state["open"] == {}
    assert engine.store.open_trades() == []


async def test_order_that_timed_out_but_filled_halts_as_unknown_position() -> None:
    engine, exchange, _ = build()
    await exchange.open_market("BTCUSDT", "BUY", 1.0, "lost-response")
    await engine.start()
    await engine.tick()
    assert engine.halted == "unknown_position"
    assert not engine.running


async def test_repeated_failures_halt() -> None:
    engine, exchange, _ = build()

    async def down() -> dict:
        raise RuntimeError("network down")

    exchange.positions = down  # type: ignore[method-assign]
    await engine.start()
    for _ in range(3):
        await engine.tick()
    assert engine.halted == "repeated_failures"


async def test_restart_never_resumes_and_keeps_halt_and_limits() -> None:
    engine, exchange, market = build()
    await engine.set_limits(Limits(capital_usdt=500))
    await engine.start()
    engine.halt("panic_button")
    restarted = Engine(engine.store, exchange, settings())
    assert not restarted.running
    assert restarted.halted == "panic_button"
    assert restarted.limits.capital_usdt == 500


async def test_limits_cannot_change_while_running() -> None:
    engine, _, _ = build()
    await engine.start()
    with pytest.raises(ValueError):
        await engine.set_limits(Limits(capital_usdt=5000))


def test_api_requires_token_and_serves_status() -> None:
    engine, _, _ = build()
    with TestClient(create_app(engine, "secret", background=False)) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/status").status_code == 401
        assert client.post("/panic").status_code == 401
        headers = {"Authorization": "Bearer secret"}
        body = client.get("/status", headers=headers).json()
        assert body["mode"] == "paper"
        assert body["strategy_proven"] is False
        bad = client.put("/limits", headers=headers, json={"leverage": 50})
        assert bad.status_code == 422


def test_demo_client_refuses_any_other_url() -> None:
    with pytest.raises(ValueError):
        BinanceDemo(httpx.AsyncClient(base_url=PUBLIC_URL), "k", "s")


async def test_demo_client_signs_and_sends_stop_as_algo_order() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"algoId": 1})

    client = httpx.AsyncClient(base_url=DEMO_URL, transport=httpx.MockTransport(handler))
    demo = BinanceDemo(client, "key", "secret")
    await demo.protect("BTCUSDT", "BUY", 74000.5, 77000.0)
    assert [item.url.path for item in seen] == ["/fapi/v1/algoOrder"] * 2
    query = str(seen[0].url.query, "ascii") if isinstance(seen[0].url.query, bytes) else ""
    unsigned, signature = query.rsplit("&signature=", 1)
    expected = hmac.new(b"secret", unsigned.encode(), hashlib.sha256).hexdigest()
    assert signature == expected
    fields = parse_qs(urlsplit(f"?{query}").query)
    assert fields["side"] == ["SELL"]
    assert fields["type"] == ["STOP_MARKET"]
    assert fields["triggerPrice"] == ["74000.5"]
    assert fields["closePosition"] == ["true"]
    assert seen[0].headers["X-MBX-APIKEY"] == "key"


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


def test_ten_dollar_amount_sizes_small_coins_but_not_btc() -> None:
    limits = Limits(capital_usdt=10, max_daily_loss_usdt=3, leverage=1)
    assert max_order_usdt(limits, 10) == 10.0
    small_coin = Filters("1", "0.0001", 1.0, 5.0)
    assert size_position(limits, 2.0, 0.0, small_coin).qty == 5.0
    btc = Filters("0.001", "0.1", 0.001, 50.0)
    assert size_position(limits, 75000.0, 0.0, btc).reason == "below_exchange_minimum"


def test_limits_reject_unknown_coin() -> None:
    with pytest.raises(ValueError, match="unknown_symbol"):
        Limits(symbols=["SCAMUSDT"])


async def test_losing_the_whole_amount_closes_and_halts() -> None:
    engine, exchange, market = build()
    # A 100 USDT order (the market minimum) on a 20 USDT amount, then a crash through the stop.
    limits = Limits(
        capital_usdt=20,
        max_daily_loss_usdt=20,
        risk_per_trade_pct=5,
        stop_loss_pct=1,
        leverage=5,
        symbols=["BTCUSDT"],
    )
    await engine.set_limits(limits)
    await engine.start()
    await engine.tick()
    assert engine.store.open_trades()
    market.last = 50.0
    await engine.tick()
    assert engine.halted == "budget_used"
    assert exchange.state["open"] == {}
    with pytest.raises(ValueError, match="budget_still_used"):
        await engine.clear_halt()


async def test_trades_shrink_after_a_loss() -> None:
    engine, exchange, market = build()
    await engine.set_limits(Limits(capital_usdt=1000, max_daily_loss_usdt=500, symbols=["BTCUSDT"]))
    await engine.start()
    await engine.tick()
    first = engine.store.open_trades()[0]["qty"]
    market.last = 98.0
    await engine.tick()
    market.last = 100.0
    market.bar += 1
    await engine.tick()
    second = engine.store.open_trades()[0]["qty"]
    assert engine.budget_realized() < 0
    assert second < first


async def test_saving_amount_starts_a_new_budget() -> None:
    engine, _, market = build()
    await engine.start()
    await engine.tick()
    market.last = 98.0
    await engine.tick()
    assert engine.budget_realized() < 0
    await engine.stop()
    engine.clock = lambda: 1_800_000_100.0
    await engine.set_limits(Limits(capital_usdt=500, symbols=["BTCUSDT"]))
    assert engine.budget_realized() == 0
    assert engine.budget_left() == 500


def test_api_lists_markets_and_explains_limit_errors() -> None:
    engine, _, _ = build()
    with TestClient(create_app(engine, "secret", background=False)) as client:
        headers = {"Authorization": "Bearer secret"}
        markets = client.get("/markets", headers=headers).json()
        assert len(markets) == 12
        assert markets[0]["min_order_usdt"] == 100.0
        too_big = {"capital_usdt": 10, "max_daily_loss_usdt": 30}
        response = client.put("/limits", headers=headers, json=too_big)
        assert response.json()["detail"] == "daily_loss_above_capital"
        response = client.put("/limits", headers=headers, json={"symbols": ["SCAMUSDT"]})
        assert response.json()["detail"] == "unknown_symbol"
        status = client.get("/status", headers=headers).json()
        assert status["budget_left"] == 1000.0


def test_api_refuses_a_coin_the_amount_cannot_trade() -> None:
    engine, _, _ = build()
    with TestClient(create_app(engine, "secret", background=False)) as client:
        headers = {"Authorization": "Bearer secret"}
        ten_dollars = {"capital_usdt": 10, "max_daily_loss_usdt": 3, "leverage": 1}
        body = {**ten_dollars, "symbols": ["BTCUSDT"]}
        response = client.put("/limits", headers=headers, json=body)
        assert response.status_code == 422
        assert response.json()["detail"] == "coin_below_minimum"
        assert engine.limits.capital_usdt == 1000
