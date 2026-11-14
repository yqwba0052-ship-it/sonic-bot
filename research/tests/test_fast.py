"""Sonic Fast research module (docs/superpowers/specs/2026-11-14-sonic-fast-design.md)."""

from __future__ import annotations

import numpy as np

from sonic_research import fast
from sonic_research.data import Bars

H4 = 4 * 3600 * 1000
M15 = 15 * 60 * 1000
T0 = 1_700_000_000_000 - 1_700_000_000_000 % H4


def bars(symbol: str, closes: list[float], step: int, start: int = T0) -> Bars:
    c = np.array(closes, dtype=float)
    o = np.concatenate([[c[0]], c[:-1]])
    t = np.array([start + i * step for i in range(len(c))], dtype=np.int64)
    return Bars(symbol, t, o, np.maximum(o, c) * 1.002, np.minimum(o, c) * 0.998, c)


def rising(n: int, start: float = 100.0, rate: float = 0.002) -> list[float]:
    return [start * (1 + rate) ** i for i in range(n)]


# ---------- Task 1: 4h regime aligned to fast bars without look-ahead

def test_regime_uses_only_closed_4h_bars() -> None:
    t4 = np.array([T0, T0 + H4, T0 + 2 * H4], dtype=np.int64)
    # a 15m bar closing at T0+H4 sees the first 4h bar (closed at T0+H4), not the second
    fast_t = np.array([T0 + H4 - M15, T0 + 2 * H4 - 2 * M15, T0 + 2 * H4 - M15], dtype=np.int64)
    idx = fast.closed_4h_index(fast_t, M15, t4)
    assert idx.tolist() == [0, 0, 1]
    before_first_close = fast.closed_4h_index(np.array([T0], dtype=np.int64), M15, t4)
    assert before_first_close.tolist() == [-1]


def test_regime_needs_btc_coin_and_trend() -> None:
    n = 700
    btc_up, coin_up = bars("BTCUSDT", rising(n), H4), bars("SOLUSDT", rising(n), H4)
    fast_t = np.array([T0 + (n - 1) * H4], dtype=np.int64)  # last 4h bar closed
    assert fast.regime(fast_t, H4, coin_up, btc_up)["ok"].tolist() == [True]
    btc_down = bars("BTCUSDT", rising(n, rate=-0.002), H4)
    assert fast.regime(fast_t, H4, coin_up, btc_down)["ok"].tolist() == [False]
    coin_down = bars("SOLUSDT", rising(n, rate=-0.002), H4)
    assert fast.regime(fast_t, H4, coin_down, btc_up)["ok"].tolist() == [False]
    # coin above its EMA 200 but EMA 20 under EMA 100 (a fresh dip after a long rise)
    dip = rising(n - 30) + [rising(n - 30)[-1] * (1 - 0.004) ** i for i in range(1, 31)]
    coin_dip = bars("SOLUSDT", dip, H4)
    closes = np.array(dip)
    assert closes[-1] > fast.ema(closes, 200)[-1]  # the setup: still above EMA 200 ...
    assert fast.ema(closes, 20)[-1] < fast.ema(closes, 100)[-1]  # ... but EMA 20 under 100
    assert fast.regime(fast_t, H4, coin_dip, btc_up)["ok"].tolist() == [False]


def test_missing_bars_do_not_shift_alignment() -> None:
    t4 = np.array([T0, T0 + H4, T0 + 4 * H4, T0 + 5 * H4], dtype=np.int64)  # two 4h bars missing
    fast_t = np.array([T0 + 4 * H4 + H4 - M15], dtype=np.int64)  # closes when bar at +4H closes
    assert fast.closed_4h_index(fast_t, M15, t4).tolist() == [2]
    fast_t = np.array([T0 + 3 * H4], dtype=np.int64)  # inside the gap: last closed is index 1
    assert fast.closed_4h_index(fast_t, M15, t4).tolist() == [1]


def test_coin_listed_late_is_never_ok_before_listing() -> None:
    n = 700
    btc = bars("BTCUSDT", rising(n), H4)
    late = bars("SUIUSDT", rising(400), H4, start=T0 + 300 * H4)
    fast_t = np.array([T0 + 100 * H4, T0 + (n - 1) * H4], dtype=np.int64)
    ok = fast.regime(fast_t, H4, late, btc)["ok"].tolist()
    assert ok[0] is False
