"""Classical Strategy Lab tests: causality first, then patterns, risk engine and evaluation."""

import numpy as np
import pytest

from sonic_research.classical.core import K, Series, candles, clusters, pivots

H4 = 4 * 3_600_000
T0 = 1_640_995_200_000


def _series(close, spread=0.01, seed=1):
    rng = np.random.default_rng(seed)
    c = np.asarray(close, dtype=float)
    o = np.r_[c[0], c[:-1]]
    h = np.maximum(o, c) * (1 + rng.uniform(0, spread, len(c)))
    low = np.minimum(o, c) * (1 - rng.uniform(0, spread, len(c)))
    return Series("SOLUSDT", T0 + H4 * np.arange(len(c), dtype=np.int64), o, h, low, c)


def _walk(n=1500, seed=7):
    rng = np.random.default_rng(seed)
    return _series(100 * np.cumprod(1 + rng.normal(0, 0.012, n)), seed=seed)


def test_pivot_needs_k_bars_after_it():
    c = [10, 11, 12, 13, 12, 11, 10, 11, 12]
    s = Series("X", T0 + H4 * np.arange(9), np.array(c, float), np.array(c, float),
               np.array(c, float), np.array(c, float))
    highs, _ = pivots(s)
    assert highs == [(3, 13.0)]
    assert pivots(s.cut(7))[0] == [(3, 13.0)]  # known once bar 3 + K = 6 exists
    assert pivots(s.cut(6))[0] == []  # not yet known one bar earlier
    assert K == 3


def test_clusters_group_nearby_levels():
    assert clusters([10.0, 10.2, 15.0, 10.1], tol=0.25) == [(pytest.approx(10.1), 3), (15.0, 1)]


def test_bullish_engulfing_and_hammer():
    o = np.array([10, 10, 10.0, 9.0])
    c = np.array([10, 10, 9.2, 10.3])
    h = np.array([10, 10, 10.1, 10.4])
    low = np.array([10, 10, 9.1, 8.9])
    s = Series("X", T0 + H4 * np.arange(4), o, h, low, c)
    assert "engulfing" in candles(s, 3)
    hammer = Series("X", T0 + H4 * np.arange(3), np.array([10, 10, 9.9]),
                    np.array([10, 10, 10.05]), np.array([10, 10, 8.9]), np.array([10, 10, 10.0]))
    assert "hammer" in candles(hammer, 2)


ALL_RULES = ["sr_bounce", "sr_bounce_nocandle", "breakout_retest", "trendline", "channel",
             "head_shoulders", "double_bottom", "triangle", "flag", "range_bounce",
             "range_breakout", "wedge", "candle_trend"]


@pytest.mark.parametrize("name", ALL_RULES)
@pytest.mark.parametrize("side", ["long", "short"])
def test_no_repaint_signals_identical_on_cut_data(name, side):
    from sonic_research.classical.strategies import signals
    s = _walk()
    if side == "short":
        s = s.inverted()
    full = signals(s, name)
    for cut in (700, 1013, 1377):
        part = signals(s.cut(cut), name)
        assert [x for x in full if x.i < cut] == part


def test_rules_fire_on_random_data_somewhere():
    from sonic_research.classical.strategies import signals
    s = _walk(3000, seed=11)
    fired = {n for n in ALL_RULES for side in (s, s.inverted()) if signals(side, n)}
    assert len(fired) >= 10  # the detectors are not dead code


def test_double_bottom_detected_on_a_built_shape():
    from sonic_research.classical.strategies import Ctx, double_bottom
    up = list(np.linspace(80, 100, 220))
    shape = (list(np.linspace(100, 90, 8)) + list(np.linspace(90, 96, 8))
             + list(np.linspace(96, 90.2, 8)) + list(np.linspace(90.2, 95.5, 10)) + [99.0])
    s = _series(up + shape, spread=0.001)
    ctx = Ctx(s)
    sig = double_bottom(ctx, len(s) - 1)
    assert sig is not None and sig.stop < 90.2 and sig.target > sig.entry
    assert "bottom" in sig.reason


def test_inverted_series_mirrors_prices():
    s = _walk(300)
    inv = s.inverted()
    assert np.allclose(inv.h, -s.low) and np.allclose(inv.low, -s.h) and inv.side == "short"
