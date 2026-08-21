"""核查引擎：编排 L0–L2，按文件/批次汇总 Issue 与判定。

判定规则（实施方案 §5）：有 blocker → 打回；否则有 review → 待复核；否则有 format → 待修正；全绿 → 可入库。
参照数据（模板注册表 / 资源项码映射）通过参数注入，便于测试与复用（不在引擎内硬加载）。
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from award_audit.core.models.issue import Issue, Severity
from award_audit.core.models.record import ImportedFile
from award_audit.core.models.template import TemplateSpec
from award_audit.core.pipeline import importer
from award_audit.core.pipeline.checks import l0_file, l1_field, l2_consistency, l3_completeness, l4_dedup
from award_audit.core.reference.ledger import LedgerEntry, load_ledger
from award_audit.core.reference.resource_map import ResourceMapEntry, load_resource_map
from award_audit.core.reference.template_registry import load_template_registry


class FileResult(BaseModel):
    """单个文件的核查结果：定位信息 + Issue 列表 + 判定。"""

    file: str
    claimed_table_code: str
    n_rows: int
    issues: list[Issue]

    # 各严重度计数
    def count(self, sev: Severity) -> int:
        return sum(1 for i in self.issues if i.severity == sev)

    # 文件判定
    @property
    def verdict(self) -> str:
        if self.count(Severity.BLOCKER):
            return "打回"
        if self.count(Severity.REVIEW):
            return "待复核"
        if self.count(Severity.FORMAT):
            return "待修正"
        return "可入库"


class BatchResult(BaseModel):
    """整个批次的核查结果。"""

    batch: str
    files: list[FileResult]

    # 全批 Issue 总数
    @property
    def total_issues(self) -> int:
        return sum(len(fr.issues) for fr in self.files)

    # 全批各严重度计数
    def count(self, sev: Severity) -> int:
        return sum(fr.count(sev) for fr in self.files)


# 核查单个已导入文件：解析文件名声称的模板 → 跑 L0/L1/L2/L4（L3 是批次级，见 check_imported_files）
def check_file(
    imported: ImportedFile,
    registry: dict[str, TemplateSpec],
    resource_map: dict[str, ResourceMapEntry],
    current_keys: set[str] | None = None,
) -> FileResult:
    claimed_spec = registry.get(imported.claimed_table_code)
    issues: list[Issue] = []
    issues += l0_file.run(imported, claimed_spec, resource_map)
    issues += l1_field.run(imported, claimed_spec, resource_map)
    issues += l2_consistency.run(imported, claimed_spec)
    issues += l4_dedup.run(imported, claimed_spec, current_keys)
    return FileResult(
        file=imported.file_name,
        claimed_table_code=imported.claimed_table_code,
        n_rows=imported.n_rows,
        issues=issues,
    )


# 核查一组已导入文件：逐文件 L0–L2/L4 + 批次级 L3（按资源项码跨文件聚合），结果顺序与输入一致
def check_imported_files(
    batch_name: str,
    files: list[ImportedFile],
    registry: dict[str, TemplateSpec],
    resource_map: dict[str, ResourceMapEntry],
    ledger: dict[str, LedgerEntry] | None = None,
    current_keys: set[str] | None = None,
) -> BatchResult:
    results = [check_file(imp, registry, resource_map, current_keys) for imp in files]
    if ledger is not None:
        by_file = {fr.file: fr for fr in results}
        for iss in l3_completeness.run_batch(files, ledger):
            target = by_file.get(iss.file)
            if target is not None:
                target.issues.append(iss)
    return BatchResult(batch=batch_name, files=results)


# 核查整个批次文件夹：加载参照（未注入则默认加载）→ 导入 → 核查
def check_batch(
    folder: Path,
    registry: dict[str, TemplateSpec] | None = None,
    resource_map: dict[str, ResourceMapEntry] | None = None,
    ledger: dict[str, LedgerEntry] | None = None,
    current_keys: set[str] | None = None,
) -> BatchResult:
    reg = registry if registry is not None else load_template_registry()
    rmap = resource_map if resource_map is not None else load_resource_map()
    led = ledger if ledger is not None else load_ledger()
    files = importer.import_batch(folder)
    return check_imported_files(folder.name, files, reg, rmap, led, current_keys)
