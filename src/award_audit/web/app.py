"""FastAPI application factory for the local M5.6 review console."""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import shutil
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from fastapi import FastAPI, File, Header, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from award_audit.agent.harness.persistence import CaseRepository
from award_audit.agent.memory import CaseMemoryService, MemoryRepository
from award_audit.agent.review_workflow import load_prepared_from_context
from award_audit.agent.toolkit.safety import SafetyError, validate_local_path
from award_audit.core.pipeline import provenance
from award_audit.core.pipeline.checks import l5_precheck
from award_audit.core.pipeline.store import PromoteBlocked, StateConflictError, Store
from award_audit.core.reference.ledger import load_ledger
from award_audit.core.reference.template_registry import load_template_registry
from award_audit.web.jobs import JobHandler, JobRepository, JobWorker
from award_audit.web.models import HumanAction, JobRecord


class BatchImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    folder: str = Field(min_length=1, max_length=2000)


class SupplementRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request: str = Field(min_length=1, max_length=1000)
    expected_version: int = Field(ge=1)


class CaseReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: str = Field(pattern=r"^(accepted|rejected|insufficient)$")
    summary: str = Field(min_length=1, max_length=2000)
    expected_version: int = Field(ge=1)


class MemoryActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    merged_into_id: int | None = Field(default=None, gt=0)


class PromoteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_status: str = Field(default="", max_length=40)


class AuditConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    preview_digest: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")


class JobCancelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)


def _reviewer(value: str) -> str:
    reviewer = " ".join(unquote(value).split()).strip()
    if not reviewer or len(reviewer) > 200:
        raise HTTPException(status_code=422, detail="X-Reviewer must contain 1-200 characters")
    return reviewer


def _public_value(value: Any, *, depth: int = 0) -> Any:
    if depth >= 7:
        return "[DEPTH_LIMIT]"
    if isinstance(value, dict):
        public: dict[str, Any] = {}
        for raw_key, item in list(value.items())[:100]:
            key = str(raw_key)[:100]
            normalized = key.lower().replace("-", "_")
            if any(part in normalized for part in (
                "api_key", "authorization", "cookie", "password", "secret", "token"
            )):
                public[key] = "[REDACTED]"
            elif (
                normalized in {
                    "path", "folder", "local_path", "destination_dir", "output_dir"
                }
                or normalized.endswith(("_path", "_dir", "_file", "_folder"))
            ):
                public[key] = Path(str(item)).name
            else:
                public[key] = _public_value(item, depth=depth + 1)
        return public
    if isinstance(value, list):
        return [_public_value(item, depth=depth + 1) for item in value[:100]]
    if isinstance(value, str):
        return value[:4000]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:1000]


def _job_payload(job: JobRecord) -> dict[str, Any]:
    data = job.model_dump(mode="json")
    data["input"] = _public_value(data["input"])
    data["result"] = _public_value(data["result"])
    return data


def _stage_payload(store: Store, batch_id: int) -> dict[str, dict[str, Any]]:
    stages = {
        stage: {"status": "pending", "attempt": 0, "error_code": ""}
        for stage in ("local", "m4", "m5")
    }
    for row in store.get_batch_stage_runs(batch_id):
        stage = str(row["stage"])
        if stage in stages:
            stages[stage] = {
                "status": str(row["status"]),
                "attempt": int(row["attempt"]),
                "error_code": str(row["error_code"]),
            }
    m4_counts: dict[str, int] = {}
    for item in store.get_stage_items(batch_id, stage="m4"):
        item_status = str(item["status"])
        m4_counts[item_status] = m4_counts.get(item_status, 0) + 1
    case_counts: dict[str, int] = {}
    for case in store.list_audit_cases(batch_id=batch_id):
        case_status = str(case["status"])
        case_counts[case_status] = case_counts.get(case_status, 0) + 1
    stages["m4"]["item_counts"] = m4_counts
    stages["m5"]["case_counts"] = case_counts
    stages["m5"]["required"] = bool(case_counts)
    return stages


def _json_list(value: object) -> list[Any]:
    try:
        parsed = json.loads(str(value or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return parsed if isinstance(parsed, list) else []


def _m4_result_items(store: Store, batch_id: int) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for stage_item in store.get_stage_items(batch_id, stage="m4"):
        current_result_id = int(stage_item["current_result_id"] or 0)
        result = store.get_audit_row(current_result_id) if current_result_id else None
        case = store.conn.execute(
            "SELECT id,status,origin_m4_result_id FROM audit_case "
            "WHERE batch_id=? AND resource_code=? AND year=? ORDER BY id DESC LIMIT 1",
            (
                batch_id,
                str(stage_item["resource_code"]),
                str(stage_item["year"]),
            ),
        ).fetchone()
        binding = None
        if case is not None:
            origin_id = int(case["origin_m4_result_id"] or 0)
            binding = {
                "case_id": int(case["id"]),
                "case_status": str(case["status"]),
                "origin_m4_result_id": origin_id,
                "is_current": current_result_id > 0 and origin_id == current_result_id,
            }
        history_count = int(store.conn.execute(
            "SELECT COUNT(*) FROM audit_result "
            "WHERE batch_id=? AND resource_code=? AND year=?",
            (
                batch_id,
                str(stage_item["resource_code"]),
                str(stage_item["year"]),
            ),
        ).fetchone()[0])
        item: dict[str, Any] = {
            "stage_item_id": int(stage_item["id"]),
            "resource_code": str(stage_item["resource_code"]),
            "year": str(stage_item["year"]),
            "stage_status": str(stage_item["status"]),
            "attempt": int(stage_item["attempt"]),
            "stage_error_code": str(stage_item["error_code"]),
            "stage_error_message": str(stage_item["error_message"]),
            "current_result_id": current_result_id,
            "history_count": history_count,
            "binding": binding,
        }
        if result is not None:
            item.update({
                "award_name": str(result["award_name"]),
                "verdict": str(result["verdict"]),
                "confidence": str(result["confidence"]),
                "triage": str(result["triage"]),
                "review_status": str(result["review_status"]),
                "identity_version": str(result["identity_version"]),
                "source_kind": str(result["source_kind"]),
                "source_url": str(result["source_url"]),
                "source_urls": _public_value(_json_list(result["source_urls_json"])),
                "found_assets": _public_value(_json_list(result["found_assets_json"])),
                "page_year": str(result["page_year"]),
                "extracted_count": int(result["extracted_count"]),
                "submitted_count": int(result["submitted_count"]),
                "missing": _public_value(_json_list(result["missing_json"])),
                "extra": _public_value(_json_list(result["extra_json"])),
                "reason_codes": _public_value(_json_list(result["reason_codes_json"])),
                "notes": _public_value(str(result["notes"])),
                "created_at": str(result["created_at"]),
            })
        items.append(item)
    return items


def _audit_preview(
    store: Store,
    batch_id: int,
    *,
    import_roots: list[Path],
) -> dict[str, Any]:
    local = store.get_batch_stage_run(batch_id, "local")
    if local is None or str(local["status"]) != "done":
        raise StateConflictError("local 阶段尚未完成，不能预览 M4")
    prepared = load_prepared_from_context(
        store,
        batch_id,
        allowed_roots=import_roots,
    )
    precheck = l5_precheck.run_batch(
        list(prepared.imported_files), prepared.ledger, prober=None
    )
    targets = [
        target.model_dump(mode="json")
        for target in sorted(
            precheck.candidate_targets,
            key=lambda item: (item.resource_code, item.year),
        )
    ]
    raw_context = store.get_import_context(batch_id)
    if raw_context is None:
        raise StateConflictError("批次缺少导入上下文")
    binding = {
        "batch_id": batch_id,
        "files": json.loads(str(raw_context["files_json"])),
        "context_version": int(raw_context["context_version"]),
        "template_fingerprint": str(raw_context["template_fingerprint"]),
        "ledger_fingerprint": str(raw_context["ledger_fingerprint"]),
        "targets": targets,
    }
    digest = hashlib.sha256(
        json.dumps(binding, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {
        "batch_id": batch_id,
        "candidate_targets": targets,
        "issues": [issue.model_dump(mode="json") for issue in precheck.issues],
        "probe_status": "not_checked",
        "preview_digest": digest,
    }


def create_app(
    db_path: str | Path,
    *,
    evidence_roots: list[str | Path],
    import_roots: list[str | Path] | None = None,
    job_handlers: dict[str, JobHandler] | None = None,
    static_dir: str | Path | None = None,
    start_worker: bool = True,
    environment: str = "development",
) -> FastAPI:
    """Create without opening the database, reading config, or constructing model clients."""

    resolved_evidence_roots = [Path(root).resolve(strict=False) for root in evidence_roots]
    resolved_import_roots = [
        Path(root).resolve(strict=False) for root in (import_roots or [])
    ]
    if not resolved_evidence_roots:
        raise ValueError("at least one evidence root is required")
    if environment not in {"development", "acceptance", "production"}:
        raise ValueError("environment must be development, acceptance or production")
    database_label = Path(db_path).name if str(db_path) != ":memory:" else ":memory:"

    def current_promotion_context() -> dict[str, Any]:
        registry = load_template_registry()
        ledger = load_ledger()
        return {
            "allowed_roots": resolved_import_roots,
            "template_fingerprint": provenance.template_fingerprint(registry),
            "ledger_fingerprint": provenance.ledger_fingerprint(ledger),
            "context_version": provenance.CONTEXT_VERSION,
        }

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        store = Store(db_path)
        jobs = JobRepository(store)
        handlers = job_handlers
        if handlers is None:
            from award_audit.web.review_jobs import build_default_job_handlers

            handlers = build_default_job_handlers(
                db_path,
                evidence_roots=resolved_evidence_roots,
                import_roots=resolved_import_roots,
            )
        worker = JobWorker(jobs, handlers)
        app.state.store = store
        app.state.cases = CaseRepository(store)
        app.state.memories = MemoryRepository(store)
        app.state.memory_service = CaseMemoryService(store)
        app.state.jobs = jobs
        app.state.worker = worker
        if start_worker:
            worker.start()
        try:
            yield
        finally:
            if start_worker:
                await worker.stop()
            store.close()

    app = FastAPI(
        title="award-audit Web Review Console",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Any:
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self'; connect-src 'self'; frame-src 'self'; object-src 'none'; "
            "base-uri 'none'; form-action 'self'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(StateConflictError)
    async def state_conflict(_request: Request, exc: StateConflictError) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content={"error": "STATE_CONFLICT", "message": str(exc)[:300]},
        )

    @app.exception_handler(PromoteBlocked)
    async def promote_blocked(_request: Request, exc: PromoteBlocked) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content={"error": "PROMOTE_BLOCKED", "reasons": exc.reasons},
        )

    @app.exception_handler(provenance.ImportContextError)
    async def invalid_import_context(
        _request: Request, exc: provenance.ImportContextError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content={"error": "IMPORT_CONTEXT_INVALID", "message": str(exc)[:300]},
        )

    @app.exception_handler(KeyError)
    async def missing_resource(_request: Request, exc: KeyError) -> JSONResponse:
        return JSONResponse(
            status_code=404,
            content={"error": "NOT_FOUND", "message": str(exc)[:300]},
        )

    @app.get("/api/health")
    async def health(request: Request) -> dict[str, Any]:
        mode = str(request.app.state.store.conn.execute(
            "PRAGMA journal_mode"
        ).fetchone()[0]).lower()
        return {
            "ok": True,
            "journal_mode": mode,
            "worker": start_worker,
            "environment": environment,
            "database": database_label,
        }

    @app.get("/api/batches")
    async def batches(request: Request) -> dict[str, Any]:
        store: Store = request.app.state.store
        promotion_context = current_promotion_context()
        items: list[dict[str, Any]] = []
        for row in store.list_batches():
            batch_id = int(row["id"])
            issue_counts = {"blocker": 0, "format": 0, "review": 0}
            for staging in store.staging_of(batch_id):
                for issue in json.loads(staging["issues_json"]):
                    severity = str(issue.get("severity", ""))
                    if severity in issue_counts:
                        issue_counts[severity] += 1
            case_counts = {
                "total": 0, "queued": 0, "running": 0, "waiting_human": 0,
                "completed": 0, "failed": 0,
            }
            for case in store.list_audit_cases(batch_id=batch_id):
                case_counts["total"] += 1
                case_status = str(case["status"])
                if case_status in case_counts:
                    case_counts[case_status] += 1
            item = dict(row)
            item["issue_counts"] = issue_counts
            item["case_counts"] = case_counts
            item["l5_count"] = len(store.audit_results_of(batch_id))
            item["stages"] = _stage_payload(store, batch_id)
            item["promotion_readiness"] = store.promotion_readiness(
                batch_id, **promotion_context
            )
            items.append(item)
        return {"batches": items}

    @app.get("/api/batches/{batch_id}")
    async def batch_detail(batch_id: int, request: Request) -> dict[str, Any]:
        store: Store = request.app.state.store
        row = store.get_batch(batch_id)
        if row is None:
            raise KeyError(f"batch not found: {batch_id}")
        jobs: JobRepository = request.app.state.jobs
        related_jobs = [
            _job_payload(job) for job in jobs.list(limit=200) if job.batch_id == batch_id
        ]
        return {
            "batch": dict(row),
            "staging_count": len(store.staging_of(batch_id)),
            "l5_count": len(store.audit_results_of(batch_id)),
            "case_count": len(store.list_audit_cases(batch_id=batch_id)),
            "stages": _stage_payload(store, batch_id),
            "promotion_readiness": store.promotion_readiness(
                batch_id, **current_promotion_context()
            ),
            "jobs": related_jobs[:20],
        }

    @app.get("/api/batches/{batch_id}/audit-results")
    async def batch_audit_results(batch_id: int, request: Request) -> dict[str, Any]:
        store: Store = request.app.state.store
        if store.get_batch(batch_id) is None:
            raise KeyError(f"batch not found: {batch_id}")
        return {
            "batch_id": batch_id,
            "history_count": len(store.audit_results_of(batch_id)),
            "items": _m4_result_items(store, batch_id),
        }

    @app.post("/api/batches", status_code=status.HTTP_202_ACCEPTED)
    async def import_batch(
        body: BatchImportRequest,
        request: Request,
        x_reviewer: str = Header(..., alias="X-Reviewer"),
    ) -> dict[str, Any]:
        reviewer = _reviewer(x_reviewer)
        if not resolved_import_roots:
            raise HTTPException(status_code=403, detail="batch import roots are not configured")
        try:
            folder = validate_local_path(
                body.folder, resolved_import_roots, must_exist=True, file_only=False
            )
        except SafetyError as exc:
            raise HTTPException(status_code=403, detail=str(exc)[:300]) from exc
        if not folder.is_dir():
            raise HTTPException(status_code=422, detail="batch folder is not a directory")
        jobs: JobRepository = request.app.state.jobs
        return {"job": _job_payload(jobs.enqueue(
            "import_batch", {"folder": str(folder)}, created_by=reviewer
        ))}

    @app.post("/api/batches/upload", status_code=status.HTTP_202_ACCEPTED)
    async def upload_batch(
        request: Request,
        files: list[UploadFile] = File(...),
        x_reviewer: str = Header(..., alias="X-Reviewer"),
    ) -> dict[str, Any]:
        reviewer = _reviewer(x_reviewer)
        if not resolved_import_roots:
            raise HTTPException(status_code=403, detail="batch import roots are not configured")
        if not files or len(files) > 100:
            raise HTTPException(status_code=422, detail="upload must contain 1-100 XLSX files")

        names: list[str] = []
        for upload in files:
            raw_name = str(upload.filename or "").strip()
            name = Path(raw_name).name
            if (
                not name
                or name != raw_name
                or name.startswith("~$")
                or Path(name).suffix.casefold() != ".xlsx"
            ):
                raise HTTPException(status_code=422, detail="only plain XLSX filenames are allowed")
            names.append(name)
        if len(set(name.casefold() for name in names)) != len(names):
            raise HTTPException(status_code=422, detail="duplicate upload filenames are not allowed")

        upload_root = resolved_import_roots[0]
        upload_root.mkdir(parents=True, exist_ok=True)
        batch_name = f"upload-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(4)}"
        batch_folder = (upload_root / batch_name).resolve(strict=False)
        if batch_folder.parent != upload_root.resolve(strict=False):
            raise HTTPException(status_code=500, detail="managed upload directory is invalid")
        batch_folder.mkdir()
        total_bytes = 0
        try:
            for upload, name in zip(files, names, strict=True):
                destination = batch_folder / name
                file_bytes = 0
                with destination.open("xb") as handle:
                    while chunk := await upload.read(1024 * 1024):
                        file_bytes += len(chunk)
                        total_bytes += len(chunk)
                        if file_bytes > 50 * 1024 * 1024 or total_bytes > 250 * 1024 * 1024:
                            raise HTTPException(
                                status_code=413,
                                detail="upload exceeds the 50 MB file or 250 MB batch limit",
                            )
                        handle.write(chunk)
                await upload.close()
            jobs: JobRepository = request.app.state.jobs
            job = jobs.enqueue(
                "import_batch",
                {"folder": str(batch_folder), "upload": True, "file_count": len(names)},
                created_by=reviewer,
            )
        except Exception:
            if batch_folder.parent == upload_root.resolve(strict=False) and batch_folder.exists():
                shutil.rmtree(batch_folder)
            raise
        finally:
            for upload in files:
                await upload.close()
        return {
            "job": _job_payload(job),
            "upload": {
                "batch_name": batch_name,
                "file_count": len(names),
                "file_names": names,
            },
        }

    @app.post("/api/batches/{batch_id}/audit/preview")
    async def preview_batch_audit(
        batch_id: int,
        request: Request,
        x_reviewer: str = Header(..., alias="X-Reviewer"),
    ) -> dict[str, Any]:
        _reviewer(x_reviewer)
        store: Store = request.app.state.store
        if store.get_batch(batch_id) is None:
            raise KeyError(f"batch not found: {batch_id}")
        return _audit_preview(
            store, batch_id, import_roots=resolved_import_roots
        )

    @app.post("/api/batches/{batch_id}/audit", status_code=status.HTTP_202_ACCEPTED)
    async def start_batch_audit(
        batch_id: int,
        body: AuditConfirmRequest,
        request: Request,
        x_reviewer: str = Header(..., alias="X-Reviewer"),
    ) -> dict[str, Any]:
        reviewer = _reviewer(x_reviewer)
        store: Store = request.app.state.store
        preview = _audit_preview(
            store, batch_id, import_roots=resolved_import_roots
        )
        if preview["preview_digest"] != body.preview_digest:
            raise StateConflictError("预览内容已变化，请重新预览后确认")
        m4 = store.get_batch_stage_run(batch_id, "m4")
        if m4 is not None and str(m4["status"]) == "done":
            raise StateConflictError("M4 阶段已完成")
        jobs: JobRepository = request.app.state.jobs
        return {"job": _job_payload(jobs.enqueue_once(
            "audit_batch", {"batch_id": batch_id, "preview_digest": body.preview_digest},
            created_by=reviewer, batch_id=batch_id,
        ))}

    @app.post("/api/batches/{batch_id}/review", status_code=status.HTTP_202_ACCEPTED)
    async def start_batch_review(
        batch_id: int,
        request: Request,
        x_reviewer: str = Header(..., alias="X-Reviewer"),
    ) -> dict[str, Any]:
        reviewer = _reviewer(x_reviewer)
        store: Store = request.app.state.store
        if store.get_batch(batch_id) is None:
            raise KeyError(f"batch not found: {batch_id}")
        m4 = store.get_batch_stage_run(batch_id, "m4")
        if m4 is None or str(m4["status"]) not in {"done", "partial"}:
            raise StateConflictError("M4 阶段尚未收口，不能启动 M5")
        m5 = store.get_batch_stage_run(batch_id, "m5")
        queued_cases = sum(
            str(case["status"]) == "queued"
            for case in store.list_audit_cases(batch_id=batch_id)
        )
        if m5 is not None and str(m5["status"]) == "done" and queued_cases == 0:
            raise StateConflictError("M5 阶段已完成且没有待补证案件")
        jobs: JobRepository = request.app.state.jobs
        return {"job": _job_payload(jobs.enqueue_once(
            "review_batch", {"batch_id": batch_id}, created_by=reviewer, batch_id=batch_id
        ))}

    @app.post("/api/batches/{batch_id}/promote")
    async def promote_batch(
        batch_id: int,
        body: PromoteRequest,
        request: Request,
        x_reviewer: str = Header(..., alias="X-Reviewer"),
    ) -> dict[str, Any]:
        reviewer = _reviewer(x_reviewer)
        store: Store = request.app.state.store
        row = store.get_batch(batch_id)
        if row is None:
            raise KeyError(f"batch not found: {batch_id}")
        if body.expected_status and str(row["status"]) != body.expected_status:
            raise StateConflictError(
                f"batch status changed from {body.expected_status} to {row['status']}"
            )
        registry = load_template_registry()
        ledger = load_ledger()
        result = store.promote_batch(
            batch_id,
            operator=reviewer,
            allowed_roots=resolved_import_roots,
            template_fingerprint=provenance.template_fingerprint(registry),
            ledger_fingerprint=provenance.ledger_fingerprint(ledger),
            context_version=provenance.CONTEXT_VERSION,
        )
        jobs: JobRepository = request.app.state.jobs
        jobs.log_human_action(HumanAction(
            reviewer=reviewer,
            action="promote",
            target_type="batch",
            target_id=batch_id,
            before_version=0,
            after_version=0,
            summary=json.dumps(result, ensure_ascii=False)[:1000],
        ))
        return result

    @app.get("/api/issues")
    async def issues(
        request: Request,
        batch_id: int | None = None,
        severity: str = "",
        file: str = "",
        resource_code: str = "",
        field_code: str = "",
    ) -> dict[str, Any]:
        store: Store = request.app.state.store
        batches_to_scan = [batch_id] if batch_id is not None else [
            int(row["id"]) for row in store.list_batches()
        ]
        results: list[dict[str, Any]] = []
        for current_batch in batches_to_scan:
            for staging in store.staging_of(current_batch):
                if file and file.lower() not in str(staging["file"]).lower():
                    continue
                if resource_code and resource_code != str(staging["resource_code"]):
                    continue
                for issue in json.loads(staging["issues_json"]):
                    if severity and severity != str(issue.get("severity", "")):
                        continue
                    if field_code and field_code != str(issue.get("field_code") or ""):
                        continue
                    results.append({
                        "staging_id": int(staging["id"]),
                        "batch_id": current_batch,
                        "file": str(staging["file"]),
                        "sheet": str(staging["sheet"]),
                        "row_no": int(staging["row_no"]),
                        "resource_code": str(staging["resource_code"]),
                        **_public_value(issue),
                    })
        return {"issues": results[:2000], "is_truncated": len(results) > 2000}

    @app.get("/api/audit-cases")
    async def audit_cases(
        request: Request,
        batch_id: int | None = None,
        case_status: str = Query(default="", alias="status"),
    ) -> dict[str, Any]:
        store: Store = request.app.state.store
        rows = store.list_audit_cases(batch_id=batch_id, status=case_status)
        return {"cases": [{
            "case_id": int(row["id"]),
            "batch_id": int(row["batch_id"]),
            "resource_code": str(row["resource_code"]),
            "award_name": str(row["award_name"]),
            "year": str(row["year"]),
            "trigger_codes": json.loads(row["trigger_codes_json"]),
            "status": str(row["status"]),
            "confidence": str(row["confidence"]),
            "step_count": int(row["step_count"]),
            "token_used": int(row["token_used"]),
            "elapsed_ms": int(row["elapsed_ms"]),
            "reflection_count": int(row["reflection_count"]),
            "recommendation": str(row["recommendation"]),
            "human_decision": str(row["human_decision"]),
            "human_decision_summary": str(row["human_decision_summary"]),
            "reviewed_by": str(row["reviewed_by"]),
            "reviewed_at": str(row["reviewed_at"]),
            "state_version": int(row["state_version"]),
            "updated_at": str(row["updated_at"]),
        } for row in rows]}

    @app.get("/api/audit-cases/{case_id}")
    async def audit_case(case_id: int, request: Request) -> dict[str, Any]:
        cases: CaseRepository = request.app.state.cases
        public_state = _public_value(cases.load(case_id).model_dump(mode="json"))
        if not isinstance(public_state, dict):
            raise TypeError("public audit case state must be an object")
        state = public_state
        store: Store = request.app.state.store
        artifacts = store.conn.execute(
            "SELECT * FROM evidence_artifact WHERE case_id=? ORDER BY id", (case_id,)
        ).fetchall()
        state["artifacts"] = [{
            "artifact_id": int(row["id"]),
            "kind": str(row["kind"]),
            "source_url": str(row["source_url"]),
            "file_name": Path(str(row["local_path"])).name,
            "content_type": str(row["content_type"]),
            "sha256": str(row["sha256"]),
            "size_bytes": int(row["size_bytes"]),
            "fetched_at": str(row["fetched_at"]),
            "metadata": _public_value(json.loads(row["metadata_json"])),
            "preview_url": f"/api/audit-cases/{case_id}/artifacts/{int(row['id'])}",
        } for row in artifacts]
        state["attempts"] = _public_value(store.list_audit_attempts(case_id))
        attempts = state["attempts"]
        latest_attempt = attempts[-1] if isinstance(attempts, list) and attempts else None
        latest_attempt_id = (
            int(latest_attempt.get("attempt_id", 0))
            if isinstance(latest_attempt, dict) else 0
        )
        state["evidence_workflow"] = _public_value(
            store.evidence_workflow_summary(
                case_id, attempt_id=latest_attempt_id or None
            )
        )
        state["evidence_groups"] = _public_value(
            store.list_evidence_groups(case_id)
        )
        state["evidence_asset_routes"] = _public_value(
            store.list_evidence_asset_routes(case_id)
        )
        state["submission_conservation"] = _public_value(
            store.submission_conservation_summary(case_id)
        )
        state["comparison"] = _public_value(
            store.latest_evidence_comparison(case_id)
        )
        state["scopes"] = _public_value(store.list_audit_scopes(case_id))
        scope_comparisons = store.list_scope_comparisons(
            case_id, attempt_id=latest_attempt_id or None
        )
        for comparison in scope_comparisons:
            verifier = comparison.get("verifier", {})
            comparison["semantic_identity_decisions"] = (
                verifier.get("semantic_identity_decisions", [])
                if isinstance(verifier, dict) else []
            )
        state["scope_comparisons"] = _public_value(scope_comparisons)
        state["conclusion_readiness"] = (
            latest_attempt.get("conclusion_readiness", "incomplete")
            if isinstance(latest_attempt, dict) else "incomplete"
        )
        return {"case": state}

    @app.post("/api/audit-cases/{case_id}/supplement")
    async def supplement_case(
        case_id: int,
        body: SupplementRequest,
        request: Request,
        x_reviewer: str = Header(..., alias="X-Reviewer"),
    ) -> dict[str, Any]:
        reviewer = _reviewer(x_reviewer)
        cases: CaseRepository = request.app.state.cases
        updated = cases.request_supplement(
            case_id, body.request, expected_version=body.expected_version
        )
        jobs: JobRepository = request.app.state.jobs
        jobs.log_human_action(HumanAction(
            reviewer=reviewer,
            action="supplement",
            target_type="audit_case",
            target_id=case_id,
            before_version=body.expected_version,
            after_version=updated.state_version,
            summary=body.request,
        ))
        return {"case": updated.model_dump(mode="json")}

    @app.post("/api/audit-cases/{case_id}/review")
    async def review_case(
        case_id: int,
        body: CaseReviewRequest,
        request: Request,
        x_reviewer: str = Header(..., alias="X-Reviewer"),
    ) -> dict[str, Any]:
        reviewer = _reviewer(x_reviewer)
        cases: CaseRepository = request.app.state.cases
        updated = cases.finalize(
            case_id,
            body.decision,
            body.summary,
            reviewer,
            expected_version=body.expected_version,
        )
        memory_service: CaseMemoryService = request.app.state.memory_service
        candidate = memory_service.propose_from_case(updated)
        jobs: JobRepository = request.app.state.jobs
        jobs.log_human_action(HumanAction(
            reviewer=reviewer,
            action=f"case.{body.decision}",
            target_type="audit_case",
            target_id=case_id,
            before_version=body.expected_version,
            after_version=updated.state_version,
            summary=body.summary,
        ))
        return {
            "case": updated.model_dump(mode="json"),
            "candidate_memory": candidate.model_dump(mode="json") if candidate else None,
        }

    @app.get("/api/audit-cases/{case_id}/artifacts/{artifact_id}")
    async def artifact(case_id: int, artifact_id: int, request: Request) -> FileResponse:
        store: Store = request.app.state.store
        row = store.conn.execute(
            "SELECT * FROM evidence_artifact WHERE id=? AND case_id=?",
            (artifact_id, case_id),
        ).fetchone()
        if row is None:
            raise KeyError(f"artifact not found: {artifact_id}")
        try:
            path = validate_local_path(
                str(row["local_path"]),
                resolved_evidence_roots,
                must_exist=True,
                file_only=True,
            )
        except SafetyError as exc:
            raise HTTPException(status_code=403, detail="artifact path is not allowed") from exc
        content_type = str(row["content_type"]).lower()
        inline_types = {
            "application/pdf", "image/png", "image/jpeg", "image/webp", "image/gif"
        }
        if content_type in inline_types:
            return FileResponse(
                path,
                media_type=content_type,
                filename=path.name,
                content_disposition_type="inline",
            )
        return FileResponse(
            path,
            media_type="application/octet-stream",
            filename=path.name,
            content_disposition_type="attachment",
        )

    @app.get("/api/memories")
    async def memories(
        request: Request,
        memory_status: str = Query(default="", alias="status"),
    ) -> dict[str, Any]:
        repository: MemoryRepository = request.app.state.memories
        return {"memories": [
            item.model_dump(mode="json") for item in repository.list(status=memory_status)
        ]}

    async def memory_transition(
        memory_id: int,
        target_status: str,
        body: MemoryActionRequest,
        request: Request,
        reviewer_header: str,
    ) -> dict[str, Any]:
        reviewer = _reviewer(reviewer_header)
        repository: MemoryRepository = request.app.state.memories
        updated = repository.transition(
            memory_id,
            target_status,
            reviewer,
            expected_version=body.expected_version,
            merged_into_id=body.merged_into_id,
        )
        jobs: JobRepository = request.app.state.jobs
        jobs.log_human_action(HumanAction(
            reviewer=reviewer,
            action=f"memory.{target_status}",
            target_type="case_memory",
            target_id=memory_id,
            before_version=body.expected_version,
            after_version=updated.state_version,
            summary=(
                f"merged_into={body.merged_into_id}" if body.merged_into_id else target_status
            ),
        ))
        return {"memory": updated.model_dump(mode="json")}

    @app.post("/api/memories/{memory_id}/approve")
    async def approve_memory(
        memory_id: int,
        body: MemoryActionRequest,
        request: Request,
        x_reviewer: str = Header(..., alias="X-Reviewer"),
    ) -> dict[str, Any]:
        return await memory_transition(memory_id, "active", body, request, x_reviewer)

    @app.post("/api/memories/{memory_id}/deprecate")
    async def deprecate_memory(
        memory_id: int,
        body: MemoryActionRequest,
        request: Request,
        x_reviewer: str = Header(..., alias="X-Reviewer"),
    ) -> dict[str, Any]:
        return await memory_transition(memory_id, "deprecated", body, request, x_reviewer)

    @app.post("/api/memories/{memory_id}/merge")
    async def merge_memory(
        memory_id: int,
        body: MemoryActionRequest,
        request: Request,
        x_reviewer: str = Header(..., alias="X-Reviewer"),
    ) -> dict[str, Any]:
        if body.merged_into_id is None:
            raise HTTPException(status_code=422, detail="merged_into_id is required")
        return await memory_transition(memory_id, "merged", body, request, x_reviewer)

    @app.get("/api/jobs")
    async def list_jobs(request: Request) -> dict[str, Any]:
        jobs: JobRepository = request.app.state.jobs
        return {"jobs": [_job_payload(job) for job in jobs.list()]}

    @app.get("/api/jobs/{job_id}")
    async def get_job(job_id: int, request: Request) -> dict[str, Any]:
        jobs: JobRepository = request.app.state.jobs
        return {"job": _job_payload(jobs.get(job_id))}

    @app.post("/api/jobs/{job_id}/cancel")
    async def cancel_job(
        job_id: int,
        body: JobCancelRequest,
        request: Request,
        x_reviewer: str = Header(..., alias="X-Reviewer"),
    ) -> dict[str, Any]:
        reviewer = _reviewer(x_reviewer)
        jobs: JobRepository = request.app.state.jobs
        cancelled = jobs.cancel(job_id, expected_version=body.expected_version)
        jobs.log_human_action(HumanAction(
            reviewer=reviewer,
            action="job.cancel",
            target_type="audit_job",
            target_id=job_id,
            before_version=body.expected_version,
            after_version=cancelled.state_version,
            summary="cancelled before execution",
        ))
        return {"job": _job_payload(cancelled)}

    @app.get("/api/events")
    async def events(
        request: Request,
        after: int = Query(default=0, ge=0),
        once: bool = False,
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    ) -> StreamingResponse:
        if last_event_id and last_event_id.isdigit():
            after = max(after, int(last_event_id))
        jobs: JobRepository = request.app.state.jobs

        async def stream() -> AsyncIterator[str]:
            cursor = after
            idle_ticks = 0
            while True:
                records = jobs.events_after(cursor)
                for event in records:
                    cursor = event.event_id
                    data = json.dumps(event.model_dump(mode="json"), ensure_ascii=False)
                    yield f"id: {event.event_id}\nevent: {event.event_type}\ndata: {data}\n\n"
                if once:
                    return
                if await request.is_disconnected():
                    return
                idle_ticks += 1
                if idle_ticks % 30 == 0:
                    yield ": keepalive\n\n"
                await asyncio.sleep(0.5)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"X-Accel-Buffering": "no"},
        )

    resolved_static = Path(static_dir).resolve(strict=False) if static_dir else None
    if resolved_static is not None and resolved_static.is_dir():
        app.mount("/", StaticFiles(directory=resolved_static, html=True), name="webui")

    return app
