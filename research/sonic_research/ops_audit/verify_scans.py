# ruff: noqa: E501 - the remote read-only command is one long shell line
"""Verify every scan since a given bar against Binance's public klines (read-only).

One ssh session reads the server's scan_log, events, equity_daily, backups and switches through
SQLite mode=ro (nothing is written, no key is printed). For every scan row it checks:
  - the bar it judged is the newest bar closed at scan time (no stale bar, no future bar);
  - BTC's close and every coin's close equal Binance's close of that same bar;
  - every coin is marked fresh and data_ok = 1;
and over all rows: no missing bar between the first and the newest row (duplicates are
impossible: bar_ms is the primary key).

    server/.venv/Scripts/python -I research/sonic_research/ops_audit/verify_scans.py \
        --since "2026-11-18 00:00" --json OUT.json
"""

from __future__ import annotations

import argparse
import json
import subprocess
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

BAR_MS = 4 * 3600 * 1000
CLOSE_MARGIN_MS = 5_000

REMOTE = r'''sudo -u sonic python3 -c "
import sqlite3, json, sys
c = sqlite3.connect('file:/opt/sonic/app/server/sonic.db?mode=ro', uri=True)
c.row_factory = sqlite3.Row
since = int(sys.argv[1])
q = lambda sql, *a: [dict(r) for r in c.execute(sql, a)]
out = {
 'rows': q('select * from scan_log where bar_ms >= ? order by bar_ms', since),
 'events': q(\"select at_ms, level, code, substr(detail,1,160) detail from events where at_ms >= ? and level in ('warning','error','critical') order by id\", since),
 'event_codes': q('select code, count(*) n from events where at_ms >= ? group by code', since),
 'equity': q('select * from equity_daily order by day'),
 'trades': q('select count(*) n, sum(closed_ms is null) open from trades')[0],
 'live_trades': q('select count(*) n from live_trades')[0]['n'],
 'allocations': q('select count(*) n from allocations')[0]['n'],
 'kv': {k: v for k, v in c.execute(\"select key, value from kv where key in ('last_bar_ms','last_scan','halted','was_running','equity_peak')\")},
}
print(json.dumps(out))
" SINCE; echo "SERVICE=$(systemctl is-active sonic)|$(systemctl show sonic -p ActiveEnterTimestamp --value)|$(systemctl show sonic -p NRestarts --value)|$(sudo -u sonic git -C /opt/sonic/app rev-parse --short HEAD)"; echo "LIVE=$(grep -c '^SONIC_LIVE' /opt/sonic/app/server/.env)"; echo "TIMER=$(systemctl is-active sonic-backup.timer)|$(systemctl show sonic-backup.timer -p NextElapseUSecRealtime --value)"; echo "BACKUPS=$(ls /opt/sonic/backups/ | grep -c 'db.gz$')"; echo "LOG=$(tail -n 5 /opt/sonic/backups/backup.log | tr '\n' '~')"'''


def kline(symbol: str, bar_ms: int) -> list:
    url = (f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}&interval=4h"
           f"&startTime={bar_ms}&limit=1")
    with urllib.request.urlopen(url, timeout=20) as response:  # noqa: S310 - fixed public URL
        row = json.loads(response.read())[0]
    if int(row[0]) != bar_ms:
        raise RuntimeError(f"no {symbol} kline for {bar_ms}")
    return row


def fmt(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%d %H:%M:%S")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--since", required=True, help='bar open time, UTC, e.g. "2026-11-18 00:00"')
    parser.add_argument("--host", default="root@138.68.90.218")
    parser.add_argument("--key", default=str(Path.home() / ".ssh" / "sonic_cloud"))
    parser.add_argument("--json")
    args = parser.parse_args()
    since = int(datetime.strptime(args.since, "%Y-%m-%d %H:%M").replace(tzinfo=UTC).timestamp() * 1000)
    out = subprocess.run(["ssh", "-i", args.key, "-o", "BatchMode=yes", "-o", "ConnectTimeout=25",
                          args.host, REMOTE.replace("SINCE", str(since))],
                         capture_output=True, text=True, timeout=180, check=True).stdout.splitlines()
    data = json.loads(out[0])
    tags = dict(line.split("=", 1) for line in out[1:] if "=" in line)

    checked, problems = [], []
    for row in data["rows"]:
        bar, at = row["bar_ms"], row["at_ms"]
        coins = json.loads(row["coins"] or "{}")
        newest_closed = (at - CLOSE_MARGIN_MS) // BAR_MS * BAR_MS - BAR_MS
        btc = kline("BTCUSDT", bar)
        item = {
            "bar_utc": fmt(bar), "scanned_utc": fmt(at),
            "seconds_after_close": round((at - bar - BAR_MS) / 1000, 1),
            "status": row["status"], "attempts": row["attempts"], "data_ok": row["data_ok"],
            "market": row["market"], "signals": row["signals"], "opened": row["opened"],
            "btc_server": row["btc_close"], "btc_binance": float(btc[4]),
            "coins": {},
        }
        if bar != newest_closed:
            problems.append(f"{fmt(bar)}: judged bar is not the newest closed one at scan time")
        if bar + BAR_MS > at:
            problems.append(f"{fmt(bar)}: future bar (closes after the scan)")
        if row["status"] != "scanned":
            problems.append(f"{fmt(bar)}: status {row['status']} {row['error']}")
        if row["btc_close"] != float(btc[4]):
            problems.append(f"{fmt(bar)}: BTC {row['btc_close']} != Binance {btc[4]}")
        if row["data_ok"] != 1:
            problems.append(f"{fmt(bar)}: data_ok {row['data_ok']}")
        for symbol, c in coins.items():
            ref = float(kline(symbol, bar)[4])
            item["coins"][symbol] = {"server": c.get("close"), "binance": ref, "fresh": c.get("fresh"),
                                     "result": c.get("result")}
            if c.get("close") != ref:
                problems.append(f"{fmt(bar)}: {symbol} {c.get('close')} != Binance {ref}")
            if c.get("fresh") is not True:
                problems.append(f"{fmt(bar)}: {symbol} not fresh")
        checked.append(item)

    bars = [r["bar_ms"] for r in data["rows"]]
    missing = [fmt(b) for b in range(bars[0], bars[-1] + 1, BAR_MS) if b not in bars] if bars else []
    if missing:
        problems.append(f"missing scans: {missing}")
    service = tags.get("SERVICE", "").split("|")
    timer = tags.get("TIMER", "").split("|")
    result = {
        "checked_at_utc": fmt(int(datetime.now(UTC).timestamp() * 1000)),
        "since_bar_utc": args.since, "scans": len(checked), "missing": missing,
        "problems": problems, "all_ok": not problems,
        "scans_detail": checked,
        "service": {"state": service[0], "since": service[1] if len(service) > 1 else None,
                    "restarts": service[2] if len(service) > 2 else None,
                    "commit": service[3] if len(service) > 3 else None},
        "live_switch_lines": tags.get("LIVE"),
        "backup_timer": {"state": timer[0], "next": timer[1] if len(timer) > 1 else None},
        "backups_on_server": tags.get("BACKUPS"),
        "backup_log_tail": [json.loads(x) for x in tags.get("LOG", "").split("~") if x.strip()],
        "warnings_errors": data["events"], "event_codes": data["event_codes"],
        "equity_daily": data["equity"], "trades": data["trades"],
        "live_trades": data["live_trades"], "allocations": data["allocations"], "kv": data["kv"],
    }
    text = json.dumps(result, indent=1, ensure_ascii=False)
    if args.json:
        Path(args.json).write_text(text, encoding="utf-8")
    summary = {k: result[k] for k in ("scans", "missing", "all_ok", "service", "live_switch_lines",
                                      "backup_timer", "backups_on_server", "trades", "live_trades",
                                      "allocations")}
    print(json.dumps(summary, indent=1, ensure_ascii=False))
    for line in problems:
        print("PROBLEM:", line)
    for item in checked:
        print(item["bar_utc"], "scanned", item["scanned_utc"], f"(+{item['seconds_after_close']}s)",
              item["status"], "attempts", item["attempts"], "data_ok", item["data_ok"], item["market"],
              "signals", item["signals"], "BTC", item["btc_server"], "vs", item["btc_binance"])


if __name__ == "__main__":
    main()
