"""Opens and closes one simulated trade at real Binance prices. Run with: pytest -m live"""

from __future__ import annotations

import httpx
import pytest

from sonic.config import Settings
from sonic.engine import Engine
from sonic.exchange import PUBLIC_URL, PaperExchange, PublicMarket
from sonic.store import Store

pytestmark = pytest.mark.live


async def test_paper_trade_at_real_prices_opens_protects_and_closes() -> None:
    async with httpx.AsyncClient(base_url=PUBLIC_URL, timeout=10) as client:
        store = Store(":memory:")
        exchange = PaperExchange(PublicMarket(client), store, 10000.0)
        settings = Settings(mode="paper", token="t", db_path=":memory:", symbols=("BTCUSDT",))
        engine = Engine(store, exchange, settings)
        await engine.start()
        engine.limits = engine.limits.model_copy(
            update={"capital_usdt": 1000.0, "symbols": ["BTCUSDT"]}
        )
        price = await exchange.price("BTCUSDT")
        async with engine.lock:
            used = await engine._open("BTCUSDT", "BUY", price * 0.03, 0.0)
        trade = store.open_trades()[0]
        assert used > 0
        assert trade["stop_price"] < trade["entry_price"]
        assert trade["risk_usdt"] > 0
        await engine.panic()
    closed = store.trades()[0]
    assert closed["closed_ms"] is not None
    assert closed["fees"] > 0
    assert exchange.state["open"] == {}
    assert abs(closed["pnl"]) < 50
