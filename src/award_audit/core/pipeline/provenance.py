"""导入溯源：文件哈希 + 模板/台账指纹（统一编排 L5 阶段读取时重验，漂移即 fail-closed）。

放在 core.pipeline：ingest（写）与 agent.review_workflow（读校验）共用；依赖方向 agent→core 不反向。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from award_audit.core.models.record import ImportedFile
from award_audit.core.models.template import TemplateSpec
from award_audit.core.reference.ledger import LedgerEntry

CONTEXT_VERSION = 1


class ImportContextError(RuntimeError):
    """Persisted import provenance is missing, malformed, or no longer trustworthy."""


# 单文件 SHA-256（分块，兼顾大文件）；读不到返回 ""（上层视为溯源失配 fail-closed）
def file_sha256(path: str | Path) -> str:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return ""


# 导入文件清单 [{file_name, path, sha256}]（溯源持久化 + L5 读时逐一重算比对）
def import_files_manifest(imported: Sequence[ImportedFile]) -> list[dict[str, str]]:
    return [
        {
            "file_name": imp.file_name,
            "path": str(Path(imp.path).resolve(strict=False)),
            "sha256": file_sha256(imp.path),
        }
        for imp in imported
    ]


def _under(path: Path, roots: Sequence[Path]) -> bool:
    return any(path == root or path.is_relative_to(root) for root in roots)


def validate_import_context(
    context: Mapping[str, Any],
    *,
    allowed_roots: Sequence[str | Path],
    template_fingerprint: str,
    ledger_fingerprint: str,
    context_version: int = CONTEXT_VERSION,
) -> dict[str, Any]:
    """Decode and revalidate persisted import inputs before audit or promotion."""

    roots = [Path(root).resolve(strict=False) for root in allowed_roots]
    if not roots:
        raise ImportContextError("导入上下文未配置 allowed roots")
    try:
        stored_version = int(context["context_version"])
        source = Path(str(context["source_folder"])).resolve(strict=False)
        files = json.loads(str(context["files_json"]))
        check_result = json.loads(str(context["check_result_json"]))
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ImportContextError("导入上下文结构无效") from exc
    if stored_version != int(context_version):
        raise ImportContextError("导入上下文版本不匹配")
    if not _under(source, roots):
        raise ImportContextError("导入源目录不在 allowed roots 内")
    if str(context["template_fingerprint"]) != template_fingerprint:
        raise ImportContextError("模板指纹已变化")
    if str(context["ledger_fingerprint"]) != ledger_fingerprint:
        raise ImportContextError("采集台账指纹已变化")
    if not isinstance(files, list) or not isinstance(check_result, dict):
        raise ImportContextError("导入上下文 JSON 类型无效")

    decoded_files: list[dict[str, str]] = []
    seen_paths: set[Path] = set()
    for raw in files:
        if not isinstance(raw, dict):
            raise ImportContextError("导入文件清单项无效")
        file_name = str(raw.get("file_name", "")).strip()
        stored_hash = str(raw.get("sha256", "")).strip().lower()
        if not file_name or len(stored_hash) != 64:
            raise ImportContextError("导入文件名或 SHA-256 无效")
        path = Path(str(raw.get("path", ""))).resolve(strict=False)
        if path.name.casefold() == ".env":
            raise ImportContextError("环境配置文件不能作为导入材料")
        if path.name != file_name or not _under(path, roots) or not _under(path, [source]):
            raise ImportContextError("导入文件路径不在允许目录内")
        if path in seen_paths:
            raise ImportContextError("导入文件清单包含重复路径")
        seen_paths.add(path)
        current_hash = file_sha256(path)
        if not current_hash or current_hash.lower() != stored_hash:
            raise ImportContextError(f"导入文件哈希已变化：{file_name}")
        decoded_files.append({
            "file_name": file_name,
            "path": str(path),
            "sha256": stored_hash,
        })
    return {
        "batch_id": int(context["batch_id"]),
        "source_folder": str(source),
        "files": decoded_files,
        "check_result": check_result,
        "context_version": stored_version,
        "template_fingerprint": str(context["template_fingerprint"]),
        "ledger_fingerprint": str(context["ledger_fingerprint"]),
        "created_at": str(context["created_at"]),
    }


# 模板注册表指纹：结构变化（新增/改字段角色）即变，L5 读到旧指纹→要求重新导入
def template_fingerprint(registry: Mapping[str, TemplateSpec]) -> str:
    payload = sorted(
        [code, spec.sheet_name, list(spec.field_codes)] for code, spec in registry.items()
    )
    blob = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:32]


# 采集清单指纹：网址/应采数变化即变
def ledger_fingerprint(ledger: Mapping[str, LedgerEntry]) -> str:
    payload = sorted(
        [code, entry.collect_url, entry.expected_count] for code, entry in ledger.items()
    )
    blob = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:32]


# 把 BatchResult 序列化为可持久化 dict（保留文件级问题——staging 只存行级，文件级 row=None 会丢）
def check_result_to_json(result: Any) -> dict[str, Any]:
    return {
        "batch": result.batch,
        "files": [
            {
                "file": fr.file,
                "claimed_table_code": fr.claimed_table_code,
                "n_rows": fr.n_rows,
                "issues": [issue.model_dump(mode="json") for issue in fr.issues],
            }
            for fr in result.files
        ],
    }
