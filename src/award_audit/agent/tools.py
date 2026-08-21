"""Compatibility facade for the M4 tool API.

Implementations moved to :mod:`award_audit.agent.toolkit` in M5.1.  Existing
imports and monkeypatch points remain stable so M1-M4 callers need no changes.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from award_audit.agent.toolkit.spreadsheet import (
    AcquiredGrid,
    extract_semantic_roster_records,
    parse_award_excel,
)
from award_audit.agent.toolkit.spreadsheet import acquire_excel_grid as _acquire_excel_grid
from award_audit.agent.toolkit.web import (
    EXCEL_EXTS,
    IMAGE_EXTS,
    MAX_DOWNLOAD_MB,
    MAX_TEXT_CHARS,
    OTHER_DOC_EXTS,
    Attachment,
    PageContent,
    download_file,
    extract_years,
    fetch_page,
    parse_html,
)


def acquire_excel_grid(
    urls: list[str],
    workdir: Path,
    max_total: int = 100,
    *,
    direct_attachment_urls: list[str] | None = None,
    direct_referer: str = "",
    direct_attachment_parent_urls: dict[str, str] | None = None,
    attachment_filter_fn: Callable[[Attachment], bool] | None = None,
) -> AcquiredGrid | None:
    """Call the migrated implementation through facade-level patch points."""

    return _acquire_excel_grid(
        urls,
        workdir,
        max_total,
        direct_attachment_urls=direct_attachment_urls,
        direct_referer=direct_referer,
        direct_attachment_parent_urls=direct_attachment_parent_urls,
        attachment_filter_fn=attachment_filter_fn,
        fetch_page_fn=fetch_page,
        download_file_fn=download_file,
        parse_excel_fn=parse_award_excel,
    )


__all__ = [
    "EXCEL_EXTS",
    "IMAGE_EXTS",
    "MAX_DOWNLOAD_MB",
    "MAX_TEXT_CHARS",
    "OTHER_DOC_EXTS",
    "AcquiredGrid",
    "Attachment",
    "PageContent",
    "acquire_excel_grid",
    "download_file",
    "extract_years",
    "extract_semantic_roster_records",
    "fetch_page",
    "parse_award_excel",
    "parse_html",
]
