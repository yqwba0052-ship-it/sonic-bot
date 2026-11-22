"""Intrabar paper core: definitions, fills, exits, day rules, no look-ahead."""

from __future__ import annotations

from intrabar.core import (
    FEE_MAKER,
    FEE_TAKER,
    SLIP,
    Account,
    Engine,
    Second,
    SymbolBars,
    Variant,
)
from intrabar.thresholds import quantile

T0 = 1_795_000_000 - 1_795_000_000 % 900   # a 15-minute boundary
V = Variant("B1", 60, 0.01, 0.002, "all_day")


def feed(bars: SymbolBars, rows):
    closed = []
    for sec, price, qty, maker in rows:
        c = bars.add_trade(sec, price, qty, maker)
        if c:
            closed.append(c)
    return closed


def test_seconds_close_only_when_the_next_second_starts():
    b = SymbolBars((60,))
    assert b.add_trade(T0, 100.0, 1, False) is None
    assert b.add_trade(T0, 101.0, 1, True) is None
    closed = b.add_trade(T0 + 1, 100.5, 1, False)
    assert closed.sec == T0 and closed.o == 100 and closed.h == 101 and closed.c == 101
    assert closed.buy == 1 and closed.sell == 1


def test_features_use_only_the_past():
    rows = [(T0 + i, 100 + i * 0.01, 1, False) for i in range(20)]
    b1, b2 = SymbolBars((60,)), SymbolBars((60,))
    c1 = feed(b1, rows + [(T0 + 20, 100.2, 1, False)])
    c2 = feed(b2, rows + [(T0 + 20, 999.0, 50, True)])   # a wild FUTURE trade
    f1 = b1.features(c1[18], 60)
    f2 = b2.features(c2[18], 60)
    assert f1 == f2   # the decision at the end of second 18 cannot see second 20


def test_breakout_means_above_every_earlier_high_of_the_candle():
    b = SymbolBars((60,))
    closed = feed(b, [(T0, 100, 1, False), (T0 + 1, 99, 1, False), (T0 + 2, 100.5, 1, False),
                      (T0 + 3, 100.4, 1, False)])
    assert b.features(closed[2], 60)["breakout"] is True     # 100.5 > 100 and 99
    assert b.features(closed[1], 60)["breakout"] is False


def test_limit_fills_only_when_a_trade_prints_below_it():
    a = Account(V)
    a.place("SOLUSDT", Second(T0, 100, 100, 100, 100), T0 + 60)
    assert a.on_trade("SOLUSDT", T0 + 1, 100.0, 100.0) is None and a.position is None
    a.on_trade("SOLUSDT", T0 + 2, 99.99, 100.0)
    assert a.position is not None and a.position["entry"] == 100


def test_target_needs_a_trade_above_it_and_pays_maker_fees():
    a = Account(V)
    a.place("SOLUSDT", Second(T0, 100, 100, 100, 100), T0 + 60)
    a.on_trade("SOLUSDT", T0 + 2, 99.99, 100)
    assert a.on_trade("SOLUSDT", T0 + 3, 100.2, 99.99) is None     # touching is not enough
    rec = a.on_trade("SOLUSDT", T0 + 4, 100.21, 100.2)
    assert rec["kind"] == "target"
    assert abs(rec["net"] - (0.002 - 2 * FEE_MAKER)) < 1e-12


def test_stop_and_candle_close_are_market_exits():
    a = Account(V)
    a.place("SOLUSDT", Second(T0, 100, 100, 100, 100), T0 + 60)
    a.on_trade("SOLUSDT", T0 + 2, 99.99, 100)
    rec = a.on_trade("SOLUSDT", T0 + 5, 99.7, 99.9)
    assert rec["kind"] == "stop" and abs(rec["exit"] - 99.7 * (1 - SLIP)) < 1e-9
    assert abs(rec["fees"] - (FEE_MAKER + FEE_TAKER)) < 1e-12
    b = Account(V)
    b.place("SOLUSDT", Second(T0, 100, 100, 100, 100), T0 + 60)
    b.on_trade("SOLUSDT", T0 + 2, 99.99, 100)
    rec = b.on_trade("SOLUSDT", T0 + 61, 100.05, 100.1)
    assert rec["kind"] == "candle_close" and abs(rec["exit"] - 100.1 * (1 - SLIP)) < 1e-9


def test_unfilled_limit_expires():
    a = Account(V)
    a.place("SOLUSDT", Second(T0, 100, 100, 100, 100), T0 + 60)
    a.on_trade("SOLUSDT", T0 + 61, 99.0, 100)
    assert a.pending is None and a.position is None


def _close_one(a: Account, win: bool, sec: int) -> dict:
    a.place("SOLUSDT", Second(sec, 100, 100, 100, 100), sec + 60)
    a.on_trade("SOLUSDT", sec + 1, 99.99, 100)
    return a.on_trade("SOLUSDT", sec + 2, 100.3 if win else 99.7, 100)


def test_day_rules():
    a = Account(V)                       # two wins -> stop for the day
    _close_one(a, True, T0)
    a.days[T0 // 86400]["per_coin"] = {}  # allow a 3rd attempt only to test the stop flag
    _close_one(a, True, T0 + 100)
    assert a.days[T0 // 86400]["stopped"]
    b = Account(V)                       # win then loss: go on until the day is positive
    _close_one(b, True, T0)
    _close_one(b, False, T0 + 100)
    assert not b.days[T0 // 86400]["stopped"]
    c = Account(V)                       # two losses -> stop
    _close_one(c, False, T0)
    _close_one(c, False, T0 + 100)
    assert c.days[T0 // 86400]["stopped"]


def test_two_trades_per_coin_per_day_at_most():
    a = Account(Variant("B1", 60, 0.01, 0.002, "all_day"))
    _close_one(a, True, T0)
    _close_one(a, False, T0 + 100)
    assert not a.can_open("SOLUSDT", T0 + 200)   # day not stopped, but coin limit reached
    assert a.can_open("XRPUSDT", T0 + 200)


def test_engine_places_after_the_second_closes_and_never_fills_on_the_same_trade():
    thr = {"SOLUSDT": {60: {0.01: {"rise": 0.0001, "buyshare30": 0.5}}}}
    e = Engine([V], ["SOLUSDT"], thr)
    for i in range(12):
        e.on_trade("SOLUSDT", (T0 + i) * 1000, 100 + i * 0.01, 1, False)
    acc = e.accounts[V.name]
    assert acc.pending is not None and acc.position is None
    limit = acc.pending["price"]
    e.on_trade("SOLUSDT", (T0 + 12) * 1000 + 500, limit - 0.01, 1, True)
    assert acc.position is not None and acc.position["entry"] == limit
    # a trade far below both the limit and the stop fills and stops out at once (conservative)
    e2 = Engine([V], ["SOLUSDT"], thr)
    for i in range(12):
        e2.on_trade("SOLUSDT", (T0 + i) * 1000, 100 + i * 0.01, 1, False)
    out = e2.on_trade("SOLUSDT", (T0 + 12) * 1000, 99.0, 1, True)
    assert out and out[0]["kind"] == "stop"


def test_quantile_matches_numpy_default():
    vals = [1.0, 2.0, 3.0, 4.0, 10.0]
    assert quantile(vals, 0.5) == 3.0
    assert abs(quantile(vals, 0.9) - 7.6) < 1e-12
