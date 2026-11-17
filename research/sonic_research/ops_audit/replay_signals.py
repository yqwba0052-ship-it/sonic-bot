"""Offline check of what Sonic's scan should have seen, bar by bar.

Read-only research tool: it fetches PUBLIC 4h candles from Binance futures (no key, no account),
and runs Sonic's own `server/sonic/strategy.py` functions (imported, never modified) on every
closed bar in a window. It answers "why were there no paper trades?" without touching the server.

    python research/sonic_research/ops_audit/replay_signals.py --since 2026-11-09 [--json out.json]

The symbol list defaults to Sonic's SMALL_ACCOUNT_SYMBOLS (the server's default limits).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "server"))

from sonic import risk, strategy  # noqa: E402

API = "https://fapi.binance.com/fapi/v1/klines"
BAR_MS = strategy.BAR_MS


def fetch(symbol: str, end_ms: int, limit: int = 1500) -> list[strategy.Bar]:
    """Public klines that opened before end_ms, oldest first (one request, up to 1500 bars)."""
    url = f"{API}?symbol={symbol}&interval=4h&endTime={end_ms}&limit={limit}"
    with urllib.request.urlopen(url, timeout=30) as response:  # noqa: S310 - fixed public URL
        rows = json.loads(response.read())
    return [strategy.Bar(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]))
            for r in rows]


def closed_before(bars: list[strategy.Bar], at_ms: int) -> list[strategy.Bar]:
    """Bars fully closed at at_ms, the last HISTORY_BARS of them (what the server sees)."""
    done = [bar for bar in bars if bar.t + BAR_MS <= at_ms]
    return done[-strategy.HISTORY_BARS:]


def replay(series: dict[str, list[strategy.Bar]], since_ms: int, until_ms: int,
           symbols: list[str]) -> list[dict]:
    """One row per 4h bar close in [since, until): BTC regime, per-coin state, signals."""
    rows = []
    first = (since_ms // BAR_MS + 1) * BAR_MS
    for close_ms in range(first, until_ms + 1, BAR_MS):
        btc = closed_before(series["BTCUSDT"], close_ms)
        closes = [bar.c for bar in btc]
        btc_ema = float(strategy.ema(closes, strategy.REGIME_PERIOD)[-1])
        btc_up = strategy.btc_uptrend(btc)
        coins = {}
        for symbol in symbols:
            bars = closed_before(series[symbol], close_ms)
            side = strategy.entry_signal(bars, btc_up)
            now, before = strategy._above(bars)  # noqa: SLF001 - same rule the engine uses
            c = [bar.c for bar in bars]
            coins[symbol] = {
                "signal": side,
                "cross_up": bool(now and not before),
                "fast_above_slow": bool(now),
                "coin_up": bool(c[-1] > strategy.ema(c, strategy.COIN_TREND_PERIOD)[-1]),
            }
        rows.append({
            "bar_close_utc": dt.datetime.fromtimestamp(close_ms / 1000, dt.UTC).isoformat(),
            "btc_close": closes[-1], "btc_ema300": round(btc_ema, 2),
            "btc_gap_pct": round((closes[-1] / btc_ema - 1) * 100, 2), "btc_up": bool(btc_up),
            "coins": coins,
        })
    return rows


def summary(rows: list[dict], symbols: list[str]) -> dict:
    longs = [(r["bar_close_utc"], s) for r in rows for s in symbols
             if r["coins"][s]["signal"] == "BUY"]
    crosses = [(r["bar_close_utc"], s, r["coins"][s]["coin_up"]) for r in rows for s in symbols
               if r["coins"][s]["cross_up"]]
    return {
        "bars": len(rows),
        "btc_up_bars": sum(r["btc_up"] for r in rows),
        "btc_gap_pct_range": [min(r["btc_gap_pct"] for r in rows),
                              max(r["btc_gap_pct"] for r in rows)] if rows else None,
        "long_signals": longs,
        "ema_crosses_up_blocked": [c for c in crosses if (c[0], c[1]) not in set(longs)],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--since", required=True, help="UTC date, e.g. 2026-11-09")
    parser.add_argument("--until", help="UTC date (default: now)")
    parser.add_argument("--symbols", nargs="*", default=list(risk.SMALL_ACCOUNT_SYMBOLS))
    parser.add_argument("--json", help="write rows + summary here")
    args = parser.parse_args()
    since = int(dt.datetime.fromisoformat(args.since).replace(tzinfo=dt.UTC).timestamp() * 1000)
    until = (int(dt.datetime.fromisoformat(args.until).replace(tzinfo=dt.UTC).timestamp() * 1000)
             if args.until else int(dt.datetime.now(dt.UTC).timestamp() * 1000))
    series = {s: fetch(s, until) for s in ["BTCUSDT", *args.symbols]}
    rows = replay(series, since, until, args.symbols)
    result = {"since": args.since, "until_ms": until, "symbols": args.symbols,
              "summary": summary(rows, args.symbols), "rows": rows}
    print(json.dumps(result["summary"], indent=1))
    if rows:
        last = rows[-1]
        print("last bar:", last["bar_close_utc"], "BTC", last["btc_close"], "EMA300",
              last["btc_ema300"], f"gap {last['btc_gap_pct']}%")
    if args.json:
        Path(args.json).write_text(json.dumps(result, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
