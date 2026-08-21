"""award-core daemon：持有台账 Store，通过 SocketServer 对外提供命令，状态变化广播事件。

单一 async 入口 CoreApp.run()：建 Store → 起 SocketServer → 注册 handlers → 发 CoreStarted →
常驻直到 Ctrl+C。TUI/CLI 是它的客户端——TUI 崩了 daemon 不受影响，多个客户端看到同一份台账与事件流。
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from award_audit import __version__
from award_audit.core import config
from award_audit.core.bus.events import (
    AuditReviewedEvent,
    BatchImportedEvent,
    BatchPromotedEvent,
    CoreStartedEvent,
    RecordReviewedEvent,
)
from award_audit.core.models.issue import Severity
from award_audit.core.models.template import TemplateSpec
from award_audit.core.models.triage import decide_triage, triage_sort_key
from award_audit.core.pipeline.ingest import ingest_batch
from award_audit.core.pipeline.store import Store
from award_audit.core.reference.ledger import LedgerEntry, load_ledger
from award_audit.core.reference.resource_map import ResourceMapEntry, load_resource_map
from award_audit.core.reference.template_registry import load_template_registry
from award_audit.core.transport.socket_server import SocketServer

DEFAULT_PORT = 7438


# 读 daemon 监听端口（环境变量 AWARD_AUDIT_PORT 覆盖）
def daemon_port() -> int:
    return int(os.environ.get("AWARD_AUDIT_PORT", DEFAULT_PORT))


class CoreApp:
    """daemon 应用：Store + SocketServer + 命令处理器。"""

    # 建 Store 与服务器，注册全部命令；参照三件套懒加载（首次 import 时加载一次并缓存，测试可注入）
    def __init__(self, host: str = "127.0.0.1", port: int | None = None,
                 db_path: str | Path | None = None) -> None:
        self.store = Store(db_path if db_path is not None else config.db_path())
        self._registry: dict[str, TemplateSpec] | None = None
        self._resource_map: dict[str, ResourceMapEntry] | None = None
        self._ledger: dict[str, LedgerEntry] | None = None
        self.server = SocketServer(host, port if port is not None else daemon_port())
        self.server.register("cmd.ping", self._h_ping)
        self.server.register("cmd.list_batches", self._h_list_batches)
        self.server.register("cmd.get_staging", self._h_get_staging)
        self.server.register("cmd.review_record", self._h_review)
        self.server.register("cmd.promote_batch", self._h_promote)
        self.server.register("cmd.import_batch", self._h_import)
        self.server.register("cmd.get_audit_results", self._h_get_audit_results)
        self.server.register("cmd.review_audit", self._h_review_audit)

    # 从同步 handler 上下文调度一次事件广播
    def _publish(self, event: BaseModel) -> None:
        asyncio.get_running_loop().create_task(self.server.publish(event))

    # ping → pong
    def _h_ping(self, params: dict[str, Any]) -> dict[str, Any]:
        return {"pong": True, "version": __version__}

    # 列出全部批次
    def _h_list_batches(self, params: dict[str, Any]) -> dict[str, Any]:
        return {"batches": [dict(r) for r in self.store.list_batches()]}

    # 取某批次暂存记录（瘦身：只返回列表展示字段 + 问题摘要，不含完整数据字典——
    # 大批次全量 data 会把单行 NDJSON 响应撑到数 MB）
    def _h_get_staging(self, params: dict[str, Any]) -> dict[str, Any]:
        batch_id = int(params["batch_id"])
        records = []
        for r in self.store.staging_of(batch_id):
            issues = json.loads(r["issues_json"])
            records.append({
                "id": r["id"], "file": r["file"], "sheet": r["sheet"], "row_no": r["row_no"],
                "table_code": r["table_code"], "resource_code": r["resource_code"],
                "check_status": r["check_status"], "review_status": r["review_status"],
                "n_issues": len(issues), "issues": issues[:3],
            })
        return {"records": records}

    # 复核一条暂存记录（通过/打回）并广播事件
    def _h_review(self, params: dict[str, Any]) -> dict[str, Any]:
        staging_id = int(params["staging_id"])
        decision = str(params["decision"])
        reviewer = str(params.get("reviewer", ""))
        self.store.set_review_status(staging_id, decision, reviewer)
        row = self.store.get_staging_row(staging_id)
        batch_id = int(row["batch_id"]) if row is not None else 0
        self._publish(RecordReviewedEvent(
            staging_id=staging_id, batch_id=batch_id, decision=decision, reviewer=reviewer))
        return {"ok": True}

    # 批次入正式库（版本化+闸门）并广播事件
    def _h_promote(self, params: dict[str, Any]) -> dict[str, Any]:
        batch_id = int(params["batch_id"])
        stats = self.store.promote_batch(batch_id, operator=str(params.get("operator", "")))
        self._publish(BatchPromotedEvent(batch_id=batch_id, **stats_subset(stats)))
        return stats

    # 确保参照三件套就位（各自独立懒加载，测试可单独注入某一件）
    def _ensure_refs(self) -> None:
        if self._registry is None:
            self._registry = load_template_registry()
        if self._resource_map is None:
            self._resource_map = load_resource_map()
        if self._ledger is None:
            self._ledger = load_ledger()

    # 导入并核查一个批次文件夹（写台账暂存）并广播事件
    def _h_import(self, params: dict[str, Any]) -> dict[str, Any]:
        folder = Path(str(params["folder"]))
        if not folder.is_dir():
            raise ValueError(f"批次文件夹不存在：{folder}")
        self._ensure_refs()
        batch_id, result = ingest_batch(
            folder, self.store, operator=str(params.get("operator", "")),
            registry=self._registry, resource_map=self._resource_map, ledger=self._ledger)
        n_rows = sum(fr.n_rows for fr in result.files)
        self._publish(BatchImportedEvent(
            batch_id=batch_id, name=result.batch, n_files=len(result.files),
            n_rows=n_rows, n_blocker=result.count(Severity.BLOCKER)))
        return {"batch_id": batch_id, "n_files": len(result.files), "n_rows": n_rows,
                "n_issues": result.total_issues}

    # 取某批次的联网核对结论（资源项级，进「联网核对」复核页；含证据链/found_assets 供人工终审）。
    # 按分诊排序：转人工/低把握浮顶、快速放行沉底——复核队列自然把"重点盯"排前面。
    def _h_get_audit_results(self, params: dict[str, Any]) -> dict[str, Any]:
        batch_id = int(params["batch_id"])
        results = []
        for r in self.store.audit_results_of(batch_id):
            triage = decide_triage(r["verdict"], r["confidence"])  # 实时派生，旧库行也正确
            results.append({
                "id": r["id"], "resource_code": r["resource_code"], "award_name": r["award_name"],
                "year": r["year"], "verdict": r["verdict"], "confidence": r["confidence"],
                "triage": triage, "reason_codes": json.loads(r["reason_codes_json"]),
                "source_kind": r["source_kind"], "source_url": r["source_url"],
                "page_year": r["page_year"],
                "extracted_count": r["extracted_count"], "submitted_count": r["submitted_count"],
                "missing": json.loads(r["missing_json"]), "extra": json.loads(r["extra_json"]),
                "source_urls": json.loads(r["source_urls_json"]),
                "found_assets": json.loads(r["found_assets_json"]),
                "evidence": json.loads(r["evidence_json"]), "notes": r["notes"],
                "review_status": r["review_status"], "reviewer": r["reviewer"],
            })
        results.sort(key=lambda d: triage_sort_key(str(d["triage"]), str(d["confidence"])))
        return {"results": results}

    # 复核一条联网核对结论（通过/打回）并广播事件
    def _h_review_audit(self, params: dict[str, Any]) -> dict[str, Any]:
        audit_id = int(params["audit_id"])
        decision = str(params["decision"])
        reviewer = str(params.get("reviewer", ""))
        self.store.set_audit_review(audit_id, decision, reviewer)
        row = self.store.get_audit_row(audit_id)
        batch_id = int(row["batch_id"]) if row is not None else 0
        self._publish(AuditReviewedEvent(
            audit_id=audit_id, batch_id=batch_id, decision=decision, reviewer=reviewer))
        return {"ok": True}

    # 常驻运行：启动 → 广播 CoreStarted → 等待取消/Ctrl+C → 收尾
    async def run(self) -> None:
        await self.server.start()
        await self.server.publish(CoreStartedEvent(version=__version__))
        print(f"award-core 已启动：{self.server.host}:{self.server.port}（Ctrl+C 停止）")
        try:
            await asyncio.Event().wait()  # 永久等待，直到 KeyboardInterrupt/取消
        except asyncio.CancelledError:
            pass
        finally:
            await self.server.stop()
            self.store.close()


# 提取事件需要的三个统计键（stats 里可能还有 skipped_rejected 等扩展键）
def stats_subset(stats: dict[str, int]) -> dict[str, int]:
    return {k: stats[k] for k in ("inserted", "skipped_dup", "skipped_fail")}


# 同步入口：award-core
def run() -> None:
    try:
        asyncio.run(CoreApp().run())
    except KeyboardInterrupt:
        print("\naward-core 已停止")


if __name__ == "__main__":
    run()
