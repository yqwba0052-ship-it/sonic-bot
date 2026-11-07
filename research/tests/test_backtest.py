from __future__ import annotations

import numpy as np
import pytest

from sonic_research.backtest import FEE, SLIPPAGE, Signals, simulate
from sonic_research.data import BAR_MS, Bars
from sonic_research.metrics import summarize


def make_bars(rows: list[tuple[float, float, float, float]]) -> Bars:
    data = np.array(rows, dtype=float)
    times = np.arange(len(rows), dtype=np.int64) * BAR_MS
    return Bars("TEST", times, data[:, 0], data[:, 1], data[:, 2], data[:, 3])


def one_signal(n: int, at: int, side: int, dist: float) -> Signals:
    entry = np.zeros(n, dtype=np.int8)
    entry[at] = side
    return Signals(entry, np.zeros(n, bool), np.zeros(n, bool), np.full(n, dist))


def test_signal_fills_at_next_open_not_same_close() -> None:
    bars = make_bars([(100, 101, 99, 100), (110, 111, 109, 110), (110, 111, 109, 110)])
    trade = simulate(bars, one_signal(3, 0, 1, 50), None, 1, {})[0]
    assert trade.entry == pytest.approx(110 * (1 + SLIPPAGE))


def test_stop_wins_when_bar_touches_stop_and_target() -> None:
    bars = make_bars([(100, 100, 100, 100), (100, 100, 100, 100), (100, 120, 80, 100)])
    trade = simulate(bars, one_signal(3, 0, 1, 10), 1.0, 10, {})[0]
    assert trade.reason == "stop"
    assert trade.r < -1


def test_gap_through_stop_fills_at_open() -> None:
    bars = make_bars([(100, 100, 100, 100), (100, 100, 100, 100), (70, 72, 69, 71)])
    trade = simulate(bars, one_signal(3, 0, 1, 10), None, 10, {})[0]
    assert trade.reason == "stop_gap"
    assert trade.exit == pytest.approx(70 * (1 - SLIPPAGE))


def test_flat_trade_loses_exactly_costs() -> None:
    bars = make_bars([(100, 100, 100, 100)] * 4)
    trade = simulate(bars, one_signal(4, 0, 1, 10), None, 2, {})[0]
    entry, exit_ = 100 * (1 + SLIPPAGE), 100 * (1 - SLIPPAGE)
    assert trade.ret == pytest.approx((exit_ - entry) / entry - 2 * FEE)


def test_long_pays_and_short_receives_positive_funding() -> None:
    bars = make_bars([(100, 100, 100, 100)] * 5)
    funding = {int(2 * BAR_MS): 0.001}
    long = simulate(bars, one_signal(5, 0, 1, 10), None, 3, funding)[0]
    short = simulate(bars, one_signal(5, 0, -1, 10), None, 3, funding)[0]
    long_free = simulate(bars, one_signal(5, 0, 1, 10), None, 3, {})[0]
    short_free = simulate(bars, one_signal(5, 0, -1, 10), None, 3, {})[0]
    assert long.ret == pytest.approx(long_free.ret - 0.001)
    assert short.ret == pytest.approx(short_free.ret + 0.001)


def test_no_new_trade_while_one_is_open() -> None:
    bars = make_bars([(100, 100, 100, 100)] * 6)
    entry = np.zeros(6, dtype=np.int8)
    entry[[0, 1, 2]] = 1
    signals = Signals(entry, np.zeros(6, bool), np.zeros(6, bool), np.full(6, 10.0))
    trades = simulate(bars, signals, None, 3, {})
    assert len(trades) == 1


def test_summary_counts_profit_factor_and_drawdown() -> None:
    bars = make_bars([(100, 100, 100, 100)] * 4)
    trades = simulate(bars, one_signal(4, 0, 1, 10), None, 2, {})
    summary = summarize(trades)
    assert summary["trades"] == 1
    assert summary["win_rate"] == 0.0
    assert summary["max_drawdown"] > 0
