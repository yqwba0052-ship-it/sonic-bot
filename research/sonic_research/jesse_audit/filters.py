"""Binance order filters frozen in a file, so every run of a study uses the same ones."""

from __future__ import annotations

import json
import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "server"))

from sonic.risk import Filters  # noqa: E402


def load_filters(path: Path) -> dict[str, Filters]:
    return {s: Filters(**f) for s, f in json.loads(path.read_text(encoding="utf-8")).items()}


def save_filters(path: Path, filters: dict[str, Filters]) -> None:
    path.write_text(json.dumps({s: asdict(f) for s, f in sorted(filters.items())}, indent=1),
                    encoding="utf-8")
