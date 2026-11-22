"""Report of the intrabar paper trader (read-only on its own database).

    python -m intrabar.report --db intrabar.db [--top 10]
"""

from __future__ import annotations

import argparse
import sqlite3
from datetime import UTC, datetime

LEVERAGE_SHOWN = 24


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--top", type=int, default=10)
    args = parser.parse_args()
    db = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    rows = db.execute(
        "SELECT variant, COUNT(*), AVG(net > 0), AVG(net), SUM(net), MIN(closed), MAX(closed) "
        "FROM trades GROUP BY variant ORDER BY SUM(net) DESC").fetchall()
    total = db.execute("SELECT COUNT(*), AVG(net > 0), AVG(net) FROM trades").fetchone()
    started = db.execute("SELECT MIN(at) FROM events WHERE code='started'").fetchone()[0]
    print(f"since {datetime.fromtimestamp(started, UTC):%Y-%m-%d %H:%M} UTC" if started else "no run yet")
    if not total[0]:
        print("no paper trades yet")
        return
    print(f"all variants: {total[0]} trades, win {100 * total[1]:.1f}%, mean {100 * total[2]:+.4f}% per trade")
    print(f"{'variant':45} {'trades':>6} {'win%':>6} {'mean%':>9} {'sum%':>8} {'x24 sum% of margin':>18}")
    for v, n, win, mean, total_net, _, _ in rows[: args.top]:
        print(f"{v:45} {n:6} {100 * win:6.1f} {100 * mean:+9.4f} {100 * total_net:+8.3f} "
              f"{100 * total_net * LEVERAGE_SHOWN:+18.2f}")
    gaps = db.execute("SELECT COUNT(*) FROM events WHERE code='disconnected'").fetchone()[0]
    print(f"stream gaps: {gaps}")


if __name__ == "__main__":
    main()
