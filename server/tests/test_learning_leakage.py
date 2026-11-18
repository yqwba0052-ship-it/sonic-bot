"""No future data in learning: the model learned from the training part must not change when
anything after the train/test split changes (later prices, later trades' leverage or results).

Two leaks found in the 2026-11-18 review:
1. the model's leverage was the median over ALL the trader's trades, test period included;
2. training trades still open at the split used prices from the test period for their
   outcome (adverse move, result, holding time).
"""

from __future__ import annotations

import random

from sonic.strategy import Bar
from tests.test_lab import MARKET, four_coins, lab


def with_leverage(trades: list[dict], before_ms: int, early: list[float], late: float) -> list:
    out = []
    for i, t in enumerate(sorted(trades, key=lambda t: t["opened_ms"])):
        lev = early[i % len(early)] if t["opened_ms"] < before_ms else late
        out.append({**t, "leverage": lev})
    return out


def changed_after(split_ms: int, seed: int) -> dict[str, list[Bar]]:
    """The same market up to the split, different prices from the split on."""
    rng = random.Random(seed)
    out = {}
    for symbol, bars in MARKET.items():
        new = []
        for b in bars:
            if b.t < split_ms:
                new.append(b)
            else:
                k = 1 + rng.uniform(-0.08, 0.08)
                new.append(Bar(b.t, b.o * k, b.h * k * 1.01, b.low * k * 0.99, b.c * k))
        out[symbol] = new
    return out


async def study_on(market: dict[str, list[Bar]], trades: list[dict]) -> dict:
    async def bars_until(symbol: str, interval: str, end_ms: int, limit: int) -> list[Bar]:
        return [b for b in market[symbol] if b.t < end_ms][-limit:]

    lab_ = lab()
    lab_.add("trend1", "Trend")
    lab_.import_trades("trend1", trades, "csv")
    report = await lab_.study("trend1", bars_until)
    assert report["learn"]["status"] == "ok", report.get("learn")
    return report


async def test_the_learned_model_ignores_everything_after_the_split() -> None:
    base = four_coins()
    first = await study_on(MARKET, with_leverage(base, 0, [1.0], 1.0))
    split = first["learn"]["split_ms"]
    # A: the real record. B: same up to the split; after it other prices, other leverage,
    # other results for the test-period trades.
    trades_a = with_leverage(base, split, [1.0, 5.0], 5.0)
    trades_b = []
    for t in with_leverage(base, split, [1.0, 5.0], 1.0):
        if t["opened_ms"] >= split:
            t = {**t, "exit_price": t["exit_price"] * 0.9, "pnl": -abs(t["pnl"] or 1.0)}
        trades_b.append(t)
    a = await study_on(MARKET, trades_a)
    b = await study_on(changed_after(split, 11), trades_b)
    assert a["learn"]["split_ms"] == b["learn"]["split_ms"] == split
    assert a["model"] == b["model"]
    assert a["train"] == b["train"]


async def test_training_trades_still_open_at_the_split_are_left_out_and_counted() -> None:
    report = await study_on(MARKET, four_coins())
    learn = report["learn"]
    assert learn["purged"] >= 0
    parts = learn["train_trades"] + learn["purged"] + learn["test_trades"]
    assert parts == report["data"]["usable"]


def test_too_few_trades_left_after_the_split_cut_learns_nothing() -> None:
    from sonic import leader

    rows = [{"opened_ms": i * 10, "closed_ms": i * 10 + 1000} for i in range(30)]  # all overlap
    trades = [{"opened_ms": r["opened_ms"], "closed_ms": r["closed_ms"]} for r in rows]
    cut, split, train_rows, train_trades = leader.train_part(rows, trades)
    assert cut == 21 and split == 210
    assert all(r["closed_ms"] <= split for r in train_rows) and len(train_rows) < 10
    assert all(t["closed_ms"] <= split for t in train_trades)
