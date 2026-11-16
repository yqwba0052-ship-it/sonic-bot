"""Freeze the audit method before the 2024-2026 hold-out is opened (owner, 2026-11-17).

The manifest records a sha256 of every file that decides a result (comparison code, costs, the
Jesse bridge, Sonic's own strategy/risk/exchange/engine, the frozen Binance filters) and the
settings they hold. The hold-out run refuses to start if any of them differs. Line endings are
normalised so a Windows checkout does not change a hash.

python -m sonic_research.jesse_audit.freeze      writes reports/jesse_holdout_manifest.json once
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "server"))

from sonic import exchange as sonic_exchange  # noqa: E402
from sonic import strategy as sonic  # noqa: E402
from sonic.risk import Limits  # noqa: E402

from . import compare, costs  # noqa: E402

MANIFEST = REPO / "research" / "reports" / "jesse_holdout_manifest.json"
AUDIT = "research/sonic_research/jesse_audit/"
METHOD_FILES = [
    *(AUDIT + name for name in ("__init__.py", "bridge.py", "compare.py", "costs.py",
                                "data_1m.py", "explain.py", "filters.py", "full.py",
                                "offline.py", "planner.py", "synth.py")),
    "research/sonic_research/data.py",
    "research/sonic_research/engine_replay.py",
    "server/sonic/strategy.py",
    "server/sonic/risk.py",
    "server/sonic/exchange.py",
    "server/sonic/engine.py",
    "research/reports/jesse_filters_full.json",
]
COINS = ["SOLUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT", "TRXUSDT", "BNBUSDT", "AVAXUSDT"]
WARMUP_FIRST, START, END = "2023-07", "2024-01", "2026-10"
SONIC_CONSTANTS = ("NAME", "INTERVAL", "FAST", "SLOW", "ATR_PERIOD", "ATR_K", "REGIME_PERIOD",
                   "COIN_TREND_PERIOD", "MAX_HOLD_BARS", "HISTORY_BARS", "HALF_AT_R",
                   "DRIFT_WINDOW", "DRIFT_FLOOR")


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def build_manifest() -> dict:
    assert (costs.SLIPPAGE, costs.FEE_RATE) == (sonic_exchange.SLIPPAGE, sonic_exchange.FEE_RATE)
    return {
        "purpose": "Jesse audit of execution accuracy on 2024-01..2026-10 (run once)",
        "files": {p: file_hash(REPO / p) for p in METHOD_FILES},
        "coins": COINS, "btc_filter": "BTCUSDT",
        "warmup_first": WARMUP_FIRST, "start": START, "end": END,
        "costs": {"slippage": costs.SLIPPAGE, "fee_rate": costs.FEE_RATE},
        "comparison": {"price_tol": compare.PRICE_TOL, "categories": list(compare.CATEGORIES)},
        "sonic": {name: getattr(sonic, name) for name in SONIC_CONSTANTS},
        "limits": Limits(symbols=COINS).model_dump(),
    }


def differences(manifest: dict) -> list[str]:
    """Method files whose hash differs from the manifest (empty = unchanged)."""
    now = build_manifest()
    changed = [p for p in manifest["files"] if manifest["files"][p] != now["files"].get(p)]
    for key in ("coins", "warmup_first", "start", "end", "costs", "comparison", "sonic",
                "limits"):
        if manifest.get(key) != now[key]:
            changed.append(key)
    return changed


def write_manifest(path: Path = MANIFEST) -> dict:
    if path.exists():
        raise SystemExit(f"already frozen: {path}")
    body = build_manifest()
    text = json.dumps(body, indent=1, sort_keys=True)
    out = {"sha256": hashlib.sha256(text.encode()).hexdigest(), **body}
    path.write_text(json.dumps(out, indent=1, sort_keys=True), encoding="utf-8")
    return out


if __name__ == "__main__":
    print(json.dumps(write_manifest(), indent=1, sort_keys=True))
