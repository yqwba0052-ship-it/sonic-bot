"""Offline re-evaluation of trader learning (no server, no keys, nothing activated).

Runs leader.Lab.study on a copy of a trader's closed trades, in an in-memory database, with
PUBLIC Binance 4h candles (cached in a file so two code versions see identical data):

    python -I learning_eval.py --server-dir <dir with sonic/> --trades data.json
        --trader <id> --now-ms <fixed clock> --cache klines.json --json out.json
        [--shares 0.5 0.6 0.7 0.8]

`--server-dir` picks which version of the learning code is run (deployed vs leak-fixed).
Each share is a train/test split point (TRAIN_SHARE); the test part is always later in time.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
import urllib.request
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--server-dir", required=True)
parser.add_argument("--trades", required=True)
parser.add_argument("--trader", required=True)
parser.add_argument("--now-ms", type=int, required=True)
parser.add_argument("--cache", required=True)
parser.add_argument("--json", required=True)
parser.add_argument("--shares", type=float, nargs="*", default=[0.7])
args = parser.parse_args()
sys.path.insert(0, args.server_dir)

from sonic import leader  # noqa: E402
from sonic.strategy import Bar  # noqa: E402

cache_path = Path(args.cache)
cache: dict[str, list] = json.loads(cache_path.read_text()) if cache_path.exists() else {}


async def bars_until(symbol: str, interval: str, end_ms: int, limit: int) -> list[Bar]:
    key = f"{symbol}|{interval}|{end_ms}|{limit}"
    if key not in cache:
        url = (f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}&interval={interval}"
               f"&endTime={end_ms}&limit={min(limit, 1500)}")
        with urllib.request.urlopen(url, timeout=30) as response:  # noqa: S310 - public URL
            cache[key] = [[int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4])]
                          for r in json.loads(response.read())]
    return [Bar(*row) for row in cache[key]]


def pick(result: dict | None) -> dict | None:
    keys = ("trades", "wins", "win_rate", "net_pnl", "net_pct", "profit_factor", "max_drawdown",
            "months", "months_positive", "worst_trade", "days")
    return {k: (result or {}).get(k) for k in keys} if result else None


def qualified(test: dict | None) -> tuple[bool, list[str]]:
    """The lab's own bar for a strategy (see leader exclusions): a learned model must pass all."""
    if not test:
        return False, ["no_test"]
    why = []
    if (test.get("days") or 0) < 60:
        why.append("test < 60 days")
    if (test.get("trades") or 0) < 10:
        why.append("< 10 trades")
    if (test.get("net_pnl") or 0) <= 0:
        why.append("net <= 0")
    if (test.get("profit_factor") or 0) < 1.2:
        why.append("PF < 1.2")
    if (test.get("max_drawdown") or 0) > 0.20:
        why.append("DD > 20%")
    if (test.get("months_positive") or 0) < 0.5:
        why.append("positive months < 50%")
    if (test.get("worst_trade") or 0) < -0.03:
        why.append("worst trade < -3%")
    return not why, why


async def main() -> None:
    data = json.loads(Path(args.trades).read_text(encoding="utf-8"))
    trades = [t for t in data["trader_trades"] if t["trader_id"] == args.trader]
    out = {"server_dir": args.server_dir, "trades": len(trades), "runs": []}
    for share in args.shares:
        leader.TRAIN_SHARE = share
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        lab = leader.Lab(db, lambda: args.now_ms)
        lab.add(args.trader, "trader", kind="lead")
        lab.import_trades(args.trader, [{k: v for k, v in t.items() if k != "trader_id"}
                                        for t in trades], "csv")
        report = await lab.study(args.trader, bars_until)
        learn = report.get("learn", {})
        test = (report.get("test") or {})
        learned = pick(test.get("learned"))
        test_days = test.get("days")
        if learned is not None:
            learned["days"] = test_days
        ok, why = qualified(learned)
        out["runs"].append({
            "train_share": share, "learn": learn,
            "model_leverage": (report.get("model") or {}).get("leverage"),
            "train": pick((report.get("train") or {}).get("learned")),
            "test_learned": learned,
            "test_sonic": pick(test.get("sonic")),
            "test_trader_actual": test.get("trader"),
            "test_days": test_days,
            "qualified": ok, "why_not": why,
        })
        db.close()
    Path(args.json).write_text(json.dumps(out, indent=1), encoding="utf-8")
    cache_path.write_text(json.dumps(cache))
    for run in out["runs"]:
        t = run["test_learned"] or {}
        print(f"share {run['train_share']}: train_trades {run['learn'].get('train_trades')} "
              f"purged {run['learn'].get('purged')} lev {run['model_leverage']} | test "
              f"{run['test_days']}d trades {t.get('trades')} PF {t.get('profit_factor')} "
              f"net% {t.get('net_pct')} DD {t.get('max_drawdown')} -> "
              f"{'QUALIFIED' if run['qualified'] else 'not: ' + ', '.join(run['why_not'])}")


asyncio.run(main())
