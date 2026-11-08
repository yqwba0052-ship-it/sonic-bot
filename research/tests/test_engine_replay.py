"""Two months of paper trading through the real engine on cached history (skipped without it)."""

from __future__ import annotations

import asyncio

import pytest

from sonic_research.data import DATA_DIR
from sonic_research.engine_replay import Limits, replay

pytestmark = pytest.mark.skipif(
    not any((DATA_DIR / "klines").glob("*-1h-*.zip")) if (DATA_DIR / "klines").exists() else True,
    reason="history not downloaded",
)


def test_engine_runs_months_without_errors_and_respects_limits() -> None:
    report = asyncio.run(replay("2025-09", "2025-10", Limits()))
    assert report["errors"] == []
    assert report["open_at_end"] <= Limits().max_open
    assert report["end_amount"] > 0
    assert report["max_drop"] < 0.5
