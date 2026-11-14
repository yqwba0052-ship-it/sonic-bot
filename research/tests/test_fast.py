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


# ---------- Task 2: candidate entries, reversal guards, fee coverage

def pullback_series(n_up: int = 300, dip: int = 6, dip_rate: float = 0.008) -> list[float]:
    """A steady rise, a short pullback, then a strong bounce bar."""
    up = rising(n_up, rate=0.002)
    down = [up[-1] * (1 - dip_rate) ** i for i in range(1, dip + 1)]
    bounce = [down[-1] * 1.015, down[-1] * 1.02, down[-1] * 1.025]
    return up + down + bounce


def open_regime(n: int, **overrides) -> dict:
    reg = {"ok": np.ones(n, dtype=bool), "btc_drop_4h": np.zeros(n),
           "btc_atr_pct_4h": np.full(n, 0.01), "coin_above_ema20_4h": np.ones(n, dtype=bool)}
    reg.update(overrides)
    return reg


P = fast.Params(L=40, k=6, ks=1.5, kt=2.0, guard="fixed")


def test_entry_is_next_open_plus_slippage() -> None:
    b = bars("SOLUSDT", pullback_series(), M15)
    cands = fast.candidates("SOLUSDT", b, M15, open_regime(len(b)), P, slip=0.0002)
    assert cands, "the pullback and bounce must give a signal"
    for c in cands:
        assert c.entry == b.o[c.i + 1] * 1.0002
        assert c.t_ms == int(b.t[c.i + 1])
        assert c.stop == c.entry - 1.5 * c.atr and c.target == c.entry + 2.0 * c.atr


def test_future_bars_do_not_change_past_candidates() -> None:
    closes = pullback_series() + rising(60, start=pullback_series()[-1], rate=-0.003)
    b = bars("SOLUSDT", closes, M15)
    reg = open_regime(len(b))
    first = fast.candidates("SOLUSDT", b, M15, reg, P)[0]
    cut = first.i + 2  # everything after the fill bar is the future
    rng = np.random.default_rng(1)
    noisy = Bars("SOLUSDT", b.t, b.o.copy(), b.h.copy(), b.low.copy(), b.c.copy())
    for arr in (noisy.o, noisy.h, noisy.low, noisy.c):
        arr[cut:] = arr[cut:] * rng.uniform(0.8, 1.2, len(arr) - cut)
    again = [c for c in fast.candidates("SOLUSDT", noisy, M15, reg, P) if c.i <= first.i]
    assert again[0] == first


def test_guards_block_entries() -> None:
    b = bars("SOLUSDT", pullback_series(), M15)
    n = len(b)
    assert not fast.candidates("SOLUSDT", b, M15, open_regime(n, ok=np.zeros(n, bool)), P)
    weak4h = open_regime(n, coin_above_ema20_4h=np.zeros(n, bool))
    assert not fast.candidates("SOLUSDT", b, M15, weak4h, P)
    crash = open_regime(n, btc_drop_4h=np.full(n, 0.025))
    assert not fast.candidates("SOLUSDT", b, M15, crash, P)  # fixed: drop above 2%
    small = open_regime(n, btc_drop_4h=np.full(n, 0.01), btc_atr_pct_4h=np.full(n, 0.005))
    assert fast.candidates("SOLUSDT", b, M15, small, P)  # fixed 2%: a 1% drop is fine
    vol = fast.Params(L=40, k=6, ks=1.5, kt=2.0, guard="vol")
    assert not fast.candidates("SOLUSDT", b, M15, small, vol)  # 1% > 1.5 x 0.5%
    deep = bars("SOLUSDT", pullback_series(dip=25, dip_rate=0.012), M15)
    assert not fast.candidates("SOLUSDT", deep, M15, open_regime(len(deep)), P)  # under EMA100


def test_fee_coverage_blocks_small_targets() -> None:
    # a quiet coin: same pullback-and-bounce shape, tiny candles (every other rule passes)
    calm = [100 * (1.0002) ** i for i in range(300)]
    calm += [calm[-1] * (1 - 0.001) ** i for i in range(1, 5)]
    calm += [calm[-1] * (1 + 0.0012 * k) for k in (1, 2, 3)]
    c = np.array(calm)
    o = np.concatenate([[c[0]], c[:-1]])
    t = np.array([T0 + i * M15 for i in range(len(c))], dtype=np.int64)
    tight = Bars("TRXUSDT", t, o, np.maximum(o, c) * 1.00005, np.minimum(o, c) * 0.99995, c)
    reg = open_regime(len(c))
    assert not fast.candidates("TRXUSDT", tight, M15, reg, P)  # 2 x ATR is under 0.42%
    wide = fast.Params(L=40, k=6, ks=1.5, kt=200.0, guard="fixed")
    assert fast.candidates("TRXUSDT", tight, M15, reg, wide)  # a far target clears the margin


def test_zero_atr_gives_no_candidate() -> None:
    b = bars("SOLUSDT", [100.0] * 400, M15)
    assert not fast.candidates("SOLUSDT", b, M15, open_regime(len(b)), P)
