"""Fix the pilot's two coins and 6-month window from our own replay, before Jesse runs.

Rule (design 7.3): among all coin pairs and 6-month windows inside 2021-12..2023-12, the most
trades opened and closed inside the window, skipping any window with a safety halt; ties go to
the earliest window, then alphabetical order.
"""

from __future__ import annotations

from datetime import UTC, datetime
from itertools import combinations

FIRST_START = "2021-12"  # leaves 6 warm-up months after 2021-06
LAST_END = "2023-12"
WINDOW = 6


def _ms(month: str) -> int:
    return int(datetime.strptime(month, "%Y-%m").replace(tzinfo=UTC).timestamp() * 1000)


def _next(month: str) -> str:
    year, m = map(int, month.split("-"))
    return f"{year + m // 12:04d}-{m % 12 + 1:02d}"


def pick_window(
    trades: list[dict], halts: list[tuple[str, int]], symbols: list[str], months: list[str]
) -> dict:
    usable = sorted(m for m in months if FIRST_START <= m <= LAST_END)
    best: tuple | None = None
    for i in range(len(usable) - WINDOW + 1):
        start, end = usable[i], usable[i + WINDOW - 1]
        lo, hi = _ms(start), _ms(_next(end))
        if any(lo <= at < hi for _, at in halts):
            continue
        inside = [t for t in trades if lo <= t["opened_ms"] and t["closed_ms"] < hi]
        for pair in combinations(sorted(symbols), 2):
            n = sum(t["symbol"] in pair for t in inside)
            key = (-n, start, pair)
            if best is None or key < best:
                best = key
    if best is None:
        raise ValueError("no window without a halt")
    n, start, pair = -best[0], best[1], best[2]
    end = usable[usable.index(start) + WINDOW - 1]
    return {"coins": list(pair), "start": start, "end": end, "trades": n}


def main() -> None:
    """Run our replay on 2021-06..2023-12 and freeze the pilot choice (with its sha256)."""
    import asyncio
    import hashlib
    import json
    from pathlib import Path

    from ..data import month_range
    from ..engine_replay import Limits, replay

    limits = Limits()
    report = asyncio.run(replay("2021-06", LAST_END, limits))
    halts = [(code, at) for code, at in report["halts"]]
    choice = pick_window(report["closed"], halts, list(limits.symbols),
                         month_range("2021-06", LAST_END))
    body = {"choice": choice, "rule": __doc__.split("\n\n")[1].replace("\n", " "),
            "replay_trades": report["trades"], "halts": halts,
            "symbols": list(limits.symbols)}
    text = json.dumps(body, indent=1, sort_keys=True)
    out = {"sha256": hashlib.sha256(text.encode()).hexdigest(), **body}
    path = Path(__file__).resolve().parents[2] / "reports" / "jesse_pilot_choice.json"
    if path.exists():
        raise SystemExit(f"already frozen: {path}")
    path.write_text(json.dumps(out, indent=1, sort_keys=True), encoding="utf-8")
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
