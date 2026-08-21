"""Whitelist registry and fail-closed executor for M5 tools."""

from __future__ import annotations

import hashlib
import queue
import re
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Literal
from urllib.parse import unquote, urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

import award_audit.agent.toolkit.image as image_tools
import award_audit.agent.toolkit.pdf as pdf_tools
import award_audit.agent.toolkit.provenance as provenance
import award_audit.agent.toolkit.search as search_tools
import award_audit.agent.toolkit.spreadsheet as spreadsheet
import award_audit.agent.toolkit.web as web
from award_audit.agent.toolkit.contracts import (
    EvidenceArtifact,
    EvidenceFact,
    FactMatch,
    FactStatus,
    ToolBudgetLimits,
    ToolBudgetState,
    ToolKind,
    ToolObservation,
    ToolResult,
    ToolSpec,
    utc_now,
)
from award_audit.agent.toolkit.isolation import (
    IsolatedCallError,
    IsolatedCallTimeout,
    run_isolated,
)
from award_audit.agent.toolkit.safety import (
    SafetyError,
    inspect_evidence_file,
    validate_local_path,
)
from award_audit.core.identity import IDENTITY_VERSION, build_identities, normalize_identity

ToolHandler = Callable[[BaseModel, "ToolExecutionContext"], ToolResult | dict[str, Any]]

_SECRET_KEYS = ("api_key", "apikey", "authorization", "password", "secret", "token", "cookie")
_CONTENT_KEYS = {
    "candidates", "entries", "level", "name", "official_pages", "org", "queries",
    "query", "snippet", "submitted", "text", "title", "unreadable"
}
_MAX_TRACE_STRING = 300
_SECRET_QUERY = re.compile(
    r"([?&](?:api[_-]?key|access[_-]?token|token|signature|sig|key)=)[^&\s]+",
    re.IGNORECASE,
)
_MULTI_VALUE_SEPARATOR = re.compile(r"[;；、,/，]+")
_PERSON_GROUP_ROSTER = re.compile(
    r"(?P<names>[\u3400-\u9fff·]{2,30}(?:、[\u3400-\u9fff·]{2,30}){1,200})"
    r"等(?P<count>\d{1,4})名(?:同志)?和"
    r"(?P<group>[^。；]{2,100}?(?:群体|集体)(?:代表)?)"
    r"(?:光荣)?(?:入选|获奖)"
)


def _normalise_submitted_paths(
    submitted_path: Path | None,
    submitted_paths: list[Path],
) -> list[Path]:
    paths = [*submitted_paths]
    if submitted_path is not None and submitted_path not in paths:
        paths.insert(0, submitted_path)
    return list(dict.fromkeys(paths))[:20]


class ToolBudgetError(RuntimeError):
    """A media-specific case budget was exhausted before work started."""


def _redact_text(value: str) -> str:
    return _SECRET_QUERY.sub(r"\1[REDACTED]", value)


def _sanitize(value: Any, key: str = "") -> Any:
    """Bound trace values and remove common secret-bearing fields."""

    low_key = key.lower()
    if low_key == "headers" or any(secret in low_key for secret in _SECRET_KEYS):
        return "[REDACTED]"
    if low_key in _CONTENT_KEYS:
        if isinstance(value, (list, tuple, set, dict)):
            return {"content_redacted": True, "item_count": len(value)}
        return "[CONTENT_REDACTED]"
    if isinstance(value, dict):
        return {str(k)[:80]: _sanitize(v, str(k)) for k, v in list(value.items())[:30]}
    if isinstance(value, (list, tuple, set)):
        return [_sanitize(item) for item in list(value)[:30]]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, str):
        value = _redact_text(value)
        if len(value) <= _MAX_TRACE_STRING:
            return value
        return value[:_MAX_TRACE_STRING] + "...[truncated]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:_MAX_TRACE_STRING]


@dataclass
class ToolExecutionContext:
    """Per-case mutable execution state; never share it across audit cases."""

    allowed_roots: tuple[Path, ...]
    budget: ToolBudgetState = field(default_factory=ToolBudgetState)
    trace: list[ToolObservation] = field(default_factory=list)
    _started_monotonic: float = field(default_factory=time.monotonic, repr=False)
    _seen_pdf_pages: set[str] = field(default_factory=set, repr=False)

    @classmethod
    def create(
        cls,
        allowed_roots: Iterable[str | Path],
        limits: ToolBudgetLimits | None = None,
    ) -> ToolExecutionContext:
        roots = tuple(Path(root).resolve(strict=False) for root in allowed_roots)
        if not roots:
            raise ValueError("at least one allowed root is required")
        return cls(allowed_roots=roots,
                   budget=ToolBudgetState(limits=limits or ToolBudgetLimits()))

    @property
    def elapsed_seconds(self) -> float:
        return time.monotonic() - self._started_monotonic

    def add_candidate_urls(self, count: int) -> None:
        if count < 0:
            raise ValueError("candidate URL count cannot be negative")
        new_total = self.budget.candidate_urls + count
        if new_total > self.budget.limits.max_candidate_urls:
            raise ToolBudgetError("candidate URL budget exhausted")
        self.budget.candidate_urls = new_total

    def reserve_additional_downloads(self, count: int) -> None:
        """Account for extra files handled by one bounded composite Tool call."""

        if count < 0:
            raise ValueError("additional download count cannot be negative")
        new_total = self.budget.downloads + count
        if new_total > self.budget.limits.max_downloads:
            raise ToolBudgetError("download-count budget exhausted")
        self.budget.downloads = new_total

    def reserve_pdf_pages(self, document_sha256: str, pages: Iterable[int]) -> None:
        """Count each source page once across inspect/extract/render calls."""

        tokens = {f"{document_sha256}:{page}" for page in pages}
        unseen = tokens - self._seen_pdf_pages
        new_total = self.budget.pdf_pages + len(unseen)
        if new_total > self.budget.limits.max_pdf_pages:
            raise ToolBudgetError("PDF page budget exhausted")
        self._seen_pdf_pages.update(unseen)
        self.budget.pdf_pages = new_total

    def reserve_media_work(
        self,
        *,
        rendered_pages: int = 0,
        ocr_pages: int = 0,
        vision_pages: int = 0,
        image_pixels: int = 0,
    ) -> None:
        """Atomically reserve render/OCR/vision work and aggregate pixels."""

        values = {
            "rendered_pages": (rendered_pages, self.budget.limits.max_render_pages),
            "ocr_pages": (ocr_pages, self.budget.limits.max_ocr_pages),
            "vision_pages": (vision_pages, self.budget.limits.max_vision_pages),
            "image_pixels": (image_pixels, self.budget.limits.max_total_image_pixels),
        }
        for name, (amount, limit) in values.items():
            if amount < 0:
                raise ValueError(f"{name} reservation cannot be negative")
            if getattr(self.budget, name) + amount > limit:
                raise ToolBudgetError(f"{name.replace('_', ' ')} budget exhausted")
        for name, (amount, _limit) in values.items():
            setattr(self.budget, name, getattr(self.budget, name) + amount)


class ToolRegistry:
    """Explicit whitelist of tool names, schemas and handlers."""

    def __init__(self) -> None:
        self._entries: dict[str, tuple[ToolSpec, ToolHandler]] = {}

    def register(self, spec: ToolSpec, handler: ToolHandler) -> None:
        if spec.name in self._entries:
            raise ValueError(f"tool already registered: {spec.name}")
        self._entries[spec.name] = (spec, handler)

    def get(self, name: str) -> tuple[ToolSpec, ToolHandler] | None:
        return self._entries.get(name)

    def specs(self) -> list[ToolSpec]:
        return [entry[0] for entry in self._entries.values()]

    def openai_tools(self) -> list[dict[str, Any]]:
        return [spec.openai_schema() for spec in self.specs()]


class SafeToolExecutor:
    """Validate, budget, time, trace and contain every registered tool call."""

    def __init__(self, registry: ToolRegistry) -> None:
        self.registry = registry

    @staticmethod
    def _reserve(context: ToolExecutionContext, kind: ToolKind, name: str) -> str:
        limits = context.budget.limits
        budget = context.budget
        asset_tool = name in {
            "collect_spreadsheet_attachments", "inspect_pdf", "extract_pdf_text",
            "render_pdf_pages", "ocr_image", "verify_page_image_roster",
            "download_evidence", "inspect_evidence_file", "parse_spreadsheet_roster",
        }
        if context.elapsed_seconds >= limits.wall_time_seconds:
            return "wall-time budget exhausted"
        if asset_tool and budget.asset_calls >= limits.max_asset_calls:
            return "asset-tool budget exhausted"
        if not asset_tool and budget.calls - budget.asset_calls >= limits.max_calls:
            return "tool-call budget exhausted"
        if kind == "search" and budget.searches >= limits.max_searches:
            return "search budget exhausted"
        if kind == "download" and budget.downloads >= limits.max_downloads:
            return "download-count budget exhausted"
        budget.calls += 1
        if asset_tool:
            budget.asset_calls += 1
        if kind == "search":
            budget.searches += 1
        elif kind == "download":
            budget.downloads += 1
        return ""

    @staticmethod
    def _invoke(
        handler: ToolHandler,
        arguments: BaseModel,
        context: ToolExecutionContext,
        timeout_seconds: float,
    ) -> ToolResult:
        result_queue: queue.Queue[ToolResult | dict[str, Any] | BaseException]
        result_queue = queue.Queue(maxsize=1)

        def run() -> None:
            try:
                result_queue.put(handler(arguments, context))
            except BaseException as exc:  # contained and converted below
                result_queue.put(exc)

        worker = threading.Thread(target=run, name="award-audit-tool", daemon=True)
        worker.start()
        worker.join(timeout_seconds)
        if worker.is_alive():
            return ToolResult.failure("TOOL_TIMEOUT", f"tool exceeded {timeout_seconds:g}s timeout")
        raw = result_queue.get_nowait()
        if isinstance(raw, BaseException):
            if isinstance(raw, ToolBudgetError):
                return ToolResult.failure("TOOL_BUDGET_EXCEEDED", str(raw))
            if isinstance(raw, SafetyError):
                return ToolResult.failure(raw.code, _redact_text(str(raw)))
            return ToolResult.failure("TOOL_EXECUTION_ERROR",
                                      _redact_text(f"{type(raw).__name__}: {str(raw)[:300]}"))
        try:
            return raw if isinstance(raw, ToolResult) else ToolResult.model_validate(raw)
        except ValidationError as exc:
            return ToolResult.failure("TOOL_OUTPUT_INVALID", str(exc)[:500])

    def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolResult:
        started_at = utc_now()
        started = time.monotonic()
        call_id = uuid.uuid4().hex
        entry = self.registry.get(name)
        kind: ToolKind = entry[0].kind if entry is not None else "general"
        budget_error = self._reserve(context, kind, name)
        if budget_error:
            result = ToolResult.failure("TOOL_BUDGET_EXCEEDED", budget_error)
        elif entry is None:
            result = ToolResult.failure("TOOL_NOT_REGISTERED", f"tool is not registered: {name}")
        else:
            spec, handler = entry
            try:
                validated = spec.input_model.model_validate(arguments)
            except ValidationError as exc:
                result = ToolResult.failure("TOOL_INPUT_INVALID", str(exc)[:500])
            else:
                remaining = context.budget.limits.wall_time_seconds - context.elapsed_seconds
                timeout = min(spec.timeout_seconds, max(0.001, remaining))
                result = self._invoke(handler, validated, context, timeout)
                if not result.ok:
                    result.error_message = _redact_text(result.error_message)
                if result.ok and kind == "download":
                    downloaded = sum(artifact.size_bytes for artifact in result.artifacts)
                    context.budget.download_bytes += downloaded
        finished = time.monotonic()
        summary = {
            "data_keys": sorted(result.data)[:30],
            "source_url": result.source_url,
            "local_path": result.local_path,
            "content_type": result.content_type,
            "sha256": result.sha256,
            "is_truncated": result.is_truncated,
            "warning_count": len(result.warnings),
            "artifact_count": len(result.artifacts),
            "error_message": result.error_message,
        }
        verification_keys = {
            "observed_award_name",
            "observed_year",
            "award_name_match",
            "award_name_match_mode",
            "year_match",
            "source_level",
            "expected_count",
            "observed_count",
            "page_total_count",
            "coverage_complete",
            "candidate_count",
            "related_candidate_count",
            "official_candidate_count",
            "year_conflict_count",
            "year_conflict_candidates",
            "unqualified_candidate_count",
            "unqualified_candidates",
            "manual_required",
            "comparison_note",
            "next_evidence_stage",
            "attachment_count",
            "provider",
            "query",
            "strategy",
            "relationship_confirmed",
            "relationship_summary",
            "missing_item_count",
            "extra_item_count",
            "unresolved_item_count",
            "vision_error_count",
        }
        verification_facts = {
            key: result.data[key]
            for key in verification_keys
            if key in result.data
            and result.data[key] is not None
            and isinstance(result.data[key], (str, int, float, bool))
        }
        for key in (
            "matched_items",
            "split_matched_items",
            "missing_items",
            "extra_items",
            "unresolved_items",
            "attachment_names",
            "attachment_urls",
            "relationship_terms",
            "vision_error_pages",
            "vision_error_codes",
        ):
            value = result.data.get(key)
            if isinstance(value, list) and all(isinstance(item, str) for item in value):
                verification_facts[key] = value[:30]
        if verification_facts:
            summary["verification_facts"] = verification_facts
        context.trace.append(ToolObservation(
            call_id=call_id,
            tool_name=name,
            started_at=started_at,
            finished_at=utc_now(),
            duration_ms=max(0, round((finished - started) * 1000)),
            input_summary=_sanitize(arguments),
            output_summary=_sanitize(summary),
            ok=result.ok,
            error_code=result.error_code,
        ))
        return result


class FetchWebPageInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str
    max_chars: int = Field(default=web.MAX_TEXT_CHARS, ge=1, le=web.MAX_TEXT_CHARS)
    expected_award_name: str = Field(default="", max_length=200)
    award_aliases: list[str] = Field(default_factory=list, max_length=8)
    official_domains: list[str] = Field(default_factory=list, max_length=8)
    official_secondary_domains: list[str] = Field(default_factory=list, max_length=8)
    section_keywords: list[str] = Field(default_factory=list, max_length=8)
    section_exclude_keywords: list[str] = Field(default_factory=list, max_length=8)
    expected_year: str = Field(default="", max_length=20)
    submitted_path: Path | None = None
    submitted_paths: list[Path] = Field(default_factory=list, max_length=20)
    match_fields: list[str] = Field(default_factory=list, max_length=8)
    match_combine: Literal["first", "all"] = "first"
    expected_scope_count: int | None = Field(default=None, ge=0, le=1_000_000)
    page_total_count: int | None = Field(default=None, ge=0, le=1_000_000)
    relationship_terms: list[str] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def _coherent_comparison(self) -> FetchWebPageInput:
        self.submitted_paths = _normalise_submitted_paths(
            self.submitted_path, self.submitted_paths
        )
        if bool(self.submitted_paths) != bool(self.match_fields):
            raise ValueError("submitted paths and match_fields must be supplied together")
        if self.submitted_paths and self.submitted_path is None:
            self.submitted_path = self.submitted_paths[0]
        if any(
            not re.fullmatch(r"[A-Za-z0-9_]{1,40}", field)
            for field in self.match_fields
        ):
            raise ValueError("match_fields must contain bounded field codes")
        self.match_fields = list(dict.fromkeys(self.match_fields))
        if any(not item.strip() or len(item) > 80 for item in self.award_aliases):
            raise ValueError("award aliases must be bounded non-empty text")
        if any(not item.strip() or len(item) > 80 for item in self.section_keywords):
            raise ValueError("section keywords must be bounded non-empty text")
        if any(not item.strip() or len(item) > 80 for item in self.section_exclude_keywords):
            raise ValueError("section exclude keywords must be bounded non-empty text")
        if any(not item.strip() or len(item) > 80 for item in self.relationship_terms):
            raise ValueError("relationship terms must be bounded non-empty text")
        self.award_aliases = list(dict.fromkeys(item.strip() for item in self.award_aliases))
        self.section_keywords = list(dict.fromkeys(
            item.strip() for item in self.section_keywords
        ))
        self.section_exclude_keywords = list(dict.fromkeys(
            item.strip() for item in self.section_exclude_keywords
        ))
        self.relationship_terms = list(dict.fromkeys(
            item.strip() for item in self.relationship_terms
        ))
        return self


class DownloadEvidenceInput(BaseModel):
    url: str
    destination_dir: Path | None = None
    referer: str = ""
    max_bytes: int = Field(default=web.MAX_DOWNLOAD_BYTES, ge=1, le=web.MAX_DOWNLOAD_BYTES)


class VerifyPageImageRosterInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    page_url: str
    page_title: str = Field(default="", max_length=500)
    image_urls: list[str] = Field(min_length=1, max_length=image_tools.MAX_PAGE_IMAGES)
    submitted_path: Path | None = None
    submitted_paths: list[Path] = Field(default_factory=list, max_length=20)
    match_fields: list[str] = Field(min_length=1, max_length=8)
    match_combine: Literal["first", "all"] = "first"
    expected_award_name: str = Field(default="", max_length=200)
    award_aliases: list[str] = Field(default_factory=list, max_length=8)
    section_keywords: list[str] = Field(default_factory=list, max_length=8)
    section_exclude_keywords: list[str] = Field(default_factory=list, max_length=8)
    expected_year: str = Field(default="", max_length=20)
    official_domains: list[str] = Field(default_factory=list, max_length=8)
    official_secondary_domains: list[str] = Field(default_factory=list, max_length=8)
    expected_scope_count: int | None = Field(default=None, ge=0, le=1_000_000)
    scope_id: int = Field(default=0, ge=0)
    role_type: str = Field(default="", max_length=40)
    submitted_scope_filter: dict[str, str] = Field(default_factory=dict, max_length=12)
    submitted_scope_exclude: dict[str, list[str]] = Field(
        default_factory=dict, max_length=12
    )
    destination_dir: Path | None = None

    @model_validator(mode="after")
    def _bounded_metadata(self) -> VerifyPageImageRosterInput:
        self.submitted_paths = _normalise_submitted_paths(
            self.submitted_path, self.submitted_paths
        )
        if not self.submitted_paths:
            raise ValueError("at least one submitted path is required")
        if self.submitted_path is None:
            self.submitted_path = self.submitted_paths[0]
        if any(
            not re.fullmatch(r"[A-Za-z0-9_]{1,40}", field)
            for field in self.match_fields
        ):
            raise ValueError("match_fields must contain bounded field codes")
        for values, label in (
            (self.award_aliases, "award aliases"),
            (self.section_keywords, "section keywords"),
            (self.section_exclude_keywords, "section exclude keywords"),
        ):
            if any(not item.strip() or len(item) > 80 for item in values):
                raise ValueError(f"{label} must be bounded non-empty text")
        self.image_urls = list(dict.fromkeys(self.image_urls))
        self.match_fields = list(dict.fromkeys(self.match_fields))
        self.award_aliases = list(dict.fromkeys(item.strip() for item in self.award_aliases))
        self.section_keywords = list(dict.fromkeys(
            item.strip() for item in self.section_keywords
        ))
        self.section_exclude_keywords = list(dict.fromkeys(
            item.strip() for item in self.section_exclude_keywords
        ))
        return self


class CollectSpreadsheetAttachmentsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    page_urls: list[str] = Field(min_length=1, max_length=20)
    attachment_urls: list[str] = Field(default_factory=list, max_length=100)
    attachment_parent_urls: dict[str, str] = Field(default_factory=dict, max_length=100)
    submitted_path: Path | None = None
    submitted_paths: list[Path] = Field(default_factory=list, max_length=20)
    match_fields: list[str] = Field(
        min_length=1,
        max_length=8,
        description=(
            "Ordered submitted field codes; the first non-empty field on each row forms "
            "that row's deterministic identity."
        ),
    )
    match_combine: Literal["first", "all"] = "first"
    include_attachment_keywords: list[str] = Field(default_factory=list, max_length=8)
    exclude_attachment_keywords: list[str] = Field(default_factory=list, max_length=8)
    destination_dir: Path | None = None
    max_attachments: int = Field(default=100, ge=1, le=100)
    max_rows_per_file: int = Field(
        default=spreadsheet.MAX_EXCEL_ROWS_PER_SHEET,
        ge=1,
        le=spreadsheet.MAX_EXCEL_ROWS_PER_SHEET,
    )
    expected_award_name: str = Field(default="", max_length=200)
    award_aliases: list[str] = Field(default_factory=list, max_length=8)
    expected_year: str = Field(default="", max_length=20)
    expected_scope_count: int | None = Field(default=None, ge=0, le=1_000_000)
    scope_id: int = Field(default=0, ge=0)
    role_type: str = Field(default="", max_length=40)
    submitted_scope_filter: dict[str, str] = Field(default_factory=dict, max_length=12)
    submitted_scope_exclude: dict[str, list[str]] = Field(
        default_factory=dict, max_length=12
    )
    official_domains: list[str] = Field(default_factory=list, max_length=8)
    official_secondary_domains: list[str] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def _bounded_metadata(self) -> CollectSpreadsheetAttachmentsInput:
        self.submitted_paths = _normalise_submitted_paths(
            self.submitted_path, self.submitted_paths
        )
        if not self.submitted_paths:
            raise ValueError("at least one submitted path is required")
        if self.submitted_path is None:
            self.submitted_path = self.submitted_paths[0]
        if any(
            not re.fullmatch(r"[A-Za-z0-9_]{1,40}", field)
            for field in self.match_fields
        ):
            raise ValueError("match_fields must contain bounded field codes")
        for keywords in (
            self.include_attachment_keywords,
            self.exclude_attachment_keywords,
        ):
            if any(
                not keyword.strip()
                or len(keyword) > 80
                or any(ord(char) < 32 for char in keyword)
                for keyword in keywords
            ):
                raise ValueError("attachment keywords must be non-empty bounded text")
        self.match_fields = list(dict.fromkeys(self.match_fields))
        self.include_attachment_keywords = list(dict.fromkeys(
            keyword.strip() for keyword in self.include_attachment_keywords
        ))
        self.exclude_attachment_keywords = list(dict.fromkeys(
            keyword.strip() for keyword in self.exclude_attachment_keywords
        ))
        self.award_aliases = list(dict.fromkeys(
            alias.strip() for alias in self.award_aliases if alias.strip()
        ))
        return self


class ParseSpreadsheetInput(BaseModel):
    path: Path
    max_rows: int = Field(
        default=spreadsheet.MAX_EXCEL_ROWS_PER_SHEET,
        ge=1,
        le=spreadsheet.MAX_EXCEL_ROWS_PER_SHEET,
    )


class InspectPdfInput(BaseModel):
    path: Path
    max_pages: int = Field(default=pdf_tools.MAX_PDF_PAGES, ge=1,
                           le=pdf_tools.MAX_PDF_PAGES)


class ExtractPdfTextInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: Path
    pages: list[int] = Field(min_length=1, max_length=pdf_tools.MAX_PDF_PAGES)
    max_chars_per_page: int = Field(default=pdf_tools.MAX_TEXT_CHARS_PER_PAGE,
                                    ge=100, le=pdf_tools.MAX_TEXT_CHARS_PER_PAGE)
    extract_tables: bool = True
    submitted_path: Path | None = None
    submitted_paths: list[Path] = Field(default_factory=list, max_length=20)
    match_fields: list[str] = Field(default_factory=list, max_length=8)
    match_combine: Literal["first", "all"] = "first"
    expected_award_name: str = Field(default="", max_length=200)
    award_aliases: list[str] = Field(default_factory=list, max_length=8)
    expected_year: str = Field(default="", max_length=20)
    expected_scope_count: int | None = Field(default=None, ge=0, le=1_000_000)
    scope_id: int = Field(default=0, ge=0)
    role_type: str = Field(default="", max_length=40)
    submitted_scope_filter: dict[str, str] = Field(default_factory=dict, max_length=12)
    submitted_scope_exclude: dict[str, list[str]] = Field(
        default_factory=dict, max_length=12
    )
    section_keywords: list[str] = Field(default_factory=list, max_length=8)
    section_exclude_keywords: list[str] = Field(default_factory=list, max_length=8)
    source_url: str = Field(default="", max_length=2048)
    official_domains: list[str] = Field(default_factory=list, max_length=8)
    official_secondary_domains: list[str] = Field(default_factory=list, max_length=8)
    parent_page_url: str = Field(default="", max_length=2048)
    parent_attachment_linked: bool = False
    parent_award_name: str = Field(default="", max_length=200)
    parent_year: str = Field(default="", max_length=20)
    parent_source_level: str = Field(default="unknown", max_length=80)

    @model_validator(mode="after")
    def _unique_pages(self) -> ExtractPdfTextInput:
        if len(set(self.pages)) != len(self.pages):
            raise ValueError("pages must be unique")
        self.submitted_paths = _normalise_submitted_paths(
            self.submitted_path, self.submitted_paths
        )
        if bool(self.submitted_paths) != bool(self.match_fields):
            raise ValueError("submitted paths and match_fields must be supplied together")
        if self.submitted_paths and self.submitted_path is None:
            self.submitted_path = self.submitted_paths[0]
        if any(
            not re.fullmatch(r"[A-Za-z0-9_]{1,40}", field)
            for field in self.match_fields
        ):
            raise ValueError("match_fields must contain bounded field codes")
        return self


class RenderPdfPagesInput(BaseModel):
    path: Path
    pages: list[int] = Field(min_length=1, max_length=pdf_tools.MAX_RENDER_PAGES)
    output_dir: Path | None = None
    dpi: int = Field(default=pdf_tools.DEFAULT_RENDER_DPI,
                     ge=pdf_tools.MIN_RENDER_DPI, le=pdf_tools.MAX_RENDER_DPI)
    source_url: str = Field(default="", max_length=2048)

    @model_validator(mode="after")
    def _unique_pages(self) -> RenderPdfPagesInput:
        if len(set(self.pages)) != len(self.pages):
            raise ValueError("pages must be unique")
        return self


class OcrImageInput(BaseModel):
    images: list[image_tools.ImagePageRef] = Field(
        min_length=1, max_length=image_tools.MAX_OCR_IMAGES
    )

    @model_validator(mode="after")
    def _coherent_pages(self) -> OcrImageInput:
        pages = [image.page for image in self.images]
        totals = {image.total_pages for image in self.images}
        if len(set(pages)) != len(pages) or len(totals) != 1:
            raise ValueError("images require unique pages and one total_pages value")
        return self


class VisionExtractRosterInput(BaseModel):
    images: list[image_tools.ImagePageRef] = Field(
        min_length=1, max_length=image_tools.MAX_VISION_IMAGES
    )
    ocr_text_by_page: dict[int, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _coherent_pages(self) -> VisionExtractRosterInput:
        pages = [image.page for image in self.images]
        totals = {image.total_pages for image in self.images}
        if len(set(pages)) != len(pages) or len(totals) != 1:
            raise ValueError("images require unique pages and one total_pages value")
        if any(page not in set(pages) for page in self.ocr_text_by_page):
            raise ValueError("OCR context may reference only pages in this vision batch")
        if any(len(text) > 8000 for text in self.ocr_text_by_page.values()):
            raise ValueError("OCR context exceeds the per-page character limit")
        return self


class CompareRosterInput(BaseModel):
    submitted: list[image_tools.RosterEntry] = Field(max_length=5000)
    official_pages: list[image_tools.VisionRosterPage] = Field(
        min_length=1, max_length=pdf_tools.MAX_PDF_PAGES
    )
    expected_total: int | None = Field(default=None, ge=0, le=5000)
    expected_first_no: int = Field(default=1, ge=1, le=1_000_000)

    @model_validator(mode="after")
    def _bounded_official_entries(self) -> CompareRosterInput:
        pages = [page.page for page in self.official_pages]
        entries = sum(len(page.entries) for page in self.official_pages)
        if len(set(pages)) != len(pages):
            raise ValueError("official_pages must have unique page numbers")
        if entries > 5000:
            raise ValueError("official roster exceeds 5000 entries")
        return self


class SearchOfficialAwardInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    award_name: str = Field(min_length=1, max_length=100)
    year: str = Field(default="", max_length=12, pattern=r"^$|^[0-9]{4}(?:-[0-9]{4})?$")
    organizer: str = Field(default="", max_length=80)
    award_type: str = Field(default="", max_length=40)
    session: str = Field(default="", max_length=40)
    english_name: str = Field(default="", max_length=100)
    strategy: Literal[
        "broad", "site", "international", "discrepancy", "attachment"
    ] = "broad"
    site_domains: list[str] = Field(default_factory=list, max_length=8)
    official_domains: list[str] = Field(default_factory=list, max_length=8)
    official_secondary_domains: list[str] = Field(default_factory=list, max_length=8)
    exclude_urls: list[str] = Field(default_factory=list, max_length=20)
    discrepancy_terms: list[str] = Field(default_factory=list, max_length=8)
    recovery_terms: list[str] = Field(default_factory=list, max_length=4)
    require_award_name_match: bool = False
    max_results: int = Field(default=5, ge=1, le=8)

    @model_validator(mode="after")
    def _normalize_and_validate(self) -> SearchOfficialAwardInput:
        values = [
            self.award_name,
            self.organizer,
            self.award_type,
            self.session,
            self.english_name,
        ]
        if any(any(ord(char) < 32 for char in value) for value in values):
            raise ValueError("search metadata cannot contain control characters")
        self.official_domains = list(dict.fromkeys(
            provenance.normalize_domain(item) for item in self.official_domains
        ))
        self.site_domains = list(dict.fromkeys(
            provenance.normalize_domain(item) for item in self.site_domains
        ))
        self.official_secondary_domains = list(dict.fromkeys(
            provenance.normalize_domain(item) for item in self.official_secondary_domains
        ))
        if self.strategy == "site" and not self.site_domains:
            self.site_domains = list(self.official_domains)
        if self.strategy == "site" and not self.site_domains:
            raise ValueError("site strategy requires at least one site domain")
        if self.strategy == "discrepancy" and len(self.discrepancy_terms) < 2:
            raise ValueError("discrepancy strategy requires at least two public terms")
        if any(not item.strip() or len(item) > 80 for item in self.discrepancy_terms):
            raise ValueError("discrepancy terms must be bounded non-empty text")
        self.discrepancy_terms = list(dict.fromkeys(
            item.strip() for item in self.discrepancy_terms
        ))
        if any(
            not item.strip()
            or len(item) > 80
            or any(char.isspace() or ord(char) < 32 for char in item)
            or any(char in "/\\?#&=" for char in item)
            for item in self.recovery_terms
        ):
            raise ValueError("recovery terms must be bounded public URL file names")
        self.recovery_terms = list(dict.fromkeys(
            item.strip() for item in self.recovery_terms
        ))
        return self


class ExtractSearchDocumentInput(BaseModel):
    """Bounded third-party extraction fallback after direct official fetch failure."""

    model_config = ConfigDict(extra="forbid")

    url: str
    search_query: str = Field(default="", max_length=100)
    expected_award_name: str = Field(default="", max_length=200)
    award_aliases: list[str] = Field(default_factory=list, max_length=8)
    expected_year: str = Field(default="", max_length=20)
    submitted_path: Path | None = None
    submitted_paths: list[Path] = Field(default_factory=list, max_length=20)
    match_fields: list[str] = Field(default_factory=list, max_length=8)
    match_combine: Literal["first", "all"] = "first"
    expected_scope_count: int | None = Field(default=None, ge=0, le=1_000_000)
    page_total_count: int | None = Field(default=None, ge=0, le=1_000_000)
    section_keywords: list[str] = Field(default_factory=list, max_length=8)
    relationship_terms: list[str] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def _coherent_extraction(self) -> ExtractSearchDocumentInput:
        self.submitted_paths = _normalise_submitted_paths(
            self.submitted_path, self.submitted_paths
        )
        if bool(self.submitted_paths) != bool(self.match_fields):
            raise ValueError("submitted paths and match_fields must be supplied together")
        if self.submitted_paths and self.submitted_path is None:
            self.submitted_path = self.submitted_paths[0]
        if any(not item.strip() or len(item) > 80 for item in self.section_keywords):
            raise ValueError("section keywords must be bounded non-empty text")
        if any(not item.strip() or len(item) > 80 for item in self.award_aliases):
            raise ValueError("award aliases must be bounded non-empty text")
        if any(not item.strip() or len(item) > 80 for item in self.relationship_terms):
            raise ValueError("relationship terms must be bounded non-empty text")
        self.relationship_terms = list(dict.fromkeys(
            item.strip() for item in self.relationship_terms
        ))
        return self


def _attachment_search_anchor(award_name: str) -> str:
    """Broaden attachment discovery by removing generic scope/stage labels."""

    anchor = award_name.strip()
    if anchor.startswith("全国") and len(anchor) - len("全国") >= 4:
        anchor = anchor[len("全国") :]
    for suffix in ("评选活动", "年度人物", "候选人名单", "获奖名单", "认定名单"):
        if anchor.endswith(suffix) and len(anchor) - len(suffix) >= 4:
            anchor = anchor[: -len(suffix)]
    return anchor or award_name.strip()


def _official_search_query(args: SearchOfficialAwardInput) -> str:
    if args.strategy == "site":
        parts = [
            f"site:{args.site_domains[0]}",
            args.award_name,
            args.year,
            *args.recovery_terms,
            args.session,
            "获奖名单 公示",
        ]
    elif args.strategy == "international":
        parts = [
            args.english_name or args.award_name,
            args.year,
            *args.recovery_terms,
            "winners official",
        ]
    elif args.strategy == "discrepancy":
        parts = [args.award_name, args.year, *args.discrepancy_terms, "对应关系"]
    elif args.strategy == "attachment":
        parts = [
            args.award_name,
            args.year,
            *args.recovery_terms,
            "名单",
        ]
    else:
        parts = [
            args.award_name,
            args.year,
            *args.recovery_terms,
            args.session,
            args.organizer,
            "获奖名单 公示 附件 xlsx pdf",
        ]
    query = " ".join(part.strip() for part in parts if part.strip())
    if len(query) > search_tools.MAX_QUERY_CHARS:
        query = query[:search_tools.MAX_QUERY_CHARS].rstrip()
    return query


def _match_award_title(
    expected: str,
    title: str,
    *,
    aliases: list[str] | tuple[str, ...] = (),
) -> tuple[bool, str]:
    """Match harmless title variants while retaining a conservative similarity floor."""

    wanted = _normalise_match(expected)
    observed = _normalise_match(title)
    if not wanted or not observed:
        return False, "missing"
    if wanted in observed or observed in wanted:
        return True, "exact"
    for alias in aliases:
        normalized_alias = _normalise_match(alias)
        if normalized_alias and normalized_alias in observed:
            return True, "configured_alias"
    for prefix in ("全国",):
        if wanted.startswith(prefix):
            core_name = wanted[len(prefix):]
            if len(core_name) >= 6 and core_name in observed:
                return True, "scope_variant"
    concept_core = wanted
    for prefix in ("国家级", "全国", "中国"):
        if concept_core.startswith(prefix):
            concept_core = concept_core[len(prefix):]
            break
    for suffix in (
        "候选人名单",
        "获奖名单",
        "认定名单",
        "年度人物",
        "评选活动",
        "推选活动",
        "评选",
        "推选",
        "竞赛",
        "大赛",
        "名单",
        "奖",
    ):
        if concept_core.endswith(suffix):
            concept_core = concept_core[: -len(suffix)]
            break
    result_stage = any(
        marker in observed
        for marker in ("公示", "公布", "候选", "名单", "获奖", "入选", "表彰", "认定")
    )
    if len(concept_core) >= 5 and concept_core in observed and result_stage:
        return True, "semantic_core"
    matcher = SequenceMatcher(a=wanted, b=observed, autojunk=False)
    longest = matcher.find_longest_match().size
    if longest >= 8 and longest >= round(min(len(wanted), len(observed)) * 0.4):
        return True, "shared_core"
    ratio = matcher.ratio()
    required_overlap = max(4, round(min(len(wanted), len(observed)) * 0.6))
    if ratio >= 0.68 and longest >= required_overlap:
        return True, "fuzzy"
    return False, "none"


def _is_related_award_lead(expected: str, title: str) -> bool:
    """Keep a strong shared award concept as a lead, never as evidence."""

    wanted = _normalise_match(expected)
    observed = _normalise_match(title)
    if not wanted or not observed:
        return False
    match = SequenceMatcher(a=wanted, b=observed, autojunk=False).find_longest_match()
    if match.size < 5 or match.size / len(wanted) < 0.45:
        return False
    shared = wanted[match.a : match.a + match.size]
    generic_fragments = {
        "全国高校",
        "年度人物",
        "候选人名单",
        "获奖名单",
        "认定名单",
    }
    return shared not in generic_fragments


def _matches_result_stage(text: str) -> bool:
    """Require evidence about a result, not merely an application workflow."""

    normalized = _normalise_match(text)
    application_terms = (
        "申报表",
        "推荐名额",
        "推荐汇总表",
        "网上提交",
        "材料报送",
        "开展创建",
        "开展申报",
        "创建指标",
    )
    has_publication_action = bool(
        "现予以公示" in normalized
        or "关于公布" in normalized
        or ("现将" in normalized and "予以公布" in normalized)
        or ("拟确定" in normalized and "公示" in normalized)
    )
    if (
        any(_normalise_match(term) in normalized for term in application_terms)
        and not has_publication_action
    ):
        return False
    result_terms = (
        "获奖",
        "名单",
        "结果",
        "公示",
        "公布",
        "入选",
        "入围",
        "认定",
        "表彰",
        "授予",
        "奖励决定",
        "winners",
        "awardees",
    )
    return any(_normalise_match(term) in normalized for term in result_terms)


def _fact_status(
    target_match: bool,
    year_match: bool,
    coverage_complete: bool | None,
) -> FactStatus:
    if not target_match or not year_match:
        return "conflict"
    if coverage_complete is True:
        return "complete"
    if coverage_complete is False:
        return "partial"
    return "unverified"


def _fetch_web_page(arguments: BaseModel, context: ToolExecutionContext) -> ToolResult:
    args = FetchWebPageInput.model_validate(arguments)
    page = web.fetch_page(args.url)
    if page.status != 200:
        return ToolResult.failure("HTTP_ERROR", f"HTTP {page.status}", source_url=page.url,
                                  fetched_at=utc_now())
    page_data = page.model_dump(mode="json")
    page_data["text"] = page.text[:args.max_chars]
    text_truncated = page.text_truncated or len(page.text) > args.max_chars
    page_data["text_truncated"] = text_truncated
    page_data["original_text_chars"] = page.original_text_chars or len(page.text)
    assessment = provenance.classify_source(
        page.url,
        official_domains=args.official_domains,
        official_secondary_domains=args.official_secondary_domains,
    )
    structured: dict[str, Any] = {
        "source_level": assessment.level,
        "source_reason": assessment.reason,
    }
    if page.attachments:
        structured.update({
            "attachment_count": len(page.attachments),
            "attachment_names": [
                item.text or item.url.rsplit("/", 1)[-1]
                for item in page.attachments[:100]
            ],
            "attachment_urls": [item.url for item in page.attachments[:100]],
            "attachment_discovery_truncated": len(page.attachments) > 100,
        })
    normalized_text = _normalise_match(page.text)
    normalized_page = _normalise_match(f"{page.title} {page.text}")
    relationship_confirmed: bool | None = None
    relationship_summary = ""
    if args.relationship_terms:
        relationship_confirmed = all(
            _normalise_match(term) in normalized_page for term in args.relationship_terms
        )
        structured["relationship_terms"] = args.relationship_terms
        structured["relationship_confirmed"] = relationship_confirmed
        if relationship_confirmed:
            relationship_summary = (
                "该补证来源同时出现差异姓名与群体名称，支持二者属于同一业务名额的对应关系"
            )
            structured["relationship_summary"] = relationship_summary
    award_matches, award_match_mode = _match_award_title(
        args.expected_award_name, page.title, aliases=args.award_aliases
    )
    if args.expected_award_name and award_matches:
        structured["observed_award_name"] = args.expected_award_name
        structured["award_name_match"] = True
        structured["award_name_match_mode"] = award_match_mode
    elif args.expected_award_name:
        structured["award_name_match"] = False
    if args.expected_year and args.expected_year in web.extract_years(
        f"{page.title} {page.text}"
    ):
        structured["observed_year"] = args.expected_year
        structured["year_match"] = True
    elif args.expected_year:
        structured["year_match"] = False

    expected_count: int | None = args.expected_scope_count
    observed_count: int | None = None
    coverage_complete: bool | None = None
    missing_evidence: list[str] = []
    section_hits = [
        keyword
        for keyword in args.section_keywords
        if _normalise_match(keyword) in _normalise_match(f"{page.title} {page.text}")
    ]
    structured["section_hits"] = section_hits
    if args.page_total_count is not None:
        structured["page_total_count"] = args.page_total_count
    matched_items: list[str] = []
    split_matched_items: list[str] = []
    missing_items: list[str] = []
    extra_items: list[str] = []
    unresolved_items: list[str] = []
    contradictions: list[str] = []
    if args.submitted_paths:
        identities = _submitted_match_items_from_paths(
            args.submitted_paths,
            args.match_fields,
            context,
            match_combine=args.match_combine,
        )
        submitted_atomic: dict[str, str] = {}
        for _field, normalized, display in identities:
            parts = _identity_parts(display, normalized)
            for normalized_part, display_part in parts:
                submitted_atomic.setdefault(normalized_part, display_part)
            absent = [
                display_part
                for normalized_part, display_part in parts
                if normalized_part not in normalized_text
            ]
            if absent:
                missing_items.extend(absent)
            else:
                matched_items.append(display)
                if len(parts) > 1:
                    split_matched_items.append(display)
        official_items = _extract_person_group_roster(page.text, expected=args.expected_scope_count)
        if official_items:
            extra_items = [
                display
                for display in official_items
                if _normalise_match(display) not in submitted_atomic
            ]
            if extra_items and missing_items:
                structured["comparison_note"] = "来源使用群体名额，提交材料使用个人姓名"
                contradictions.append(
                    "来源使用群体名额，提交材料使用个人姓名，需人工确认对应关系"
                )
        matched = len(matched_items)
        submitted_count = len(identities)
        expected = max(submitted_count, args.expected_scope_count or 0)
        expected_count = expected
        observed_count = matched
        coverage_complete = expected > 0 and matched >= expected and not extra_items
        structured.update({
            "identity_version": IDENTITY_VERSION,
            "expected_count": expected,
            "observed_count": matched,
            "submitted_count": submitted_count,
            "reference_count": args.expected_scope_count,
            "submitted_match_count": matched,
            "submitted_match_total": submitted_count,
            "coverage_complete": coverage_complete,
            "matched_items": matched_items[:10_000],
            "split_matched_items": split_matched_items[:10_000],
            "missing_items": missing_items[:10_000],
            "extra_items": extra_items,
            "missing_item_count": len(missing_items),
            "extra_item_count": len(extra_items),
        })
        if args.expected_scope_count and args.expected_scope_count > submitted_count:
            missing_evidence.append(
                f"目标分组口径为{args.expected_scope_count}，提交仅{submitted_count}条"
            )
            structured["scope_gap"] = args.expected_scope_count - submitted_count
    if args.section_keywords and len(section_hits) != len(args.section_keywords):
        missing_evidence.append("页面中未完整定位目标名单分组")
        coverage_complete = False if coverage_complete is not None else None
        structured["coverage_complete"] = coverage_complete
    if page.attachments and coverage_complete is not True:
        coverage_complete = False
        structured["coverage_complete"] = False
        unresolved_items = missing_items
        missing_items = []
        missing_evidence.append(
            "页面存在尚未解析的附件，正文匹配不能单独证明完整名单，"
            "需下载并按实际文件类型继续核验"
        )
        structured["next_evidence_stage"] = "spreadsheet_processing"
        structured["candidate_attachment_urls"] = [
            item.url for item in page.attachments[:100]
        ]
    elif page.related_pages and coverage_complete is not True:
        unresolved_items = missing_items
        missing_items = []
        missing_evidence.append(
            "当前页面为通知列表或赛事入口，需继续核验相关详情页中的名单附件"
        )
        structured["next_evidence_stage"] = "page_recovery"
        structured["candidate_page_urls"] = [
            item.url for item in page.related_pages[:20]
        ]
        structured["candidate_page_titles"] = [
            item.text for item in page.related_pages[:20]
        ]
    elif page.images and coverage_complete is not True:
        coverage_complete = False
        structured["coverage_complete"] = False
        unresolved_items = missing_items
        missing_items = []
        missing_evidence.append(
            "页面存在尚未解析的候选图片，正文匹配不能单独证明完整名单，"
            "需按目标分组继续进行 OCR/Vision 提取"
        )
        structured["next_evidence_stage"] = "image_processing"
        structured["candidate_image_urls"] = page.images[:100]
        structured["image_count"] = len(page.images)
        structured["image_discovery_truncated"] = len(page.images) > 100
    elif missing_items:
        missing_evidence.append(
            f"提交名单有、该来源未找到：{'、'.join(missing_items[:20])}"
        )
    if extra_items:
        missing_evidence.append(
            f"该来源有、提交名单未提供：{'、'.join(extra_items[:20])}"
        )
    if text_truncated:
        coverage_complete = False
        missing_evidence.append("网页正文已截断，无法证明名单完整")
        structured["coverage_complete"] = False
    structured.update({
        "missing_items": missing_items[:10_000],
        "unresolved_items": unresolved_items[:10_000],
        "missing_item_count": len(missing_items),
        "unresolved_item_count": len(unresolved_items),
    })
    data = {**structured, **page_data}
    target_state = bool(structured.get("award_name_match"))
    year_state = bool(structured.get("year_match"))
    fact = EvidenceFact(
        status=_fact_status(target_state, year_state, coverage_complete),
        award_name=(args.expected_award_name if target_state else ""),
        year=(args.expected_year if year_state else ""),
        target_match="yes" if target_state else "no",
        year_match="yes" if year_state else "no",
        source_url=page.url,
        source_level=assessment.level,
        expected_count=expected_count,
        observed_count=observed_count,
        submitted_count=(len(identities) if args.submitted_paths else None),
        reference_count=args.page_total_count,
        coverage_complete=coverage_complete,
        document_complete=bool(
            target_state and year_state and not text_truncated
            and args.submitted_paths and observed_count is not None
            and not unresolved_items
        ),
        extraction_method="direct_html",
        comparison_scope=(
            args.section_keywords[0] if args.section_keywords else "submitted_roster"
        ),
        matched_items=matched_items[:10_000],
        split_matched_items=split_matched_items[:10_000],
        missing_items=missing_items[:10_000],
        extra_items=extra_items,
        unresolved_items=unresolved_items[:10_000],
        missing_item_count=len(missing_items),
        extra_item_count=len(extra_items),
        unresolved_item_count=len(unresolved_items),
        missing_evidence=missing_evidence,
        contradictions=contradictions,
        relationship_terms=args.relationship_terms,
        relationship_confirmed=relationship_confirmed,
        relationship_summary=relationship_summary,
    )
    data["document_complete"] = fact.document_complete
    return ToolResult(
        ok=True,
        data=data,
        source_url=page.url,
        fetched_at=utc_now(),
        is_truncated=text_truncated,
        evidence_facts=[fact],
    )


def _download_evidence(arguments: BaseModel, context: ToolExecutionContext) -> ToolResult:
    args = DownloadEvidenceInput.model_validate(arguments)
    destination = validate_local_path(args.destination_dir or context.allowed_roots[0],
                                      context.allowed_roots,
                                      must_exist=False, file_only=False)
    remaining = context.budget.limits.max_total_download_bytes - context.budget.download_bytes
    if remaining <= 0:
        return ToolResult.failure("TOOL_BUDGET_EXCEEDED", "download-byte budget exhausted")
    max_bytes = min(args.max_bytes, context.budget.limits.max_file_bytes, remaining)
    path = web.download_file(args.url, destination, referer=args.referer, max_bytes=max_bytes)
    safe_path = validate_local_path(path, context.allowed_roots, file_only=True)
    inspection = inspect_evidence_file(safe_path, max_bytes=max_bytes)
    fetched_at = utc_now()
    artifact = EvidenceArtifact(
        kind=inspection.kind,
        source_url=args.url,
        local_path=str(safe_path),
        content_type=inspection.content_type,
        sha256=inspection.sha256,
        size_bytes=inspection.size_bytes,
        fetched_at=fetched_at,
    )
    data: dict[str, Any] = {
        "size_bytes": inspection.size_bytes,
        "kind": inspection.kind,
    }
    warnings: list[str] = []
    if inspection.kind == "pdf":
        pdf_inspection = _inspect_pdf(
            InspectPdfInput(path=safe_path),
            context,
        )
        if pdf_inspection.ok:
            data["pdf_inspection"] = pdf_inspection.data
            page_count = int(pdf_inspection.data.get("page_count", 0) or 0)
            processed_pages = len(pdf_inspection.data.get("digital_pages", [])) + len(
                pdf_inspection.data.get("scan_candidate_pages", [])
            )
            artifact.metadata.update({
                "automatic_pdf_inspection": True,
                "total_pages": page_count,
                "processed_pages": processed_pages,
                "inspection_truncated": bool(
                    pdf_inspection.data.get("truncated", False)
                ),
            })
        else:
            data["pdf_inspection_error_code"] = pdf_inspection.error_code
            warnings.append("automatic_pdf_inspection_failed")
    return ToolResult(
        ok=True,
        data=data,
        source_url=args.url,
        local_path=str(safe_path),
        content_type=inspection.content_type,
        sha256=inspection.sha256,
        fetched_at=fetched_at,
        artifacts=[artifact],
        warnings=warnings,
    )


def _normalise_match(value: object) -> str:
    return normalize_identity(value)


def _identity_matches(display: str, normalized: str, source_text: str) -> tuple[bool, bool]:
    parts = [
        _normalise_match(part)
        for part in _MULTI_VALUE_SEPARATOR.split(display)
        if _normalise_match(part)
    ]
    if len(parts) > 1:
        return all(part in source_text for part in parts), True
    return normalized in source_text, False


def _identity_parts(display: str, normalized: str) -> list[tuple[str, str]]:
    raw_parts = [
        part.strip()
        for part in _MULTI_VALUE_SEPARATOR.split(display)
        if part.strip()
    ]
    if len(raw_parts) <= 1:
        return [(normalized, display)]
    return [(_normalise_match(part), part) for part in raw_parts]


def _extract_person_group_roster(text: str, *, expected: int | None) -> list[str]:
    """Extract a bounded `N people plus one group` roster only when counts agree."""

    for match in _PERSON_GROUP_ROSTER.finditer(text or ""):
        names = [item.strip() for item in match.group("names").split("、") if item.strip()]
        stated = int(match.group("count"))
        group = match.group("group").strip()
        if len(names) != stated or not group:
            continue
        items = [*names, group]
        if expected is None or len(items) == expected:
            return items
    return []


def _submitted_match_items(
    grid: dict[str, object],
    match_fields: list[str],
    *,
    match_combine: Literal["first", "all"] = "first",
    scope_filter: Mapping[str, str] | None = None,
    scope_exclude: Mapping[str, list[str]] | None = None,
) -> list[tuple[str, str, str]]:
    row_maps: list[dict[str, object]] = []

    def collect_rows(raw_grid: dict[str, object]) -> None:
        sheet_grids = raw_grid.get("sheet_grids")
        if isinstance(sheet_grids, list) and sheet_grids:
            for raw_sheet in sheet_grids:
                if isinstance(raw_sheet, dict):
                    collect_rows(raw_sheet)
            return
        rows = raw_grid.get("rows", [])
        if not isinstance(rows, list) or len(rows) < 3 or not isinstance(rows[0], list):
            return
        codes = [str(item) for item in rows[0]]
        for raw_row in rows[2:]:
            if not isinstance(raw_row, list):
                continue
            row_maps.append({
                field: raw_row[index] if index < len(raw_row) else ""
                for index, field in enumerate(codes)
            })

    collect_rows(grid)
    expected_scope = {
        str(field): _normalise_match(value)
        for field, value in (scope_filter or {}).items() if str(value).strip()
    }
    if expected_scope:
        row_maps = [row for row in row_maps if all(
            _normalise_match(row.get(field, "")) == expected
            for field, expected in expected_scope.items()
        )]
    excluded_scope = {
        str(field): {_normalise_match(value) for value in values if str(value).strip()}
        for field, values in (scope_exclude or {}).items()
        if isinstance(values, list)
    }
    if excluded_scope:
        row_maps = [row for row in row_maps if not any(
            _normalise_match(row.get(field, "")) in excluded
            for field, excluded in excluded_scope.items()
            if excluded
        )]
    return [
        (item.field_code, item.key, item.display)
        for item in build_identities(row_maps, match_fields, combine=match_combine)
    ]


def _submitted_match_items_from_paths(
    paths: Iterable[Path],
    match_fields: list[str],
    context: ToolExecutionContext,
    *,
    max_rows: int = spreadsheet.MAX_EXCEL_ROWS_PER_SHEET,
    inspect_files: bool = False,
    match_combine: Literal["first", "all"] = "first",
    scope_filter: Mapping[str, str] | None = None,
    scope_exclude: Mapping[str, list[str]] | None = None,
) -> list[tuple[str, str, str]]:
    values: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str]] = set()
    for raw_path in paths:
        path = validate_local_path(raw_path, context.allowed_roots, file_only=True)
        if inspect_files:
            inspect_evidence_file(
                path,
                max_bytes=context.budget.limits.max_file_bytes,
                allowed_kinds={"xlsx", "xls"},
            )
        grid = spreadsheet.parse_award_excel(path, max_rows=max_rows)
        if grid.get("truncated") is True:
            raw_sheets = grid.get("truncated_sheets", [])
            sheets = raw_sheets if isinstance(raw_sheets, list) else []
            raise spreadsheet.SpreadsheetRowLimitError(
                "submitted spreadsheet exceeded the per-sheet row limit: "
                + ",".join(str(item) for item in sheets if str(item))
            )
        for field_code, normalized, display in _submitted_match_items(
            grid, match_fields, match_combine=match_combine,
            scope_filter=scope_filter, scope_exclude=scope_exclude,
        ):
            key = (field_code, normalized)
            if key not in seen:
                values.append((field_code, normalized, display))
                seen.add(key)
    return values


def _collect_spreadsheet_attachments(
    arguments: BaseModel,
    context: ToolExecutionContext,
) -> ToolResult:
    args = CollectSpreadsheetAttachmentsInput.model_validate(arguments)
    destination = validate_local_path(
        args.destination_dir or context.allowed_roots[0],
        context.allowed_roots,
        must_exist=False,
        file_only=False,
    )
    try:
        submitted_items = _submitted_match_items_from_paths(
            args.submitted_paths,
            args.match_fields,
            context,
            max_rows=args.max_rows_per_file,
            inspect_files=True,
            match_combine=args.match_combine,
            scope_filter=args.submitted_scope_filter,
            scope_exclude=args.submitted_scope_exclude,
        )
    except spreadsheet.SpreadsheetRowLimitError as exc:
        return ToolResult.failure("SPREADSHEET_ROW_LIMIT_EXCEEDED", str(exc))
    submitted_values = {
        (field, normalized) for field, normalized, _display in submitted_items
    }
    if not submitted_values:
        return ToolResult.failure(
            "MATCH_FIELDS_UNAVAILABLE",
            "submitted spreadsheet does not contain usable requested match fields",
        )

    downloaded: list[tuple[str, Path, Any, str]] = []
    fetched_pages: list[web.PageContent] = []
    remaining = context.budget.limits.max_total_download_bytes - context.budget.download_bytes
    attempts = 0

    def downloader(
        url: str,
        _workdir: Path,
        timeout: float = 30.0,
        **kwargs: Any,
    ) -> Path:
        nonlocal attempts, remaining
        if remaining <= 0:
            raise ToolBudgetError("download-byte budget exhausted")
        max_bytes = min(context.budget.limits.max_file_bytes, remaining)
        path: Path | None = None
        for attempt_index, attempt_timeout in enumerate((timeout, max(timeout, 60.0))):
            if attempts:
                context.reserve_additional_downloads(1)
            attempts += 1
            try:
                path = web.download_file(
                    url,
                    destination,
                    timeout=attempt_timeout,
                    excel_only=False,
                    referer=str(kwargs.get("referer", "")),
                    max_bytes=max_bytes,
                )
                break
            except Exception as exc:
                transient_timeout = type(exc).__name__ in {"ReadTimeout", "ConnectTimeout"}
                if attempt_index == 0 and transient_timeout:
                    continue
                raise
        if path is None:  # pragma: no cover - the loop either returns a path or raises
            raise RuntimeError("attachment download produced no file")
        safe_path = validate_local_path(path, context.allowed_roots, file_only=True)
        inspection = inspect_evidence_file(safe_path, max_bytes=max_bytes)
        remaining -= inspection.size_bytes
        downloaded.append((url, safe_path, inspection, str(kwargs.get("referer", ""))))
        return safe_path

    keywords = args.include_attachment_keywords
    excluded_keywords = args.exclude_attachment_keywords
    attachment_errors: list[tuple[str, str]] = []

    def attachment_filter(attachment: web.Attachment) -> bool:
        included = not keywords or any(keyword in attachment.text for keyword in keywords)
        excluded = any(keyword in attachment.text for keyword in excluded_keywords)
        return included and not excluded

    def attachment_error(attachment: web.Attachment, exc: Exception) -> None:
        attachment_errors.append((attachment.url, type(exc).__name__))

    def fetch_page(url: str, timeout: float = 15.0) -> web.PageContent:
        del timeout
        page = web.fetch_page(url)
        fetched_pages.append(page)
        return page

    acquired = spreadsheet.acquire_excel_grid(
        args.page_urls,
        destination,
        max_total=args.max_attachments,
        direct_attachment_urls=args.attachment_urls,
        direct_referer=args.page_urls[0],
        direct_attachment_parent_urls=args.attachment_parent_urls,
        fetch_page_fn=fetch_page,
        download_file_fn=downloader,
        parse_excel_fn=lambda path, max_rows=args.max_rows_per_file: (
            spreadsheet.parse_award_excel(path, max_rows=max_rows)
        ),
        attachment_filter_fn=attachment_filter,
        attachment_error_fn=attachment_error,
    )
    fetched_at = utc_now()
    def attachment_metadata(attachment_url: str, referer: str) -> dict[str, Any]:
        parent = next(
            (page for page in fetched_pages if page.url == referer),
            fetched_pages[0] if fetched_pages else None,
        )
        if parent is None:
            return {"page_url": referer}
        attachment_label = next(
            (
                attachment.text
                for attachment in parent.attachments
                if attachment.url == attachment_url
            ),
            "",
        )
        identity_context = f"{parent.title}\n{parent.text}\n{attachment_label}"
        target_match, target_mode = _match_award_title(
            args.expected_award_name,
            identity_context,
            aliases=args.award_aliases,
        )
        year_match = bool(
            args.expected_year
            and args.expected_year in web.extract_years(identity_context)
        )
        source_level = provenance.classify_source(
            parent.url,
            official_domains=args.official_domains,
            official_secondary_domains=args.official_secondary_domains,
        ).level
        return {
            "page_url": parent.url,
            "attachment_linked": bool(attachment_label),
            "attachment_label": attachment_label[:200],
            "page_target_match": target_match,
            "page_target_match_mode": target_mode,
            "page_year_match": year_match,
            "page_observed_award_name": (
                args.expected_award_name if target_match else ""
            ),
            "page_observed_year": args.expected_year if year_match else "",
            "page_source_level": source_level,
        }

    all_artifacts = [
        EvidenceArtifact(
            kind=inspection.kind,
            source_url=url,
            local_path=str(path),
            content_type=inspection.content_type,
            sha256=inspection.sha256,
            size_bytes=inspection.size_bytes,
            fetched_at=fetched_at,
            metadata=attachment_metadata(url, referer),
        )
        for url, path, inspection, referer in downloaded
    ]
    discovered_attachment_urls = list(dict.fromkeys([
        *(
            attachment.url
            for page in fetched_pages
            for attachment in page.attachments
            if attachment_filter(attachment)
        ),
        *(
            url
            for url in args.attachment_urls
            if attachment_filter(web.Attachment(
                text=unquote(url.split("?", 1)[0]).rsplit("/", 1)[-1],
                url=url,
                is_excel=Path(unquote(url.split("?", 1)[0])).suffix.casefold()
                in web.EXCEL_EXTS,
            ))
        ),
    ]))
    downloaded_by_url = {item.source_url: item for item in all_artifacts}
    attempted_attachment_urls = list(dict.fromkeys([
        *(url for url, _path, _inspection, _referer in downloaded),
        *(url for url, _error in attachment_errors),
    ]))
    failed_attachment_urls = list(dict.fromkeys(
        url
        for url, _error in attachment_errors
        if url not in downloaded_by_url
        or downloaded_by_url[url].kind in {"xls", "xlsx"}
    ))
    unprocessed_attachment_urls = [
        url for url in discovered_attachment_urls
        if url not in set(attempted_attachment_urls)
    ]
    processed_attachment_urls = [
        url for url in discovered_attachment_urls
        if url not in set(unprocessed_attachment_urls + failed_attachment_urls)
    ]
    pending_kinds = list(dict.fromkeys(
        item.kind for item in all_artifacts if item.kind not in {"xls", "xlsx"}
    ))
    all_attachments_processed = not (
        unprocessed_attachment_urls or failed_attachment_urls or pending_kinds
    )

    document_identity_counts: dict[str, int] = {}

    def build_attachment_manifest(selected_urls: set[str]) -> list[dict[str, Any]]:
        def row_count(value: object) -> int:
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                return value
            return 0

        documents = {
            document.source_url: document
            for document in (acquired.documents if acquired is not None else [])
        }
        error_by_url = dict(attachment_errors)
        manifest: list[dict[str, Any]] = []
        for url in discovered_attachment_urls:
            parent_url = args.attachment_parent_urls.get(url, "")
            label = ""
            for page in fetched_pages:
                linked = next(
                    (attachment for attachment in page.attachments if attachment.url == url),
                    None,
                )
                if linked is not None:
                    parent_url = page.url
                    label = linked.text
                    break
            if not label:
                label = unquote(url.split("?", 1)[0]).rsplit("/", 1)[-1]
            document = documents.get(url)
            artifact = downloaded_by_url.get(url)
            if document is not None:
                status = "parsed"
            elif url in failed_attachment_urls:
                status = "failed"
            elif artifact is not None:
                status = "pending_content"
            else:
                status = "unprocessed"
            raw_sheet_grids = document.grid.get("sheet_grids", []) if document else []
            sheet_rows: list[dict[str, Any]] = []
            if isinstance(raw_sheet_grids, list):
                for raw_sheet in raw_sheet_grids:
                    if not isinstance(raw_sheet, dict):
                        continue
                    sheet_rows.append({
                        "sheet": str(raw_sheet.get("sheet", ""))[:200],
                        "row_count": row_count(raw_sheet.get("n_rows", 0)),
                        "truncated": bool(raw_sheet.get("truncated", False)),
                    })
            if document is not None and not sheet_rows:
                sheet_rows.append({
                    "sheet": str(document.grid.get("sheet", ""))[:200],
                    "row_count": row_count(document.grid.get("n_rows", 0)),
                    "truncated": bool(document.grid.get("truncated", False)),
                })
            manifest.append({
                "manifest_version": 1,
                "url": url,
                "parent_url": parent_url,
                "label": label[:500],
                "kind": (
                    artifact.kind
                    if artifact is not None
                    else Path(unquote(url.split("?", 1)[0])).suffix.casefold().lstrip(".")
                    or "unknown"
                ),
                "status": status,
                "selected": url in selected_urls,
                "matched_identity_count": document_identity_counts.get(url, 0),
                "truncated": any(sheet["truncated"] for sheet in sheet_rows),
                "error_code": error_by_url.get(url, ""),
                "sheets": sheet_rows,
            })
        return manifest

    group_data: dict[str, Any] = {
        "discovered_attachment_count": len(discovered_attachment_urls),
        "attempted_attachment_count": len(attempted_attachment_urls),
        "processed_attachment_urls": processed_attachment_urls,
        "unprocessed_attachment_urls": unprocessed_attachment_urls,
        "failed_attachment_urls": failed_attachment_urls,
        "all_attachments_processed": all_attachments_processed,
        "attachment_manifest": build_attachment_manifest(set()),
    }
    if acquired is None:
        non_spreadsheets = [item for item in all_artifacts if item.kind not in {"xls", "xlsx"}]
        if non_spreadsheets:
            kinds = list(dict.fromkeys(item.kind for item in non_spreadsheets))
            return ToolResult(
                ok=True,
                data={
                    **group_data,
                    "attachment_count": len(non_spreadsheets),
                    "detected_attachment_kinds": kinds,
                    "coverage_complete": False,
                    "next_evidence_stage": (
                        "pdf_processing" if "pdf" in kinds else "document_processing"
                    ),
                },
                source_url=(fetched_pages[0].url if fetched_pages else args.page_urls[0]),
                fetched_at=fetched_at,
                artifacts=non_spreadsheets,
                warnings=[
                    "agent_requested_spreadsheet_but_magic_detected_other_type",
                    *(["pdf_processing_required"] if "pdf" in kinds else []),
                ],
                evidence_facts=[EvidenceFact(
                    status="unverified",
                    award_name=args.expected_award_name,
                    year=args.expected_year,
                    target_match="uncertain",
                    year_match="uncertain",
                    source_url=(fetched_pages[0].url if fetched_pages else args.page_urls[0]),
                    source_level=provenance.classify_source(
                        fetched_pages[0].url if fetched_pages else args.page_urls[0],
                        official_domains=args.official_domains,
                        official_secondary_domains=args.official_secondary_domains,
                    ).level,
                    document_count=len(non_spreadsheets),
                    artifact_hashes=[item.sha256 for item in non_spreadsheets],
                    extraction_method="attachment_magic_detection",
                    missing_evidence=["附件已识别但尚未完成内容提取和覆盖核验"],
                )],
            )
        return ToolResult.failure(
            "SPREADSHEET_ATTACHMENTS_UNAVAILABLE",
            "no selected page attachment could be downloaded and parsed as a spreadsheet; "
            f"errors={','.join(error for _url, error in attachment_errors[:8]) or 'none'}",
            data=group_data,
        )

    semantic_identity_records: list[dict[str, Any]] = []
    document_matches: list[
        tuple[spreadsheet.AcquiredDocument, set[tuple[str, str]], bool]
    ] = []
    for document in acquired.documents:
        rows = document.grid.get("rows", [])
        document_text = "\n".join(
            "\t".join(str(cell or "") for cell in row)
            for row in rows
            if isinstance(row, list)
        ) if isinstance(rows, list) else ""
        normalized_document = _normalise_match(document_text)
        document_records = spreadsheet.extract_semantic_roster_records(document.grid)
        normalized_record_identities = {
            _normalise_match(str(record.get("identity", "")))
            for record in document_records
            if _normalise_match(str(record.get("identity", "")))
        }
        matches = {
            (field, value)
            for field, value in submitted_values
            if (
                value in normalized_record_identities
                if normalized_record_identities
                else value in normalized_document
            )
        }
        semantic_identity_records.extend({
            **record,
            "source_url": document.source_url,
            "parent_url": document.page_url,
            "attachment_label": document.label,
        } for record in document_records)
        parent = next(
            (page for page in fetched_pages if page.url == document.page_url),
            None,
        )
        identity_context = " ".join([
            parent.title if parent is not None else "",
            parent.text if parent is not None else "",
            document.label,
        ])
        title_match, _title_mode = _match_award_title(
            args.expected_award_name,
            identity_context,
            aliases=args.award_aliases,
        )
        document_matches.append((document, matches, title_match))
        document_identity_counts[document.source_url] = len(matches)

    selected_documents: list[spreadsheet.AcquiredDocument] = []
    matched: set[tuple[str, str]] = set()
    for document, matches, title_match in sorted(
        document_matches,
        key=lambda item: (len(item[1]), int(item[2])),
        reverse=True,
    ):
        if not matches - matched:
            continue
        selected_documents.append(document)
        matched.update(matches)
        if len(matched) >= len(submitted_values):
            break
    selected_urls = {document.source_url for document in selected_documents}
    selected_artifacts = [
        item for item in all_artifacts if item.source_url in selected_urls
    ]
    submitted_total = len(submitted_values)
    submitted_identity_items = {
        hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]: display
        for _field, normalized, display in submitted_items
    }
    matched_normalized = {normalized for _field, normalized in matched}
    matched_identity_hashes = [
        identity_hash for identity_hash, display in submitted_identity_items.items()
        if _normalise_match(display) in matched_normalized
    ]
    missing_items = [
        display for identity_hash, display in submitted_identity_items.items()
        if identity_hash not in set(matched_identity_hashes)
    ]
    total = max(submitted_total, args.expected_scope_count or 0)
    attachment_labels = [document.label[:200] for document in selected_documents]
    parsed_rows = sum(
        row_count
        for document in selected_documents
        if isinstance((row_count := document.grid.get("n_rows")), int)
        and not isinstance(row_count, bool)
    )
    selected_page_url = (
        selected_documents[0].page_url if selected_documents else acquired.page_url
    )
    page_title = next(
        (page.title for page in fetched_pages if page.url == selected_page_url),
        "",
    )
    award_context = " ".join([
        page_title,
        *(page.text for page in fetched_pages if page.url == selected_page_url),
        *attachment_labels,
    ])
    award_match, _award_mode = _match_award_title(
        args.expected_award_name,
        award_context,
        aliases=args.award_aliases,
    )
    page_years = web.extract_years(award_context)
    year_match = bool(args.expected_year and args.expected_year in page_years)
    assessment = provenance.classify_source(
        selected_page_url,
        official_domains=args.official_domains,
        official_secondary_domains=args.official_secondary_domains,
    )
    spreadsheet_truncated = any(
        document.grid.get("truncated") is True for document in selected_documents
    )
    coverage_complete = (
        total > 0 and len(matched) >= total and not spreadsheet_truncated
    )
    missing: list[str] = []
    if spreadsheet_truncated:
        missing.append("表格读取达到行数上限，无法确认附件名单完整性")
    if args.expected_scope_count and args.expected_scope_count > submitted_total:
        missing.append(
            f"业务参考口径为{args.expected_scope_count}，提交仅{submitted_total}条"
        )
    if pending_kinds:
        coverage_complete = False
        missing.append(
            "同页还包含待处理的非表格附件：" + ",".join(pending_kinds)
        )
    if not selected_documents:
        missing.append("没有附件能够覆盖提交名单中的人员或项目")
    if not award_match:
        missing.append("页面及所选附件未确认目标奖项")
    if not year_match:
        missing.append("页面及所选附件未确认目标年份")
    if len(matched) < total:
        missing.append(f"名单仅覆盖 {len(matched)}/{total} 条")
    unresolved_attachment_urls = [
        document.source_url
        for document in acquired.documents
        if document.source_url not in selected_urls
    ]
    if unresolved_attachment_urls:
        coverage_complete = False
        missing.append("同组仍有未纳入名单范围的表格附件，需确认其是否属于获奖名单")
    if not all_attachments_processed:
        coverage_complete = False
        missing.append("附件组尚未全部处理完成")
    fact = EvidenceFact(
        status=_fact_status(award_match, year_match, coverage_complete),
        award_name=args.expected_award_name if award_match else "",
        year=args.expected_year if year_match else "",
        target_match="yes" if award_match else "no",
        year_match="yes" if year_match else "no",
        source_url=selected_page_url,
        source_level=assessment.level,
        expected_count=total,
        observed_count=len(matched),
        submitted_count=submitted_total,
        reference_count=args.expected_scope_count,
        coverage_complete=coverage_complete,
        document_complete=(
            all_attachments_processed and not spreadsheet_truncated
            and award_match and year_match
        ),
        document_count=len(selected_artifacts),
        artifact_hashes=[item.sha256 for item in selected_artifacts],
        extraction_method="joint_spreadsheet_attachments",
        scope_id=args.scope_id,
        role_type=args.role_type,
        matched_items=[
            display for identity_hash, display in submitted_identity_items.items()
            if identity_hash in set(matched_identity_hashes)
        ],
        missing_items=missing_items,
        missing_item_count=len(missing_items),
        missing_evidence=missing,
    )
    return ToolResult(
        ok=True,
        data={
            **group_data,
            "attachment_manifest": build_attachment_manifest(selected_urls),
            "identity_version": IDENTITY_VERSION,
            "attachment_count": len(selected_documents),
            "downloaded_attachment_count": len(acquired.source_urls),
            "artifact_count": len(selected_artifacts),
            "downloaded_artifact_count": len(all_artifacts),
            "detected_attachment_kinds": list(dict.fromkeys(
                item.kind for item in selected_artifacts
            )),
            "attachment_labels": attachment_labels,
            "parsed_rows": parsed_rows,
            "spreadsheet_truncated": spreadsheet_truncated,
            "match_fields": args.match_fields,
            "observed_award_name": fact.award_name,
            "observed_year": fact.year,
            "source_level": fact.source_level,
            "expected_count": total,
            "observed_count": len(matched),
            "submitted_count": submitted_total,
            "reference_count": args.expected_scope_count,
            "submitted_match_count": len(matched),
            "submitted_match_total": submitted_total,
            "coverage_complete": coverage_complete,
            "document_complete": fact.document_complete,
            "scope_id": args.scope_id,
            "role_type": args.role_type,
            "evidence_group": selected_page_url,
            "submitted_identity_items": submitted_identity_items,
            "matched_identity_hashes": matched_identity_hashes,
            "missing_items": missing_items,
            "missing_item_count": len(missing_items),
            "unresolved_attachment_urls": unresolved_attachment_urls,
            "spreadsheet_identity_records": semantic_identity_records,
        },
        source_url=selected_page_url,
        local_path=str(
            selected_documents[0].raw_path
            if selected_documents
            else acquired.raw_path
        ),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        sha256=selected_artifacts[0].sha256 if selected_artifacts else "",
        fetched_at=fetched_at,
        artifacts=all_artifacts,
        evidence_facts=[fact],
        warnings=[
            *(
                ["attachments_selected_by_roster_overlap"]
                if len(selected_documents) < len(acquired.documents)
                else []
            ),
            *(["spreadsheet_row_limit_reached"] if spreadsheet_truncated else []),
        ],
    )


def _parse_spreadsheet(arguments: BaseModel, context: ToolExecutionContext) -> ToolResult:
    args = ParseSpreadsheetInput.model_validate(arguments)
    path = validate_local_path(args.path, context.allowed_roots, file_only=True)
    inspection = inspect_evidence_file(path, max_bytes=context.budget.limits.max_file_bytes,
                                       allowed_kinds={"xlsx", "xls"})
    grid = spreadsheet.parse_award_excel(path, max_rows=args.max_rows)
    return ToolResult(ok=True, data=grid, local_path=str(path),
                      content_type=inspection.content_type, sha256=inspection.sha256)


def _pdf_failure(exc: pdf_tools.PdfError) -> ToolResult:
    if isinstance(exc, pdf_tools.PdfDependencyError):
        code = "DEPENDENCY_UNAVAILABLE"
    elif isinstance(exc, pdf_tools.PdfEncryptedError):
        code = "PDF_ENCRYPTED"
    elif isinstance(exc, pdf_tools.PdfLimitError):
        code = "PDF_LIMIT_EXCEEDED"
    else:
        code = "PDF_PROCESSING_FAILED"
    return ToolResult.failure(code, str(exc)[:500])


def _image_failure(exc: image_tools.ImageToolError) -> ToolResult:
    if isinstance(exc, image_tools.ImageDependencyError):
        code = "DEPENDENCY_UNAVAILABLE"
    elif isinstance(exc, image_tools.ImageLimitError):
        code = "IMAGE_LIMIT_EXCEEDED"
    elif isinstance(exc, image_tools.VisionOutputError):
        code = "VISION_OUTPUT_INVALID"
    elif isinstance(exc, image_tools.ImageDecodeError):
        code = "IMAGE_DECODE_FAILED"
    else:
        code = "IMAGE_PROCESSING_FAILED"
    return ToolResult.failure(code, str(exc)[:500])


def _run_pdf_isolated(
    function: Callable[..., Any],
    *,
    args: tuple[Any, ...],
    kwargs: dict[str, Any] | None = None,
    timeout_seconds: float,
) -> Any:
    try:
        return run_isolated(
            function, args=args, kwargs=kwargs, timeout_seconds=timeout_seconds
        )
    except IsolatedCallTimeout as exc:
        raise pdf_tools.PdfRuntimeError(str(exc)) from exc
    except IsolatedCallError as exc:
        error_types: dict[str, type[pdf_tools.PdfError]] = {
            "PdfDependencyError": pdf_tools.PdfDependencyError,
            "PdfEncryptedError": pdf_tools.PdfEncryptedError,
            "PdfLimitError": pdf_tools.PdfLimitError,
            "PdfRuntimeError": pdf_tools.PdfRuntimeError,
        }
        error_type = error_types.get(exc.error_type, pdf_tools.PdfRuntimeError)
        raise error_type(str(exc)) from exc


def _run_image_isolated(
    function: Callable[..., Any],
    *,
    args: tuple[Any, ...],
    kwargs: dict[str, Any] | None = None,
    timeout_seconds: float,
) -> Any:
    try:
        return run_isolated(
            function, args=args, kwargs=kwargs, timeout_seconds=timeout_seconds
        )
    except IsolatedCallTimeout as exc:
        raise image_tools.ImageToolError(str(exc)) from exc
    except IsolatedCallError as exc:
        error_types: dict[str, type[image_tools.ImageToolError]] = {
            "ImageDependencyError": image_tools.ImageDependencyError,
            "ImageLimitError": image_tools.ImageLimitError,
            "ImageDecodeError": image_tools.ImageDecodeError,
            "VisionOutputError": image_tools.VisionOutputError,
            "ImageToolError": image_tools.ImageToolError,
        }
        error_type = error_types.get(exc.error_type, image_tools.ImageToolError)
        raise error_type(str(exc)) from exc


def _inspect_pdf(arguments: BaseModel, context: ToolExecutionContext) -> ToolResult:
    args = InspectPdfInput.model_validate(arguments)
    path = validate_local_path(args.path, context.allowed_roots, file_only=True)
    inspection = inspect_evidence_file(
        path, max_bytes=context.budget.limits.max_file_bytes, allowed_kinds={"pdf"}
    )
    try:
        report = _run_pdf_isolated(
            pdf_tools.inspect_pdf,
            args=(path,),
            kwargs={
                "max_pages": min(args.max_pages, context.budget.limits.max_pdf_pages)
            },
            timeout_seconds=40,
        )
        context.reserve_pdf_pages(
            inspection.sha256, range(1, report.page_count + 1)
        )
    except pdf_tools.PdfError as exc:
        return _pdf_failure(exc)
    return ToolResult(
        ok=True,
        data=report.model_dump(mode="json"),
        local_path=str(path),
        content_type=inspection.content_type,
        sha256=inspection.sha256,
        is_truncated=report.truncated,
    )


def _extract_pdf_text(arguments: BaseModel, context: ToolExecutionContext) -> ToolResult:
    args = ExtractPdfTextInput.model_validate(arguments)
    path = validate_local_path(args.path, context.allowed_roots, file_only=True)
    inspection = inspect_evidence_file(
        path, max_bytes=context.budget.limits.max_file_bytes, allowed_kinds={"pdf"}
    )
    try:
        context.reserve_pdf_pages(inspection.sha256, args.pages)
        pages = _run_pdf_isolated(
            pdf_tools.extract_pdf_text,
            args=(path, args.pages),
            kwargs={
                "max_pages": context.budget.limits.max_pdf_pages,
                "max_chars_per_page": args.max_chars_per_page,
                "extract_tables": args.extract_tables,
            },
            timeout_seconds=55,
        )
    except pdf_tools.PdfError as exc:
        return _pdf_failure(exc)
    evidence_facts: list[EvidenceFact] = []
    evidence_summary: dict[str, Any] = {}
    if args.submitted_paths and args.match_fields:
        submitted_items = _submitted_match_items_from_paths(
            args.submitted_paths,
            args.match_fields,
            context,
            inspect_files=True,
            match_combine=args.match_combine,
            scope_filter=args.submitted_scope_filter,
            scope_exclude=args.submitted_scope_exclude,
        )
        extracted_text = "\n".join([
            page.text
            + "\n"
            + "\n".join(
                "\t".join(str(cell or "") for cell in row)
                for table in page.tables
                for row in table.rows
            )
            for page in pages
        ])
        normalized_text = _normalise_match(extracted_text)
        compared_entries = [
            (normalized, display, _identity_matches(display, normalized, normalized_text)[0])
            for _field, normalized, display in submitted_items
        ]
        matched_entries = [
            (normalized, display)
            for normalized, display, matched in compared_entries
            if matched
        ]
        matched_items = [display for _normalized, display in matched_entries]
        missing_items = [
            display for _normalized, display, matched in compared_entries if not matched
        ]
        matched_identity_hashes = [
            hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]
            for normalized, _display in matched_entries
        ]
        submitted_identity_items = {
            hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]: display
            for _field, normalized, display in submitted_items
        }
        pdf_inspection = _run_pdf_isolated(
            pdf_tools.inspect_pdf,
            args=(path,),
            kwargs={"max_pages": context.budget.limits.max_pdf_pages},
            timeout_seconds=45,
        )
        full_document = set(args.pages) == set(range(1, pdf_inspection.page_count + 1))
        truncated = any(page.is_truncated for page in pages)
        expected_count = max(len(submitted_items), args.expected_scope_count or 0)
        coverage_complete = bool(
            expected_count > 0
            and len(matched_items) >= expected_count
            and full_document
            and not truncated
        )
        direct_target_match, _target_mode = _match_award_title(
            args.expected_award_name,
            extracted_text,
            aliases=args.award_aliases,
        )
        direct_year_match = bool(
            args.expected_year
            and args.expected_year in web.extract_years(extracted_text)
        )
        parent_context_valid = bool(
            args.parent_attachment_linked
            and args.parent_page_url.startswith(("http://", "https://"))
        )
        parent_target_match = bool(
            parent_context_valid
            and args.expected_award_name
            and args.parent_award_name == args.expected_award_name
        )
        parent_year_match = bool(
            parent_context_valid
            and args.expected_year
            and args.parent_year == args.expected_year
        )
        target_match = direct_target_match or parent_target_match
        year_match = direct_year_match or parent_year_match
        source_level = "unknown"
        if args.source_url.startswith(("http://", "https://")):
            source_level = provenance.classify_source(
                args.source_url,
                official_domains=args.official_domains,
                official_secondary_domains=args.official_secondary_domains,
            ).level
        missing_evidence: list[str] = []
        if not target_match:
            missing_evidence.append("PDF 文本中未确认目标奖项")
        if not year_match:
            missing_evidence.append("PDF 文本中未确认目标年份")
        if not full_document:
            missing_evidence.append("PDF 尚未覆盖全部页面")
        if truncated:
            missing_evidence.append("PDF 文本提取存在截断")
        if len(matched_items) < expected_count:
            missing_evidence.append(
                f"PDF 名单覆盖不足：{len(matched_items)}/{expected_count}"
            )
        fact = EvidenceFact(
            status=_fact_status(target_match, year_match, coverage_complete),
            award_name=args.expected_award_name if target_match else "",
            year=args.expected_year if year_match else "",
            target_match="yes" if target_match else "no",
            year_match="yes" if year_match else "no",
            source_url=args.source_url,
            source_level=source_level,
            expected_count=expected_count,
            observed_count=len(matched_items),
            submitted_count=len(submitted_items),
            reference_count=args.expected_scope_count,
            coverage_complete=coverage_complete,
            document_complete=full_document and not truncated,
            document_count=1,
            artifact_hashes=[inspection.sha256],
            extraction_method=(
                "digital_pdf_text_roster_with_verified_parent_page"
                if parent_target_match or parent_year_match
                else "digital_pdf_text_roster"
            ),
            comparison_scope=(
                "verified_parent_page_and_pdf_roster"
                if parent_context_valid
                else "pdf_roster"
            ),
            scope_id=args.scope_id,
            role_type=args.role_type,
            matched_items=matched_items[:10_000],
            missing_items=missing_items[:10_000],
            missing_item_count=len(missing_items),
            missing_evidence=missing_evidence,
            relationship_confirmed=(True if parent_context_valid else None),
            relationship_summary=(
                f"PDF attachment linked from verified page {args.parent_page_url}"[:500]
                if parent_context_valid
                else ""
            ),
        )
        evidence_facts.append(fact)
        evidence_summary = {
            "observed_award_name": fact.award_name,
            "observed_year": fact.year,
            "source_level": fact.source_level,
            "expected_count": expected_count,
            "observed_count": len(matched_items),
            "coverage_complete": coverage_complete,
            "document_count": 1,
            "full_document_extracted": full_document,
            "document_complete": full_document and not truncated,
            "scope_id": args.scope_id,
            "role_type": args.role_type,
            "evidence_group": (
                args.parent_page_url if parent_context_valid else ""
            ),
            "matched_identity_hashes": matched_identity_hashes,
            "submitted_identity_items": submitted_identity_items,
            "matched_items": matched_items[:10_000],
            "missing_items": missing_items[:10_000],
            "missing_item_count": len(missing_items),
            "extra_items": [],
            "extra_item_count": 0,
        }
    return ToolResult(
        ok=True,
        data={
            "pages": [page.model_dump(mode="json") for page in pages],
            **evidence_summary,
        },
        local_path=str(path),
        content_type=inspection.content_type,
        sha256=inspection.sha256,
        is_truncated=any(page.is_truncated for page in pages),
        warnings=["external_content_untrusted"],
        evidence_facts=evidence_facts,
    )


def _render_pdf_pages(
    arguments: BaseModel,
    context: ToolExecutionContext,
    *,
    pdftoppm: str,
) -> ToolResult:
    args = RenderPdfPagesInput.model_validate(arguments)
    path = validate_local_path(args.path, context.allowed_roots, file_only=True)
    output_dir = validate_local_path(
        args.output_dir or context.allowed_roots[0],
        context.allowed_roots,
        must_exist=False,
        file_only=False,
    )
    inspection = inspect_evidence_file(
        path, max_bytes=context.budget.limits.max_file_bytes, allowed_kinds={"pdf"}
    )
    try:
        estimates = _run_pdf_isolated(
            pdf_tools.estimated_page_pixels,
            args=(path, args.pages, args.dpi),
            timeout_seconds=10,
        )
        if any(pixels > context.budget.limits.max_image_pixels for pixels in estimates):
            raise pdf_tools.PdfLimitError(
                f"rendered page would exceed {context.budget.limits.max_image_pixels} pixels"
            )
        context.reserve_pdf_pages(inspection.sha256, args.pages)
        context.reserve_media_work(
            rendered_pages=len(args.pages), image_pixels=sum(estimates)
        )
        pages = _run_pdf_isolated(
            pdf_tools.render_pdf_pages,
            args=(path, args.pages, output_dir),
            kwargs={
                "dpi": args.dpi,
                "pdftoppm": pdftoppm,
                "max_pages": context.budget.limits.max_render_pages,
                "max_pixels_per_page": context.budget.limits.max_image_pixels,
                "timeout_seconds": 145,
            },
            timeout_seconds=155,
        )
        actual_pixels = sum(page.pixels for page in pages)
        if actual_pixels > sum(estimates):
            context.reserve_media_work(image_pixels=actual_pixels - sum(estimates))
    except pdf_tools.PdfError as exc:
        return _pdf_failure(exc)
    fetched_at = utc_now()
    source_url = args.source_url or f"local-evidence:{inspection.sha256}"
    artifacts = [
        EvidenceArtifact(
            kind="pdf_page_image",
            source_url=source_url,
            local_path=str(page.path),
            content_type=page.content_type,
            sha256=page.sha256,
            size_bytes=page.size_bytes,
            fetched_at=fetched_at,
            metadata={
                "page": page.page,
                "dpi": page.dpi,
                "width": page.width,
                "height": page.height,
                "pixels": page.pixels,
                "derived_from_sha256": inspection.sha256,
            },
        )
        for page in pages
    ]
    return ToolResult(
        ok=True,
        data={"pages": [page.model_dump(mode="json") for page in pages]},
        source_url=args.source_url,
        local_path=str(output_dir),
        content_type="image/png",
        sha256=inspection.sha256,
        fetched_at=fetched_at,
        artifacts=artifacts,
    )


def _inspect_image_batch(
    images: list[image_tools.ImagePageRef], context: ToolExecutionContext
) -> list[image_tools.ImageInspection]:
    inspected: list[image_tools.ImageInspection] = []
    for reference in images:
        path = validate_local_path(reference.path, context.allowed_roots, file_only=True)
        reference.path = path
        inspected.append(_run_image_isolated(
            image_tools.inspect_image,
            args=(path,),
            kwargs={
                "max_bytes": context.budget.limits.max_file_bytes,
                "max_pixels": context.budget.limits.max_image_pixels,
            },
            timeout_seconds=20,
        ))
    return inspected


def _ocr_image(
    arguments: BaseModel,
    context: ToolExecutionContext,
    *,
    engine_factory: image_tools.OcrFactory | None,
) -> ToolResult:
    args = OcrImageInput.model_validate(arguments)
    try:
        inspected = _inspect_image_batch(args.images, context)
        context.reserve_media_work(
            ocr_pages=len(args.images),
            image_pixels=sum(image.pixels for image in inspected),
        )
        ocr_kwargs: dict[str, Any] = {
            "max_bytes": context.budget.limits.max_file_bytes,
            "max_pixels": context.budget.limits.max_image_pixels,
            "engine_factory": engine_factory,
        }
        pages = (
            _run_image_isolated(
                image_tools.run_rapid_ocr,
                args=(args.images,),
                kwargs=ocr_kwargs,
                timeout_seconds=220,
            )
            if engine_factory is None
            else image_tools.run_rapid_ocr(
                args.images,
                max_bytes=context.budget.limits.max_file_bytes,
                max_pixels=context.budget.limits.max_image_pixels,
                engine_factory=engine_factory,
            )
        )
    except image_tools.ImageToolError as exc:
        return _image_failure(exc)
    warnings_out = [
        f"page_{page.page}:{warning}"
        for page in pages
        for warning in page.warnings
    ]
    if any(page.needs_vision for page in pages):
        warnings_out.append("vision_review_recommended")
    return ToolResult(
        ok=True,
        data={"backend": "rapidocr", "pages": [
            page.model_dump(mode="json") for page in pages
        ]},
        local_path=str(args.images[0].path),
        content_type=inspected[0].content_type if len(inspected) == 1 else "",
        sha256=inspected[0].sha256 if len(inspected) == 1 else "",
        is_truncated=any(page.text_truncated for page in pages),
        warnings=warnings_out,
    )


def _default_vision_client() -> image_tools.VisionClient:
    from award_audit.agent.llm import LlmClient

    return LlmClient()


def _vision_extract_roster(
    arguments: BaseModel,
    context: ToolExecutionContext,
    *,
    client_factory: image_tools.VisionClientFactory,
    isolate_client: bool,
) -> ToolResult:
    args = VisionExtractRosterInput.model_validate(arguments)
    try:
        inspected = _inspect_image_batch(args.images, context)
        context.reserve_media_work(
            vision_pages=len(args.images),
            image_pixels=sum(image.pixels for image in inspected),
        )
        vision_kwargs: dict[str, Any] = {
            "max_bytes": context.budget.limits.max_file_bytes,
            "max_pixels": context.budget.limits.max_image_pixels,
            "client_factory": client_factory,
            "ocr_text_by_page": args.ocr_text_by_page,
        }
        batch = (
            _run_image_isolated(
                image_tools.extract_roster_vision,
                args=(args.images,),
                kwargs=vision_kwargs,
                timeout_seconds=460,
            )
            if isolate_client
            else image_tools.extract_roster_vision(
                args.images,
                max_bytes=context.budget.limits.max_file_bytes,
                max_pixels=context.budget.limits.max_image_pixels,
                client_factory=client_factory,
                ocr_text_by_page=args.ocr_text_by_page,
            )
        )
    except image_tools.ImageToolError as exc:
        return _image_failure(exc)
    payload = batch.model_dump(mode="json")
    payload["vision_error_count"] = len(batch.errors)
    payload["vision_error_pages"] = [
        str(int(error.get("page", 0) or 0)) for error in batch.errors
    ]
    payload["vision_error_codes"] = [
        str(error.get("error_code", "VISION_PAGE_FAILED"))
        for error in batch.errors
    ]
    if not batch.complete:
        return ToolResult.failure(
            "VISION_EXTRACTION_FAILED",
            "one or more vision pages failed validation",
            data=payload,
            warnings=["coverage_unknown"],
        )
    return ToolResult(ok=True, data=payload)


def _verify_page_image_roster(
    arguments: BaseModel,
    context: ToolExecutionContext,
    *,
    client_factory: image_tools.VisionClientFactory,
    isolate_client: bool,
) -> ToolResult:
    """Acquire bounded page images, select the target section and compare names."""

    args = VerifyPageImageRosterInput.model_validate(arguments)
    destination = validate_local_path(
        args.destination_dir or context.allowed_roots[0],
        context.allowed_roots,
        must_exist=False,
        file_only=False,
    )
    submitted_items = _submitted_match_items_from_paths(
        args.submitted_paths,
        args.match_fields,
        context,
        match_combine=args.match_combine,
        scope_filter=args.submitted_scope_filter,
        scope_exclude=args.submitted_scope_exclude,
    )
    if not submitted_items:
        return ToolResult.failure(
            "MATCH_FIELDS_UNAVAILABLE",
            "submitted spreadsheet does not contain usable requested match fields",
        )
    submitted_identity_items = {
        hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]: display
        for _field, normalized, display in submitted_items
    }

    remaining = context.budget.limits.max_total_download_bytes - context.budget.download_bytes
    artifacts: list[EvidenceArtifact] = []
    references: list[image_tools.ImagePageRef] = []
    download_errors: list[str] = []
    attempted_image_urls: list[str] = []
    fetched_at = utc_now()
    for index, url in enumerate(args.image_urls, start=1):
        if remaining <= 0:
            download_errors.append("download_byte_budget_exhausted")
            break
        if index > 1:
            context.reserve_additional_downloads(1)
        attempted_image_urls.append(url)
        max_bytes = min(context.budget.limits.max_file_bytes, remaining)
        try:
            path = web.download_file(
                url,
                destination,
                referer=args.page_url,
                max_bytes=max_bytes,
            )
            safe_path = validate_local_path(path, context.allowed_roots, file_only=True)
            inspection = inspect_evidence_file(
                safe_path,
                max_bytes=max_bytes,
                allowed_kinds={"png", "jpeg", "gif", "webp"},
            )
        except Exception as exc:
            download_errors.append(f"image_{index}:{type(exc).__name__}")
            continue
        remaining -= inspection.size_bytes
        artifacts.append(EvidenceArtifact(
            kind=inspection.kind,
            source_url=url,
            local_path=str(safe_path),
            content_type=inspection.content_type,
            sha256=inspection.sha256,
            size_bytes=inspection.size_bytes,
            fetched_at=fetched_at,
            metadata={"page_url": args.page_url, "candidate_index": index},
        ))

    total_images = len(artifacts)
    references = [
        image_tools.ImagePageRef(
            path=Path(artifact.local_path),
            page=index,
            total_pages=total_images,
        )
        for index, artifact in enumerate(artifacts, start=1)
    ]
    if not references:
        return ToolResult.failure(
            "PAGE_IMAGES_UNAVAILABLE",
            "no candidate page image could be safely downloaded",
            data={
                "processed_image_urls": [],
                "failed_image_urls": attempted_image_urls,
                "unprocessed_image_urls": [
                    url for url in args.image_urls if url not in set(attempted_image_urls)
                ],
                "all_images_processed": False,
            },
            source_url=args.page_url,
            warnings=download_errors[:10],
        )

    try:
        inspected = _inspect_image_batch(references, context)
        roster_pairs = [
            (artifact, inspection)
            for artifact, inspection in zip(artifacts, inspected, strict=True)
            if (
                inspection.width >= 300
                and inspection.height >= 300
                and inspection.pixels >= 150_000
            )
        ]
        selected_artifacts = [artifact for artifact, _inspection in roster_pairs]
        selected_inspections = [inspection for _artifact, inspection in roster_pairs]
        references = [
            image_tools.ImagePageRef(
                path=Path(artifact.local_path),
                page=index,
                total_pages=len(selected_artifacts),
                source_url=artifact.source_url,
            )
            for index, artifact in enumerate(selected_artifacts, start=1)
        ]
        if not references:
            raise image_tools.ImageDecodeError(
                "no roster-sized page images remained after decoration filtering"
            )
        context.reserve_media_work(
            vision_pages=len(references),
            image_pixels=sum(image.pixels for image in selected_inspections),
        )
        vision_kwargs: dict[str, Any] = {
            "max_bytes": context.budget.limits.max_file_bytes,
            "max_pixels": context.budget.limits.max_image_pixels,
            "client_factory": client_factory,
        }
        batches: list[image_tools.VisionBatch] = []
        for start in range(0, len(references), image_tools.MAX_VISION_IMAGES):
            chunk = references[start:start + image_tools.MAX_VISION_IMAGES]
            batches.append(
                _run_image_isolated(
                    image_tools.extract_roster_vision,
                    args=(chunk,),
                    kwargs=vision_kwargs,
                    timeout_seconds=460,
                )
                if isolate_client
                else image_tools.extract_roster_vision(
                    chunk,
                    max_bytes=context.budget.limits.max_file_bytes,
                    max_pixels=context.budget.limits.max_image_pixels,
                    client_factory=client_factory,
                )
            )
        batch = image_tools.VisionBatch(
            provider=next((item.provider for item in batches if item.provider), ""),
            model=next((item.model for item in batches if item.model), ""),
            pages=[page for item in batches for page in item.pages],
            errors=[error for item in batches for error in item.errors],
            complete=all(item.complete for item in batches),
        )
        failed_page_numbers = {
            int(error.get("page", 0) or 0) for error in batch.errors
        }
        retry_references = [
            reference for reference in references
            if reference.page in failed_page_numbers
        ]
        if retry_references:
            retry_pixels = sum(
                selected_inspections[reference.page - 1].pixels
                for reference in retry_references
                if 1 <= reference.page <= len(selected_inspections)
            )
            context.reserve_media_work(
                vision_pages=len(retry_references),
                image_pixels=retry_pixels,
            )
            retry_batch = (
                _run_image_isolated(
                    image_tools.extract_roster_vision,
                    args=(retry_references,),
                    kwargs=vision_kwargs,
                    timeout_seconds=460,
                )
                if isolate_client
                else image_tools.extract_roster_vision(
                    retry_references,
                    max_bytes=context.budget.limits.max_file_bytes,
                    max_pixels=context.budget.limits.max_image_pixels,
                    client_factory=client_factory,
                )
            )
            recovered_pages = {page.page: page for page in retry_batch.pages}
            batch = image_tools.VisionBatch(
                provider=batch.provider or retry_batch.provider,
                model=batch.model or retry_batch.model,
                pages=sorted(
                    [
                        page for page in batch.pages
                        if page.page not in recovered_pages
                    ] + list(recovered_pages.values()),
                    key=lambda page: page.page,
                ),
                errors=[
                    error for error in batch.errors
                    if int(error.get("page", 0) or 0) not in recovered_pages
                ] + retry_batch.errors,
                complete=not (
                    [
                        error for error in batch.errors
                        if int(error.get("page", 0) or 0) not in recovered_pages
                    ] + retry_batch.errors
                ),
            )
    except image_tools.ImageToolError as exc:
        return ToolResult(
            ok=True,
            data={
                "coverage_complete": False,
                "unresolved_items": [item[2] for item in submitted_items][:100],
                "unresolved_item_count": len(submitted_items),
                "submitted_identity_items": submitted_identity_items,
                "matched_identity_hashes": [],
                "processed_image_urls": [],
                "failed_image_urls": attempted_image_urls,
                "unprocessed_image_urls": [
                    url for url in args.image_urls if url not in set(attempted_image_urls)
                ],
                "all_images_processed": False,
            },
            source_url=args.page_url,
            fetched_at=fetched_at,
            artifacts=artifacts,
            warnings=[f"vision_failed:{type(exc).__name__}", *download_errors[:9]],
            evidence_facts=[EvidenceFact(
                status="unverified",
                award_name=args.expected_award_name,
                year=args.expected_year,
                target_match="uncertain",
                year_match="uncertain",
                source_url=args.page_url,
                extraction_method="page_image_vision",
                unresolved_items=[item[2] for item in submitted_items][:100],
                unresolved_item_count=len(submitted_items),
                artifact_hashes=[item.sha256 for item in artifacts],
                document_count=len(artifacts),
                missing_evidence=["页面图片识别失败，名单仍待人工核验"],
            )],
        )

    include = [_normalise_match(item) for item in args.section_keywords]
    exclude = [_normalise_match(item) for item in args.section_exclude_keywords]

    submitted_atomic: dict[str, str] = {}
    for _field, normalized, display in submitted_items:
        split_parts = [
            part.strip()
            for part in _MULTI_VALUE_SEPARATOR.split(display)
            if part.strip()
        ]
        parts = (
            [(_normalise_match(part), part) for part in split_parts]
            if len(split_parts) > 1
            else [(normalized, display)]
        )
        for normalized_part, display_part in parts:
            submitted_atomic.setdefault(normalized_part, display_part)

    def page_names(page: image_tools.VisionRosterPage) -> set[str]:
        return {
            _normalise_match(entry.name)
            for entry in page.entries
            if _normalise_match(entry.name)
        }

    def target_page(page: image_tools.VisionRosterPage) -> bool:
        if not page.is_roster_page:
            return False
        title = _normalise_match(page.section_title)
        if not title or any(item and item in title for item in exclude):
            return False
        if include:
            return all(item in title for item in include)
        matched, _mode = _match_award_title(
            args.expected_award_name,
            page.section_title,
            aliases=args.award_aliases,
        )
        return matched

    if include or exclude:
        selected = [page for page in batch.pages if target_page(page)]
    else:
        roster_pages = [page for page in batch.pages if page.is_roster_page]
        ranked = sorted(
            roster_pages,
            key=lambda page: (
                len(page_names(page).intersection(submitted_atomic)),
                int(_match_award_title(
                    args.expected_award_name,
                    page.section_title,
                    aliases=args.award_aliases,
                )[0]),
                -len(page_names(page) - set(submitted_atomic)),
            ),
            reverse=True,
        )
        selected = []
        covered: set[str] = set()
        for page in ranked:
            overlap = page_names(page).intersection(submitted_atomic)
            if not overlap - covered:
                continue
            selected.append(page)
            covered.update(overlap)
            if covered == set(submitted_atomic):
                break
        if not selected:
            selected = [page for page in roster_pages if target_page(page)]
        selected.sort(key=lambda page: page.page)
    official_entries = [entry for page in selected for entry in page.entries]
    official_names = {
        _normalise_match(entry.name): entry.name.strip()
        for entry in official_entries
        if _normalise_match(entry.name)
    }
    matched_items: list[str] = []
    missing_items: list[str] = []
    for _field, normalized, display in submitted_items:
        parts = [
            (normalized, display)
        ]
        split_parts = [
            part.strip()
            for part in _MULTI_VALUE_SEPARATOR.split(display)
            if part.strip()
        ]
        if len(split_parts) > 1:
            parts = [(_normalise_match(part), part) for part in split_parts]
        missing_parts = [
            display_part
            for normalized_part, display_part in parts
            if normalized_part not in official_names
        ]
        if missing_parts:
            missing_items.extend(missing_parts)
        else:
            matched_items.append(display)
    matched_item_set = set(matched_items)
    matched_identity_hashes = [
        identity_hash
        for identity_hash, display in submitted_identity_items.items()
        if display in matched_item_set
    ]
    extra_items = [
        display
        for normalized, display in official_names.items()
        if normalized not in submitted_atomic
    ]
    selected_pages_incomplete = any(page.unreadable for page in selected)
    unresolved_items = (
        [item[2] for item in submitted_items]
        if not selected or selected_pages_incomplete
        else []
    )
    if unresolved_items:
        missing_items = []
        extra_items = []

    expected_count = max(len(submitted_items), args.expected_scope_count or 0)
    pages_readable = bool(selected) and all(
        not page.unreadable for page in selected
    )
    coverage_complete = bool(
        pages_readable
        and len(matched_items) == expected_count
        and not missing_items
        and not extra_items
    )
    page_award_match, _page_award_mode = _match_award_title(
        args.expected_award_name,
        args.page_title,
        aliases=args.award_aliases,
    )
    section_award_match = any(
        _match_award_title(
            args.expected_award_name,
            page.section_title,
            aliases=args.award_aliases,
        )[0]
        for page in selected
    )
    target_match = bool(selected and (page_award_match or section_award_match or include))
    years = set(web.extract_years(args.page_title))
    years.update(
        year
        for page in selected
        for year in web.extract_years(page.section_title)
    )
    year_match = bool(args.expected_year and args.expected_year in years)
    assessment = provenance.classify_source(
        args.page_url,
        official_domains=args.official_domains,
        official_secondary_domains=args.official_secondary_domains,
    )
    missing_evidence: list[str] = []
    if not selected:
        missing_evidence.append("页面图片中未定位到目标奖项分组")
    if unresolved_items:
        missing_evidence.append("页面图片未全部可靠识别，姓名差异尚不能定性")
    elif missing_items:
        missing_evidence.append("提交名单有、该来源未找到：" + "、".join(missing_items[:20]))
    if extra_items:
        missing_evidence.append("该来源有、提交名单未提供：" + "、".join(extra_items[:20]))
    if not year_match:
        missing_evidence.append("页面标题或目标图片标题未确认目标年份")

    status = _fact_status(target_match, year_match, coverage_complete)
    selected_urls = {artifact.source_url for artifact in selected_artifacts}
    decorative_urls = {
        artifact.source_url for artifact in artifacts
        if artifact.source_url not in selected_urls
    }
    def page_rows_complete(page: image_tools.VisionRosterPage) -> bool:
        if not page.is_roster_page:
            return True
        if not page.entries:
            return False
        if page.first_no is None or page.last_no is None:
            return True
        return len(page.entries) >= page.last_no - page.first_no + 1

    readable_vision_urls = {
        selected_artifacts[page.page - 1].source_url
        for page in batch.pages
        if 1 <= page.page <= len(selected_artifacts)
        and not page.unreadable
        and page_rows_complete(page)
    }
    processed_url_set = decorative_urls | readable_vision_urls
    processed_image_urls = [
        url for url in args.image_urls if url in processed_url_set
    ]
    failed_image_urls = [
        url for url in attempted_image_urls if url not in processed_url_set
    ]
    unprocessed_image_urls = [
        url for url in args.image_urls if url not in set(attempted_image_urls)
    ]
    fact = EvidenceFact(
        status=status,
        award_name=args.expected_award_name if target_match else "",
        year=args.expected_year if year_match else "",
        target_match="yes" if target_match else "uncertain",
        year_match="yes" if year_match else "uncertain",
        source_url=args.page_url,
        source_level=assessment.level,
        expected_count=expected_count,
        observed_count=len(matched_items),
        submitted_count=len(submitted_items),
        coverage_complete=coverage_complete,
        document_count=len(selected_artifacts),
        artifact_hashes=[item.sha256 for item in selected_artifacts],
        extraction_method="page_image_vision",
        scope_id=args.scope_id,
        role_type=args.role_type,
        comparison_scope=(
            args.section_keywords[0]
            if args.section_keywords
            else args.expected_award_name
        ),
        matched_items=matched_items[:10_000],
        missing_items=missing_items[:10_000],
        extra_items=extra_items[:10_000],
        unresolved_items=unresolved_items[:10_000],
        missing_item_count=len(missing_items),
        extra_item_count=len(extra_items),
        unresolved_item_count=len(unresolved_items),
        missing_evidence=missing_evidence[:20],
    )
    identity_records = [
        {
            "source_url": selected_artifacts[page.page - 1].source_url,
            "page": page.page,
            "section_title": page.section_title,
            "name": entry.name.strip(),
            "org": entry.org.strip(),
            "level": entry.level.strip(),
        }
        for page in batch.pages
        if 1 <= page.page <= len(selected_artifacts)
        for entry in page.entries
    ]
    return ToolResult(
        ok=True,
        data={
            "observed_award_name": fact.award_name,
            "observed_year": fact.year,
            "award_name_match": target_match,
            "year_match": year_match,
            "source_level": assessment.level,
            "selected_sections": [page.section_title for page in selected],
            "detected_sections": [
                page.section_title
                for page in batch.pages
                if page.section_title
            ][:20],
            "identity_records": identity_records[:10_000],
            "image_page_summaries": [
                {
                    "source_url": selected_artifacts[page.page - 1].source_url,
                    "is_roster_page": page.is_roster_page,
                    "section_title": page.section_title,
                    "entry_count": len(page.entries),
                    "first_no": page.first_no,
                    "last_no": page.last_no,
                    "truncated": page.truncated,
                    "unreadable": list(page.unreadable),
                    "confidence": page.confidence,
                    "row_count_complete": page_rows_complete(page),
                }
                for page in batch.pages
                if 1 <= page.page <= len(selected_artifacts)
            ],
            "expected_count": expected_count,
            "observed_count": len(matched_items),
            "coverage_complete": coverage_complete,
            "scope_id": args.scope_id,
            "role_type": args.role_type,
            "matched_items": matched_items[:10_000],
            "submitted_identity_items": submitted_identity_items,
            "matched_identity_hashes": matched_identity_hashes,
            "missing_items": missing_items[:10_000],
            "extra_items": extra_items[:10_000],
            "unresolved_items": unresolved_items[:10_000],
            "missing_item_count": len(missing_items),
            "extra_item_count": len(extra_items),
            "unresolved_item_count": len(unresolved_items),
            "vision_errors": batch.errors,
            "vision_error_count": len(batch.errors),
            "vision_error_codes": ",".join(sorted({
                str(error.get("error_code", "UNKNOWN"))
                for error in batch.errors
            })),
            "processed_image_urls": processed_image_urls,
            "failed_image_urls": failed_image_urls,
            "unprocessed_image_urls": unprocessed_image_urls,
            "all_images_processed": not (
                failed_image_urls or unprocessed_image_urls
            ),
        },
        source_url=args.page_url,
        fetched_at=fetched_at,
        artifacts=artifacts,
        evidence_facts=[fact],
        warnings=download_errors[:10],
    )


def _compare_roster(arguments: BaseModel, _context: ToolExecutionContext) -> ToolResult:
    args = CompareRosterInput.model_validate(arguments)
    comparison = image_tools.compare_rosters(
        args.submitted,
        args.official_pages,
        expected_total=args.expected_total,
        expected_first_no=args.expected_first_no,
    )
    return ToolResult(
        ok=True,
        data=comparison.model_dump(mode="json"),
        warnings=comparison.reason_codes,
    )


def _default_search_provider() -> search_tools.SearchProvider:
    return search_tools.FallbackSearchProvider(
        search_tools.AnySearchProvider(),
        search_tools.BingHtmlSearchProvider(),
    )


def _search_official_award(
    arguments: BaseModel,
    context: ToolExecutionContext,
    *,
    provider_factory: Callable[[], search_tools.SearchProvider],
) -> ToolResult:
    args = SearchOfficialAwardInput.model_validate(arguments)
    remaining = (
        context.budget.limits.max_candidate_urls - context.budget.candidate_urls
    )
    if remaining <= 0:
        raise ToolBudgetError("candidate URL budget exhausted")
    query = _official_search_query(args)
    provider = provider_factory()
    try:
        response = provider.search(query, max_results=min(args.max_results, remaining))
    except search_tools.SearchProviderError as exc:
        return ToolResult.failure(exc.code, str(exc)[:500])

    candidates: list[provenance.OfficialSearchCandidate] = []
    seen: set[str] = set()
    rejected = 0
    year_conflicts: list[dict[str, Any]] = []
    excluded = {item.strip().rstrip("/") for item in args.exclude_urls if item.strip()}
    excluded_count = 0
    expected_years = set(re.findall(r"(?:19|20)\d{2}", args.year))
    recovery_document_ids = {item.casefold() for item in args.recovery_terms}
    for hit in response.hits:
        observed_years = web.extract_years(f"{hit.title} {hit.snippet}")
        if expected_years and observed_years and expected_years.isdisjoint(observed_years):
            year_conflicts.append({
                "title": hit.title[:300],
                "url": hit.url[:2048],
                "observed_years": sorted(observed_years)[:8],
            })
            continue
        try:
            candidate = provenance.build_candidate(
                title=hit.title,
                url=hit.url,
                snippet=hit.snippet,
                provider=response.provider,
                rank=hit.rank,
                query=query,
                award_name=args.award_name,
                year=args.year,
                organizer=args.organizer,
                session=args.session,
                official_domains=args.official_domains,
                official_secondary_domains=args.official_secondary_domains,
            )
        except (SafetyError, ValueError):
            rejected += 1
            continue
        title_match, title_match_mode = _match_award_title(
            args.award_name,
            f"{hit.title} {hit.snippet}",
        )
        if title_match and "award_name_match" not in candidate.match_reasons:
            candidate = candidate.model_copy(update={
                "match_reasons": [
                    *candidate.match_reasons,
                    "award_name_match",
                    f"award_name_{title_match_mode}",
                ][:8],
            })
        elif _is_related_award_lead(
            args.award_name,
            f"{hit.title} {hit.snippet}",
        ):
            candidate = candidate.model_copy(update={
                "match_reasons": [
                    *candidate.match_reasons,
                    "award_related_candidate",
                ][:8],
            })
        if _matches_result_stage(f"{hit.title} {hit.snippet}"):
            candidate = candidate.model_copy(update={
                "match_reasons": [
                    *candidate.match_reasons,
                    "result_stage_match",
                ][:8],
            })
        candidate_document_id = unquote(
            urlsplit(candidate.url).path
        ).rstrip("/").rsplit("/", 1)[-1].casefold()
        if candidate_document_id in recovery_document_ids:
            candidate = candidate.model_copy(update={
                "match_reasons": [
                    *candidate.match_reasons,
                    "document_id_match",
                ][:8],
            })
        if candidate.url.rstrip("/") in excluded:
            excluded_count += 1
            continue
        if candidate.url in seen:
            continue
        seen.add(candidate.url)
        candidates.append(candidate)
        if len(candidates) >= remaining:
            break
    unqualified: list[provenance.OfficialSearchCandidate] = []
    award_unqualified: list[provenance.OfficialSearchCandidate] = []
    result_stage_mismatches: list[provenance.OfficialSearchCandidate] = []
    if args.require_award_name_match:
        require_result_stage = args.strategy != "discrepancy"
        award_unqualified = [
            item
            for item in candidates
            if not {
                "award_name_match",
                "award_related_candidate",
            }.intersection(item.match_reasons)
        ]
        result_stage_mismatches = [
            item
            for item in candidates
            if require_result_stage
            and {
                "award_name_match",
                "award_related_candidate",
            }.intersection(item.match_reasons)
            and not {
                "result_stage_match",
                "document_id_match",
            }.intersection(item.match_reasons)
        ]
        unqualified = [*award_unqualified, *result_stage_mismatches]
        candidates = [
            item
            for item in candidates
            if {
                "award_name_match",
                "award_related_candidate",
            }.intersection(item.match_reasons)
            and (
                not require_result_stage
                or {
                    "result_stage_match",
                    "document_id_match",
                }.intersection(item.match_reasons)
            )
        ]
    deferred_recovery_candidates: list[provenance.OfficialSearchCandidate] = []
    if args.recovery_terms:
        exact_recovery_candidates = [
            item for item in candidates if "document_id_match" in item.match_reasons
        ]
        if exact_recovery_candidates:
            deferred_recovery_candidates = [
                item for item in candidates if "document_id_match" not in item.match_reasons
            ]
            candidates = exact_recovery_candidates
    source_priority = {
        "official_primary": 0,
        "official_secondary": 1,
        "institutional_secondary": 2,
        "publisher_secondary": 3,
        "media_secondary": 4,
        "unknown": 5,
    }
    candidates.sort(key=lambda item: (
        0 if "document_id_match" in item.match_reasons else 1,
        0 if "result_stage_match" in item.match_reasons else 1,
        0 if "explicit_official_domain_match" in item.match_reasons else 1,
        source_priority.get(item.source_level, 6),
        0 if "award_name_match" in item.match_reasons else 1,
        0 if "year_match" in item.match_reasons else 1,
        item.rank,
    ))
    context.add_candidate_urls(len(candidates))
    official = [
        item
        for item in candidates
        if item.source_level == "official_primary"
        or (
            item.source_level == "official_secondary"
            and "award_name_match" in item.match_reasons
        )
    ]
    related = [
        item for item in candidates if "award_related_candidate" in item.match_reasons
    ]
    manual_required = not official
    warnings_out = ["search_results_are_leads_not_evidence", *response.warnings]
    if rejected:
        warnings_out.append(f"unsafe_or_invalid_candidates_rejected:{rejected}")
    if excluded_count:
        warnings_out.append(f"previously_attempted_candidates_excluded:{excluded_count}")
    if year_conflicts:
        warnings_out.append(f"explicit_year_conflicts_rejected:{len(year_conflicts)}")
    if award_unqualified:
        warnings_out.append(
            f"award_name_unmatched_candidates_rejected:{len(award_unqualified)}"
        )
    if result_stage_mismatches:
        warnings_out.append(
            f"result_stage_candidates_rejected:{len(result_stage_mismatches)}"
        )
    if related:
        warnings_out.append(
            f"related_award_candidates_require_fetch:{len(related)}"
        )
    if deferred_recovery_candidates:
        warnings_out.append(
            "nonmatching_recovery_candidates_deferred:"
            f"{len(deferred_recovery_candidates)}"
        )
    if manual_required:
        warnings_out.append("no_qualified_official_candidate")
    return ToolResult(
        ok=True,
        data={
            "provider": response.provider,
            "strategy": args.strategy,
            "query": query,
            "candidates": [item.model_dump(mode="json") for item in candidates],
            "candidate_count": len(candidates),
            "related_candidate_count": len(related),
            "official_candidate_count": len(official),
            "year_conflict_count": len(year_conflicts),
            "year_conflict_candidates": year_conflicts[:10],
            "unqualified_candidate_count": len(unqualified),
            "unqualified_candidates": [
                {
                    "title": item.title,
                    "url": item.url,
                    "source_level": item.source_level,
                    "match_reasons": item.match_reasons,
                }
                for item in unqualified[:10]
            ],
            "result_stage_mismatch_count": len(result_stage_mismatches),
            "result_stage_mismatch_candidates": [
                {
                    "title": item.title,
                    "url": item.url,
                    "source_level": item.source_level,
                    "match_reasons": item.match_reasons,
                }
                for item in result_stage_mismatches[:10]
            ],
            "deferred_recovery_candidate_count": len(
                deferred_recovery_candidates
            ),
            "manual_required": manual_required,
            "next_action": "fetch_and_verify" if candidates else "manual",
            "request_id": response.request_id,
        },
        warnings=warnings_out,
    )


def _extract_search_document(
    arguments: BaseModel,
    context: ToolExecutionContext,
    *,
    provider_factory: Callable[[], search_tools.SearchProvider],
) -> ToolResult:
    """Extract one already selected public URL through the configured search provider."""

    args = ExtractSearchDocumentInput.model_validate(arguments)
    provider = provider_factory()
    try:
        extracted = provider.extract(args.url, query_hint=args.search_query)
    except search_tools.SearchProviderError as exc:
        return ToolResult.failure(exc.code, str(exc)[:500], source_url=args.url)
    normalized = _normalise_match(extracted.text)
    relationship_confirmed: bool | None = None
    relationship_summary = ""
    if args.relationship_terms:
        relationship_confirmed = all(
            _normalise_match(term) in normalized for term in args.relationship_terms
        )
        if relationship_confirmed:
            relationship_summary = (
                "该补证来源同时出现差异姓名与群体名称，支持二者属于同一业务名额的对应关系"
            )
    award_match, award_match_mode = _match_award_title(
        args.expected_award_name, extracted.text, aliases=args.award_aliases
    )
    year_match = bool(args.expected_year and args.expected_year in web.extract_years(
        extracted.text
    ))
    has_full_text_context = len(normalized) >= 1000
    target_match_state: FactMatch = (
        "yes" if award_match else ("no" if has_full_text_context else "uncertain")
    )
    year_match_state: FactMatch = (
        "yes" if year_match else ("no" if has_full_text_context else "uncertain")
    )
    section_hits = [
        keyword for keyword in args.section_keywords if _normalise_match(keyword) in normalized
    ]
    expected_count = args.expected_scope_count
    observed_count: int | None = None
    submitted_count: int | None = None
    coverage_complete: bool | None = None
    missing: list[str] = []
    if args.submitted_paths:
        identities = _submitted_match_items_from_paths(
            args.submitted_paths,
            args.match_fields,
            context,
            match_combine=args.match_combine,
        )
        submitted_count = len(identities)
        expected_count = max(submitted_count, args.expected_scope_count or 0)
        matches = [
            (*identity, *_identity_matches(identity[2], identity[1], normalized))
            for identity in identities
        ]
        matched_items = [display for _field, _value, display, matched, _split in matches if matched]
        split_matched_items = [
            display for _field, _value, display, matched, split in matches if matched and split
        ]
        missing_items = [
            display for _field, _value, display, matched, _split in matches if not matched
        ]
        observed_count = len(matched_items)
        coverage_complete = bool(
            expected_count and observed_count >= expected_count and not extracted.is_truncated
        )
        if not matched_items and not has_full_text_context:
            observed_count = None
            coverage_complete = None
            missing_items = []
            missing.append("搜索服务仅返回摘要，缺少可核验名单正文")
        elif missing_items:
            missing.append(
                f"提交名单有、该来源未找到：{'、'.join(missing_items[:20])}"[:500]
            )
    else:
        matched_items = []
        split_matched_items = []
        missing_items = []
    if args.section_keywords and len(section_hits) != len(args.section_keywords):
        missing.append("指定名单章节未完整提取")
        coverage_complete = False if coverage_complete is not None else None
    if extracted.is_truncated:
        missing.append("Provider 提取结果被截断")
        coverage_complete = False if coverage_complete is not None else None
    assessment = provenance.classify_source(
        extracted.url,
        official_domains=[],
        official_secondary_domains=[],
    )
    if "no" in {target_match_state, year_match_state}:
        fact_status: FactStatus = "conflict"
    elif "uncertain" in {target_match_state, year_match_state}:
        fact_status = "unverified"
    else:
        fact_status = _fact_status(award_match, year_match, coverage_complete)
    fact = EvidenceFact(
        status=fact_status,
        award_name=args.expected_award_name if award_match else "",
        year=args.expected_year if year_match else "",
        target_match=target_match_state,
        year_match=year_match_state,
        source_url=extracted.url,
        source_level=assessment.level,
        expected_count=expected_count,
        observed_count=observed_count,
        submitted_count=submitted_count,
        reference_count=args.page_total_count,
        coverage_complete=coverage_complete,
        extraction_method=f"{extracted.provider}_extract",
        comparison_scope=(
            args.section_keywords[0] if args.section_keywords else "submitted_roster"
        ),
        matched_items=matched_items[:10_000],
        split_matched_items=split_matched_items[:10_000],
        missing_items=missing_items[:10_000],
        missing_item_count=len(missing_items),
        missing_evidence=missing,
        relationship_terms=args.relationship_terms,
        relationship_confirmed=relationship_confirmed,
        relationship_summary=relationship_summary,
    )
    return ToolResult(
        ok=True,
        data={
            "provider": extracted.provider,
            "observed_award_name": fact.award_name,
            "award_name_match": (
                True if target_match_state == "yes"
                else False if target_match_state == "no"
                else None
            ),
            "award_name_match_mode": award_match_mode,
            "observed_year": fact.year,
            "year_match": (
                True if year_match_state == "yes"
                else False if year_match_state == "no"
                else None
            ),
            "source_level": fact.source_level,
            "section_hits": section_hits,
            "expected_count": expected_count,
            "observed_count": observed_count,
            "coverage_complete": coverage_complete,
            "page_total_count": args.page_total_count,
            "matched_items": matched_items[:10_000],
            "split_matched_items": split_matched_items[:10_000],
            "missing_items": missing_items[:10_000],
            "extra_items": [],
            "relationship_terms": args.relationship_terms,
            "relationship_confirmed": relationship_confirmed,
            "relationship_summary": relationship_summary,
        },
        source_url=extracted.url,
        fetched_at=utc_now(),
        is_truncated=extracted.is_truncated,
        evidence_facts=[fact],
        warnings=["content_extracted_via_search_provider"],
    )


def build_default_registry(
    *,
    pdftoppm: str = "",
    ocr_engine_factory: image_tools.OcrFactory | None = None,
    vision_client_factory: image_tools.VisionClientFactory | None = None,
    search_provider_factory: Callable[[], search_tools.SearchProvider] | None = None,
) -> ToolRegistry:
    """Build the M5.1-M5.3 production whitelist with injectable external backends."""

    registry = ToolRegistry()
    registry.register(ToolSpec(
        name="fetch_web_page",
        description=(
            "Fetch a supplied public HTTP(S) page before searching elsewhere. Optionally "
            "verify its title award/year and deterministically measure page-text coverage "
            "against selected fields in a validated submitted spreadsheet."
        ),
        input_model=FetchWebPageInput,
        kind="general",
        risk="network",
        timeout_seconds=20,
    ), _fetch_web_page)
    registry.register(ToolSpec(
        name="download_evidence",
        description=(
            "Download one public evidence file into the executor-controlled evidence "
            "directory; destination_dir is optional and remains whitelist constrained. "
            "PDF downloads also return bounded page/text-versus-scan inspection metadata."
        ),
        input_model=DownloadEvidenceInput,
        kind="download",
        risk="high",
        timeout_seconds=75,
    ), _download_evidence)
    registry.register(ToolSpec(
        name="verify_page_image_roster",
        description=(
            "Download up to six roster images already discovered on one public page, "
            "extract each image's visible section title and names, select only the trusted "
            "target section, and return concrete submitted-versus-source differences."
        ),
        input_model=VerifyPageImageRosterInput,
        kind="download",
        risk="high",
        timeout_seconds=480,
    ), lambda args, context: _verify_page_image_roster(
        args,
        context,
        client_factory=vision_client_factory or _default_vision_client,
        isolate_client=vision_client_factory is None,
    ))
    registry.register(ToolSpec(
        name="collect_spreadsheet_attachments",
        description=(
            "Download and combine related spreadsheet roster fragments within the per-case "
            "100-download safety budget from "
            "official page URLs, then deterministically measure their joint coverage of "
            "selected submitted field codes. Use attachment keywords to exclude unrelated "
            "categories such as organizer awards."
        ),
        input_model=CollectSpreadsheetAttachmentsInput,
        kind="download",
        risk="high",
        timeout_seconds=120,
    ), _collect_spreadsheet_attachments)
    registry.register(ToolSpec(
        name="parse_spreadsheet",
        description="Parse a validated local XLS/XLSX evidence file into a bounded grid.",
        input_model=ParseSpreadsheetInput,
        kind="general",
        risk="filesystem",
        timeout_seconds=30,
    ), _parse_spreadsheet)
    registry.register(ToolSpec(
        name="inspect_pdf",
        description=(
            "Inspect a validated local PDF and report page-level text presence and scan "
            "candidates without returning raw document text."
        ),
        input_model=InspectPdfInput,
        kind="general",
        risk="high",
        timeout_seconds=45,
    ), _inspect_pdf)
    registry.register(ToolSpec(
        name="extract_pdf_text",
        description=(
            "Extract bounded text and table candidates from explicit 1-based PDF pages."
        ),
        input_model=ExtractPdfTextInput,
        kind="general",
        risk="high",
        timeout_seconds=60,
    ), _extract_pdf_text)
    registry.register(ToolSpec(
        name="render_pdf_pages",
        description=(
            "Render only explicit PDF pages to verified PNG evidence under the "
            "executor-controlled evidence root; output_dir is optional and whitelist "
            "constrained."
        ),
        input_model=RenderPdfPagesInput,
        kind="general",
        risk="high",
        timeout_seconds=180,
    ), lambda args, context: _render_pdf_pages(
        args, context, pdftoppm=pdftoppm
    ))
    registry.register(ToolSpec(
        name="ocr_image",
        description=(
            "Run bounded local RapidOCR over verified page images; OCR cannot assert roster "
            "completeness by itself."
        ),
        input_model=OcrImageInput,
        kind="general",
        risk="high",
        timeout_seconds=240,
    ), lambda args, context: _ocr_image(
        args, context, engine_factory=ocr_engine_factory
    ))
    registry.register(ToolSpec(
        name="vision_extract_roster",
        description=(
            "Extract fixed-schema roster entries from at most six verified candidate page "
            "images using the configured vision model."
        ),
        input_model=VisionExtractRosterInput,
        kind="general",
        risk="high",
        timeout_seconds=480,
    ), lambda args, context: _vision_extract_roster(
        args,
        context,
        client_factory=vision_client_factory or _default_vision_client,
        isolate_client=vision_client_factory is None,
    ))
    registry.register(ToolSpec(
        name="compare_roster",
        description=(
            "Deterministically compare submitted and official roster entries after page, "
            "sequence, total and truncation coverage checks."
        ),
        input_model=CompareRosterInput,
        kind="general",
        risk="low",
        timeout_seconds=30,
    ), _compare_roster)
    registry.register(ToolSpec(
        name="search_official_award",
        description=(
            "Search for bounded official-award leads by deterministic metadata. Results are "
            "not evidence until fetched and verified with another tool."
        ),
        input_model=SearchOfficialAwardInput,
        kind="search",
        risk="network",
        timeout_seconds=30,
    ), lambda args, context: _search_official_award(
        args,
        context,
        provider_factory=search_provider_factory or _default_search_provider,
    ))
    registry.register(ToolSpec(
        name="extract_search_document",
        description=(
            "Extract bounded text and requested sections from one already selected public URL "
            "through the configured search provider after direct fetch failure. The URL remains "
            "the provenance source and the result is re-verified locally."
        ),
        input_model=ExtractSearchDocumentInput,
        kind="general",
        risk="network",
        timeout_seconds=45,
    ), lambda args, context: _extract_search_document(
        args,
        context,
        provider_factory=search_provider_factory or _default_search_provider,
    ))
    return registry
