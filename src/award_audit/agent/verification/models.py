"""Provider-neutral evidence snapshot and VerificationReport contracts."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

MatchState = Literal["yes", "no", "uncertain"]
SourceAuthority = Literal["official", "secondary", "unknown"]
VerificationAction = Literal["accept_evidence", "supplement", "manual"]
ReviewRoute = Literal["auto_approve", "waiting_human", "fail_closed"]
ShortFact = Annotated[str, Field(max_length=500)]
ShortCode = Annotated[str, Field(max_length=100)]


class SupplementRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1, max_length=100)
    question: str = Field(min_length=1, max_length=500)
    suggested_tools: list[ShortCode] = Field(default_factory=list, max_length=5)


class EvidenceSnapshot(BaseModel):
    """Bounded structured facts; raw pages and hidden reasoning never enter Verifier."""

    model_config = ConfigDict(extra="forbid")

    expected_award_name: str = Field(default="", max_length=200)
    expected_year: str = Field(default="", max_length=20)
    observed_award_names: list[ShortFact] = Field(default_factory=list, max_length=10)
    observed_years: list[ShortCode] = Field(default_factory=list, max_length=20)
    source_levels: list[ShortCode] = Field(default_factory=list, max_length=20)
    explicit_target_match: MatchState | None = None
    explicit_year_match: MatchState | None = None
    expected_count: int | None = Field(default=None, ge=0, le=1_000_000)
    observed_count: int | None = Field(default=None, ge=0, le=1_000_000)
    total_pages: int | None = Field(default=None, ge=0, le=10_000)
    processed_pages: int | None = Field(default=None, ge=0, le=10_000)
    sequence_complete: bool | None = None
    explicit_coverage_complete: bool | None = None
    zero_overlap: bool = False
    contradictions: list[ShortFact] = Field(default_factory=list, max_length=20)
    missing_evidence: list[ShortFact] = Field(default_factory=list, max_length=20)


class VerificationReport(BaseModel):
    """Evidence-quality decision only; this contract has no approval action."""

    model_config = ConfigDict(extra="forbid")

    target_match: MatchState
    year_match: MatchState
    source_authority: SourceAuthority
    coverage_complete: MatchState
    contradictions: list[ShortFact] = Field(default_factory=list, max_length=20)
    missing_evidence: list[ShortFact] = Field(default_factory=list, max_length=20)
    supplement_requests: list[SupplementRequest] = Field(default_factory=list, max_length=20)
    recommended_action: VerificationAction
    reason_codes: list[ShortCode] = Field(default_factory=list, max_length=30)
    deterministic_action: VerificationAction
    model_used: bool = False


class AutoApprovalPolicy(BaseModel):
    """Explicit business gate; disabled deployments can never auto-approve."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    require_official_primary: bool = True
    require_complete_coverage: bool = True
    require_target_and_year: bool = True
    reject_any_missing_evidence: bool = True
    reject_any_contradiction: bool = True


class VerifierCallUsage(BaseModel):
    """Provider usage and safe route metadata for one model Verifier call."""

    route: Literal["native", "structured"]
    outcome: Literal["success", "failed"] = "success"
    provider_usage_reported: bool = False
    total_tokens: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cached_input_tokens: int = Field(default=0, ge=0)
    reasoning_output_tokens: int = Field(default=0, ge=0)
    cache_detail_reported: bool = False
    prompt_chars: int = Field(default=0, ge=0)
    schema_chars: int = Field(default=0, ge=0)
