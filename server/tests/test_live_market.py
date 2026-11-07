"""Reads real public data from Binance. Run with: pytest -m live"""

from __future__ import annotations

import httpx
import pytest

from sonic.exchange import DEMO_URL, PUBLIC_URL, PublicMarket

pytestmark = pytest.mark.live


@pytest.mark.parametrize("base_url", [PUBLIC_URL, DEMO_URL])
async def test_public_market_data_parses(base_url: str) -> None:
    async with httpx.AsyncClient(base_url=base_url, timeout=10) as client:
        market = PublicMarket(client)
        filters = await market.filters("BTCUSDT")
        bars = await market.bars("BTCUSDT", "15m", 100)
        price = await market.price("BTCUSDT")
    assert float(filters.step_size) > 0
    assert filters.min_notional > 0
    assert len(bars) == 100
    assert price > 0
