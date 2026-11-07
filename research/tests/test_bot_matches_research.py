"""The live bot must make the same decisions as the strategy that was tested.

Uses the cached history in research/data (skipped when it is not downloaded).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from sonic_research.data import DATA_DIR, load_bars, month_range
from sonic_research.strategies import Params, btc_trend, build

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "server"))
from sonic import strategy as bot  # noqa: E402

BEST = Params("trend", 20, 100, 3.0, None, 120, True)

pytestmark = pytest.mark.skipif(
    not (DATA_DIR / "klines").exists(), reason="history not downloaded"
)


@pytest.mark.parametrize("symbol", ["SOLUSDT", "XRPUSDT"])
def test_entries_and_stops_match_the_backtest(symbol: str) -> None:
    months = month_range("2021-01", "2026-10")
    btc = load_bars("BTCUSDT", months)
    coin = load_bars(symbol, months)
    regime = btc_trend(btc, coin)
    signals = build(coin, BEST, regime)
    as_bot = [bot.Bar(int(t), o, h, low, c) for t, o, h, low, c in
              zip(coin.t, coin.o, coin.h, coin.low, coin.c, strict=True)]
    btc_index = {int(t): i for i, t in enumerate(btc.t)}
    btc_bars = [bot.Bar(int(t), o, h, low, c) for t, o, h, low, c in
                zip(btc.t, btc.o, btc.h, btc.low, btc.c, strict=True)]
    checked = entries = 0
    for i in range(len(coin) - 400, len(coin)):
        j = btc_index.get(int(coin.t[i]))
        if j is None:
            continue
        btc_up = bot.btc_uptrend(btc_bars[: j + 1])
        assert btc_up == bool(regime[i])
        decision = bot.entry_signal(as_bot[: i + 1], btc_up)
        expected = {1: "BUY", -1: "SELL", 0: None}[int(signals.entry[i])]
        assert decision == expected, (symbol, i)
        if expected:
            entries += 1
            assert bot.stop_distance(as_bot[: i + 1]) == pytest.approx(signals.stop_dist[i])
        checked += 1
    assert checked > 350
    assert entries > 0

