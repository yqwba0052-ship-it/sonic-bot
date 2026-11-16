"""Guards for the one-time 2024-2026 hold-out run (owner, 2026-11-17).

- check_ready: the manifest must be intact (its own sha256) and every frozen file/setting must
  match it; a lock file means the hold-out has already produced results, so it never runs again.
- window_guard: replaces the training-only guard for this run alone; it still refuses anything
  after the frozen end month.
- progress: an append-only log of what ran and what came out, so an interrupted run can be
  resumed safely and nothing that was shown is hidden.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from . import freeze


def _month_end_ms(month: str) -> int:
    year, m = map(int, month.split("-"))
    year, m = (year + 1, 1) if m == 12 else (year, m + 1)
    return int(datetime(year, m, 1, tzinfo=UTC).timestamp() * 1000)


def load_manifest(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    body = {k: v for k, v in data.items() if k != "sha256"}
    text = json.dumps(body, indent=1, sort_keys=True)
    if hashlib.sha256(text.encode()).hexdigest() != data.get("sha256"):
        raise SystemExit(f"manifest changed after it was frozen: {path}")
    return body


def check_ready(manifest_path: Path, lock_path: Path) -> dict:
    if lock_path.exists():
        raise SystemExit(f"hold-out already run (lock {lock_path}); results stay as they are")
    manifest = load_manifest(manifest_path)
    changed = freeze.differences(manifest)
    if changed:
        raise SystemExit(f"frozen method changed, hold-out refused: {changed}")
    return manifest


def window_guard(end_month: str) -> Callable[[int], None]:
    limit = _month_end_ms(end_month)

    def guard(ms: int) -> None:
        if ms >= limit:
            raise ValueError("beyond_frozen_window")

    return guard


def progress(path: Path, step: str, **details) -> None:
    steps = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    steps.append({"at": datetime.now(UTC).isoformat(timespec="seconds"), "step": step,
                  **details})
    path.write_text(json.dumps(steps, indent=1, default=float), encoding="utf-8")


def write_lock(path: Path, details: dict) -> None:
    path.write_text(json.dumps({"at": datetime.now(UTC).isoformat(timespec="seconds"),
                                **details}, indent=1), encoding="utf-8")
