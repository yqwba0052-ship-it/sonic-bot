"""deploy/backup_db.py: consistent copies while the bot writes, checked, kept by policy,
logged on success and failure, and restorable into a separate folder."""

from __future__ import annotations

import datetime as dt
import gzip
import importlib.util
import json
import os
import shutil
import sqlite3
import threading
from pathlib import Path

import pytest

from sonic import monitor
from sonic.store import Store

SPEC = importlib.util.spec_from_file_location(
    "backup_db", Path(__file__).resolve().parents[2] / "deploy" / "backup_db.py")
backup_db = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(backup_db)


def live_db(path: Path) -> Store:
    store = Store(str(path))
    monitor.ensure(store.db)
    store.put("last_scan", {"at_ms": 1})
    for i in range(50):
        store.add_event(i, "info", "test", "x" * 50)
    return store


def test_backup_is_consistent_while_the_engine_writes(tmp_path: Path) -> None:
    db = tmp_path / "sonic.db"
    store = live_db(db)
    stop = threading.Event()

    def writer() -> None:
        con = sqlite3.connect(db, timeout=30)
        n = 0
        while not stop.is_set():
            con.execute("INSERT INTO events (at_ms, level, code, detail) VALUES (?,?,?,?)",
                        (n, "info", "w", "y" * 200))
            con.commit()
            n += 1
        con.close()

    thread = threading.Thread(target=writer)
    thread.start()
    try:
        results = [backup_db.run(db, tmp_path / "b", 14, 6,
                                 now=dt.datetime(2026, 11, 18, 2, 30, i)) for i in range(5)]
    finally:
        stop.set()
        thread.join()
    assert all(r["ok"] for r in results), results
    assert all(r["counts"]["events"] >= 50 for r in results)
    store.db.close()


def test_backup_files_are_private_and_verified(tmp_path: Path) -> None:
    db = tmp_path / "sonic.db"
    live_db(db).db.close()
    folder = tmp_path / "b"
    entry = backup_db.run(db, folder, 14, 6, now=dt.datetime(2026, 11, 18, 2, 30))
    assert entry["ok"] and entry["counts"]["events"] == 50 and entry["counts"]["kv"] == 1
    path = folder / entry["file"]
    if os.name == "posix":
        assert (path.stat().st_mode & 0o777) == 0o600
        assert (folder.stat().st_mode & 0o777) == 0o700
    assert backup_db.verify(path)["counts"]["events"] == 50
    last = json.loads((folder / "last_backup.json").read_text(encoding="utf-8"))
    assert last["ok"] and last["file"] == entry["file"]


def test_a_damaged_backup_is_refused(tmp_path: Path) -> None:
    db = tmp_path / "sonic.db"
    live_db(db).db.close()
    folder = tmp_path / "b"
    entry = backup_db.run(db, folder, 14, 6, now=dt.datetime(2026, 11, 18, 2, 30))
    path = folder / entry["file"]
    data = bytearray(path.read_bytes())
    data[len(data) // 2] ^= 0xFF
    path.write_bytes(bytes(data))
    with pytest.raises(Exception):  # noqa: B017 - sha256 mismatch (or a broken gzip)
        backup_db.verify(path)


def test_failure_is_logged_and_reported(tmp_path: Path) -> None:
    folder = tmp_path / "b"
    entry = backup_db.run(tmp_path / "missing.db", folder, 14, 6)
    assert entry["ok"] is False and "no database" in entry["error"]
    lines = (folder / "backup.log").read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[-1])["ok"] is False


def test_retention_keeps_recent_days_and_one_per_month() -> None:
    names = []
    day = dt.datetime(2026, 1, 1, 2, 30)
    for i in range(320):
        names.append(f"sonic-{(day + dt.timedelta(days=i)).strftime('%Y%m%d-%H%M%S')}.db.gz")
    keep = backup_db.to_keep(names, 14, 6)
    newest = sorted(names)[-14:]
    assert set(newest) <= keep
    months = {n[6:12] for n in keep}
    assert len(months) == 6  # Nov back to Jun 2026
    assert len(keep) == 14 + 5  # the newest month's copy is already among the 14


def test_old_copies_are_removed_only_after_a_good_new_one(tmp_path: Path) -> None:
    db = tmp_path / "sonic.db"
    live_db(db).db.close()
    folder = tmp_path / "b"
    start = dt.datetime(2026, 11, 1, 2, 30)
    for i in range(20):
        assert backup_db.run(db, folder, 14, 6, now=start + dt.timedelta(days=i))["ok"]
    files = sorted(p.name for p in folder.glob("sonic-*.db.gz"))
    assert len(files) == 14 and files[-1].startswith("sonic-20261120")
    assert all((folder / f"{f}.sha256").exists() for f in files)
    db.unlink()  # a failing run must not delete anything
    assert backup_db.run(db, folder, 14, 6, now=start + dt.timedelta(days=30))["ok"] is False
    assert len(list(folder.glob("sonic-*.db.gz"))) == 14


def test_restore_into_a_separate_folder_gives_the_same_data(tmp_path: Path) -> None:
    db = tmp_path / "live" / "sonic.db"
    db.parent.mkdir()
    store = live_db(db)
    store.open_trade(symbol="SOLUSDT", side="BUY", qty=1.0, entry_price=100.0, stop_price=90.0,
                     target_price=102.0, margin=33.0, opened_ms=5, risk_usdt=10.0, plan="{}")
    entry = backup_db.run(db, tmp_path / "b", 14, 6, now=dt.datetime(2026, 11, 18))
    before = db.read_bytes()
    restored = tmp_path / "restore" / "sonic.db"
    restored.parent.mkdir()
    with gzip.open(tmp_path / "b" / entry["file"], "rb") as src, restored.open("wb") as dst:
        shutil.copyfileobj(src, dst)
    copy = Store(str(restored))
    assert copy.open_trades()[0]["symbol"] == "SOLUSDT"
    assert len(copy.events(1000)) == 50 and copy.get("last_scan") == {"at_ms": 1}
    assert db.read_bytes() == before  # the live database was not touched
    store.db.close()
    copy.db.close()
