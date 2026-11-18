# ruff: noqa: E501 - the remote read-only command is one long shell line
"""After a 4h close: read the server's newest scan row (read-only: SQLite mode=ro over one ssh
session) and compare the bar it used, and every price in it, with Binance's public klines for
that bar. Prints no keys. Usage (from the repo root):

    server/.venv/Scripts/python -I research/sonic_research/ops_audit/check_scan.py OUT.json
"""

import json
import subprocess
import sys
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

REMOTE = r'''sudo -u sonic python3 -c "
import sqlite3, json
c = sqlite3.connect('file:/opt/sonic/app/server/sonic.db?mode=ro', uri=True)
c.row_factory = sqlite3.Row
rows = [dict(r) for r in c.execute('select * from scan_log order by bar_ms desc limit 2')]
ev = [dict(r) for r in c.execute(\"select at_ms, level, code, detail from events where code in ('stale_bars','tick_failed','scan_log_failed','equity_snapshot_failed') order by id desc limit 5\")]
kv = {k: v for k, v in c.execute(\"select key, value from kv where key in ('last_bar_ms','last_scan','halted','was_running')\")}
print(json.dumps({'rows': rows, 'events': ev, 'kv': kv}))
"; systemctl is-active sonic; grep -c '^SONIC_LIVE' /opt/sonic/app/server/.env; cat /opt/sonic/backups/last_backup.json; systemctl show sonic-backup.timer -p NextElapseUSecRealtime --value'''

key = str(Path.home() / ".ssh" / "sonic_cloud")
out = subprocess.run(["ssh", "-i", key, "-o", "BatchMode=yes", "-o", "ConnectTimeout=25",
                      "root@138.68.90.218", REMOTE], capture_output=True, text=True, timeout=120,
                     check=True).stdout.strip().splitlines()
data = json.loads(out[0])
row = data["rows"][0]
bar = row["bar_ms"]
coins = json.loads(row["coins"])


def kline_close(symbol: str) -> float:
    url = (f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}&interval=4h"
           f"&startTime={bar}&limit=1")
    with urllib.request.urlopen(url, timeout=20) as r:  # noqa: S310 - fixed public URL
        k = json.loads(r.read())[0]
    assert int(k[0]) == bar
    return float(k[4])


fmt = lambda ms: datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%d %H:%M:%S")  # noqa: E731
result = {
    "scan_bar_utc": fmt(bar), "scanned_at_utc": fmt(row["at_ms"]),
    "seconds_after_close": round((row["at_ms"] - bar - 4 * 3600 * 1000) / 1000, 1),
    "status": row["status"], "attempts": row["attempts"], "data_ok": row["data_ok"],
    "market": row["market"], "signals": row["signals"], "opened": row["opened"],
    "btc": {"server": row["btc_close"], "binance": kline_close("BTCUSDT")},
    "coins": {s: {"server": c.get("close"), "binance": kline_close(s), "fresh": c.get("fresh"),
                  "result": c.get("result")} for s, c in coins.items()},
    "events": data["events"], "kv": data["kv"],
    "service": out[1], "live_switch_lines": out[2], "last_backup_ok": json.loads(out[3])["ok"],
    "backup_timer_next": out[4],
}
result["all_prices_match"] = (result["btc"]["server"] == result["btc"]["binance"] and all(
    v["server"] == v["binance"] for v in result["coins"].values()))
text = json.dumps(result, indent=1)
print(text)
Path(sys.argv[1]).write_text(text, encoding="utf-8")
