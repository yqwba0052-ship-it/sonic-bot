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


# ---------- Task 3: exits, costs, sizing

def flat_after(entry_bar_closes: list[float], highs: list[float], lows: list[float]) -> Bars:
    """Bars 0..k-1 as given (bar 0 is the signal bar, bar 1 the fill bar)."""
    c = np.array(entry_bar_closes, dtype=float)
    t = np.array([T0 + i * M15 for i in range(len(c))], dtype=np.int64)
    o = np.concatenate([[c[0]], c[:-1]])
    return Bars("SOLUSDT", t, o, np.array(highs, float), np.array(lows, float), c)


CAND = fast.Candidate("SOLUSDT", 0, T0 + M15, 100.0, 98.0, 104.0, 1.0)


def test_stop_first_when_both_touched() -> None:
    b = flat_after([100, 100, 100], [100, 105, 100], [100, 97, 100])
    assert fast.exit_of(CAND, b, hold=10) == (1, 98.0, "stop")


def test_target_and_time_exits() -> None:
    hit = flat_after([100, 101, 103], [100, 101, 104.5], [100, 99.5, 102])
    assert fast.exit_of(CAND, hit, hold=10) == (2, 104.0, "target")
    quiet = flat_after([100, 100.5, 101, 101.2, 101.1], [101] * 5, [99.5] * 5)
    assert fast.exit_of(CAND, quiet, hold=3) == (3, 101.2, "time")


def test_gap_below_stop_fills_at_the_open() -> None:
    b = flat_after([100, 96, 96], [100, 96.5, 96.5], [100, 95.5, 95.5])
    b.o[1] = 96.5  # the fill bar already opens under the stop
    idx, price, why = fast.exit_of(CAND, b, hold=10)
    assert (idx, why) == (1, "stop") and price == 96.5  # worse than planned, as in reality


def test_open_at_end_is_closed_at_last_close_and_marked_end() -> None:
    b = flat_after([100, 100.5, 100.7], [101] * 3, [99.5] * 3)
    assert fast.exit_of(CAND, b, hold=50) == (2, 100.7, "end")


def test_planned_risk_is_half_percent_with_costs() -> None:
    c = fast.Candidate("SOLUSDT", 0, 0, 100.0, 99.0, 102.0, 0.5)  # stop 1% away
    qty, planned = fast.sized(c, equity=30.0, cost=0.0014)
    assert abs(planned - 0.15) < 1e-9
    per_unit = 1.0 + 0.0007 * (100.0 + 99.0)  # price distance + half the cost at each end
    assert abs(qty - 0.15 / per_unit) < 1e-9
    notional = qty * 100.0
    assert 13.0 < notional < 13.5  # the position (~13.2$) is not the risk (0.15$)
    assert notional / 3 < 30.0  # margin at leverage 3


def test_size_is_capped_by_leverage() -> None:
    tight = fast.Candidate("SOLUSDT", 0, 0, 100.0, 99.99, 100.5, 0.01)  # 0.01% stop
    qty, planned = fast.sized(tight, equity=30.0, cost=0.0014)
    assert qty * 100.0 <= 30.0 * 3 + 1e-9 and planned < 0.15


def test_cost_only_changes_pnl() -> None:
    c = fast.Candidate("SOLUSDT", 0, 0, 100.0, 99.0, 102.0, 0.5)
    qty, _ = fast.sized(c, equity=30.0, cost=0.0014)  # sized at the base cost, always
    results = [fast.pnl(c, qty, 102.0, cost) for cost in (0.0014, 0.0020, 0.0030)]
    assert results[0] > results[1] > results[2]
    assert abs(results[0] - (qty * 2.0 - qty * 0.0007 * 202.0)) < 1e-12


# ---------- Task 4: portfolio limits and Sonic overlap

DAY_BARS = 96  # 15m bars per day


def track(symbol: str, n: int, dips: tuple[int, ...] = ()) -> Bars:
    """Flat at 100; at each index in `dips` the low falls to 90 (any stop at 98 is hit)."""
    c = np.full(n, 100.0)
    low = np.full(n, 99.5)
    low[list(dips)] = 90.0
    t = np.array([T0 + i * M15 for i in range(n)], dtype=np.int64)
    return Bars(symbol, t, c.copy(), np.full(n, 100.5), low, c)


def cand(symbol: str, i: int) -> fast.Candidate:
    return fast.Candidate(symbol, i, T0 + (i + 1) * M15, 100.0, 98.0, 104.0, 1.0)


END = T0 + 400 * M15


def test_max_two_open() -> None:
    b = {s: track(s, 400) for s in ("SOLUSDT", "XRPUSDT", "ADAUSDT")}
    cands = [cand("SOLUSDT", 5), cand("XRPUSDT", 5), cand("ADAUSDT", 5)]
    trades = fast.run(cands, b, hold=96, cost=0.0014, sonic=[], start_ms=T0, end_ms=END)
    assert sorted(t.symbol for t in trades) == ["SOLUSDT", "XRPUSDT"]


def test_one_trade_per_coin_and_skip_coin_with_open_sonic_trade() -> None:
    b = {s: track(s, 400) for s in ("SOLUSDT", "XRPUSDT")}
    sonic = [{"symbol": "XRPUSDT", "opened_ms": T0, "closed_ms": END, "risk_usdt": 0.3, "pnl": 0}]
    cands = [cand("SOLUSDT", 5), cand("SOLUSDT", 6), cand("XRPUSDT", 5)]
    trades = fast.run(cands, b, hold=96, cost=0.0014, sonic=sonic, start_ms=T0, end_ms=END)
    assert [t.symbol for t in trades] == ["SOLUSDT"]


def test_daily_limit_counts_realized_open_and_new_risk_with_fees() -> None:
    b = {"SOLUSDT": track("SOLUSDT", 400, dips=(2, 5, 8)),
         "XRPUSDT": track("XRPUSDT", 400), "ADAUSDT": track("ADAUSDT", 400)}
    cands = [cand("SOLUSDT", 0), cand("SOLUSDT", 3), cand("SOLUSDT", 6),  # three losses today
             cand("XRPUSDT", 10),  # stays open: 0.5% planned
             cand("ADAUSDT", 12),  # 1.5% lost + 0.5% open + 0.5% new > 2% -> refused
             cand("ADAUSDT", DAY_BARS + 20)]  # next UTC day -> allowed
    trades = fast.run(cands, b, hold=1000, cost=0.0014, sonic=[], start_ms=T0, end_ms=END)
    opened = [(t.symbol, t.opened_ms) for t in trades]
    assert ("ADAUSDT", cands[4].t_ms) not in opened
    assert ("ADAUSDT", cands[5].t_ms) in opened
    assert sum(1 for t in trades if t.why == "stop") == 3


def test_combined_limit_with_sonic_open_risk() -> None:
    b = {s: track(s, 400) for s in ("SOLUSDT", "XRPUSDT")}
    sonic = [{"symbol": "BNBUSDT", "opened_ms": T0, "closed_ms": END, "risk_usdt": 2.2, "pnl": 0}]
    cands = [cand("SOLUSDT", 5), cand("XRPUSDT", 6)]
    trades = fast.run(cands, b, hold=96, cost=0.0014, sonic=sonic, start_ms=T0, end_ms=END)
    # 2.2 + 0.15 <= 4% of 60 = 2.4 is allowed; 2.2 + 0.15 + 0.15 = 2.5 is not
    assert [t.symbol for t in trades] == ["SOLUSDT"]


def test_no_size_increase_after_loss() -> None:
    b = {"SOLUSDT": track("SOLUSDT", 400, dips=(2, 5))}
    cands = [cand("SOLUSDT", 0), cand("SOLUSDT", 3), cand("SOLUSDT", 6)]
    trades = fast.run(cands, b, hold=96, cost=0.0014, sonic=[], start_ms=T0, end_ms=END)
    equity = 30.0
    for t in trades:  # every trade risks 0.5% of the equity it had, never more
        assert abs(t.planned_risk - 0.005 * equity) < 1e-9
        equity += t.pnl
    assert trades[2].qty < trades[0].qty  # smaller after two losses, not doubled


# ---------- Task 5: metrics and the owner's separate report

def day_ms(d: int, hour: int = 10) -> int:
    return int(np.datetime64("2025-01-01").astype("datetime64[ms]").astype(np.int64)) \
        + d * 86_400_000 + hour * 3_600_000


def trade(d: int, pnl: float, planned: float = 0.15, hour: int = 10) -> fast.Trade:
    return fast.Trade("SOLUSDT", day_ms(d, hour), day_ms(d, hour + 1), 100.0, 101.0, 1.0,
                      pnl, planned, "target" if pnl > 0 else "stop")


def test_metrics_on_known_trades() -> None:
    import datetime as dt

    first = dt.date(2025, 1, 1)
    trades = [trade(0, 0.30), trade(0, -0.15, hour=14), trade(2, 0.30), trade(9, 0.15)]
    suitable = {first, dt.date(2025, 1, 2), dt.date(2025, 1, 3), dt.date(2025, 1, 10)}
    m = fast.metrics(trades, suitable, first, dt.date(2025, 1, 10), 30.0, cost=0.0)
    assert m["trades"] == 4 and m["suitable_days"] == 4
    assert m["per_suitable_day"] == 1.0  # 4 trades on 4 suitable days
    assert m["days_2plus"] == 1 and m["days_without_trade"] == 7
    assert m["win_rate"] == 0.75 and m["profit_factor"] == 5.0
    assert abs(m["net_pnl"] - 0.60) < 1e-9
    assert abs(m["max_drawdown"] - 0.15 / 30.30) < 1e-6
    assert m["recovery_days_max"] == 2  # peak on day 0, back above it on day 2
    assert m["longest_pause_days"] == 7  # between day 2 and day 9
    assert m["months_positive"] == 1.0 and m["worst_month"][0] == "2025-01"


def test_fee_share_and_loss_beyond_plan() -> None:
    import datetime as dt

    gapped = trade(1, -0.30, planned=0.15)  # lost twice what was planned (a gap)
    m = fast.metrics([trade(0, 0.30), gapped], set(), dt.date(2025, 1, 1),
                     dt.date(2025, 1, 2), 30.0, cost=0.0014)
    assert m["worst_loss_vs_planned"] == 2.0
    fee_each = 0.0007 * 1.0 * (100.0 + 101.0)
    gross_wins = 0.30 + fee_each  # the winner before its fees
    assert abs(m["fee_share"] - round(2 * fee_each / gross_wins, 6)) < 1e-9  # fees / gross wins


def test_suitable_days_from_regime() -> None:
    import datetime as dt

    t = np.array([day_ms(0), day_ms(0, 12), day_ms(1), day_ms(3)], dtype=np.int64)
    regs = {"SOLUSDT": {"ok": np.array([False, True, False, False])},
            "XRPUSDT": {"ok": np.array([False, False, False, True])}}
    days = fast.suitable_days(regs, {"SOLUSDT": t, "XRPUSDT": t})
    assert days == {dt.date(2025, 1, 1), dt.date(2025, 1, 4)}


# ---------- Task 6: selection, freezing, hold-out, verdict

def test_select_respects_constraints_and_picks_max_net() -> None:
    from sonic_research import fast_run

    rows = [
        {"params": "a", "metrics": {"net_pnl": 9.0, "profit_factor": 1.1, "max_drawdown": 0.05,
                                    "per_suitable_day": 2.0}},  # PF too low
        {"params": "b", "metrics": {"net_pnl": 8.0, "profit_factor": 1.5, "max_drawdown": 0.12,
                                    "per_suitable_day": 2.0}},  # drawdown too deep
        {"params": "c", "metrics": {"net_pnl": 7.0, "profit_factor": 1.5, "max_drawdown": 0.05,
                                    "per_suitable_day": 1.2}},  # too few trades
        {"params": "d", "metrics": {"net_pnl": 3.0, "profit_factor": 1.3, "max_drawdown": 0.08,
                                    "per_suitable_day": 1.6}},
        {"params": "e", "metrics": {"net_pnl": 5.0, "profit_factor": 1.25, "max_drawdown": 0.09,
                                    "per_suitable_day": 1.5}},
    ]
    assert fast_run.select(rows)["params"] == "e"
    assert fast_run.select(rows[:3]) is None  # nothing passes: nothing is chosen


def test_holdout_refuses_without_frozen_file(tmp_path) -> None:
    import pytest

    from sonic_research import fast_run

    with pytest.raises(SystemExit, match="frozen"):
        fast_run.holdout(frozen=tmp_path / "none.json", out=tmp_path / "out.json")


def test_freeze_writes_a_hash(tmp_path) -> None:
    import hashlib

    from sonic_research import fast_run

    path = tmp_path / "frozen.json"
    digest = fast_run.freeze({"15m": None}, path)
    assert digest == hashlib.sha256(path.read_bytes()).hexdigest()


def test_verdict_rejects_each_failed_criterion() -> None:
    from sonic_research import fast_run

    good = {"net_pnl": 4.0, "profit_factor": 1.3, "max_drawdown": 0.08, "per_suitable_day": 1.6}
    ok, why = fast_run.verdict(good, combined_worst_pct=-5.0, sonic_worst_pct=-4.0)
    assert ok and not why
    for key, bad in (("net_pnl", -0.1), ("profit_factor", 1.1), ("max_drawdown", 0.11),
                     ("per_suitable_day", 1.4)):
        assert fast_run.verdict({**good, key: bad}, -5.0, -4.0)[0] is False
    assert fast_run.verdict(good, combined_worst_pct=-6.5, sonic_worst_pct=-4.0)[0] is False
