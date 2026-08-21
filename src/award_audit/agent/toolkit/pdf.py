"""Bounded PDF inspection, extraction and page rendering for M5.2."""

from __future__ import annotations

import math
import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from award_audit.agent.toolkit.safety import inspect_evidence_file

MAX_PDF_PAGES = 300
MAX_RENDER_PAGES = 20
DEFAULT_RENDER_DPI = 150
MIN_RENDER_DPI = 96
MAX_RENDER_DPI = 200
MAX_TEXT_CHARS_PER_PAGE = 20_000
MAX_TABLES_PER_PAGE = 20
MAX_TABLE_ROWS_PER_PAGE = 500
MAX_TABLE_COLUMNS = 50
MAX_TABLE_CELL_CHARS = 500
MAX_TABLE_CHARS_PER_PAGE = 100_000
SCAN_TEXT_CHAR_THRESHOLD = 30
_MEANINGFUL_TEXT = re.compile(r"[\w\u3400-\u9fff]", re.UNICODE)


class PdfError(RuntimeError):
    """Base class for expected, user-visible PDF failures."""


class PdfDependencyError(PdfError):
    """A required local parser or renderer is unavailable."""


class PdfEncryptedError(PdfError):
    """Encrypted PDFs are not accepted by the first production version."""


class PdfLimitError(PdfError):
    """A PDF operation exceeded an explicit page, output or pixel limit."""


class PdfRuntimeError(PdfError):
    """A parser or renderer failed on a validated PDF."""


class PdfPageInspection(BaseModel):
    page: int = Field(ge=1)
    width_points: float = Field(gt=0)
    height_points: float = Field(gt=0)
    text_chars: int = Field(ge=0)
    text_presence_ratio: float = Field(ge=0, le=1)
    mode: str


class PdfInspection(BaseModel):
    page_count: int = Field(ge=1)
    encrypted: bool = False
    pages: list[PdfPageInspection]
    digital_pages: list[int]
    scan_candidate_pages: list[int]
    truncated: bool = False


class PdfTableCandidate(BaseModel):
    rows: list[list[str]]
    row_count: int = Field(ge=0)
    column_count: int = Field(ge=0)
    is_truncated: bool = False


class PdfTextPage(BaseModel):
    page: int = Field(ge=1)
    text: str
    text_chars: int = Field(ge=0)
    is_truncated: bool = False
    tables: list[PdfTableCandidate] = Field(default_factory=list)
    table_rows: int = Field(ge=0)


class RenderedPdfPage(BaseModel):
    page: int = Field(ge=1)
    path: Path
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    pixels: int = Field(gt=0)
    dpi: int = Field(ge=MIN_RENDER_DPI, le=MAX_RENDER_DPI)
    content_type: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(gt=0)
    latency_ms: int = Field(ge=0)


def _meaningful_chars(text: str) -> int:
    return len(_MEANINGFUL_TEXT.findall(text or ""))


def _require_pypdf() -> Any:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise PdfDependencyError("pypdf is required; install award-audit[m5-pdf]") from exc
    return PdfReader


def _reader(path: Path) -> Any:
    PdfReader = _require_pypdf()
    try:
        reader = PdfReader(str(path), strict=False)
    except Exception as exc:  # parser boundary
        raise PdfRuntimeError(f"pypdf could not open the PDF: {type(exc).__name__}: {exc}") from exc
    if reader.is_encrypted:
        raise PdfEncryptedError("encrypted PDFs require manual handling")
    if not reader.pages:
        raise PdfRuntimeError("PDF contains no pages")
    return reader


def _require_pdfplumber() -> Any:
    try:
        import pdfplumber
    except ImportError as exc:
        raise PdfDependencyError("pdfplumber is required; install award-audit[m5-pdf]") from exc
    return pdfplumber


def _validate_page_count(page_count: int, max_pages: int) -> None:
    if page_count > max_pages:
        raise PdfLimitError(f"PDF has {page_count} pages; limit is {max_pages}")


def _selected_pages(page_count: int, pages: list[int], *, max_pages: int) -> list[int]:
    selected = pages or list(range(1, page_count + 1))
    if len(selected) > max_pages:
        raise PdfLimitError(f"requested {len(selected)} pages; limit is {max_pages}")
    if len(set(selected)) != len(selected):
        raise PdfLimitError("page selection contains duplicates")
    if any(page < 1 or page > page_count for page in selected):
        raise PdfLimitError(f"page selection must stay within 1..{page_count}")
    return selected


def inspect_pdf(path: Path, *, max_pages: int = MAX_PDF_PAGES) -> PdfInspection:
    """Inspect every allowed page without returning raw document text."""

    try:
        reader = _reader(path)
    except PdfEncryptedError:
        raise
    except PdfRuntimeError:
        return _inspect_with_pdfplumber(path, max_pages=max_pages)
    page_count = len(reader.pages)
    _validate_page_count(page_count, max_pages)
    pages: list[PdfPageInspection] = []
    for page_number, page in enumerate(reader.pages, start=1):
        try:
            text = page.extract_text() or ""
            width = float(page.mediabox.width)
            height = float(page.mediabox.height)
        except Exception as exc:
            raise PdfRuntimeError(
                f"could not inspect PDF page {page_number}: {type(exc).__name__}: {exc}"
            ) from exc
        pages.append(_page_inspection(page_number, width, height, text))
    return _inspection_from_pages(pages)


def _page_inspection(
    page_number: int,
    width: float,
    height: float,
    text: str,
) -> PdfPageInspection:
    chars = _meaningful_chars(text)
    return PdfPageInspection(
        page=page_number,
        width_points=max(width, 1.0),
        height_points=max(height, 1.0),
        text_chars=chars,
        text_presence_ratio=round(min(1.0, chars / SCAN_TEXT_CHAR_THRESHOLD), 3),
        mode="scan" if chars < SCAN_TEXT_CHAR_THRESHOLD else "digital",
    )


def _inspection_from_pages(pages: list[PdfPageInspection]) -> PdfInspection:
    return PdfInspection(
        page_count=len(pages),
        pages=pages,
        digital_pages=[page.page for page in pages if page.mode == "digital"],
        scan_candidate_pages=[page.page for page in pages if page.mode == "scan"],
    )


def _inspect_with_pdfplumber(path: Path, *, max_pages: int) -> PdfInspection:
    pdfplumber = _require_pdfplumber()
    try:
        with pdfplumber.open(str(path)) as document:
            _validate_page_count(len(document.pages), max_pages)
            pages = [
                _page_inspection(
                    page_number,
                    float(page.width),
                    float(page.height),
                    page.extract_text() or "",
                )
                for page_number, page in enumerate(document.pages, start=1)
            ]
    except PdfError:
        raise
    except Exception as exc:
        raise PdfRuntimeError(
            f"pdfplumber could not inspect the PDF: {type(exc).__name__}: {exc}"
        ) from exc
    if not pages:
        raise PdfRuntimeError("PDF contains no pages")
    return _inspection_from_pages(pages)


def _bounded_cell(value: object) -> tuple[str, bool]:
    text = "" if value is None else str(value).strip()
    return text[:MAX_TABLE_CELL_CHARS], len(text) > MAX_TABLE_CELL_CHARS


def _extract_table_candidates(page: Any) -> tuple[list[PdfTableCandidate], int]:
    tables: list[PdfTableCandidate] = []
    rows_used = 0
    chars_used = 0
    budget_exhausted = False
    extracted = page.extract_tables() or []
    for raw_table in extracted[:MAX_TABLES_PER_PAGE]:
        remaining = MAX_TABLE_ROWS_PER_PAGE - rows_used
        if remaining <= 0:
            break
        bounded_rows: list[list[str]] = []
        truncated = len(raw_table) > remaining or len(raw_table) == 0
        for raw_row in raw_table[:remaining]:
            cells: list[str] = []
            if len(raw_row) > MAX_TABLE_COLUMNS:
                truncated = True
            for value in raw_row[:MAX_TABLE_COLUMNS]:
                cell, cell_truncated = _bounded_cell(value)
                truncated = truncated or cell_truncated
                char_budget = MAX_TABLE_CHARS_PER_PAGE - chars_used
                if char_budget <= 0:
                    truncated = True
                    budget_exhausted = True
                    break
                if len(cell) > char_budget:
                    cell = cell[:char_budget]
                    truncated = True
                    budget_exhausted = True
                chars_used += len(cell)
                cells.append(cell)
            if any(cells):
                bounded_rows.append(cells)
            if budget_exhausted:
                break
        rows_used += len(bounded_rows)
        if bounded_rows:
            tables.append(PdfTableCandidate(
                rows=bounded_rows,
                row_count=len(bounded_rows),
                column_count=max(len(row) for row in bounded_rows),
                is_truncated=truncated,
            ))
        if budget_exhausted:
            break
    if len(extracted) > MAX_TABLES_PER_PAGE:
        if tables:
            tables[-1].is_truncated = True
    return tables, rows_used


def extract_pdf_text(
    path: Path,
    pages: list[int],
    *,
    max_pages: int = MAX_PDF_PAGES,
    max_chars_per_page: int = MAX_TEXT_CHARS_PER_PAGE,
    extract_tables: bool = True,
) -> list[PdfTextPage]:
    """Extract bounded page text plus bounded pdfplumber table candidates."""

    reader = None
    try:
        reader = _reader(path)
    except PdfEncryptedError:
        raise
    except PdfRuntimeError:
        pass
    pdfplumber = _require_pdfplumber()
    output: list[PdfTextPage] = []
    try:
        with pdfplumber.open(str(path)) as document:
            selected = _selected_pages(len(document.pages), pages, max_pages=max_pages)
            for page_number in selected:
                raw_text = (
                    reader.pages[page_number - 1].extract_text() or ""
                    if reader is not None
                    else document.pages[page_number - 1].extract_text() or ""
                )
                text = raw_text[:max_chars_per_page]
                tables, table_rows = ([], 0)
                if extract_tables:
                    tables, table_rows = _extract_table_candidates(
                        document.pages[page_number - 1]
                    )
                output.append(PdfTextPage(
                    page=page_number,
                    text=text,
                    text_chars=_meaningful_chars(text),
                    is_truncated=len(raw_text) > max_chars_per_page,
                    tables=tables,
                    table_rows=table_rows,
                ))
    except PdfError:
        raise
    except Exception as exc:
        raise PdfRuntimeError(
            f"PDF extraction failed: {type(exc).__name__}: {exc}"
        ) from exc
    return output


def _unwrap_windows_command(command: str, name: str) -> str:
    path = Path(command)
    if os.name != "nt" or path.suffix.lower() not in {".cmd", ".bat"}:
        return command
    candidates = [
        path.parent.parent.parent / "native/poppler/Library/bin" / f"{name}.exe",
        path.parent.parent / "Library/bin" / f"{name}.exe",
    ]
    return str(next((candidate for candidate in candidates if candidate.is_file()), path))


def resolve_poppler_command(name: str, explicit: str = "") -> str:
    """Resolve a fixed Poppler executable; command arguments never use a shell."""

    candidate = explicit or shutil.which(name) or ""
    if not candidate:
        return ""
    unwrapped = _unwrap_windows_command(candidate, name)
    return unwrapped if Path(unwrapped).is_file() else ""


def _run_poppler(command: str, arguments: list[str], timeout_seconds: float) -> None:
    invocation = [command, *arguments]
    if os.name == "nt" and Path(command).suffix.lower() in {".cmd", ".bat"}:
        invocation = [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", command, *arguments]
    try:
        result = subprocess.run(
            invocation,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise PdfRuntimeError("Poppler page rendering timed out") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "unknown Poppler error")[:300]
        raise PdfRuntimeError(f"Poppler page rendering failed: {detail}")


def estimated_page_pixels(path: Path, pages: list[int], dpi: int) -> list[int]:
    """Estimate output pixels from PDF page boxes before invoking Poppler."""

    inspection = inspect_pdf(path, max_pages=MAX_PDF_PAGES)
    selected = _selected_pages(inspection.page_count, pages, max_pages=MAX_RENDER_PAGES)
    page_by_number = {page.page: page for page in inspection.pages}
    estimates: list[int] = []
    for page_number in selected:
        page = page_by_number[page_number]
        width = math.ceil(page.width_points / 72 * dpi)
        height = math.ceil(page.height_points / 72 * dpi)
        estimates.append(max(width, 1) * max(height, 1))
    return estimates


def render_pdf_pages(
    path: Path,
    pages: list[int],
    output_dir: Path,
    *,
    dpi: int = DEFAULT_RENDER_DPI,
    pdftoppm: str = "",
    max_pages: int = MAX_RENDER_PAGES,
    max_pixels_per_page: int,
    timeout_seconds: float = 180.0,
) -> list[RenderedPdfPage]:
    """Render only explicitly selected pages and verify every output image."""

    if not MIN_RENDER_DPI <= dpi <= MAX_RENDER_DPI:
        raise PdfLimitError(f"DPI must stay within {MIN_RENDER_DPI}..{MAX_RENDER_DPI}")
    inspection = inspect_pdf(path, max_pages=MAX_PDF_PAGES)
    selected = _selected_pages(inspection.page_count, pages, max_pages=max_pages)
    page_by_number = {page.page: page for page in inspection.pages}
    estimates = [
        math.ceil(page_by_number[page].width_points / 72 * dpi)
        * math.ceil(page_by_number[page].height_points / 72 * dpi)
        for page in selected
    ]
    if any(pixels > max_pixels_per_page for pixels in estimates):
        raise PdfLimitError(f"rendered page would exceed {max_pixels_per_page} pixels")
    command = resolve_poppler_command("pdftoppm", pdftoppm)
    if not command:
        raise PdfDependencyError("Poppler pdftoppm is required for PDF page rendering")
    source = inspect_evidence_file(path, max_bytes=path.stat().st_size, allowed_kinds={"pdf"})
    output_dir.mkdir(parents=True, exist_ok=True)
    rendered: list[RenderedPdfPage] = []
    created: list[Path] = []
    temporary: list[Path] = []
    try:
        for page_number in selected:
            output = output_dir / (
                f"{path.stem}-{source.sha256[:12]}-p{page_number:04d}-d{dpi}.png"
            )
            temp_prefix = output_dir / f".m5-render-{uuid.uuid4().hex}"
            temp_output = temp_prefix.with_suffix(".png")
            temporary.append(temp_output)
            started = time.perf_counter()
            _run_poppler(command, [
                "-f", str(page_number), "-l", str(page_number), "-singlefile",
                "-png", "-r", str(dpi), str(path), str(temp_prefix),
            ], timeout_seconds)
            latency_ms = max(0, round((time.perf_counter() - started) * 1000))
            if not temp_output.is_file():
                raise PdfRuntimeError(f"Poppler did not create page {page_number}")
            temp_inspection = inspect_evidence_file(
                temp_output, max_bytes=20 * 1024 * 1024, allowed_kinds={"png"}
            )
            if output.exists():
                existing = inspect_evidence_file(
                    output, max_bytes=20 * 1024 * 1024, allowed_kinds={"png"}
                )
                if existing.sha256 != temp_inspection.sha256:
                    raise PdfRuntimeError(
                        f"render output for page {page_number} already exists with other content"
                    )
                temp_output.unlink()
                temporary.remove(temp_output)
                inspected = existing
            else:
                temp_output.replace(output)
                temporary.remove(temp_output)
                created.append(output)
                inspected = temp_inspection
            try:
                from PIL import Image
            except ImportError as exc:
                raise PdfDependencyError(
                    "Pillow is required; install award-audit[m5-pdf]"
                ) from exc
            with Image.open(output) as image:
                width, height = image.size
                image.verify()
            pixels = width * height
            if pixels > max_pixels_per_page:
                raise PdfLimitError(
                    f"rendered page {page_number} exceeds {max_pixels_per_page} pixels"
                )
            rendered.append(RenderedPdfPage(
                page=page_number,
                path=output.resolve(),
                width=width,
                height=height,
                pixels=pixels,
                dpi=dpi,
                content_type=inspected.content_type,
                sha256=inspected.sha256,
                size_bytes=inspected.size_bytes,
                latency_ms=latency_ms,
            ))
    except Exception:
        for output in temporary:
            output.unlink(missing_ok=True)
        for output in created:
            output.unlink(missing_ok=True)
        raise
    return rendered
