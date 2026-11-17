"""Freeze the execution lab before its one-time 2024-2026 historical check.

sha256 (line endings ignored) of every file that decides a hold-out result, plus the settings
(latency, wait, fees, theta). The hold-out run refuses to start if anything differs.

python -m sonic_research.execution_lab.freeze
writes research/reports/execution_holdout_manifest.json once
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from ..jesse_audit.freeze import file_hash

REPO = Path(__file__).resolve().parents[3]
MANIFEST = REPO / "research" / "reports" / "execution_holdout_manifest.json"
LAB_DIR = "research/sonic_research/execution_lab/"
FILES = [
    *(LAB_DIR + n for n in ("__init__.py", "agg_data.py", "fills.py", "sim.py", "run.py",
                            "verdict.py", "freeze.py")),
    "research/sonic_research/jesse_audit/explain.py",
    "research/sonic_research/jesse_audit/costs.py",
    "research/sonic_research/jesse_audit/data_1m.py",
    "research/sonic_research/jesse_audit/holdout.py",
    "research/sonic_research/data.py",
    "server/sonic/strategy.py", "server/sonic/risk.py",
    "research/reports/jesse_filters_full.json",
    "research/reports/execution_theta.json",
    "docs/superpowers/specs/2026-11-17-hummingbot-execution-design.md",
]


def build() -> dict:
    from . import run
    return {"files": {p: file_hash(REPO / p) for p in FILES},
            "settings": {"latency_ms": run.LATENCY_MS, "wait_min": run.WAIT_MIN,
                         "taker": run.TAKER, "maker": run.MAKER, "slippage": run.SLIP,
                         "theta_atr_pct": run.load_theta(run.THETA),
                         "window": ["2024-01", "2026-10"]}}


def differences(manifest: dict) -> list[str]:
    now = build()
    changed = [p for p in manifest["files"] if manifest["files"][p] != now["files"].get(p)]
    return changed + (["settings"] if manifest.get("settings") != now["settings"] else [])


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
        raise SystemExit("execution manifest changed after it was frozen")
    changed = differences(body)
    if changed:
        raise SystemExit(f"frozen execution method changed, hold-out refused: {changed}")
    return body


if __name__ == "__main__":
    print(json.dumps(write(), indent=1, sort_keys=True)[:300])
