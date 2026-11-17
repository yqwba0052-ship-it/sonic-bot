"""Execution lab for Sonic (research only): can Hummingbot-style execution (maker limit orders,
wait-then-fallback) improve Sonic's fills? Hummingbot itself is not installed (no Windows build,
Docker/WSL would fill C:); its own simulator rules were read from its source instead.

Uses Binance's public aggregated-trades files (price, qty, ms time, buyer-is-maker flag) and the
1-minute candles already on D:. Nothing here touches server/, app/, keys or any account.
"""

from pathlib import Path

LAB = Path("D:/Sonic-Hummingbot")
AGG = LAB / "data" / "aggTrades"
TRAIN_END_MS = 1_704_067_200_000  # 2024-01-01: the 2024-2026 check needs the frozen manifest
