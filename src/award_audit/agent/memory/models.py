"""Taxonomy and governed Case Memory contracts."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

MemoryStatus = Literal["candidate", "active", "deprecated", "merged"]
HumanDecision = Literal["accepted", "rejected", "insufficient"]


class TaxonomyEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1, max_length=80)
    version: int = Field(ge=1)
    name: str = Field(min_length=1, max_length=200)
    definition: str = Field(min_length=1, max_length=1000)
    examples: list[str] = Field(default_factory=list, max_length=20)
    candidate_eligible: bool
    status: Literal["active", "deprecated"]


class CaseMemory(BaseModel):
    model_config = ConfigDict(extra="forbid")

    memory_id: int = Field(gt=0)
    status: MemoryStatus
    category_code: str = Field(min_length=1, max_length=80)
    taxonomy_version: int = Field(ge=1)
    resource_type: str = Field(default="", max_length=80)
    field_code: str = Field(default="", max_length=80)
    symptom_text: str = Field(min_length=1, max_length=2000)
    normalized_pattern: str = Field(min_length=1, max_length=1000)
    resolution: str = Field(min_length=1, max_length=2000)
    evidence_summary: str = Field(default="", max_length=2000)
    final_human_decision: HumanDecision
    source_case_id: int = Field(gt=0)
    source_case_ids: list[int] = Field(min_length=1)
    applicable_from: str = ""
    applicable_to: str = ""
    occurrence_count: int = Field(ge=1)
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_by: str = Field(min_length=1, max_length=200)
    approved_by: str = Field(default="", max_length=200)
    merged_into_id: int | None = None
    state_version: int = Field(ge=1)
    created_at: str
    updated_at: str


class MemoryHit(BaseModel):
    memory_id: int
    category_code: str = Field(min_length=1, max_length=80)
    symptom_text: str = Field(min_length=1, max_length=2000)
    resolution: str = Field(min_length=1, max_length=2000)
    final_human_decision: HumanDecision
    applicable_from: str = ""
    applicable_to: str = ""
    source_case_ids: list[int]
    score: float = Field(ge=0)
    warning: str = "历史案例不是当前事实，必须重新核验证据。"
