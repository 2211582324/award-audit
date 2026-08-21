"""Explicit M5.4 case, action, client and Harness contracts."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from award_audit.agent.toolkit.contracts import (
    EvidenceArtifact,
    EvidenceAssetRecord,
    ToolBudgetState,
    ToolObservation,
)
from award_audit.agent.verification.models import VerificationReport, VerifierCallUsage

TriggerCode = Literal[
    "SOURCE_URL_MISSING",
    "SOURCE_UNREACHABLE",
    "PDF_ONLY",
    "IMAGE_ONLY",
    "COLUMN_AMBIGUOUS",
    "PAGE_TARGET_UNCERTAIN",
    "ZERO_OVERLAP",
    "EVIDENCE_CONFLICT",
    "COVERAGE_UNKNOWN",
    "SOFT_RULE_SUSPECT",
]
CaseStatus = Literal["queued", "running", "waiting_human", "completed", "failed"]
Confidence = Literal["low", "medium", "high"]
ActionKind = Literal["call_tool", "finish", "manual"]
EvidencePhase = Literal[
    "initial",
    "known_source",
    "candidate_search",
    "candidate_recovery",
    "spreadsheet_processing",
    "image_processing",
    "document_processing",
    "evidence_ready",
    "verifying",
    "waiting_human",
    "fail_closed",
    "auto_approved",
]


class EvidenceCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(min_length=1, max_length=2048)
    source_level: str = Field(default="unknown", max_length=80)
    provider: str = Field(default="", max_length=40)
    rank: int = Field(default=0, ge=0, le=100)
    title: str = Field(default="", max_length=300)
    query: str = Field(default="", max_length=300)
    status: Literal["pending", "succeeded", "failed", "skipped"] = "pending"
    attempts: int = Field(default=0, ge=0, le=10)
    status_reason: str = Field(default="", max_length=200)
    relevance: Literal["relevant", "unreviewed", "excluded"] = "unreviewed"
    relevance_score: int = Field(default=0, ge=-100, le=100)


class EvidenceProgress(BaseModel):
    """Persisted evidence-state machine and bounded recovery queue."""

    model_config = ConfigDict(extra="forbid")

    phase: EvidencePhase = "initial"
    candidates: list[EvidenceCandidate] = Field(default_factory=list, max_length=8)
    search_round: int = Field(default=0, ge=0, le=3)
    source_failures: int = Field(default=0, ge=0, le=100)
    successful_sources: int = Field(default=0, ge=0, le=100)
    m4_result_id: int = Field(default=0, ge=0)
    pending_attachment_page_urls: list[str] = Field(default_factory=list, max_length=20)
    pending_attachment_urls: list[str] = Field(default_factory=list, max_length=100)
    pending_attachment_parent_urls: dict[str, str] = Field(default_factory=dict, max_length=100)
    failed_attachment_urls: list[str] = Field(default_factory=list, max_length=100)
    pending_media_source_url: str = Field(default="", max_length=2048)
    pending_media_page_title: str = Field(default="", max_length=500)
    pending_media_urls: list[str] = Field(default_factory=list, max_length=100)
    pending_media_parent_urls: dict[str, str] = Field(default_factory=dict, max_length=100)
    media_expected_items: dict[str, str] = Field(default_factory=dict, max_length=10_000)
    media_matched_identity_hashes: list[str] = Field(
        default_factory=list, max_length=10_000
    )
    media_extra_items: list[str] = Field(default_factory=list, max_length=1_000)
    media_failed_urls: list[str] = Field(default_factory=list, max_length=100)
    media_scope_accumulators: dict[str, dict[str, Any]] = Field(
        default_factory=dict, max_length=50
    )

    def pending_urls(self) -> list[str]:
        return [item.url for item in self.candidates if item.status == "pending"]

    def has_pending_media(self) -> bool:
        return bool(self.pending_media_source_url and self.pending_media_urls)

    def has_pending_attachments(self) -> bool:
        return bool(self.pending_attachment_page_urls and self.pending_attachment_urls)


class NextAction(BaseModel):
    """The only action shape an Agent may return; never contains hidden reasoning."""

    model_config = ConfigDict(extra="forbid")

    action: ActionKind
    tool_name: str = Field(default="", max_length=64, pattern=r"^$|^[a-z][a-z0-9_]{1,63}$")
    arguments: dict[str, Any] = Field(default_factory=dict, max_length=30)
    reason_summary: str = Field(default="", max_length=500)
    expected_evidence: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def _coherent_action(self) -> NextAction:
        if self.action == "call_tool" and not self.tool_name:
            raise ValueError("call_tool requires tool_name")
        if self.action != "call_tool" and (self.tool_name or self.arguments):
            raise ValueError("finish/manual cannot include a tool call")
        return self


class CaseSeed(BaseModel):
    model_config = ConfigDict(extra="forbid")

    batch_id: int = Field(gt=0)
    origin_m4_result_id: int = Field(default=0, ge=0)
    resource_code: str = Field(min_length=1, max_length=40)
    award_name: str = Field(default="", max_length=200)
    year: str = Field(default="", max_length=20)
    trigger_codes: list[TriggerCode] = Field(min_length=1, max_length=10)
    objective: str = Field(min_length=1, max_length=1000)
    submitted_summary: dict[str, Any] = Field(default_factory=dict, max_length=50)
    known_urls: list[str] = Field(default_factory=list, max_length=20)
    open_questions: list[str] = Field(default_factory=list, max_length=20)


class M4EvidenceBundle(BaseModel):
    """Bounded evidence recovered from the exact current M4 result."""

    model_config = ConfigDict(extra="forbid")

    bundle_version: Literal[1] = 1
    identity_version: Literal["identity-v1", "identity-v2"] = "identity-v2"
    result_id: int = Field(gt=0)
    resource_code: str = Field(min_length=1, max_length=40)
    award_name: str = Field(default="", max_length=200)
    year: str = Field(default="", max_length=20)
    page_year: str = Field(default="", max_length=20)
    verdict: str = Field(default="无法核对", max_length=100)
    confidence: str = Field(default="low", max_length=20)
    triage: str = Field(default="manual", max_length=20)
    review_status: str = Field(default="待复核", max_length=20)
    source_kind: str = Field(default="none", max_length=40)
    source_urls: list[str] = Field(default_factory=list, max_length=20)
    found_assets: list[str] = Field(default_factory=list, max_length=50)
    assets: list[EvidenceAssetRecord] = Field(default_factory=list, max_length=100)
    evidence: list[str] = Field(default_factory=list, max_length=50)
    reason_codes: list[str] = Field(default_factory=list, max_length=50)
    submitted_count: int = Field(default=0, ge=0, le=1_000_000)
    extracted_count: int = Field(default=0, ge=0, le=1_000_000)
    missing: list[str] = Field(default_factory=list, max_length=200)
    extra: list[str] = Field(default_factory=list, max_length=200)
    notes: str = Field(default="", max_length=2000)


class HarnessLimits(BaseModel):
    max_steps: int = Field(default=3, ge=1, le=50)
    max_tokens: int = Field(default=24_000, ge=1, le=1_000_000)
    max_agent_input_tokens: int = Field(default=8_000, ge=1000, le=100_000)
    max_consecutive_tool_failures: int = Field(default=2, ge=1, le=10)
    max_observation_chars: int = Field(default=8_000, ge=1000, le=100_000)


class LlmTurnUsage(BaseModel):
    """Provider-reported usage plus local repeated-context size for one turn."""

    step: int = Field(default=0, ge=0)
    route: Literal["native", "structured", "unknown"] = "unknown"
    outcome: Literal["success", "failed"] = "success"
    provider_usage_reported: bool = False
    total_tokens: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cached_input_tokens: int = Field(default=0, ge=0)
    reasoning_output_tokens: int = Field(default=0, ge=0)
    cache_detail_reported: bool = False
    prompt_chars: int = Field(default=0, ge=0)
    tool_schema_chars: int = Field(default=0, ge=0)


class AuditCaseState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: int = Field(default=0, ge=0)
    active_attempt_id: int = Field(default=0, ge=0)
    attempt_sequence: int = Field(default=0, ge=0)
    batch_id: int = Field(gt=0)
    origin_m4_result_id: int = Field(default=0, ge=0)
    m4_evidence: M4EvidenceBundle | None = None
    resource_code: str = Field(min_length=1, max_length=40)
    award_name: str = Field(default="", max_length=200)
    year: str = Field(default="", max_length=20)
    trigger_codes: list[TriggerCode] = Field(min_length=1, max_length=10)
    objective: str = Field(min_length=1, max_length=1000)
    submitted_summary: dict[str, Any] = Field(default_factory=dict, max_length=50)
    known_urls: list[str] = Field(default_factory=list, max_length=20)
    retrieved_memories: list[dict[str, Any]] = Field(default_factory=list, max_length=20)
    artifacts: list[EvidenceArtifact] = Field(default_factory=list, max_length=100)
    tool_trace: list[ToolObservation] = Field(default_factory=list, max_length=100)
    open_questions: list[str] = Field(default_factory=list, max_length=20)
    evidence_progress: EvidenceProgress = Field(default_factory=EvidenceProgress)
    budget: ToolBudgetState = Field(default_factory=ToolBudgetState)
    step_count: int = Field(default=0, ge=0)
    token_used: int = Field(default=0, ge=0)
    llm_usage: list[LlmTurnUsage] = Field(default_factory=list, max_length=50)
    verifier_llm_usage: list[VerifierCallUsage] = Field(default_factory=list, max_length=10)
    elapsed_ms: int = Field(default=0, ge=0)
    reflection_count: int = Field(default=0, ge=0, le=1)
    latest_verification: VerificationReport | None = None
    status: CaseStatus = "queued"
    recommendation: str = Field(default="", max_length=2000)
    confidence: Confidence = "low"
    reason_codes: list[str] = Field(default_factory=list, max_length=50)
    last_action: NextAction | None = None
    last_error: str = Field(default="", max_length=500)
    last_error_detail: str = Field(default="", max_length=200)
    pending_supplement: str = Field(default="", max_length=1000)
    human_decision: Literal["", "accepted", "rejected", "insufficient"] = ""
    human_decision_summary: str = Field(default="", max_length=2000)
    reviewed_by: str = Field(default="", max_length=200)
    reviewed_at: str = Field(default="", max_length=50)
    state_version: int = Field(default=1, ge=1)

    @classmethod
    def from_seed(cls, seed: CaseSeed, budget: ToolBudgetState) -> AuditCaseState:
        return cls(**seed.model_dump(), budget=budget)


class AgentTurnContext(BaseModel):
    """Bounded model-facing context; external observations are explicitly untrusted."""

    case: dict[str, Any]
    observations: list[dict[str, Any]] = Field(default_factory=list, max_length=20)
    skill_instructions: str = Field(default="", max_length=12_000)
    external_content_is_untrusted: bool = True


class AgentDecision(BaseModel):
    action: NextAction
    token_used: int = Field(default=0, ge=0)
    usage: LlmTurnUsage = Field(default_factory=LlmTurnUsage)
    route: Literal["native", "structured", "fake"]
    warnings: list[str] = Field(default_factory=list, max_length=10)


class HarnessOutcome(BaseModel):
    state: AuditCaseState
    stopped_reason: str
