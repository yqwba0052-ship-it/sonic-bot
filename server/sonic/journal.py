"""Monthly trading journal: one Markdown file per month, one line per closed trade.

Kept on this computer (server/journal/, not uploaded): it records what the bot did and why,
so a month of paper trading can be reviewed before any real use.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import strategy

REASONS = {
    "stop_loss": "stop loss",
    "breakeven": "rest stopped at entry after the half profit",
    "trade_exit_signal": "trend turned (EMA cross back)",
    "trade_exit_time": "held 40 days",
    "panic_button": "closed by the owner (panic button)",
    "daily_loss_limit": "daily loss limit",
    "budget_used": "amount used up",
    "protection_failed": "stop could not be placed",
}

HEADER = """# Sonic trading journal {month}

Strategy `{name}`: 4-hour bars; buy when the 20-bar EMA crosses above the 100-bar EMA while BTC
is above its 300-bar EMA; stop {k} x ATR(14); half closed at +{half}R, then the rest's stop moves
to the entry; the rest exits on the reverse cross or after {days} days. Goal set by the owner:
80-90% of the month's trades end in profit.

| Closed (UTC) | Coin | Side | Entry | Exit | Result USDT | R | Why it closed | Why it opened |
|---|---|---|---|---|---|---|---|---|
"""


def month_of(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m")


def write_trade(folder: Path, trade: dict[str, Any]) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    month = month_of(trade["closed_ms"])
    path = folder / f"{month}.md"
    if not path.exists():
        path.write_text(
            HEADER.format(
                month=month,
                name=strategy.NAME,
                k=strategy.ATR_K,
                half=strategy.HALF_AT_R,
                days=strategy.MAX_HOLD_BARS // 6,
            ),
            encoding="utf-8",
        )
    plan = json.loads(trade.get("plan") or "{}")
    opened = (
        f"EMA20 {plan.get('fast', 0):.6g} > EMA100 {plan.get('slow', 0):.6g}, "
        f"BTC {'up' if plan.get('btc_up') else 'down'}, stop {plan.get('stop_pct', 0):.1%}"
        if plan
        else ""
    )
    closed = datetime.fromtimestamp(trade["closed_ms"] / 1000, UTC).strftime("%Y-%m-%d %H:%M")
    r = trade.get("r")
    reason = trade.get("exit_reason") or ""
    line = (
        f"| {closed} | {trade['symbol']} | {trade['side']} | {trade['entry_price']:.6g} | "
        f"{(trade.get('exit_price') or 0):.6g} | {(trade.get('pnl') or 0):+.2f} | "
        f"{'' if r is None else f'{r:+.2f}'} | {REASONS.get(reason, reason)} | {opened} |\n"
    )
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line)
    return path
