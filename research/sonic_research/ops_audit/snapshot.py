"""Read-only snapshot of the running Sonic server, for operational audits.

It opens ONE ssh session and runs a fixed read-only script on the server: systemd state, the
deployed commit, and SQLite queries through a `mode=ro` URI (the database cannot be written).
It never restarts, deploys or writes anything, and prints no keys or tokens (key-like kv
entries are shown as their length only).

    python research/sonic_research/ops_audit/snapshot.py [--host root@138.68.90.218]
        [--key ~/.ssh/sonic_cloud] [--json out.json]
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

DB = "/opt/sonic/app/server/sonic.db"

REMOTE_PY = r'''
import sqlite3, json, time
c = sqlite3.connect("file:@DB@?mode=ro", uri=True)
c.row_factory = sqlite3.Row
now = int(time.time() * 1000)
day = 86400000
out = {"now_ms": now}
kv = {}
for r in c.execute("select key, value from kv"):
    k = r["key"]
    secret = any(w in k.lower() for w in ("token", "key", "secret"))
    kv[k] = ("<len " + str(len(r["value"])) + ">") if secret else json.loads(r["value"])
out["kv"] = kv
q = lambda sql, *a: [dict(x) for x in c.execute(sql, a)]
out["trades"] = {
    "total": q("select count(*) n from trades")[0]["n"],
    "open": q("select symbol, side, entry_price, stop_price, target_price, opened_ms, scaled "
              "from trades where closed_ms is null"),
    "closed_7d": q("select count(*) n, sum(pnl) pnl from trades where closed_ms >= ?",
                   now - 7 * day)[0],
    "closed_30d": q("select count(*) n, sum(pnl) pnl from trades where closed_ms >= ?",
                    now - 30 * day)[0],
    "last": q("select symbol, side, opened_ms, closed_ms, pnl, exit_reason from trades "
              "order by id desc limit 5"),
}
out["events_by_code"] = q("select level, code, count(*) n, max(at_ms) last_ms from events "
                          "group by level, code order by last_ms desc")
out["problems"] = q("select at_ms, level, code, substr(detail, 1, 200) detail from events "
                    "where level in ('warning', 'error', 'critical') order by id desc limit 20")
out["lab"] = {
    "traders": q("select kind, status, count(*) n from traders group by kind, status"),
    "trader_trades": q("select trader_id, count(*) n, sum(pnl < 0) losses, max(closed_ms) "
                       "last_ms from trader_trades group by trader_id"),
    "models": q("select count(*) n, max(created_ms) last_ms from trader_models")[0],
    "trader_paper": q("select count(*) n, sum(closed_ms is null) open, sum(ret) ret "
                      "from trader_paper")[0],
    "allocations": q("select count(*) n from allocations")[0]["n"],
    "live_trades": q("select count(*) n from live_trades")[0]["n"],
}
print(json.dumps(out))
'''.replace("@DB@", DB)

REMOTE = (
    "systemctl is-active sonic; "
    "systemctl show sonic -p ActiveEnterTimestamp --value; "
    "systemctl show sonic -p NRestarts --value; "
    "sudo -u sonic git -C /opt/sonic/app log -1 --format=%h; "
    "echo LIVE_LINES=$(grep -c '^SONIC_LIVE' /opt/sonic/app/server/.env); "
    "sudo -u sonic python3 -"
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="root@138.68.90.218")
    parser.add_argument("--key", default=str(Path.home() / ".ssh" / "sonic_cloud"))
    parser.add_argument("--json", help="write the snapshot here")
    args = parser.parse_args()
    run = subprocess.run(
        ["ssh", "-i", args.key, "-o", "BatchMode=yes", "-o", "ConnectTimeout=25", args.host,
         REMOTE],
        input=REMOTE_PY, capture_output=True, text=True, timeout=120, check=True)
    lines = run.stdout.strip().splitlines()
    snapshot = {
        "service": lines[0], "active_since": lines[1], "restarts": lines[2],
        "deployed_commit": lines[3], "live_switch_lines": lines[4].split("=", 1)[1],
        **json.loads(lines[-1]),
    }
    text = json.dumps(snapshot, indent=1, ensure_ascii=False)
    print(text)
    if args.json:
        Path(args.json).write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
