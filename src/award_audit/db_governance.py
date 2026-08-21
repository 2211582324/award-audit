"""Read-only inspection and safe online backups for review databases."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

_COUNTED_TABLES = (
    "import_batch",
    "audit_case",
    "audit_attempt",
    "audit_job",
    "evidence_source",
    "evidence_asset_task",
    "evidence_identity",
    "evidence_scope_comparison",
    "case_memory",
    "tool_trace",
)


def _readonly_connection(path: Path) -> sqlite3.Connection:
    resolved = path.resolve(strict=True)
    if not resolved.is_file():
        raise ValueError(f"database path is not a file: {resolved}")
    connection = sqlite3.connect(f"{resolved.as_uri()}?mode=ro", uri=True, timeout=30.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    connection.execute("PRAGMA busy_timeout = 30000")
    return connection


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row["name"])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }


def inspect_database(path: str | Path) -> dict[str, Any]:
    """Return a non-sensitive health summary without modifying the database."""

    database = Path(path).resolve(strict=True)
    with _readonly_connection(database) as connection:
        tables = _table_names(connection)
        quick_check = [str(row[0]) for row in connection.execute("PRAGMA quick_check")]
        foreign_key_errors = sum(1 for _ in connection.execute("PRAGMA foreign_key_check"))
        page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
        page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])
        freelist_count = int(connection.execute("PRAGMA freelist_count").fetchone()[0])
        journal_mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0])
        counts = {
            table: int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
            for table in _COUNTED_TABLES
            if table in tables
        }
        migrations = []
        if "schema_migration" in tables:
            migrations = [
                str(row[0])
                for row in connection.execute(
                    "SELECT version FROM schema_migration ORDER BY version"
                )
            ]
    return {
        "schema_version": 1,
        "database_name": database.name,
        "size_bytes": database.stat().st_size,
        "journal_mode": journal_mode,
        "quick_check": quick_check,
        "foreign_key_error_count": foreign_key_errors,
        "healthy": quick_check == ["ok"] and foreign_key_errors == 0,
        "page_size": page_size,
        "page_count": page_count,
        "freelist_count": freelist_count,
        "estimated_free_bytes": page_size * freelist_count,
        "table_count": len(tables),
        "record_counts": counts,
        "applied_migrations": migrations,
        "wal_present": database.with_name(database.name + "-wal").is_file(),
        "shm_present": database.with_name(database.name + "-shm").is_file(),
    }


def backup_database(source: str | Path, output: str | Path) -> dict[str, Any]:
    """Create a consistent SQLite backup and a hash manifest without overwriting files."""

    source_path = Path(source).resolve(strict=True)
    output_path = Path(output).resolve(strict=False)
    if source_path == output_path:
        raise ValueError("backup output must differ from the source database")
    if output_path.exists():
        raise FileExistsError(f"backup output already exists: {output_path}")
    manifest_path = output_path.with_suffix(output_path.suffix + ".manifest.json")
    if manifest_path.exists():
        raise FileExistsError(f"backup manifest already exists: {manifest_path}")
    output_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        with _readonly_connection(source_path) as source_connection:
            with sqlite3.connect(output_path, timeout=30.0) as destination:
                source_connection.backup(destination)
        inspection = inspect_database(output_path)
        if not inspection["healthy"]:
            raise RuntimeError("backup database failed integrity validation")
        manifest = {
            "schema_version": 1,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "source_database_name": source_path.name,
            "backup_database_name": output_path.name,
            "sha256": _sha256(output_path),
            "size_bytes": output_path.stat().st_size,
            "inspection": inspection,
        }
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return manifest
    except Exception:
        if output_path.exists():
            output_path.unlink()
        if manifest_path.exists():
            manifest_path.unlink()
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Inspect or safely back up an Award Audit SQLite database."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect_parser = subparsers.add_parser("inspect", help="run read-only health checks")
    inspect_parser.add_argument("--db", type=Path, required=True)

    backup_parser = subparsers.add_parser("backup", help="create a verified online backup")
    backup_parser.add_argument("--db", type=Path, required=True)
    backup_parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    if args.command == "inspect":
        result = inspect_database(args.db)
    else:
        result = backup_database(args.db, args.output)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
