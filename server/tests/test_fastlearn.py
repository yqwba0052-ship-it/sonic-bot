"""Weekly variant choice of the fast desk."""

from __future__ import annotations

from sonic.fastlearn import DAY_MS, WEEK_MS, choose, is_review_due, week_index

MON = 1_795_392_000_000  # Monday 2026-11-23 00:00 UTC
NOW = MON + 3600_000


def rows(variant: str, n: int, net: float, start: int = NOW - 5 * DAY_MS) -> list:
    return [(variant, start + i * 60_000, net) for i in range(n)]


def test_monday_constant_is_a_monday() -> None:
    assert week_index(MON) == week_index(MON + WEEK_MS - 1)
    assert week_index(MON - 1) == week_index(MON) - 1


def test_picks_best_mean_with_enough_trades() -> None:
    c = choose(rows("A", 25, 0.001) + rows("B", 25, 0.002), NOW, "A")
    assert c.variant == "B" and c.changed and not c.no_profitable


def test_too_few_trades_ignored() -> None:
    c = choose(rows("A", 25, 0.001) + rows("B", 19, 0.01), NOW, "X")
    assert c.variant == "A"


def test_none_qualifies_keeps_current() -> None:
    c = choose(rows("A", 5, 0.001), NOW, "CUR")
    assert c.variant == "CUR" and not c.changed and "min_trades" in c.reason


def test_old_trades_outside_window_ignored() -> None:
    c = choose(rows("A", 25, 0.01, start=NOW - 20 * DAY_MS) + rows("B", 25, 0.001), NOW, "X")
    assert c.variant == "B"


def test_all_negative_picks_least_bad_and_warns() -> None:
    c = choose(rows("A", 25, -0.002) + rows("B", 25, -0.001), NOW, "A")
    assert c.variant == "B" and c.no_profitable


def test_three_negative_weeks_excluded() -> None:
    history = []
    for w in (1, 2, 3):  # the three finished weeks before this one
        history += rows("B", 10, -0.001, start=MON - w * WEEK_MS + DAY_MS)
    recent = rows("B", 25, 0.01, start=MON) + rows("A", 25, 0.0005, start=MON)
    c = choose(history + recent, NOW, "X")
    assert c.variant == "A"


def test_review_due_on_monday_boundary() -> None:
    assert is_review_due(MON - 60_000, MON + 60_000)
    assert not is_review_due(MON + 60_000, MON + 23 * 3600_000)
