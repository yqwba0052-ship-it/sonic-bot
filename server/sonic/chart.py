"""Data for the app's candle chart: 4-hour bars, the bot's two EMAs and its trades.

Two sources:
- live: the bot's own trades on this server (paper or demo);
- replay: trades from the last engine replay on past prices (server/replay/trades.json,
  written by research/sonic_research/engine_replay.py).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import strategy

REPLAY_FILE = Path(__file__).resolve().parent.parent / "replay" / "trades.json"
WARMUP = strategy.SLOW * 3  # bars before the window so the EMAs have settled
PAD = 20  # bars shown after a closed trade
LEAD = 90  # simulator: candles before the period, so playback starts on a full chart


def replay_trades() -> dict[str, Any]:
    if not REPLAY_FILE.is_file():
        return {"period": None, "trades": []}
    return json.loads(REPLAY_FILE.read_text(encoding="utf-8"))


def trade_view(trade: dict[str, Any]) -> dict[str, Any]:
    keys = ("id", "symbol", "side", "qty", "entry_price", "stop_price", "target_price",
            "opened_ms", "closed_ms", "exit_price", "pnl", "r", "scaled", "exit_reason", "plan")
    return {key: trade.get(key) for key in keys}


def build(bars: list[strategy.Bar], shown: int, trades: list[dict[str, Any]]) -> dict[str, Any]:
    """Cut the last `shown` bars, with EMAs computed on everything before them too."""
    closes = [bar.c for bar in bars]
    fast = strategy.ema(closes, strategy.FAST) if closes else []
    slow = strategy.ema(closes, strategy.SLOW) if closes else []
    start = max(0, len(bars) - shown)
    window = bars[start:]
    first = window[0].t if window else 0
    last = window[-1].t + strategy.BAR_MS if window else 0
    visible = [
        trade_view(t) for t in trades
        if t.get("closed_ms") is None  # an open trade is always shown
        or (t["opened_ms"] < last and t["closed_ms"] >= first)
    ]
    return {
        "bars": [[b.t, b.o, b.h, b.low, b.c] for b in window],
        "fast": fast[start:],
        "slow": slow[start:],
        "trades": visible,
        "settings": {
            "fast": strategy.FAST,
            "slow": strategy.SLOW,
            "atr_k": strategy.ATR_K,
            "half_at_r": strategy.HALF_AT_R,
            "interval": strategy.INTERVAL,
        },
    }


def half_time(trade: dict[str, Any], bars: list[strategy.Bar]) -> int | None:
    """When the half-profit target was first touched (the candle it happened in)."""
    target = trade.get("target_price")
    if not trade.get("scaled") or not target:
        return None
    start = trade["opened_ms"] - trade["opened_ms"] % strategy.BAR_MS
    end = trade.get("closed_ms") or float("inf")
    for bar in bars:
        if bar.t < start or bar.t > end:
            continue
        if (bar.h >= target) if trade["side"] == "BUY" else (bar.low <= target):
            return max(bar.t, trade["opened_ms"])
    return None


def simulation(
    bars: list[strategy.Bar], shown: int, trades: list[dict[str, Any]],
    forming: strategy.Bar | None,
) -> dict[str, Any]:
    """Everything the app's candle simulator needs to replay a period candle by candle:
    closed candles with the bot's EMAs (closed candles only, as the bot sees them), the
    candle still forming (live only), and each trade with the time of every event."""
    data = build(bars, shown + LEAD, trades)
    every = bars + ([forming] if forming else [])
    for trade in data["trades"]:
        trade["half_ms"] = half_time(trade, every)
    data["forming"] = (
        [forming.t, forming.o, forming.h, forming.low, forming.c] if forming else None
    )
    return data
