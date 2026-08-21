from __future__ import annotations

import os
from pathlib import Path

from award_audit.core import config


def test_load_env_reads_each_file_only_once(tmp_path: Path, monkeypatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("AWARD_AUDIT_TEST_ONCE=first\n", encoding="utf-8")
    monkeypatch.delenv("AWARD_AUDIT_TEST_ONCE", raising=False)

    config.load_env(env_file)
    env_file.write_text("AWARD_AUDIT_TEST_ONCE=second\n", encoding="utf-8")
    monkeypatch.delenv("AWARD_AUDIT_TEST_ONCE", raising=False)
    config.load_env(env_file)

    assert "AWARD_AUDIT_TEST_ONCE" not in os.environ


def test_load_env_does_not_overwrite_process_environment(
    tmp_path: Path, monkeypatch
) -> None:
    env_file = tmp_path / "settings.env"
    env_file.write_text("AWARD_AUDIT_TEST_PRIORITY=file\n", encoding="utf-8")
    monkeypatch.setenv("AWARD_AUDIT_TEST_PRIORITY", "process")

    config.load_env(env_file)

    assert os.environ["AWARD_AUDIT_TEST_PRIORITY"] == "process"
