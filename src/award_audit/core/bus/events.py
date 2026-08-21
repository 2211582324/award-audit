"""事件模型：daemon 里每次状态变化 = 一个 Event（type 字段判别联合），推给订阅的客户端。

TUI 收到事件即刷新对应视图——"执行过程不是黑盒"，这是 KamaClaude 事件流外化思想的落地。
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field


class CoreStartedEvent(BaseModel):
    """daemon 启动完成。"""

    type: Literal["core_started"] = "core_started"
    version: str


class BatchImportedEvent(BaseModel):
    """一个批次导入并核查完成（进入暂存·审核中）。"""

    type: Literal["batch_imported"] = "batch_imported"
    batch_id: int
    name: str
    n_files: int
    n_rows: int
    n_blocker: int


class RecordReviewedEvent(BaseModel):
    """一条暂存记录被复核（通过/打回）。"""

    type: Literal["record_reviewed"] = "record_reviewed"
    staging_id: int
    batch_id: int
    decision: str
    reviewer: str


class BatchPromotedEvent(BaseModel):
    """一个批次完成入库（版本化写入正式库）。"""

    type: Literal["batch_promoted"] = "batch_promoted"
    batch_id: int
    inserted: int
    skipped_dup: int
    skipped_fail: int


class AuditReviewedEvent(BaseModel):
    """一条联网核对结论被复核（通过/打回）。"""

    type: Literal["audit_reviewed"] = "audit_reviewed"
    audit_id: int
    batch_id: int
    decision: str
    reviewer: str


Event = Annotated[
    CoreStartedEvent
    | BatchImportedEvent
    | RecordReviewedEvent
    | BatchPromotedEvent
    | AuditReviewedEvent,
    Field(discriminator="type"),
]
