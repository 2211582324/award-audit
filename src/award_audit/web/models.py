"""Web API and persistent job contracts."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

JobStatus = Literal[
    "queued",
    "running",
    "waiting_human",
    "completed",
    "failed",
    "cancelled",
]


class JobRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: int = Field(gt=0)
    kind: str = Field(min_length=1, max_length=80)
    batch_id: int | None = None
    case_id: int | None = None
    status: JobStatus
    input: dict[str, Any]
    progress: int = Field(ge=0, le=100)
    progress_message: str = Field(max_length=500)
    result: dict[str, Any]
    error_code: str = Field(max_length=100)
    error_message: str = Field(max_length=500)
    attempt: int = Field(ge=0)
    max_attempts: int = Field(ge=1, le=10)
    lease_owner: str = Field(max_length=100)
    lease_expires_at: str = Field(max_length=50)
    state_version: int = Field(ge=1)
    created_by: str = Field(min_length=1, max_length=200)
    created_at: str
    updated_at: str
    started_at: str
    finished_at: str


class AuditEvent(BaseModel):
    event_id: int = Field(gt=0)
    event_type: str
    topic: str
    job_id: int | None = None
    case_id: int | None = None
    batch_id: int | None = None
    payload: dict[str, Any]
    created_at: str


class JobOutcome(BaseModel):
    status: Literal["completed", "waiting_human"] = "completed"
    result: dict[str, Any] = Field(default_factory=dict)


class HumanAction(BaseModel):
    reviewer: str = Field(min_length=1, max_length=200)
    action: str = Field(min_length=1, max_length=80)
    target_type: str = Field(min_length=1, max_length=80)
    target_id: int = Field(gt=0)
    before_version: int = Field(ge=0)
    after_version: int = Field(ge=0)
    summary: str = Field(default="", max_length=1000)
