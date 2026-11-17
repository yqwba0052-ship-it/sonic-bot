"""Freeze the Classical Strategy Lab before its one-time 2024-2026 historical check.

Covers: the lab code, the design, the frozen choices (also checked against the training result's
recorded sha256), the training result, the Binance filters and Sonic's verified hold-out trades;
plus the data itself: every 4h kline month the hold-out reads must already be on disk and not
empty (the loader would otherwise download a missing month or skip an empty one silently).

python -m sonic_research.classical.freeze
writes research/reports/classical_holdout_manifest.json once
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from ..data import DATA_DIR, month_range
from ..jesse_audit.freeze import file_hash

REPO = Path(__file__).resolve().parents[3]
MANIFEST = REPO / "research" / "reports" / "classical_holdout_manifest.json"
LAB = "research/sonic_research/classical/"
FILES = [
    *(LAB + n for n in ("__init__.py", "core.py", "strategies.py", "engine.py", "evaluate.py",
                        "run.py", "freeze.py")),
    "research/sonic_research/data.py",
    "research/sonic_research/jesse_audit/filters.py",
    "server/sonic/risk.py",
    "docs/superpowers/specs/2026-11-17-classical-strategies-design.md",
    "research/reports/classical_choices.json",
    "research/reports/classical_train.json",
    "research/reports/jesse_filters_full.json",
    "research/reports/jesse_holdout.json",
]
COINS = ["SOLUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT", "TRXUSDT", "BNBUSDT", "AVAXUSDT"]


def data_problems(symbols: list[str], months: list[str], klines: Path) -> list[str]:
    out = []
    for s in symbols:
        for m in months:
            p = Path(klines) / f"{s}-4h-{m}.zip"
            if not p.exists():
                out.append(f"{s}-4h-{m} missing")
            elif p.stat().st_size == 0:
                out.append(f"{s}-4h-{m} empty")
    return out


def build() -> dict:
    train = json.loads((REPO / "research/reports/classical_train.json").read_text(encoding="utf-8"))
    choices_sha = hashlib.sha256((REPO / "research/reports/classical_choices.json")
                                 .read_bytes()).hexdigest()
    return {"files": {p: file_hash(REPO / p) for p in FILES},
            "choices_match_training": choices_sha == train["choices_sha256"],
            "data_problems": data_problems(COINS, month_range("2023-06", "2026-10"),
                                           DATA_DIR / "klines")
            + data_problems(["BTCUSDT"], month_range("2023-01", "2026-10"), DATA_DIR / "klines"),
            "window": ["2024-01", "2026-10"]}


def differences(manifest: dict) -> list[str]:
    now = build()
    changed = [p for p in manifest["files"] if manifest["files"][p] != now["files"].get(p)]
    if not now["choices_match_training"]:
        changed.append("choices do not match the training result")
    if now["data_problems"]:
        changed.append(f"data: {now['data_problems'][:5]}")
    return changed


def write(path: Path = MANIFEST) -> dict:
    if path.exists():
        raise SystemExit(f"already frozen: {path}")
    body = build()
    text = json.dumps(body, indent=1, sort_keys=True)
    out = {"sha256": hashlib.sha256(text.encode()).hexdigest(), **body}
    path.write_text(json.dumps(out, indent=1, sort_keys=True), encoding="utf-8")
    return out


def check_frozen(path: Path = MANIFEST) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    body = {k: v for k, v in data.items() if k != "sha256"}
    digest = hashlib.sha256(json.dumps(body, indent=1, sort_keys=True).encode()).hexdigest()
    if digest != data["sha256"]:
        raise SystemExit("classical manifest changed after it was frozen")
    problems = differences(body)
    if problems:
        raise SystemExit(f"frozen classical lab changed, hold-out refused: {problems}")
    return body


if __name__ == "__main__":
    print(json.dumps(write(), indent=1, sort_keys=True)[:600])
