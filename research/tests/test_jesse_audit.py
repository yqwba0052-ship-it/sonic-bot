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


# ---- Task 2: minutes made from hourly bars (engine-parity check only, not real price paths)

import numpy as np  # noqa: E402

from sonic_research.jesse_audit.synth import aggregate, minutes_from_hours  # noqa: E402

H_MS = 3_600_000


def _hours(n: int, seed: int = 7, start: int = 1_640_995_200_000):
    rng = np.random.default_rng(seed)
    c = 100 * np.cumprod(1 + rng.normal(0, 0.01, n))
    o = np.r_[100.0, c[:-1]]
    h = np.maximum(o, c) * (1 + rng.uniform(0, 0.01, n))
    low = np.minimum(o, c) * (1 - rng.uniform(0, 0.01, n))
    t = start + H_MS * np.arange(n, dtype=np.int64)
    return t, o, h, low, c


def test_round_trip_hour_and_4h():
    t, o, h, low, c = _hours(48)
    m = minutes_from_hours(t, o, h, low, c)
    assert m.shape == (48 * 60, 6)
    hour = aggregate(m, 60)
    assert np.array_equal(hour[:, 0], t)
    for col, want in ((1, o), (2, c), (3, h), (4, low)):
        assert np.array_equal(hour[:, col], want)
    four = aggregate(m, 240)
    g = lambda a: a.reshape(-1, 4)  # noqa: E731
    assert np.array_equal(four[:, 1], g(o)[:, 0])
    assert np.array_equal(four[:, 2], g(c)[:, -1])
    assert np.array_equal(four[:, 3], g(h).max(axis=1))
    assert np.array_equal(four[:, 4], g(low).min(axis=1))


def test_low_before_high():
    t, o, h, low, c = _hours(24, seed=3)
    m = minutes_from_hours(t, o, h, low, c).reshape(24, 60, 6)
    for i in range(24):
        first_low = int(np.argmax(m[i, :, 4] == low[i]))
        first_high = int(np.argmax(m[i, :, 3] == h[i]))
        assert first_low < first_high


def test_flat_bar():
    one = np.array([1_640_995_200_000], dtype=np.int64)
    five = np.array([5.0])
    m = minutes_from_hours(one, five, five, five, five)
    assert np.all(m[:, 1:5] == 5.0)
    assert not np.isnan(m).any()


def test_synth_holdout_refused():
    t, o, h, low, c = _hours(2, start=1_704_067_200_000 - H_MS)
    with pytest.raises(ValueError, match="holdout_locked"):
        minutes_from_hours(t, o, h, low, c)


# ---- Task 3: real 1-minute Binance data, training months only

import io  # noqa: E402
import zipfile  # noqa: E402

from sonic_research.data import Bars  # noqa: E402
from sonic_research.jesse_audit.data_1m import (  # noqa: E402
    MissingMonth,
    compare_to_bars,
    load_minutes,
)

JAN_2022 = 1_640_995_200_000


def _zip(rows: list[list]) -> bytes:
    text = "open_time,open,high,low,close,volume,close_time\n" + "".join(
        ",".join(str(x) for x in r) + "\n" for r in rows)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("x.csv", text)
    return buf.getvalue()


def test_parses_binance_zip_to_jesse_columns(tmp_path):
    rows = [[JAN_2022 + 60_000, 2, 4, 1, 3, 10, 0], [JAN_2022, 1, 2, 0.5, 1.5, 5, 0]]
    seen = []

    def fetch(url):
        seen.append(url)
        return _zip(rows)

    m = load_minutes("SOLUSDT", ["2022-01"], fetch=fetch, cache_dir=tmp_path)
    assert seen == ["https://data.binance.vision/data/futures/um/monthly/klines/SOLUSDT/1m/"
                    "SOLUSDT-1m-2022-01.zip"]
    assert m.tolist() == [[JAN_2022, 1, 1.5, 2, 0.5, 5], [JAN_2022 + 60_000, 2, 3, 4, 1, 10]]
    assert (tmp_path / "SOLUSDT-1m-2022-01.npy").exists()
    assert not list(tmp_path.glob("*.zip"))
    again = load_minutes("SOLUSDT", ["2022-01"], fetch=lambda url: None, cache_dir=tmp_path)
    assert np.array_equal(again, m)


def test_missing_month_raises(tmp_path):
    with pytest.raises(MissingMonth, match="SOLUSDT 2022-02"):
        load_minutes("SOLUSDT", ["2022-02"], fetch=lambda url: None, cache_dir=tmp_path)


def test_holdout_month_refused(tmp_path):
    def fetch(url):
        raise AssertionError("must not download")

    with pytest.raises(ValueError, match="holdout_locked"):
        load_minutes("SOLUSDT", ["2023-12", "2024-01"], fetch=fetch, cache_dir=tmp_path)


def _bars_from(hours):
    t, o, h, low, c = hours
    return Bars("X", t, o, h, low, c)


def test_compare_identical_bars():
    hours = _hours(8, start=JAN_2022)
    report = compare_to_bars(minutes_from_hours(*hours), _bars_from(hours), 60)
    assert report["bars"] == 8
    assert report["missing_bars"] == []
    assert report["missing_minutes"] == 0
    assert report["mismatches"] == []
    assert report["max_rel_diff"] == 0.0


def test_compare_reports_missing_minute():
    hours = _hours(8, start=JAN_2022)
    m = minutes_from_hours(*hours)
    report = compare_to_bars(np.delete(m, 5, axis=0), _bars_from(hours), 60)
    assert report["missing_minutes"] == 1


def test_compare_reports_missing_bar_and_price_mismatch():
    hours = _hours(8, start=JAN_2022)
    m = minutes_from_hours(*hours)
    m = np.delete(m, slice(120, 180), axis=0)  # hour 2 gone
    m[0, 3] = hours[2][0] * 1.01  # minute high above the hour's high
    report = compare_to_bars(m, _bars_from(hours), 60)
    assert report["missing_bars"] == [int(hours[0][2])]
    assert report["mismatches"][0][:2] == (int(hours[0][0]), "h")
    assert report["max_rel_diff"] == pytest.approx(0.01)
