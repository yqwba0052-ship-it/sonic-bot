#!/usr/bin/env python3
"""Daily backup of Sonic's SQLite database, run by systemd (sonic-backup.timer) as user sonic.

- Consistent while the bot runs: SQLite's online backup API copies a coherent snapshot; the
  trading engine is never stopped (its writes simply wait a few milliseconds at most).
- Each copy is checked (PRAGMA integrity_check, table counts), compressed, given a sha256,
  and stored with mode 600 in a mode-700 folder. Database files never go to GitHub.
- Retention: the newest KEEP_DAILY copies, plus the newest copy of each of the last
  KEEP_MONTHLY months. Older copies are removed only after a new copy passed its checks.
- Every run appends one JSON line to backup.log and rewrites last_backup.json, success or
  failure, and exits non-zero on failure (systemd records it in the journal too).

Standard library only, so it runs with any Python 3.10+.
    backup_db.py --db /opt/sonic/app/server/sonic.db --dir /opt/sonic/backups
    backup_db.py --verify FILE.db.gz       # check one backup, print its table counts
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import gzip
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

KEEP_DAILY = 14
KEEP_MONTHLY = 6
PREFIX = "sonic-"
SUFFIX = ".db.gz"
TABLES = ("trades", "events", "kv", "trader_trades", "trader_models", "scan_log",
          "equity_daily")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def check(db_path: Path) -> dict[str, int]:
    """integrity_check must say ok; returns row counts of the known tables present."""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        result = con.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise RuntimeError(f"integrity_check: {result}")
        names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        return {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]  # noqa: S608
                for t in TABLES if t in names}
    finally:
        con.close()


def snapshot(source: Path, target: Path) -> None:
    """Online backup: a consistent copy even while the engine writes."""
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=30)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def stamp_of(name: str) -> dt.datetime | None:
    try:
        return dt.datetime.strptime(name[len(PREFIX):-len(SUFFIX)], "%Y%m%d-%H%M%S")
    except ValueError:
        return None


def to_keep(names: list[str], keep_daily: int, keep_monthly: int) -> set[str]:
    dated = sorted(((stamp_of(n), n) for n in names if stamp_of(n)), reverse=True)
    keep = {n for _, n in dated[:keep_daily]}
    months: dict[str, str] = {}
    for when, name in dated:
        months.setdefault(when.strftime("%Y-%m"), name)
    keep.update(list(months.values())[:keep_monthly])
    return keep


def log(folder: Path, entry: dict) -> None:
    line = json.dumps(entry, sort_keys=True)
    with contextlib.suppress(OSError):
        with (folder / "backup.log").open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        (folder / "last_backup.json").write_text(line + "\n", encoding="utf-8")
    print(line, flush=True)


def run(db: Path, folder: Path, keep_daily: int, keep_monthly: int,
        now: dt.datetime | None = None) -> dict:
    now = now or dt.datetime.now(dt.UTC).replace(tzinfo=None)
    entry: dict = {"at_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "db": str(db), "ok": False}
    folder.mkdir(parents=True, exist_ok=True)
    os.chmod(folder, 0o700)
    try:
        if not db.exists():
            raise FileNotFoundError(f"no database at {db}")
        name = f"{PREFIX}{now.strftime('%Y%m%d-%H%M%S')}{SUFFIX}"
        final = folder / name
        with tempfile.TemporaryDirectory(dir=folder) as work:
            raw = Path(work) / "copy.db"
            snapshot(db, raw)
            counts = check(raw)
            partial = Path(work) / name
            with raw.open("rb") as src, gzip.open(partial, "wb", compresslevel=6) as dst:
                shutil.copyfileobj(src, dst)
            os.chmod(partial, 0o600)
            os.replace(partial, final)  # appears complete or not at all
        digest = sha256(final)
        Path(f"{final}.sha256").write_text(f"{digest}  {name}\n", encoding="utf-8")
        os.chmod(f"{final}.sha256", 0o600)
        names = [p.name for p in folder.glob(f"{PREFIX}*{SUFFIX}")]
        keep = to_keep(names, keep_daily, keep_monthly)
        removed = []
        for old in sorted(set(names) - keep):
            (folder / old).unlink(missing_ok=True)
            (folder / f"{old}.sha256").unlink(missing_ok=True)
            removed.append(old)
        entry.update(ok=True, file=name, bytes=final.stat().st_size, sha256=digest,
                     counts=counts, kept=len(keep), removed=removed)
    except Exception as error:  # noqa: BLE001 - every failure is logged and exits non-zero
        entry["error"] = f"{type(error).__name__}: {error}"[:500]
    log(folder, entry)
    return entry


def verify(path: Path) -> dict:
    """Decompress one backup into a temporary folder and check it (never touches the live DB)."""
    side = Path(f"{path}.sha256")
    if side.exists() and side.read_text(encoding="utf-8").split()[0] != sha256(path):
        raise RuntimeError("sha256 mismatch")
    with tempfile.TemporaryDirectory() as work:
        raw = Path(work) / "restored.db"
        with gzip.open(path, "rb") as src, raw.open("wb") as dst:
            shutil.copyfileobj(src, dst)
        return {"file": path.name, "counts": check(raw)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path)
    parser.add_argument("--dir", type=Path)
    parser.add_argument("--keep-daily", type=int, default=KEEP_DAILY)
    parser.add_argument("--keep-monthly", type=int, default=KEEP_MONTHLY)
    parser.add_argument("--verify", type=Path, help="check one backup file and exit")
    args = parser.parse_args()
    os.umask(0o077)
    if args.verify:
        print(json.dumps(verify(args.verify)))
        return 0
    if not (args.db and args.dir):
        parser.error("--db and --dir are required")
    return 0 if run(args.db, args.dir, args.keep_daily, args.keep_monthly)["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
