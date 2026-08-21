"""命令模型：客户端能让 daemon 做的每一件事 = 一个 Command 类（type 字段判别联合）。

方法名约定 "cmd.<snake>"；params 即模型字段。新增操作 = 加类 + 扩展 Command 联合 + daemon 注册 handler。
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field


class PingCommand(BaseModel):
    """连通性测试。"""

    type: Literal["ping"] = "ping"


class ListBatchesCommand(BaseModel):
    """列出台账全部批次。"""

    type: Literal["list_batches"] = "list_batches"


class GetStagingCommand(BaseModel):
    """取某批次的暂存记录（复核队列）。"""

    type: Literal["get_staging"] = "get_staging"
    batch_id: int


class ReviewRecordCommand(BaseModel):
    """复核一条暂存记录：通过 / 打回。"""

    type: Literal["review_record"] = "review_record"
    staging_id: int
    decision: Literal["通过", "打回"]
    reviewer: str = ""


class PromoteBatchCommand(BaseModel):
    """把批次通过校验的记录入正式库（版本化+去重闸门）。"""

    type: Literal["promote_batch"] = "promote_batch"
    batch_id: int
    operator: str = ""


class ImportBatchCommand(BaseModel):
    """导入并核查一个批次文件夹，写台账暂存。"""

    type: Literal["import_batch"] = "import_batch"
    folder: str
    operator: str = ""


class GetAuditResultsCommand(BaseModel):
    """取某批次的联网核对结论（复核台「联网核对」页，资源项级）。"""

    type: Literal["get_audit_results"] = "get_audit_results"
    batch_id: int


class ReviewAuditCommand(BaseModel):
    """复核一条联网核对结论：通过 / 打回。"""

    type: Literal["review_audit"] = "review_audit"
    audit_id: int
    decision: Literal["通过", "打回"]
    reviewer: str = ""


Command = Annotated[
    PingCommand
    | ListBatchesCommand
    | GetStagingCommand
    | ReviewRecordCommand
    | PromoteBatchCommand
    | ImportBatchCommand
    | GetAuditResultsCommand
    | ReviewAuditCommand,
    Field(discriminator="type"),
]
