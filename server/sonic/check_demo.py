"""Read-only readiness check for the Binance demo account: python -m sonic.check_demo

Reads BINANCE_DEMO_KEY / BINANCE_DEMO_SECRET from server/.env and asks the DEMO server only
(demo-fapi.binance.com): clock, balance, position mode, open positions. It places no order and
changes no setting.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

from .config import read_env_file
from .exchange import DEMO_URL, BinanceDemo, ExchangeError


async def check(key: str, secret: str) -> list[str]:
    lines: list[str] = []
    async with httpx.AsyncClient(base_url=DEMO_URL, timeout=15) as client:
        demo = BinanceDemo(client, key, secret)
        await demo.sync_time()
        lines.append(f"OK  clock difference with Binance demo: {demo.offset_ms} ms")
        balance = await demo.balance()
        lines.append(f"OK  demo USDT balance: {balance:.2f}")
        mode = await demo.signed("GET", "/fapi/v1/positionSide/dual")
        if mode.get("dualSidePosition"):
            lines.append("FIX hedge mode is ON: in the demo futures settings choose One-way mode")
        else:
            lines.append("OK  one-way position mode")
        positions = await demo.positions()
        lines.append(f"OK  open positions now: {len(positions)}"
                     + ("" if not positions else f" ({', '.join(positions)})"))
    return lines


def main() -> None:
    values = read_env_file(Path(__file__).resolve().parent.parent / ".env")
    key = values.get("BINANCE_DEMO_KEY", "")
    secret = values.get("BINANCE_DEMO_SECRET", "")
    if not (key and secret):
        print("Missing BINANCE_DEMO_KEY / BINANCE_DEMO_SECRET in server/.env")
        raise SystemExit(1)
    try:
        for line in asyncio.run(check(key, secret)):
            print(line)
    except ExchangeError as error:
        print(f"FAIL {error}")
        raise SystemExit(1) from error
    print("Ready. Set SONIC_MODE=demo in server/.env and restart the server to trade on demo.")


if __name__ == "__main__":
    main()
