"""Offline tests for the operational-audit replay tool (no network)."""

from __future__ import annotations

from sonic import strategy

from sonic_research.ops_audit import replay_signals as rs

H = strategy.BAR_MS


def bars(closes: list[float], start: int = 0) -> list[strategy.Bar]:
    return [strategy.Bar(start + i * H, c, c * 1.01, c * 0.99, c) for i, c in enumerate(closes)]


def test_only_bars_closed_by_the_scan_time_are_seen() -> None:
    series = bars([1.0] * 10)
    seen = rs.closed_before(series, 5 * H)  # bars opening at 0..4H closed by 5H
    assert [b.t for b in seen] == [i * H for i in range(5)]
    assert rs.closed_before(series, 5 * H - 1)[-1].t == 3 * H


def test_falling_btc_blocks_every_long_and_matches_the_engine_rule() -> None:
    n = 1000
    falling = [100.0 - i * 0.05 for i in range(n)]
    series = {"BTCUSDT": bars(falling), "SOLUSDT": bars(falling)}
    rows = rs.replay(series, (n - 5) * H, n * H, ["SOLUSDT"])
    assert rows and not any(r["btc_up"] for r in rows)
    assert all(r["coins"]["SOLUSDT"]["signal"] is None for r in rows)
    last = rs.closed_before(series["BTCUSDT"], n * H)
    assert rows[-1]["btc_up"] == strategy.btc_uptrend(last)


def test_summary_lists_crosses_that_did_not_become_signals() -> None:
    rows = [{"bar_close_utc": "t1", "btc_up": False, "btc_gap_pct": -5.0,
             "coins": {"X": {"signal": None, "cross_up": True, "coin_up": True}}},
            {"bar_close_utc": "t2", "btc_up": True, "btc_gap_pct": 1.0,
             "coins": {"X": {"signal": "BUY", "cross_up": True, "coin_up": True}}}]
    s = rs.summary(rows, ["X"])
    assert s["long_signals"] == [("t2", "X")]
    assert s["ema_crosses_up_blocked"] == [("t1", "X", True)]
    assert s["btc_up_bars"] == 1 and s["btc_gap_pct_range"] == [-5.0, 1.0]
