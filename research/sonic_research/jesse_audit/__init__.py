"""Jesse as an independent auditor of Sonic: small pilot (research only).

Nothing here changes Sonic, the server, the app or any key; the 2024-2026 hold-out stays shut.
"""

HOLDOUT_START_MS = 1_704_067_200_000  # 2024-01-01 00:00 UTC
DISK_BUDGET_BYTES = 1_000_000_000  # Jesse environment + 1-minute data together


def guard_training(ms: int) -> None:
    """Refuse any time inside the 2024-2026 hold-out."""
    if ms >= HOLDOUT_START_MS:
        raise ValueError("holdout_locked")
