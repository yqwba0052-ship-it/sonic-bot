"""Connection test for the read-only Binance key: python -m sonic.check_account

Takes BINANCE_READ_KEY / BINANCE_READ_SECRET from the environment or server/.env (never from
the command line, so they stay out of the shell history). It only reads: the key's permissions
as Binance reports them, the futures balance, and the last 30 days of futures history. It places
no order, changes no setting and prints no key (only its last 4 characters).
"""

from __future__ import annotations

import asyncio

from . import account


async def check(key: str, secret: str) -> tuple[list[str], bool]:
    reader = account.AccountReader(key, secret)
    lines: list[str] = [f"key ...{key[-4:]}"]
    permissions = await reader.permissions()
    problems = account.read_only_problems(permissions)
    if problems:
        lines.append("FAIL the key can do more than read: " + ", ".join(problems))
        lines.append("     In Binance API Management keep only 'Enable Reading', then try again.")
        return lines, False
    lines.append("OK   read only: no trading, no withdrawal, no transfer")
    if permissions.get("ipRestrict"):
        lines.append("OK   restricted to trusted IPs")
    else:
        ip = account.server_ip() or "the server's IP"
        lines.append(f"WARN not restricted by IP: add {ip} in Binance (Edit restrictions)")
    balance = await reader.balance()
    lines.append(f"OK   futures wallet {balance['wallet']:.2f} USDT, "
                 f"available {balance['available']:.2f} USDT")
    income = await reader.recent_income(30)
    symbols = sorted({row["symbol"] for row in income if row.get("symbol")})
    lines.append(f"OK   last 30 days: {len(income)} history records"
                 + (f" on {', '.join(symbols)}" if symbols else ""))
    return lines, True


def main() -> None:
    saved = account.env_key()
    if saved is None:
        print("Missing BINANCE_READ_KEY / BINANCE_READ_SECRET (environment or server/.env)")
        raise SystemExit(1)
    try:
        lines, ok = asyncio.run(check(saved["key"], saved["secret"]))
    except Exception as error:  # noqa: BLE001 - shown without the key
        print(f"FAIL could not reach Binance with this key ({type(error).__name__}: "
              f"{str(error)[:80]})")
        raise SystemExit(1) from error
    for line in lines:
        print(line)
    if not ok:
        raise SystemExit(1)
    print("Connected. Restart the server; it syncs this account every 6 hours (read only).")


if __name__ == "__main__":
    main()
