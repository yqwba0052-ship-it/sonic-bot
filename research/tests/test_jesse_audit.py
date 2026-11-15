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


# ---- Task 4: coins and window fixed before Jesse runs

from sonic_research.jesse_audit.choose import pick_window  # noqa: E402


def _ms(month: str, day: int = 10) -> int:
    from datetime import UTC, datetime
    return int(datetime.strptime(f"{month}-{day:02d}", "%Y-%m-%d").replace(tzinfo=UTC)
               .timestamp() * 1000)


def _trade(symbol, opened, closed):
    return {"symbol": symbol, "opened_ms": _ms(opened), "closed_ms": _ms(closed, 20)}


MONTHS = [f"2021-{m:02d}" for m in range(6, 13)] + [f"{y}-{m:02d}" for y in (2022, 2023)
                                                   for m in range(1, 13)]


def test_pick_most_trades():
    trades = [_trade("SOLUSDT", "2022-03", "2022-03")] * 2 + [
        _trade("XRPUSDT", "2022-04", "2022-04")] * 3 + [_trade("DOGEUSDT", "2022-04", "2022-04")]
    got = pick_window(trades, [], ["DOGEUSDT", "SOLUSDT", "XRPUSDT"], MONTHS)
    assert got["coins"] == ["SOLUSDT", "XRPUSDT"]
    assert got["trades"] == 5
    assert got["start"] <= "2022-03" and got["end"] >= "2022-04"


def test_window_with_halt_excluded():
    trades = [_trade("SOLUSDT", "2022-03", "2022-03")] * 4 + [
        _trade("SOLUSDT", "2023-03", "2023-03"), _trade("XRPUSDT", "2023-03", "2023-03")]
    halts = [("daily_loss_limit", _ms("2022-03", 15))]
    got = pick_window(trades, halts, ["SOLUSDT", "XRPUSDT"], MONTHS)
    assert got["start"] > "2022-03" or got["end"] < "2022-03"
    assert got["trades"] == 2


def test_tie_breaks_earliest_then_alphabetical():
    trades = [_trade("XRPUSDT", "2022-01", "2022-01"), _trade("SOLUSDT", "2022-01", "2022-01")]
    got = pick_window(trades, [], ["XRPUSDT", "SOLUSDT", "DOGEUSDT"], MONTHS)
    assert got["start"] == "2021-12"
    assert got["coins"] == ["SOLUSDT", "XRPUSDT"]


def test_never_beyond_2023_12():
    trades = [_trade("SOLUSDT", "2023-12", "2023-12"), _trade("XRPUSDT", "2023-12", "2023-12")]
    got = pick_window(trades, [], ["SOLUSDT", "XRPUSDT"], MONTHS)
    assert got["end"] == "2023-12"
    assert got["start"] == "2023-07"


def test_trade_crossing_window_end_not_counted():
    trades = [{"symbol": "SOLUSDT", "opened_ms": _ms("2022-05"), "closed_ms": _ms("2022-07")},
              _trade("XRPUSDT", "2023-01", "2023-01")]
    got = pick_window(trades, [], ["SOLUSDT", "XRPUSDT"], MONTHS)
    assert got["trades"] == 1


def test_replay_reports_halts():
    from sonic_research.data import DATA_DIR
    if not any((DATA_DIR / "klines").glob("*-1h-2023-*.zip")):
        pytest.skip("history not downloaded")
    import asyncio

    from sonic_research.engine_replay import Limits, replay
    report = asyncio.run(replay("2023-09", "2023-10", Limits()))
    assert isinstance(report["halts"], list)
    assert all(isinstance(code, str) and isinstance(at, int) for code, at in report["halts"])


# ---- Task 5: shared cost formula and the Jesse bridge

from sonic_research.jesse_audit.costs import costs  # noqa: E402


def test_costs_match_paper_exchange_formula():
    legs = [{"kind": "entry", "ms": 0, "raw": 100.0, "qty": 1.0},
            {"kind": "exit", "ms": 1, "raw": 110.0, "qty": 1.0}]
    got = costs(legs)
    entry, exit_ = 100 * 1.0002, 110 * 0.9998
    assert got["gross"] == pytest.approx(exit_ - entry)
    assert got["fees"] == pytest.approx((entry + exit_) * 0.0005)
    assert got["slippage"] == pytest.approx(100 * 0.0002 + 110 * 0.0002)
    assert got["pnl"] == pytest.approx(got["gross"] - got["fees"])


def test_costs_with_half_leg():
    legs = [{"kind": "entry", "ms": 0, "raw": 100.0, "qty": 2.0},
            {"kind": "half", "ms": 1, "raw": 101.0, "qty": 1.0},
            {"kind": "exit", "ms": 2, "raw": 100.0, "qty": 1.0}]
    entry = 100 * 1.0002
    want = (101 * 0.9998 - entry) + (100 * 0.9998 - entry)
    fees = (2 * entry + 101 * 0.9998 + 100 * 0.9998) * 0.0005
    assert costs(legs)["pnl"] == pytest.approx(want - fees)


FOUR_H = 4 * H_MS


def _closes(kind: str) -> list[float]:
    c = [100.0]
    for _ in range(1049):
        c.append(c[-1] * 1.001)
    for _ in range(40):
        c.append(c[-1] * 0.998)
    if kind == "time":
        for _ in range(330):
            c.append(c[-1] * 1.003)
    else:  # breakeven: rise until the half target fills, then fall back through the entry
        for _ in range(19):
            c.append(c[-1] * 1.003)
        for _ in range(3):
            c.append(c[-1] * 1.01)
        for _ in range(40):
            c.append(c[-1] * 0.995)
    return c


def _minutes_for(closes: list[float]) -> np.ndarray:
    """4h closes -> hourly bars (geometric steps, +-0.1% range) -> synthetic minutes."""
    hourly = [closes[0]]
    for a, b in zip(closes, closes[1:], strict=False):
        step = (b / a) ** 0.25
        hourly += [a * step ** k for k in range(1, 5)]
    c = np.array(hourly[1:] + [hourly[-1]] * 3)
    o = np.r_[closes[0], c[:-1]]
    t = JAN_2022 - 1050 * FOUR_H + H_MS * np.arange(len(c), dtype=np.int64)
    return minutes_from_hours(t, o, np.maximum(o, c) * 1.001, np.minimum(o, c) * 0.999, c)


def _expected_entry(minutes: np.ndarray):
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "server"))
    from sonic import strategy as s
    four = aggregate(minutes, 240)
    bars = [s.Bar(int(r[0]), r[1], r[3], r[4], r[2]) for r in four]
    for i in range(1000, len(bars)):
        w = bars[i - 999:i + 1]
        if s.entry_signal(w, s.btc_uptrend(w)) == "BUY":
            return i, bars, s.stop_distance(w)
    raise AssertionError("series gives no entry")


FILTERS = None


def _filters():
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "server"))
    from sonic.risk import Filters
    return Filters(step_size="0.01", tick_size="0.01", min_qty=0.01, min_notional=5.0)


def _run(kind: str):
    pytest.importorskip("jesse")
    from sonic_research.jesse_audit.bridge import run_bridge
    minutes = _minutes_for(_closes(kind))
    ledger = run_bridge({"SOLUSDT": minutes, "BTCUSDT": minutes}, ["SOLUSDT"],
                        {"SOLUSDT": _filters(), "BTCUSDT": _filters()}, warmup_bars=1000)
    return minutes, ledger


from pathlib import Path  # noqa: E402


def test_bridge_time_exit_after_240_bars():
    minutes, ledger = _run("time")
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "server"))
    from sonic.risk import Limits, floor_to, size_position, stop_price
    i, bars, dist = _expected_entry(minutes)
    trade = ledger.trades[0]
    assert trade["opened_ms"] == bars[i].t + FOUR_H
    assert trade["legs"][0]["raw"] == bars[i].c
    qty = size_position(Limits(), bars[i].c, dist, 0.0, _filters(), 30.0).qty
    assert trade["legs"][0]["qty"] == qty
    eff = bars[i].c * 1.0002
    assert trade["stop"] == stop_price("BUY", eff, dist, "0.01")
    assert trade["target"] == floor_to(eff + 0.2 * dist, "0.01")
    assert [leg["kind"] for leg in trade["legs"]] == ["entry", "half", "exit"]
    assert trade["legs"][1]["qty"] == floor_to(qty / 2, "0.01")
    assert trade["legs"][1]["raw"] == trade["target"]
    assert trade["reason"] == "trade_exit_time"
    assert trade["closed_ms"] == bars[i + 240].t + FOUR_H
    assert trade["legs"][2]["raw"] == bars[i + 240].c
    assert trade["pnl"] == pytest.approx(costs(trade["legs"])["pnl"])


def test_bridge_moves_stop_to_entry_after_half():
    _, ledger = _run("breakeven")
    trade = ledger.trades[0]
    assert [leg["kind"] for leg in trade["legs"]] == ["entry", "half", "exit"]
    assert trade["reason"] == "breakeven"
    assert trade["legs"][2]["raw"] == pytest.approx(trade["legs"][0]["raw"] * 1.0002)
