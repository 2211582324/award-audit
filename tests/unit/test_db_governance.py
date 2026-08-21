from __future__ import annotations

import json
from pathlib import Path

import pytest

from award_audit.core.pipeline.store import Store
from award_audit.db_governance import backup_database, inspect_database


def test_inspect_database_is_read_only_and_reports_health(tmp_path: Path) -> None:
    database = tmp_path / "review.db"
    store = Store(database)
    store.create_batch("governance-test")
    store.close()
    before = database.read_bytes()

    result = inspect_database(database)

    assert result["healthy"] is True
    assert result["quick_check"] == ["ok"]
    assert result["foreign_key_error_count"] == 0
    assert result["record_counts"]["import_batch"] == 1
    assert result["applied_migrations"]
    assert database.read_bytes() == before


def test_backup_database_creates_verified_copy_and_manifest(tmp_path: Path) -> None:
    database = tmp_path / "review.db"
    store = Store(database)
    store.create_batch("governance-test")
    store.close()
    backup = tmp_path / "backups" / "review-20260814.db"

    manifest = backup_database(database, backup)

    assert backup.is_file()
    assert manifest["inspection"]["healthy"] is True
    assert manifest["inspection"]["record_counts"]["import_batch"] == 1
    manifest_path = backup.with_suffix(".db.manifest.json")
    assert json.loads(manifest_path.read_text(encoding="utf-8"))["sha256"] == manifest["sha256"]


def test_backup_database_refuses_to_overwrite(tmp_path: Path) -> None:
    database = tmp_path / "review.db"
    store = Store(database)
    store.close()
    backup = tmp_path / "backup.db"
    backup.write_bytes(b"existing")

    with pytest.raises(FileExistsError):
        backup_database(database, backup)
