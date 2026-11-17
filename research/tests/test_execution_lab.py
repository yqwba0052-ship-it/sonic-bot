"""Execution lab tests (pandas not needed)."""

import io
import zipfile

import pytest

T = 1_686_816_000_000  # 2023-06-15 08:00 UTC
MIN = 60_000


def _zip(rows, header=True):
    lines = (["agg_trade_id,price,quantity,first_trade_id,last_trade_id,transact_time,"
              "is_buyer_maker"] if header else [])
    lines += [",".join(str(x) for x in r) for r in rows]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("x.csv", "\n".join(lines) + "\n")
    return buf.getvalue()


def test_days_needed_cover_the_wait_window_across_midnight():
    from sonic_research.execution_lab.agg_data import days_needed
    late = 1_686_873_000_000  # 2023-06-15 23:50
    got = days_needed([("SOLUSDT", T), ("SOLUSDT", late)], wait_min=60)
    assert got == [("SOLUSDT", "2023-06-15"), ("SOLUSDT", "2023-06-16")]


def test_days_needed_refuse_2024_without_the_holdout_flag():
    from sonic_research.execution_lab.agg_data import days_needed
    with pytest.raises(ValueError, match="holdout_locked"):
        days_needed([("SOLUSDT", 1_704_067_200_000 + MIN)], wait_min=60)
    assert days_needed([("SOLUSDT", 1_704_067_200_000 + MIN)], wait_min=60, allow_holdout=True)


def test_trades_after_reads_only_the_window_and_parses_flags(tmp_path):
    from sonic_research.execution_lab.agg_data import trades_after
    path = tmp_path / "SOLUSDT-aggTrades-2023-06-15.zip"
    path.write_bytes(_zip([[1, 20.0, 1.0, 1, 1, T - 1, "false"],
                           [2, 20.1, 2.0, 2, 2, T + 1000, "false"],
                           [3, 19.9, 3.0, 3, 3, T + 5000, "true"],
                           [4, 20.5, 1.0, 4, 4, T + 61 * MIN, "false"]]))
    got = trades_after([path], start_ms=T, end_ms=T + 60 * MIN)
    assert [(t.ms, t.price, t.qty, t.buyer_maker) for t in got] == [
        (T + 1000, 20.1, 2.0, False), (T + 5000, 19.9, 3.0, True)]
    noheader = tmp_path / "old.zip"
    noheader.write_bytes(_zip([[2, 20.1, 2.0, 2, 2, T + 1000, "False"]], header=False))
    assert trades_after([noheader], T, T + MIN)[0].price == 20.1


# ---- fill models


def _tr(ms, price, qty, buyer_maker):
    from sonic_research.execution_lab.agg_data import Trade
    return Trade(T + ms, price, qty, buyer_maker)


def test_market_buy_takes_taker_buys_after_latency_vwap():
    from sonic_research.execution_lab.fills import market_buy
    trades = [_tr(500, 19.0, 9, False),          # before the order arrives: ignored
              _tr(3000, 20.0, 1.0, True),         # a market SELL: not our side
              _tr(4000, 20.02, 1.0, False), _tr(5000, 20.04, 2.0, False)]
    f = market_buy(trades, qty=2.0, start_ms=T + 2000)
    assert f.filled == 2.0 and f.price == pytest.approx((20.02 + 20.04) / 2)
    assert f.first_ms == T + 4000 and f.last_ms == T + 5000


def test_limit_buy_needs_a_trade_strictly_below_the_limit():
    from sonic_research.execution_lab.fills import limit_buy
    at_limit = [_tr(3000, 20.0, 50, True), _tr(4000, 20.0, 50, False)]
    f = limit_buy(at_limit, qty=1.0, limit=20.0, start_ms=T + 2000, end_ms=T + 60 * MIN)
    assert f.filled == 0.0  # touching the limit is not enough (queue ahead of us)
    through = at_limit + [_tr(6000, 19.99, 0.4, True), _tr(7000, 19.98, 5, True)]
    f = limit_buy(through, qty=1.0, limit=20.0, start_ms=T + 2000, end_ms=T + 60 * MIN)
    assert f.filled == 1.0 and f.price == 20.0 and f.last_ms == T + 7000


def test_limit_buy_partial_fill_and_window_end():
    from sonic_research.execution_lab.fills import limit_buy
    trades = [_tr(6000, 19.99, 0.3, True), _tr(61 * MIN, 19.5, 9, True)]
    f = limit_buy(trades, qty=1.0, limit=20.0, start_ms=T + 2000, end_ms=T + 60 * MIN)
    assert f.filled == pytest.approx(0.3) and f.partial is True


def test_limit_buy_ignores_trades_before_the_order():
    from sonic_research.execution_lab.fills import limit_buy
    f = limit_buy([_tr(1000, 19.0, 9, True)], qty=1.0, limit=20.0, start_ms=T + 2000,
                  end_ms=T + 60 * MIN)
    assert f.filled == 0.0


# ---- exit path on 1-minute candles (Sonic rules, stop first inside a minute)


def _minutes(rows):
    np = pytest.importorskip("numpy")
    return np.array([[T + i * MIN, o, c, h, low, 1.0] for i, (o, c, h, low) in enumerate(rows)])


def _filters():
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "server"))
    from sonic.risk import Filters
    return Filters(step_size="0.01", tick_size="0.01", min_qty=0.01, min_notional=5.0)


def test_exit_path_stop_first_then_half_then_breakeven():
    from sonic_research.execution_lab.sim import exit_path
    eff, dist = 100.0, 4.0  # stop 96.00, target 100.80
    m = _minutes([(100, 100.5, 100.9, 99.9),   # target touched -> half at 100.80
                  (100.5, 100.2, 100.6, 100.1),
                  (100.2, 99.8, 100.3, 99.7)])  # back below entry -> breakeven at 100.00
    legs, reason = exit_path(m, T, eff, 1.0, dist, _filters(), T + 240 * MIN)
    assert reason == "breakeven"
    assert [(leg["kind"], leg["raw"], leg["qty"]) for leg in legs] == [
        ("half", 100.8, 0.5), ("exit", 100.0, 0.5)]


def test_exit_path_stop_wins_when_both_in_one_minute():
    from sonic_research.execution_lab.sim import exit_path
    m = _minutes([(100, 99, 101.0, 95.0)])
    legs, reason = exit_path(m, T, 100.0, 1.0, 4.0, _filters(), T + 240 * MIN)
    assert reason == "stop_loss" and legs == [{"kind": "exit", "ms": T + MIN, "raw": 96.0,
                                               "qty": 1.0}]


def test_exit_path_market_exit_at_the_bar_close():
    from sonic_research.execution_lab.sim import exit_path
    m = _minutes([(100, 100.1, 100.2, 99.9)] * 5)
    legs, reason = exit_path(m, T, 100.0, 1.0, 4.0, _filters(), T + 3 * MIN,
                             market_reason="trade_exit_signal")
    assert reason == "trade_exit_signal" and legs[-1]["raw"] == 100.1
    assert legs[-1]["ms"] == T + 3 * MIN


def test_exit_path_gap_below_stop_fills_at_open():
    from sonic_research.execution_lab.sim import exit_path
    m = _minutes([(94.0, 94.5, 95.0, 93.0)])
    legs, _ = exit_path(m, T, 100.0, 1.0, 4.0, _filters(), T + 240 * MIN)
    assert legs[0]["raw"] == 94.0


def test_pnl_uses_given_entry_costs_and_sonic_exit_costs():
    from sonic_research.execution_lab.sim import pnl
    legs = [{"kind": "exit", "ms": 0, "raw": 110.0, "qty": 1.0}]
    got = pnl(entry_eff=100.0, entry_fee=0.02, legs=legs)
    exit_eff = 110 * 0.9998
    assert got == pytest.approx(exit_eff - 100.0 - 0.02 - exit_eff * 0.0005)


def test_target_in_the_fill_minute_is_not_counted_when_filled_mid_minute():
    from sonic_research.execution_lab.sim import exit_path
    m = _minutes([(100, 100.2, 101.0, 99.9),   # target touched in the minute we were filled
                  (100.2, 100.3, 100.4, 100.1)] + [(100.3, 100.3, 100.4, 100.2)] * 3)
    legs, _ = exit_path(m, T + 30_000, 100.0, 1.0, 4.0, _filters(), T + 4 * MIN,
                        no_target_before=T + 30_000)
    assert [leg["kind"] for leg in legs] == ["exit"]  # no half: it may have come before us
