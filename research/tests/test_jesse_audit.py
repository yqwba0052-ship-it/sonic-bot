"""Jesse audit pilot (research only; Sonic, server and app are not changed)."""

import socket

import pytest

from sonic_research.jesse_audit import HOLDOUT_START_MS, guard_training
from sonic_research.jesse_audit.offline import NetworkBlocked, block_network


def test_block_network_refuses_connect():
    original = socket.socket.connect
    with block_network(), pytest.raises(NetworkBlocked, match=r"^network_blocked: example\.com"):
        socket.create_connection(("example.com", 80), timeout=1)
    assert socket.socket.connect is original


def test_guard_training_refuses_holdout():
    guard_training(HOLDOUT_START_MS - 1)
    with pytest.raises(ValueError, match="holdout_locked"):
        guard_training(HOLDOUT_START_MS)


def test_jesse_backtest_runs_offline():
    pytest.importorskip("jesse")
    from jesse import helpers as jh
    from jesse.research import backtest, candles_from_close_prices
    from jesse.strategies import Strategy

    class OneTrade(Strategy):
        def should_long(self):
            return self.index == 10

        def go_long(self):
            self.buy = 1, self.price

        def update_position(self):
            if self.index == 20:
                self.liquidate()

        def should_cancel_entry(self):
            return False

    candles = candles_from_close_prices([100 + i % 7 for i in range(3 * 1440)])
    exchange, symbol = "Binance Perpetual Futures", "BTC-USDT"
    config = {"starting_balance": 10_000, "fee": 0, "type": "futures", "futures_leverage": 1,
              "futures_leverage_mode": "cross", "exchange": exchange, "warm_up_candles": 0}
    routes = [{"exchange": exchange, "strategy": OneTrade, "symbol": symbol, "timeframe": "1m"}]
    data = {jh.key(exchange, symbol): {"exchange": exchange, "symbol": symbol, "candles": candles}}
    with block_network():
        result = backtest(config, routes, [], data)
    assert result["metrics"]["total"] == 1
