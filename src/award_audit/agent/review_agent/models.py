"""Strict, bounded contracts for the case-level M5 review agent."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

AgentRole = Literal["project", "team", "person", "organization", "special", "mixed"]
AssetStatus = Literal[
    "discovered", "downloaded", "parsed", "failed", "access_denied", "skipped"
]
MaterialRelation = Literal[
    "primary",
    "supplement",
    "volume_pair",
    "related_out_of_scope",
    "unrelated",
    "url_migration",
]
VersionRelation = Literal["same", "old", "revision", "independent", "conflict", "unknown"]
RosterContribution = Literal["include", "cross_scope", "exclude", "manual"]
CaseRecommendation = Literal["compare", "evidence_insufficient", "manual"]
IdentityDecision = Literal[
    "same_identity", "field_conflict", "different", "uncertain"
]


class SubmissionSummary(BaseModel):
    """Bounded submission-side facts; source evidence never changes this baseline."""

    model_config = ConfigDict(extra="forbid")

    submission_files: list[str] = Field(default_factory=list, max_length=20)
    submitted_rows: int = Field(ge=0, le=1_000_000)
    expected_scope_count: int | None = Field(default=None, ge=0, le=1_000_000)
    identity_version: str = Field(default="identity-v2", max_length=40)
    match_profile: str = Field(default="", max_length=40)
    match_fields: list[str] = Field(default_factory=list, max_length=20)
    row_conservation: dict[str, int] = Field(default_factory=dict, max_length=10)
    identity_samples: list[str] = Field(default_factory=list, max_length=10)


class ScopeCandidate(BaseModel):
    """One persisted business scope exposed to the Agent as an allowed target."""

    model_config = ConfigDict(extra="forbid")

    scope_id: int = Field(gt=0)
    scope_key: str = Field(min_length=1, max_length=300)
    source_role_type: str = Field(min_length=1, max_length=80)
    role: AgentRole
    role_label: str = Field(min_length=1, max_length=200)
    required: bool = True
    business_scope: dict[str, str] = Field(default_factory=dict, max_length=20)
    submitted_row_count: int = Field(default=0, ge=0, le=1_000_000)
    submitted_identity_count: int = Field(default=0, ge=0, le=1_000_000)


class SourceCandidate(BaseModel):
    """A source URL with its observed redirect history, not a trust decision."""

    model_config = ConfigDict(extra="forbid")

    original_url: str = Field(min_length=1, max_length=2048)
    normalized_url: str = Field(default="", max_length=2048)
    redirect_chain: list[str] = Field(default_factory=list, max_length=10)
    title: str = Field(default="", max_length=500)
    source_level: str = Field(default="unknown", max_length=80)


class ParsedAsset(BaseModel):
    """One parsed source asset or a stable unit within it, bounded for model use."""

    model_config = ConfigDict(extra="forbid")

    asset_id: str = Field(min_length=1, max_length=200)
    subunit_id: str = Field(default="document", min_length=1, max_length=300)
    source_url: str = Field(min_length=1, max_length=2048)
    parent_url: str = Field(default="", max_length=2048)
    kind: str = Field(min_length=1, max_length=40)
    status: AssetStatus
    label: str = Field(default="", max_length=500)
    title: str = Field(default="", max_length=500)
    summary: str = Field(default="", max_length=4000)
    sample_rows: list[list[str]] = Field(default_factory=list, max_length=10)
    anchors: list[str] = Field(default_factory=list, max_length=50)
    document_complete: bool | None = None
    sha256: str = Field(default="", pattern=r"^$|^[0-9a-f]{64}$")
    blockers: list[str] = Field(default_factory=list, max_length=20)


class AssetAssessment(BaseModel):
    """The Agent's semantic decision for exactly one asset or subunit."""

    model_config = ConfigDict(extra="forbid")

    asset_id: str = Field(min_length=1, max_length=200)
    subunit_id: str = Field(default="document", min_length=1, max_length=300)
    scope_ids: list[int] = Field(default_factory=list, max_length=20)
    role: AgentRole
    material_relation: MaterialRelation
    version_relation: VersionRelation
    roster_contribution: RosterContribution
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str = Field(min_length=1, max_length=2000)
    requires_human_confirmation: bool = False

    @model_validator(mode="after")
    def _fail_closed_relationships(self) -> AssetAssessment:
        if len(set(self.scope_ids)) != len(self.scope_ids) or any(
            scope_id <= 0 for scope_id in self.scope_ids
        ):
            raise ValueError("scope_ids must contain unique persisted scope ids")
        if self.confidence < 0.85:
            if self.roster_contribution != "manual" or not self.requires_human_confirmation:
                raise ValueError(
                    "low confidence requires manual contribution and human confirmation"
                )
        if self.version_relation in {"conflict", "unknown"}:
            if self.roster_contribution != "manual" or not self.requires_human_confirmation:
                raise ValueError("conflict or unknown version requires manual contribution")
        if self.material_relation == "unrelated" and self.roster_contribution != "exclude":
            raise ValueError("unrelated material must be excluded")
        if self.material_relation == "related_out_of_scope":
            if self.roster_contribution != "cross_scope" or not self.scope_ids:
                raise ValueError(
                    "related out-of-scope material requires cross-scope target scopes"
                )
            if self.version_relation in {"old", "conflict", "unknown"}:
                raise ValueError(
                    "related out-of-scope material requires a current, non-conflicting version"
                )
        elif self.roster_contribution == "cross_scope":
            raise ValueError(
                "cross-scope contribution requires related_out_of_scope material"
            )
        if self.roster_contribution == "include":
            if not self.scope_ids:
                raise ValueError("included material requires at least one scope")
            if self.version_relation in {"old", "independent", "conflict", "unknown"}:
                raise ValueError("this version relation cannot contribute to a roster")
        return self


class VersionGroup(BaseModel):
    """The only explicit permission to merge more than one source asset."""

    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1, max_length=300)
    asset_ids: list[str] = Field(min_length=1, max_length=100)
    merge_allowed: bool
    reason: str = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def _unique_assets(self) -> VersionGroup:
        if len(set(self.asset_ids)) != len(self.asset_ids):
            raise ValueError("version group asset ids must be unique")
        return self


class ReviewOutcome(BaseModel):
    """Bounded case recommendation; it cannot approve ingestion or bypass comparison."""

    model_config = ConfigDict(extra="forbid")

    case_recommendation: CaseRecommendation
    assessments: list[AssetAssessment] = Field(default_factory=list, max_length=500)
    selected_assets: list[str] = Field(default_factory=list, max_length=500)
    excluded_assets: dict[str, str] = Field(default_factory=dict, max_length=500)
    version_groups: list[VersionGroup] = Field(default_factory=list, max_length=100)
    unresolved_questions: list[str] = Field(default_factory=list, max_length=50)
    reason: str = Field(min_length=1, max_length=3000)

    @model_validator(mode="after")
    def _coherent_recommendation(self) -> ReviewOutcome:
        assessment_keys = {(item.asset_id, item.subunit_id) for item in self.assessments}
        if len(assessment_keys) != len(self.assessments):
            raise ValueError("each asset/subunit may have only one assessment")
        included = {
            item.asset_id
            for item in self.assessments
            if item.roster_contribution == "include"
        }
        if set(self.selected_assets) != included:
            raise ValueError("selected_assets must equal included assessment asset ids")
        if self.case_recommendation == "compare":
            if not included:
                raise ValueError("compare requires included assets")
            if any(item.roster_contribution == "manual" for item in self.assessments):
                raise ValueError("compare cannot contain unresolved manual assessments")
        return self


class ReviewProtocolError(ValueError):
    """A model-valid output that does not match the supplied case packet."""


class IdentityAdjudication(BaseModel):
    """One bounded semantic decision over a locally generated identity pair."""

    model_config = ConfigDict(extra="forbid")

    candidate_id: str = Field(min_length=1, max_length=100)
    decision: IdentityDecision
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def _accepted_decisions_require_confidence(self) -> IdentityAdjudication:
        if self.decision in {"same_identity", "field_conflict"} and self.confidence < 0.9:
            raise ValueError("accepted identity decisions require confidence >= 0.9")
        return self


class IdentityAdjudicationBatch(BaseModel):
    """Complete model response for a bounded set of identity candidates."""

    model_config = ConfigDict(extra="forbid")

    decisions: list[IdentityAdjudication] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def _unique_candidates(self) -> IdentityAdjudicationBatch:
        ids = [item.candidate_id for item in self.decisions]
        if len(ids) != len(set(ids)):
            raise ValueError("identity candidate decisions must be unique")
        return self


class ReviewCasePacket(BaseModel):
    """One model-facing case packet with fixed boundaries and explicit source assets."""

    model_config = ConfigDict(extra="forbid")

    case_id: int = Field(gt=0)
    resource_code: str = Field(min_length=1, max_length=40)
    award_name: str = Field(default="", max_length=200)
    year: str = Field(default="", max_length=20)
    submission_summary: SubmissionSummary
    scopes: list[ScopeCandidate] = Field(min_length=1, max_length=100)
    known_urls: list[SourceCandidate] = Field(default_factory=list, max_length=20)
    assets: list[ParsedAsset] = Field(default_factory=list, max_length=500)
    local_issues: list[str] = Field(default_factory=list, max_length=200)

    @model_validator(mode="after")
    def _unique_packet_ids(self) -> ReviewCasePacket:
        scope_ids = [scope.scope_id for scope in self.scopes]
        asset_keys = [(asset.asset_id, asset.subunit_id) for asset in self.assets]
        if len(set(scope_ids)) != len(scope_ids):
            raise ValueError("case packet scope ids must be unique")
        if len(set(asset_keys)) != len(asset_keys):
            raise ValueError("case packet asset/subunit ids must be unique")
        return self


def validate_review_outcome(packet: ReviewCasePacket, outcome: ReviewOutcome) -> ReviewOutcome:
    """Validate that a schema-valid Agent decision only references packet facts."""

    assets_by_key = {
        (asset.asset_id, asset.subunit_id): asset for asset in packet.assets
    }
    asset_keys = set(assets_by_key)
    asset_ids = {asset.asset_id for asset in packet.assets}
    scopes = {scope.scope_id: scope for scope in packet.scopes}
    for assessment in outcome.assessments:
        if (assessment.asset_id, assessment.subunit_id) not in asset_keys:
            raise ReviewProtocolError(
                "assessment references unknown asset/subunit: "
                f"{assessment.asset_id}/{assessment.subunit_id}"
            )
        for scope_id in assessment.scope_ids:
            scope = scopes.get(scope_id)
            if scope is None:
                raise ReviewProtocolError(f"assessment references unknown scope: {scope_id}")
            if assessment.role == "mixed":
                continue
            if scope.role != assessment.role:
                raise ReviewProtocolError(
                    f"assessment role {assessment.role} conflicts with scope "
                    f"{scope_id} role {scope.role}"
                )
        asset = assets_by_key[(assessment.asset_id, assessment.subunit_id)]
        if assessment.roster_contribution == "include" and (
            asset.status != "parsed" or asset.document_complete is False or asset.blockers
        ):
            raise ReviewProtocolError(
                "included evidence must be parsed, complete, and free of blockers: "
                f"{asset.asset_id}/{asset.subunit_id}"
            )
    assessment_keys = {
        (assessment.asset_id, assessment.subunit_id)
        for assessment in outcome.assessments
    }
    if assessment_keys != asset_keys:
        missing = asset_keys - assessment_keys
        extra = assessment_keys - asset_keys
        raise ReviewProtocolError(
            f"outcome must assess every discovered asset/subunit: "
            f"missing={sorted(missing)}, extra={sorted(extra)}"
        )
    unknown_selected = set(outcome.selected_assets) - asset_ids
    unknown_excluded = set(outcome.excluded_assets) - asset_ids
    if unknown_selected or unknown_excluded:
        raise ReviewProtocolError("outcome references an unknown asset")
    for group in outcome.version_groups:
        if set(group.asset_ids) - asset_ids:
            raise ReviewProtocolError(f"version group {group.key} references an unknown asset")
    included_assets = {
        assessment.asset_id
        for assessment in outcome.assessments
        if assessment.roster_contribution == "include"
    }
    grouped_assets = {
        asset_id for group in outcome.version_groups for asset_id in group.asset_ids
    }
    if grouped_assets != included_assets:
        raise ReviewProtocolError(
            "version groups must cover exactly the included assets"
        )
    if any(len(group.asset_ids) > 1 and not group.merge_allowed
           for group in outcome.version_groups):
        raise ReviewProtocolError("a multi-asset version group requires merge_allowed")
    return outcome
