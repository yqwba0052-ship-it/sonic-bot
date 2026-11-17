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


# ---- risk engine


def _sig(i, entry, stop, target=None, invalid=None, htf=True, name="x"):
    from sonic_research.classical.strategies import Signal
    return Signal(name, "long", i, T0 + i * H4, entry, stop, target, invalid, "test", htf)


def _bars(rows):
    o, h, low, c = (np.array(x, float) for x in zip(*rows, strict=True))
    return Series("SOLUSDT", T0 + H4 * np.arange(len(rows), dtype=np.int64), o, h, low, c)


def test_stop_is_checked_before_target_in_one_bar():
    from sonic_research.classical.engine import Combo, outcome
    s = _bars([(100, 100, 100, 100), (100, 120, 90, 100)])
    o = outcome(s, _sig(0, 100.0, 95.0, 110.0), Combo(1.5, "X1", False))
    assert o["reason"] == "stop" and o["legs"] == [(1.0, 95.0)]


def test_x2_takes_half_at_1r_then_breakeven():
    from sonic_research.classical.engine import Combo, outcome
    s = _bars([(100, 100, 100, 100), (100, 105.5, 99.5, 105), (105, 105, 99.0, 100)])
    o = outcome(s, _sig(0, 100.0, 95.0, 115.0), Combo(1.5, "X2", False))
    assert o["legs"][0] == (0.5, 105.0) and o["reason"] == "breakeven"
    assert o["legs"][1][0] == 0.5 and o["legs"][1][1] == pytest.approx(100 * 1.0002)


def test_rr_filter_and_htf_filter():
    from sonic_research.classical.engine import Combo, outcome
    s = _bars([(100, 100, 100, 100), (100, 101, 99, 100)])
    assert outcome(s, _sig(0, 100.0, 95.0, 106.0), Combo(1.5, "X1", False)) is None  # RR 1.2
    assert outcome(s, _sig(0, 100.0, 95.0, 110.0, htf=False), Combo(1.5, "X1", True)) is None


def test_target_from_rr_when_signal_has_none():
    from sonic_research.classical.engine import Combo, outcome
    s = _bars([(100, 100, 100, 100), (100, 111, 99, 110)])
    o = outcome(s, _sig(0, 100.0, 95.0), Combo(2.0, "X1", False))
    assert o["reason"] == "target" and o["legs"] == [(1.0, 110.0)]


def test_time_exit_and_invalidation():
    from sonic_research.classical.engine import Combo, outcome
    flat = _bars([(100, 100.5, 99.5, 100)] * 40)
    o = outcome(flat, _sig(0, 100.0, 95.0, 120.0), Combo(1.5, "X1", False))
    assert o["reason"] == "time" and o["exit_j"] == 30
    inv = _bars([(100, 100, 100, 100), (100, 100.5, 97.5, 97.8)])
    o = outcome(inv, _sig(0, 100.0, 95.0, 120.0, invalid=98.0), Combo(1.5, "X1", False))
    assert o["reason"] == "invalid" and o["legs"] == [(1.0, 97.8)]


def test_unit_pnl_costs_long_and_short_mirror():
    from sonic_research.classical.engine import unit_pnl
    long_ = unit_pnl(100.0, [(1.0, 110.0)])
    entry, out = 100 * 1.0002, 110 * 0.9998
    assert long_ == pytest.approx(out - entry - 0.0005 * (entry + out))
    short = unit_pnl(-100.0, [(1.0, -90.0)])  # sell 100, buy back 90 on the inverted series
    entry_s, out_s = 100 * 0.9998, 90 * 1.0002
    assert short == pytest.approx(entry_s - out_s - 0.0005 * (entry_s + out_s))


def _filters(min_notional=5.0):
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "server"))
    from sonic.risk import Filters
    return Filters(step_size="0.01", tick_size="0.01", min_qty=0.01, min_notional=min_notional)


def _cand(coin, i, exit_j, pnl_unit, entry=100.0, stop=98.0):
    return {"coin": coin, "strategy": "x", "side": "long", "entry_t": T0 + i * H4,
            "exit_t": T0 + exit_j * H4, "entry_raw": entry, "stop": stop,
            "pnl_unit": pnl_unit, "reason": "target", "t_ms": T0 + i * H4}


def test_portfolio_sizing_limits_and_unrepresentable():
    from sonic_research.classical.engine import portfolio
    f = {"SOLUSDT": _filters(), "XRPUSDT": _filters(), "ADAUSDT": _filters(),
         "BNBUSDT": _filters(1000.0)}
    cands = [_cand("SOLUSDT", 0, 5, 2.0), _cand("SOLUSDT", 1, 6, 2.0),  # same coin: skipped
             _cand("XRPUSDT", 1, 6, 2.0), _cand("ADAUSDT", 2, 6, 2.0),
             _cand("BNBUSDT", 2, 6, 2.0)]                               # min order too big
    res = portfolio(cands, f, risk_pct=0.5, max_open=2)
    taken = [(t["coin"], t["qty"]) for t in res["trades"]]
    assert taken == [("SOLUSDT", 0.07), ("XRPUSDT", 0.07)]  # 0.15$ risk / 2$ stop -> 0.07
    reasons = sorted(x["why"] for x in res["skipped"])
    assert reasons == ["coin_open", "max_open", "not_representable"] or \
        reasons == ["coin_open", "max_open", "max_open"]
    assert all(t["notional"] / 3 <= 30 for t in res["trades"])


def test_portfolio_never_uses_more_margin_than_equity():
    from sonic_research.classical.engine import portfolio
    f = {c: _filters() for c in ("SOLUSDT", "XRPUSDT", "ADAUSDT")}
    cands = [_cand(c, 0, 9, 1.0, entry=100.0, stop=99.99) for c in ("SOLUSDT", "XRPUSDT",
                                                                      "ADAUSDT")]
    res = portfolio(cands, f, risk_pct=0.5, max_open=3)
    assert sum(t["notional"] / 3 for t in res["trades"]) <= 30 + 1e-9
    assert any(x["why"] == "capital" for x in res["skipped"])


def test_half_percent_risk_with_a_wide_stop_cannot_be_represented():
    from sonic_research.classical.engine import portfolio
    res = portfolio([_cand("SOLUSDT", 0, 5, 1.0, stop=95.0)], {"SOLUSDT": _filters()})
    assert res["trades"] == [] and res["skipped"][0]["why"] == "not_representable"


# ---- evaluation: metrics, walk-forward choice, qualification


def _tr(pnl, day, coin="SOLUSDT", strategy="x"):
    t = T0 + day * 86_400_000
    return {"coin": coin, "strategy": strategy, "entry_t": t, "exit_t": t + H4, "pnl": pnl,
            "qty": 1.0, "entry_raw": 100.0, "stop": 98.0}


def test_metrics_net_pf_drawdown_and_best_trade_removal():
    from sonic_research.classical.evaluate import metrics
    m = metrics([_tr(3.0, 0), _tr(-1.5, 1), _tr(1.0, 40)])
    assert m["trades"] == 3 and m["net"] == pytest.approx(2.5)
    assert m["profit_factor"] == pytest.approx(4.0 / 1.5)
    assert m["max_dd_pct"] == pytest.approx(100 * 1.5 / 33.0, rel=1e-6)
    assert m["net_without_best"] == pytest.approx(-0.5)
    assert m["trading_days"] == 3 and set(m["by_year"]) == {"2022"}


def test_qualification_rules_as_written():
    from sonic_research.classical.evaluate import qualifies
    good = {"trades": 12, "net": 3.0, "profit_factor": 1.5, "net_cost2": 1.0,
            "net_without_best": 0.5, "max_dd_pct": 10.0, "folds_positive": 2}
    assert qualifies(good)["ok"] is True
    for key, bad in (("trades", 9), ("net", -0.1), ("profit_factor", 1.2), ("net_cost2", 0.0),
                     ("net_without_best", -0.01), ("max_dd_pct", 21.0), ("folds_positive", 1)):
        assert qualifies(dict(good, **{key: bad}))["ok"] is False, key


def test_pooled_qualification_rules_as_written():
    from sonic_research.classical.evaluate import qualifies_pooled
    good = {"trades": 30, "net": 1.0, "profit_factor": 1.2, "net_cost2": 0.1,
            "net_without_best": 0.1}
    assert qualifies_pooled(good)["ok"] is True
    assert qualifies_pooled(dict(good, trades=29))["ok"] is False


def test_choose_combo_needs_five_trades_and_best_net():
    from sonic_research.classical.evaluate import choose
    results = {"a": {"trades": 4, "net": 9.0}, "b": {"trades": 6, "net": 1.0},
               "c": {"trades": 8, "net": 2.0}}
    assert choose(results) == "c"
    assert choose({"a": {"trades": 4, "net": 9.0}}) is None


def test_folds_select_before_they_test():
    from sonic_research.classical.evaluate import FOLDS
    for sel, test in FOLDS:
        assert sel[1] == test[0] and sel[0] < sel[1] < test[1]
