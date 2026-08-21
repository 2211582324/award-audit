"""Local-only M5.6 SQLite WAL/busy-timeout/lease-recovery probe."""

from __future__ import annotations

import argparse
import json
import sqlite3
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=2.0)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=2000")
    return connection


def run_probe(work_dir: Path) -> dict[str, Any]:
    work_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=work_dir) as raw_dir:
        db_path = Path(raw_dir) / "wal-probe.db"
        setup = _connect(db_path)
        setup.executescript(
            "CREATE TABLE item(id INTEGER PRIMARY KEY, value TEXT NOT NULL);"
            "CREATE TABLE job(id INTEGER PRIMARY KEY,status TEXT NOT NULL,lease_expires TEXT);"
        )
        setup.commit()
        setup.close()

        writer_started = threading.Event()
        release_writer = threading.Event()
        writer_error: list[str] = []

        def hold_write() -> None:
            connection = _connect(db_path)
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute("INSERT INTO item(value) VALUES ('first')")
                writer_started.set()
                release_writer.wait(timeout=2)
                connection.commit()
            except Exception as exc:  # probe records, caller evaluates
                writer_error.append(type(exc).__name__)
            finally:
                connection.close()

        thread = threading.Thread(target=hold_write, daemon=True)
        thread.start()
        if not writer_started.wait(timeout=2):
            raise RuntimeError("writer did not acquire WAL transaction")

        second = _connect(db_path)
        read_during_write = int(second.execute("SELECT COUNT(*) FROM item").fetchone()[0])
        write_result: list[str] = []

        def waiting_write() -> None:
            connection = _connect(db_path)
            try:
                connection.execute("INSERT INTO item(value) VALUES ('second')")
                connection.commit()
                write_result.append("committed")
            except Exception as exc:
                write_result.append(type(exc).__name__)
            finally:
                connection.close()

        second_writer = threading.Thread(target=waiting_write, daemon=True)
        second_writer.start()
        time.sleep(0.1)
        release_writer.set()
        thread.join(timeout=2)
        second_writer.join(timeout=2)
        final_count = int(second.execute("SELECT COUNT(*) FROM item").fetchone()[0])

        expired = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
        second.execute(
            "INSERT INTO job(id,status,lease_expires) VALUES (1,'running',?)", (expired,)
        )
        second.commit()
        now = datetime.now(timezone.utc).isoformat()
        recovered = second.execute(
            "UPDATE job SET status='queued',lease_expires=NULL "
            "WHERE status='running' AND lease_expires < ?",
            (now,),
        ).rowcount
        second.commit()
        journal_mode = str(second.execute("PRAGMA journal_mode").fetchone()[0]).lower()
        busy_timeout = int(second.execute("PRAGMA busy_timeout").fetchone()[0])
        second.close()

    passed = (
        journal_mode == "wal"
        and busy_timeout == 2000
        and read_during_write == 0
        and not writer_error
        and write_result == ["committed"]
        and final_count == 2
        and recovered == 1
    )
    return {
        "probe": "m5_sqlite_wal",
        "mode": "local-only",
        "status": "complete" if passed else "failed",
        "journal_mode": journal_mode,
        "busy_timeout_ms": busy_timeout,
        "read_during_uncommitted_write": read_during_write,
        "second_write": write_result[0] if write_result else "no_result",
        "final_row_count": final_count,
        "expired_lease_recovered": recovered == 1,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--work-dir", type=Path, default=Path("tmp") / "m5_sqlite_probe")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = run_probe(args.work_dir)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
