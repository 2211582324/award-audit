"""Explicitly triggered M5.7 handlers for persistent Web jobs."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path

from award_audit.agent.review_workflow import (
    HarnessFactory,
    load_prepared_from_context,
    prepare_review_batch,
    run_audit_stage,
    run_queued_review_cases,
)
from award_audit.agent.toolkit.safety import validate_local_path
from award_audit.core.pipeline.importer import import_batch as import_files
from award_audit.core.pipeline.ingest import ingest_batch
from award_audit.core.pipeline.store import Store
from award_audit.core.reference.ledger import load_ledger
from award_audit.core.reference.resource_map import load_resource_map
from award_audit.core.reference.template_registry import load_template_registry
from award_audit.web.jobs import JobContext, JobHandler
from award_audit.web.models import JobOutcome


def build_review_batch_handler(
    db_path: str | Path,
    *,
    evidence_roots: Sequence[str | Path],
    harness_factory: HarnessFactory | None = None,
) -> JobHandler:
    database = str(db_path)
    roots = [Path(root).resolve(strict=False) for root in evidence_roots]

    async def review_batch(context: JobContext) -> JobOutcome:
        batch_id = context.job.batch_id
        if batch_id is None:
            raise ValueError("review_batch requires batch_id")
        results = await asyncio.to_thread(
            run_queued_review_cases,
            database,
            batch_id,
            evidence_roots=roots,
            harness_factory=harness_factory,
        )
        waiting = any(item["status"] == "waiting_human" for item in results)
        return JobOutcome(
            status="waiting_human" if waiting else "completed",
            result={"batch_id": batch_id, "cases": results},
        )

    return review_batch


def build_import_batch_handler(
    db_path: str | Path,
    *,
    import_roots: Sequence[str | Path],
) -> JobHandler:
    database = str(db_path)
    roots = [Path(root).resolve(strict=False) for root in import_roots]

    def run_import(folder: Path) -> dict[str, object]:
        store = Store(database)
        try:
            registry = load_template_registry()
            resource_map = load_resource_map()
            ledger = load_ledger()
            files = import_files(folder)
            prepared = prepare_review_batch(
                folder,
                store,
                registry=registry,
                resource_map=resource_map,
                ledger=ledger,
                imported_files=files,
                ingest_runner=ingest_batch,
            )
            return {
                "batch_id": prepared.batch_id,
                "files": len(prepared.result.files),
                "rows": sum(item.n_rows for item in prepared.result.files),
                "issues": prepared.result.total_issues,
            }
        finally:
            store.close()

    async def import_batch(context: JobContext) -> JobOutcome:
        raw_folder = str(context.job.input.get("folder", ""))
        folder = validate_local_path(
            raw_folder, roots, must_exist=True, file_only=False
        )
        if not folder.is_dir():
            raise ValueError("import_batch folder is not a directory")
        await context.progress(5, "开始确定性导入")
        result = await asyncio.to_thread(run_import, folder)
        return JobOutcome(result=result)

    return import_batch


def build_audit_batch_handler(
    db_path: str | Path,
    *,
    import_roots: Sequence[str | Path],
    evidence_roots: Sequence[str | Path],
) -> JobHandler:
    database = str(db_path)
    roots = [Path(root).resolve(strict=False) for root in import_roots]
    evidence_root = next(
        (Path(root).resolve(strict=False) for root in evidence_roots),
        None,
    )

    def run_stage(batch_id: int) -> dict[str, object]:
        store = Store(database)
        try:
            prepared = load_prepared_from_context(
                store,
                batch_id,
                allowed_roots=roots,
            )
            outcome = run_audit_stage(
                store,
                prepared,
                approve=None,
                use_corpus=False,
                workdir=evidence_root,
            )
            return {
                "batch_id": batch_id,
                "status": outcome.status,
                "targets": len(outcome.precheck.passable_targets),
                "reports": len(outcome.reports),
                "cases_created": outcome.bridge.created,
                "cases_existing": outcome.bridge.existing,
            }
        finally:
            store.close()

    async def audit_batch(context: JobContext) -> JobOutcome:
        batch_id = context.job.batch_id
        if batch_id is None:
            raise ValueError("audit_batch requires batch_id")
        await context.progress(5, "开始 M4 联网核对")
        result = await asyncio.to_thread(run_stage, batch_id)
        return JobOutcome(result=result)

    return audit_batch


def build_default_job_handlers(
    db_path: str | Path,
    *,
    evidence_roots: Sequence[str | Path],
    import_roots: Sequence[str | Path],
) -> dict[str, JobHandler]:
    review_roots = list(dict.fromkeys([
        *[str(Path(root).resolve(strict=False)) for root in evidence_roots],
        *[str(Path(root).resolve(strict=False)) for root in import_roots],
    ]))
    handlers: dict[str, JobHandler] = {
        "audit_batch": build_audit_batch_handler(
            db_path,
            import_roots=import_roots,
            evidence_roots=evidence_roots,
        ),
        "review_batch": build_review_batch_handler(
            db_path, evidence_roots=review_roots
        ),
    }
    if import_roots:
        handlers["import_batch"] = build_import_batch_handler(
            db_path, import_roots=import_roots
        )
    return handlers
