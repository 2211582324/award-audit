"""Shared, provider-neutral contracts for M5 tools."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ToolKind = Literal["general", "search", "download"]
RiskLevel = Literal["low", "network", "filesystem", "high"]
FactStatus = Literal["complete", "partial", "missing", "conflict", "unverified"]
FactMatch = Literal["yes", "no", "uncertain"]
EvidenceAssetStatus = Literal[
    "discovered", "downloaded", "parsed", "failed", "access_denied", "skipped"
]


def utc_now() -> str:
    """Return a compact, timezone-aware timestamp suitable for audit records."""

    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class EvidenceArtifact(BaseModel):
    """One immutable evidence file and its provenance."""

    kind: str = Field(min_length=1)
    source_url: str = Field(min_length=1)
    local_path: str = Field(min_length=1)
    content_type: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)
    fetched_at: str = Field(min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)


class EvidenceAssetRecord(BaseModel):
    """A persistent public-source asset shared by M4 discovery and M5 recovery."""

    model_config = ConfigDict(extra="forbid")

    asset_version: Literal[1] = 1
    url: str = Field(min_length=1, max_length=2048)
    parent_url: str = Field(default="", max_length=2048)
    label: str = Field(default="", max_length=500)
    kind: str = Field(default="unknown", min_length=1, max_length=40)
    status: EvidenceAssetStatus = "discovered"
    content_type: str = Field(default="", max_length=200)
    sha256: str = Field(default="", pattern=r"^$|^[0-9a-f]{64}$")
    size_bytes: int = Field(default=0, ge=0)
    fetched_at: str = Field(default="", max_length=80)
    local_path: str = Field(default="", max_length=2048)
    truncated: bool = False
    extraction_method: str = Field(default="", max_length=100)
    error_code: str = Field(default="", max_length=100)
    error_message: str = Field(default="", max_length=500)
    metadata: dict[str, Any] = Field(default_factory=dict, max_length=30)


class EvidenceFact(BaseModel):
    """One internally coherent fact bundle passed from a Tool to the Verifier.

    Counts, target, year and provenance deliberately travel together.  This prevents
    the Verifier from combining the coverage of one document with the authority or
    year of another document.
    """

    model_config = ConfigDict(extra="forbid")

    status: FactStatus = "unverified"
    award_name: str = Field(default="", max_length=200)
    year: str = Field(default="", max_length=20)
    target_match: FactMatch = "uncertain"
    year_match: FactMatch = "uncertain"
    source_url: str = Field(default="", max_length=2048)
    source_level: str = Field(default="unknown", max_length=80)
    expected_count: int | None = Field(default=None, ge=0, le=1_000_000)
    observed_count: int | None = Field(default=None, ge=0, le=1_000_000)
    submitted_count: int | None = Field(default=None, ge=0, le=1_000_000)
    reference_count: int | None = Field(default=None, ge=0, le=1_000_000)
    coverage_complete: bool | None = None
    document_count: int = Field(default=1, ge=0, le=100)
    artifact_hashes: list[str] = Field(default_factory=list, max_length=100)
    extraction_method: str = Field(default="", max_length=100)
    comparison_scope: str = Field(default="", max_length=100)
    scope_id: int = Field(default=0, ge=0)
    role_type: str = Field(default="", max_length=40)
    document_complete: bool | None = None
    matched_items: list[str] = Field(default_factory=list, max_length=10_000)
    split_matched_items: list[str] = Field(default_factory=list, max_length=10_000)
    missing_items: list[str] = Field(default_factory=list, max_length=10_000)
    extra_items: list[str] = Field(default_factory=list, max_length=10_000)
    unresolved_items: list[str] = Field(default_factory=list, max_length=10_000)
    missing_item_count: int = Field(default=0, ge=0, le=1_000_000)
    extra_item_count: int = Field(default=0, ge=0, le=1_000_000)
    unresolved_item_count: int = Field(default=0, ge=0, le=1_000_000)
    missing_evidence: list[str] = Field(default_factory=list, max_length=20)
    contradictions: list[str] = Field(default_factory=list, max_length=20)
    relationship_terms: list[str] = Field(default_factory=list, max_length=8)
    relationship_confirmed: bool | None = None
    relationship_summary: str = Field(default="", max_length=500)
    is_evidence: bool = True


class ToolResult(BaseModel):
    """The only result shape exposed by tools to an agent or harness."""

    ok: bool
    data: dict[str, Any] = Field(default_factory=dict)
    source_url: str = ""
    local_path: str = ""
    content_type: str = ""
    sha256: str = ""
    fetched_at: str = ""
    is_truncated: bool = False
    warnings: list[str] = Field(default_factory=list)
    artifacts: list[EvidenceArtifact] = Field(default_factory=list)
    evidence_facts: list[EvidenceFact] = Field(default_factory=list, max_length=50)
    error_code: str = ""
    error_message: str = ""

    @model_validator(mode="after")
    def _consistent_status(self) -> ToolResult:
        if self.ok and (self.error_code or self.error_message):
            raise ValueError("successful ToolResult cannot contain an error")
        if not self.ok and not self.error_code:
            raise ValueError("failed ToolResult requires error_code")
        return self

    @classmethod
    def failure(cls, code: str, message: str, **kwargs: Any) -> ToolResult:
        return cls(ok=False, error_code=code, error_message=message, **kwargs)


class ToolSpec(BaseModel):
    """A registered tool's stable public contract."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    name: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    description: str = Field(min_length=1, max_length=500)
    input_model: type[BaseModel] = Field(exclude=True)
    kind: ToolKind = "general"
    risk: RiskLevel = "low"
    timeout_seconds: float = Field(default=30.0, gt=0, le=480.0)

    def openai_schema(self) -> dict[str, Any]:
        """Render the contract in OpenAI's function-tool format."""

        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_model.model_json_schema(),
            },
        }


class ToolObservation(BaseModel):
    """A bounded, secret-free record of one attempted invocation."""

    call_id: str
    tool_name: str
    started_at: str
    finished_at: str
    duration_ms: int = Field(ge=0)
    input_summary: dict[str, Any] = Field(default_factory=dict)
    output_summary: dict[str, Any] = Field(default_factory=dict)
    ok: bool
    error_code: str = ""


class ToolBudgetLimits(BaseModel):
    """v1.0 conservative per-case limits; M5.4 may calibrate them."""

    max_calls: int = Field(default=24, ge=1)
    max_asset_calls: int = Field(default=400, ge=1)
    max_searches: int = Field(default=3, ge=0)
    max_candidate_urls: int = Field(default=8, ge=0)
    max_downloads: int = Field(default=100, ge=0)
    max_file_bytes: int = Field(default=20 * 1024 * 1024, ge=1)
    max_total_download_bytes: int = Field(default=80 * 1024 * 1024, ge=1)
    max_pdf_pages: int = Field(default=300, ge=1, le=500)
    max_render_pages: int = Field(default=20, ge=1, le=100)
    max_ocr_pages: int = Field(default=20, ge=0, le=100)
    max_vision_pages: int = Field(default=80, ge=0, le=100)
    max_image_pixels: int = Field(default=20_000_000, ge=1)
    max_total_image_pixels: int = Field(default=160_000_000, ge=1)
    wall_time_seconds: float = Field(default=8 * 60, gt=0)


class ToolBudgetState(BaseModel):
    """Mutable counters owned by one execution context."""

    limits: ToolBudgetLimits = Field(default_factory=ToolBudgetLimits)
    calls: int = Field(default=0, ge=0)
    asset_calls: int = Field(default=0, ge=0)
    searches: int = Field(default=0, ge=0)
    candidate_urls: int = Field(default=0, ge=0)
    downloads: int = Field(default=0, ge=0)
    download_bytes: int = Field(default=0, ge=0)
    pdf_pages: int = Field(default=0, ge=0)
    rendered_pages: int = Field(default=0, ge=0)
    ocr_pages: int = Field(default=0, ge=0)
    vision_pages: int = Field(default=0, ge=0)
    image_pixels: int = Field(default=0, ge=0)
