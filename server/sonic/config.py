"""Settings from environment and server/.env. Only paper and demo modes exist."""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path

MODES = ("paper", "demo")


@dataclass(frozen=True)
class Settings:
    mode: str
    token: str
    db_path: str
    host: str = "0.0.0.0"
    port: int = 8787
    symbols: tuple[str, ...] = ("BTCUSDT", "ETHUSDT")
    interval: str = "15m"
    tick_seconds: float = 15.0
    paper_balance: float = 10000.0
    demo_key: str = ""
    demo_secret: str = ""


def read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return values


def load_settings(root: Path) -> Settings:
    env_path = root / ".env"
    values = {**read_env_file(env_path), **os.environ}
    token = values.get("SONIC_TOKEN", "")
    if not token:
        token = secrets.token_urlsafe(12)
        with env_path.open("a", encoding="utf-8") as handle:
            handle.write(f"SONIC_TOKEN={token}\n")
    mode = values.get("SONIC_MODE", "paper").lower()
    if mode not in MODES:
        raise SystemExit(f"SONIC_MODE must be one of {MODES}; real trading is not available.")
    key = values.get("BINANCE_DEMO_KEY", "")
    secret = values.get("BINANCE_DEMO_SECRET", "")
    if mode == "demo" and not (key and secret):
        raise SystemExit("Demo mode needs BINANCE_DEMO_KEY and BINANCE_DEMO_SECRET in server/.env")
    return Settings(
        mode=mode,
        token=token,
        db_path=str(root / "sonic.db"),
        port=int(values.get("SONIC_PORT", "8787")),
        demo_key=key,
        demo_secret=secret,
    )
