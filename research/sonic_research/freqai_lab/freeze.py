"""Freeze the FreqAI lab before its one-time 2024-2026 historical check.

The manifest holds the sha256 of every file that decides a hold-out result (FreqAI strategy and
runner, evaluation, the Sonic bridge, Sonic's own strategy/risk/exchange/engine, the filters,
the design with its fixed rules) plus the model and decision settings. The hold-out experiment
refuses to run if any of them changed. Hashes ignore line endings.

python -m sonic_research.freqai_lab.freeze
writes research/reports/freqai_holdout_manifest.json once
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from ..jesse_audit.freeze import file_hash
from .evaluate import THRESHOLD
from .predict import MODEL_PARAMS, build_config

REPO = Path(__file__).resolve().parents[3]
MANIFEST = REPO / "research" / "reports" / "freqai_holdout_manifest.json"
LAB_DIR = "research/sonic_research/freqai_lab/"
AUDIT_DIR = "research/sonic_research/jesse_audit/"
FILES = [
    *(LAB_DIR + n for n in ("__init__.py", "convert.py", "predict.py", "evaluate.py",
                            "experiment.py", "audit.py", "freeze.py",
                            "strategies/SonicFreqAI.py")),
    *(AUDIT_DIR + n for n in ("bridge.py", "planner.py", "costs.py", "data_1m.py", "filters.py",
                              "holdout.py", "offline.py", "synth.py")),
    "research/sonic_research/data.py",
    "research/sonic_research/engine_replay.py",
    "server/sonic/strategy.py", "server/sonic/risk.py", "server/sonic/exchange.py",
    "server/sonic/engine.py",
    "research/reports/jesse_filters_full.json",
    "docs/superpowers/specs/2026-11-17-freqai-design.md",
]


def build() -> dict:
    cfg = build_config("frozen", ["SOLUSDT"])["freqai"]
    return {"files": {p: file_hash(REPO / p) for p in FILES},
            "model_params": MODEL_PARAMS, "threshold": THRESHOLD,
            "train_period_days": cfg["train_period_days"],
            "backtest_period_days": cfg["backtest_period_days"],
            "label_period_candles": cfg["feature_parameters"]["label_period_candles"],
            "window": ["2024-01", "2026-10"], "warmup_first": "2023-07"}


def differences(manifest: dict) -> list[str]:
    now = build()
    changed = [p for p in manifest["files"] if manifest["files"][p] != now["files"].get(p)]
    return changed + [k for k in now if k != "files" and manifest.get(k) != now[k]]


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
    if hashlib.sha256(json.dumps(body, indent=1, sort_keys=True).encode()).hexdigest() \
            != data["sha256"]:
        raise SystemExit("freqai manifest changed after it was frozen")
    changed = differences(body)
    if changed:
        raise SystemExit(f"frozen FreqAI method changed, hold-out refused: {changed}")
    return body


if __name__ == "__main__":
    print(json.dumps(write(), indent=1, sort_keys=True)[:400])
