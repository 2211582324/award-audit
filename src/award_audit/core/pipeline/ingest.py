"""导入编排：核查一个批次并写入台账（暂存）。

串起 M1 的核查与 M2 的台账：建批次 → 逐文件核查 → 逐行算去重键与校验状态 → 写 staging_record。
校验状态：整文件有 blocker（含文件级如模板用错）或本行有 blocker → fail（不予入库）；
有其他问题 → warn；否则 pass。
入正式库由 store.promote_batch 完成（去重后写版本化 record）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from award_audit.core.models.issue import Severity
from award_audit.core.models.record import ImportedFile
from award_audit.core.models.template import TemplateSpec
from award_audit.core.pipeline import importer, provenance
from award_audit.core.pipeline.dedup import dedup_key, row_data
from award_audit.core.pipeline.engine import BatchResult, check_imported_files
from award_audit.core.pipeline.store import Store
from award_audit.core.reference.ledger import LedgerEntry, load_ledger
from award_audit.core.reference.resource_map import ResourceMapEntry, load_resource_map
from award_audit.core.reference.template_registry import load_template_registry


# 导入并核查一个批次，写入台账（暂存态），返回 (批次id, 核查结果)；参照可注入便于测试
def ingest_batch(
    folder: Path,
    store: Store,
    operator: str = "",
    *,
    registry: dict[str, TemplateSpec] | None = None,
    resource_map: dict[str, ResourceMapEntry] | None = None,
    ledger: dict[str, LedgerEntry] | None = None,
    files: list[ImportedFile] | None = None,
) -> tuple[int, BatchResult]:
    reg = registry if registry is not None else load_template_registry()
    rmap = resource_map if resource_map is not None else load_resource_map()
    led = ledger if ledger is not None else load_ledger()
    current_keys = store.current_keys(reg)

    try:
        # Parsing and local checks intentionally happen before the publication transaction.
        imported = files if files is not None else importer.import_batch(folder)
        result = check_imported_files(folder.name, imported, reg, rmap, led, current_keys)
        staging_rows: list[dict[str, Any]] = []
        total_rows = 0
        for imp, fr in zip(imported, result.files, strict=True):
            spec = reg.get(imp.claimed_table_code)
            file_blocker = any(
                i.row is None and i.severity == Severity.BLOCKER for i in fr.issues
            )
            total_rows += imp.n_rows
            for ri in range(imp.n_rows):
                row_no = ri + 1
                row_issues = [i for i in fr.issues if i.row == row_no]
                has_blocker = file_blocker or any(
                    i.severity == Severity.BLOCKER for i in row_issues
                )
                status = "fail" if has_blocker else ("warn" if row_issues else "pass")
                staging_rows.append({
                    "file": imp.file_name,
                    "sheet": imp.sheet_name,
                    "row_no": row_no,
                    "table_code": imp.claimed_table_code,
                    "resource_code": imp.value(ri, "ZYLBM"),
                    "year": imp.year,
                    "dedup_key": dedup_key(imp, ri, spec),
                    "data": row_data(imp, ri),
                    "check_status": status,
                    "issues": [
                        {
                            "rule_id": i.rule_id,
                            "severity": i.severity.value,
                            "field_code": i.field_code,
                            "message": i.message,
                        }
                        for i in row_issues
                    ],
                })

        batch_id = store.persist_import_batch(
            folder.name,
            operator=operator,
            rows=staging_rows,
            n_files=len(imported),
            n_rows=total_rows,
            source_folder=str(folder.resolve(strict=False)),
            files=provenance.import_files_manifest(imported),
            check_result=provenance.check_result_to_json(result),
            template_fingerprint=provenance.template_fingerprint(reg),
            ledger_fingerprint=provenance.ledger_fingerprint(led),
            context_version=provenance.CONTEXT_VERSION,
        )
        return batch_id, result
    except Exception as exc:
        try:
            store.record_failed_import(folder.name, operator=operator, exc=exc)
        except Exception:
            # The original import failure remains the actionable cause if SQLite itself is down.
            pass
        raise
