"""Shared batch preparation and M5 case orchestration for CLI and Web."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from award_audit.agent.harness.persistence import CaseRepository
from award_audit.agent.harness.runner import EvidenceHarness, build_default_harness
from award_audit.agent.investigation import InvestigationAgent
from award_audit.agent.integration import (
    CaseBridgeResult,
    ensure_imported_review_cases,
    ensure_review_cases,
)
from award_audit.agent.llm import LlmClient
from award_audit.agent.memory.service import CaseMemoryService
from award_audit.agent.loop import EvidenceReport, discover_resource
from award_audit.agent.review_agent.runner import SemanticReviewRunner
from award_audit.agent.toolkit import ToolBudgetLimits
from award_audit.agent.toolkit.contracts import ToolObservation, utc_now
from award_audit.agent.toolkit.registry import build_default_registry
from award_audit.agent.verification.service import (
    build_evidence_snapshot,
    deterministic_verify,
)
from award_audit.core import config
from award_audit.core.models.record import ImportedFile
from award_audit.core.models.template import TemplateSpec
from award_audit.core.pipeline import provenance
from award_audit.core.pipeline.checks import l5_precheck
from award_audit.core.pipeline.checks.l5_precheck import (
    AuditTarget,
    PrecheckResult,
    Prober,
    SearchHandoff,
    default_prober,
)
from award_audit.core.pipeline.engine import BatchResult, FileResult
from award_audit.core.pipeline.importer import import_batch as import_files
from award_audit.core.pipeline.importer import import_file
from award_audit.core.pipeline.ingest import ingest_batch
from award_audit.core.pipeline.store import Store
from award_audit.core.reference.ledger import LedgerEntry, load_ledger
from award_audit.core.reference.resource_map import ResourceMapEntry, load_resource_map
from award_audit.core.reference.template_registry import load_template_registry

FileImporter = Callable[[Path], list[ImportedFile]]
IngestRunner = Callable[..., tuple[int, BatchResult]]
HarnessFactory = Callable[[Store, list[Path]], EvidenceHarness]
SemanticRunnerFactory = Callable[[Store, list[Path]], SemanticReviewRunner]
ApproveCallback = Callable[[AuditTarget], bool]
VerifyResource = Callable[..., Any]
CaseProgress = Callable[[int, int, int], None]
ReportCallback = Callable[[dict[str, Any]], None]


@dataclass(frozen=True)
class PreparedReviewBatch:
    folder: Path
    batch_id: int
    imported_files: tuple[ImportedFile, ...]
    result: BatchResult
    registry: dict[str, TemplateSpec]
    ledger: dict[str, LedgerEntry]


@dataclass(frozen=True)
class AuditStageOutcome:
    precheck: PrecheckResult
    reports: tuple[dict[str, Any], ...]
    bridge: CaseBridgeResult
    status: str


def prepare_review_batch(
    folder: Path,
    store: Store,
    *,
    imported_files: Sequence[ImportedFile] | None = None,
    registry: Mapping[str, TemplateSpec] | None = None,
    resource_map: Mapping[str, ResourceMapEntry] | None = None,
    ledger: Mapping[str, LedgerEntry] | None = None,
    file_importer: FileImporter | None = None,
    ingest_runner: IngestRunner | None = None,
) -> PreparedReviewBatch:
    """Parse once, run L0-L4 once, and retain trusted inputs for later stages."""

    resolved_registry = dict(
        load_template_registry() if registry is None else registry
    )
    resolved_resource_map = dict(
        load_resource_map() if resource_map is None else resource_map
    )
    resolved_ledger = dict(load_ledger() if ledger is None else ledger)
    importer = file_importer or import_files
    files = list(imported_files) if imported_files is not None else importer(folder)
    ingest = ingest_runner or ingest_batch
    batch_id, result = ingest(
        folder,
        store,
        registry=resolved_registry,
        resource_map=resolved_resource_map,
        ledger=resolved_ledger,
        files=files,
    )
    return PreparedReviewBatch(
        folder=folder,
        batch_id=batch_id,
        imported_files=tuple(files),
        result=result,
        registry=resolved_registry,
        ledger=resolved_ledger,
    )


def load_prepared_from_context(
    store: Store,
    batch_id: int,
    *,
    allowed_roots: Sequence[str | Path],
    registry: Mapping[str, TemplateSpec] | None = None,
    ledger: Mapping[str, LedgerEntry] | None = None,
) -> PreparedReviewBatch:
    """Rehydrate parsed files and the persisted L0-L4 result without rerunning checks."""

    if store.needs_reimport(batch_id):
        raise RuntimeError("批次需要重新导入")
    resolved_registry = dict(load_template_registry() if registry is None else registry)
    resolved_ledger = dict(load_ledger() if ledger is None else ledger)
    context = store.load_import_context(
        batch_id,
        allowed_roots=allowed_roots,
        template_fingerprint=provenance.template_fingerprint(resolved_registry),
        ledger_fingerprint=provenance.ledger_fingerprint(resolved_ledger),
        context_version=provenance.CONTEXT_VERSION,
    )
    if context is None:
        raise RuntimeError("批次缺少导入上下文，需要重新导入")
    check_result = context["check_result"]
    batch_name = str(check_result.get("batch", ""))
    imported = tuple(
        import_file(Path(item["path"]), batch_name) for item in context["files"]
    )
    result = BatchResult(
        batch=batch_name,
        files=[FileResult.model_validate(item) for item in check_result.get("files", [])],
    )
    return PreparedReviewBatch(
        folder=Path(context["source_folder"]),
        batch_id=batch_id,
        imported_files=imported,
        result=result,
        registry=resolved_registry,
        ledger=resolved_ledger,
    )


def _normalized_code(value: object) -> str:
    text = str(value or "").strip()
    return text.zfill(8) if text.isdigit() else text.casefold()


def _claimed_groups(
    search_handoffs: Sequence[SearchHandoff],
    reports: Sequence[Mapping[str, Any]],
) -> list[tuple[str, str]]:
    groups = [
        (_normalized_code(item.resource_code), str(item.year or "").strip())
        for item in search_handoffs
        if str(item.resource_code or "").strip()
    ]
    groups.extend(
        (_normalized_code(item.get("resource_code")), str(item.get("year", "")).strip())
        for item in reports
        if str(item.get("resource_code", "")).strip()
    )
    return groups


def _is_claimed(item: ImportedFile, groups: Sequence[tuple[str, str]]) -> bool:
    code = _normalized_code(item.first_zylbm)
    year = item.year.strip()
    return any(group_code == code and (not group_year or group_year == year)
               for group_code, group_year in groups)


def queue_prepared_review_cases(
    store: Store,
    prepared: PreparedReviewBatch,
    *,
    search_handoffs: Iterable[SearchHandoff] = (),
    audit_reports: Iterable[Mapping[str, Any]] | None = None,
    tool_limits: ToolBudgetLimits | None = None,
    require_m4_binding: bool = False,
) -> CaseBridgeResult:
    """Build final cases from M4 evidence, then cover every unclaimed import group."""

    handoffs = list(search_handoffs)
    reports = list(audit_reports or ())
    evidence_result = ensure_review_cases(
        store,
        prepared.batch_id,
        search_handoffs=handoffs,
        audit_reports=reports,
        imported_files=prepared.imported_files,
        registry=prepared.registry,
        ledger=prepared.ledger,
        tool_limits=tool_limits,
        require_m4_binding=require_m4_binding,
    )
    claimed_groups = _claimed_groups(handoffs, reports)
    reviewable_files = [
        item.file_name
        for item in prepared.imported_files
        if item.first_zylbm.strip() and not _is_claimed(item, claimed_groups)
    ]
    issue_codes_by_file = {
        item.file: [issue.rule_id for issue in item.issues]
        for item in prepared.result.files
    }
    issues_by_file = {
        item.file: [
            {**issue.model_dump(mode="json"), "file": item.file}
            for issue in item.issues
        ]
        for item in prepared.result.files
    }
    generic_result = ensure_imported_review_cases(
        store,
        prepared.batch_id,
        imported_files=prepared.imported_files,
        eligible_files=reviewable_files,
        registry=prepared.registry,
        ledger=prepared.ledger,
        issue_codes_by_file=issue_codes_by_file,
        issues_by_file=issues_by_file,
        tool_limits=tool_limits,
        require_m4_binding=require_m4_binding,
    )
    return CaseBridgeResult(
        case_ids=list(dict.fromkeys([
            *evidence_result.case_ids,
            *generic_result.case_ids,
        ])),
        created=evidence_result.created + generic_result.created,
        existing=evidence_result.existing + generic_result.existing,
        skipped=evidence_result.skipped + generic_result.skipped,
    )


def run_audit_stage(
    store: Store,
    prepared: PreparedReviewBatch,
    *,
    ledger: Mapping[str, LedgerEntry] | None = None,
    prober: Prober = default_prober,
    approve: ApproveCallback | None = None,
    verify: VerifyResource | None = None,
    llm: Any | None = None,
    workdir: Path | None = None,
    use_corpus: bool = False,
    tool_limits: ToolBudgetLimits | None = None,
    on_report: ReportCallback | None = None,
) -> AuditStageOutcome:
    """Run the shared M4 stage with durable claims, retries, and fail-closed handoff."""

    if use_corpus:
        raise ValueError("统一审核流程禁止使用未按年份隔离的 corpus")
    worker = f"m4-{uuid.uuid4().hex[:12]}"
    batch_claim = store.claim_batch_stage(
        prepared.batch_id, "m4", worker=worker, lease_seconds=3600
    )
    if batch_claim is None:
        raise RuntimeError("M4 阶段正在运行，无法重复启动")
    expected_batch_version = int(batch_claim["state_version"])
    reports: list[dict[str, Any]] = []
    failed_or_skipped = 0
    try:
        resolved_ledger = dict(prepared.ledger if ledger is None else ledger)
        precheck = l5_precheck.run_batch(
            list(prepared.imported_files), resolved_ledger, prober
        )
        groups: dict[tuple[str, str], list[ImportedFile]] = {}
        for item in prepared.imported_files:
            if item.first_zylbm.strip():
                groups.setdefault(
                    (_normalized_code(item.first_zylbm), item.year.strip()), []
                ).append(item)

        handoff_keys: set[tuple[str, str]] = set()
        for handoff in precheck.search_handoffs:
            key = (_normalized_code(handoff.resource_code), handoff.year.strip())
            handoff_keys.add(key)
            item_claim = store.claim_stage_item(
                prepared.batch_id, key[0], key[1], worker=worker, lease_seconds=3600
            )
            if item_claim is not None:
                payload = EvidenceReport(
                    resource_code=key[0],
                    award_name=handoff.award_name,
                    year=key[1],
                    verdict="无法核对",
                    confidence="low",
                    source_urls=list(handoff.known_urls),
                    submitted_count=sum(item.n_rows for item in groups.get(key, [])),
                    reason_codes=[handoff.trigger_code.casefold()],
                    notes="M4 来源不可用，已形成正式结果并转 M5 补证",
                ).model_dump(mode="json")
                result_id = store.add_audit_results(prepared.batch_id, [payload])[0]
                store.finish_stage_item(
                    prepared.batch_id, key[0], key[1], status="skipped",
                    current_result_id=result_id,
                    worker=worker, expected_version=int(item_claim["state_version"]),
                    error_code=handoff.trigger_code,
                    error_message="来源不可用，已转入 M5 补证",
                )
                reports.append(payload)
                failed_or_skipped += 1

        verifier = verify or discover_resource
        resolved_llm = llm if llm is not None else (
            LlmClient() if verify is not None else None
        )
        resolved_workdir = workdir or (config.out_dir() / "agent_downloads")
        for target in [*precheck.passable_targets, *precheck.retry_targets]:
            key = (_normalized_code(target.resource_code), target.year.strip())
            members = groups.get(key, [])
            if not members or key in handoff_keys:
                continue
            item_claim = store.claim_stage_item(
                prepared.batch_id, key[0], key[1], worker=worker, lease_seconds=3600
            )
            if item_claim is None:
                continue
            if approve is not None and not approve(target):
                payload = EvidenceReport(
                    resource_code=key[0],
                    award_name=target.award_name,
                    year=key[1],
                    verdict="无法核对",
                    confidence="low",
                    source_urls=list(target.urls),
                    submitted_count=target.submitted_count,
                    reason_codes=["user_declined"],
                    notes="用户未批准 M4 联网核对，已形成正式结果并转 M5 补证",
                ).model_dump(mode="json")
                result_id = store.add_audit_results(prepared.batch_id, [payload])[0]
                store.finish_stage_item(
                    prepared.batch_id, key[0], key[1], status="skipped",
                    current_result_id=result_id,
                    worker=worker, expected_version=int(item_claim["state_version"]),
                    error_code="USER_DECLINED", error_message="用户未批准联网核对",
                )
                reports.append(payload)
                failed_or_skipped += 1
                continue
            try:
                report = verifier(
                    key[0], members, target.urls,
                    prepared.registry.get(members[0].claimed_table_code),
                    resolved_llm, resolved_workdir, use_corpus=False,
                )
                payload = (
                    report.model_dump(mode="json")
                    if hasattr(report, "model_dump") else dict(report)
                )
                result_id = store.add_audit_results(prepared.batch_id, [payload])[0]
                store.finish_stage_item(
                    prepared.batch_id, key[0], key[1], status="done",
                    current_result_id=result_id, worker=worker,
                    expected_version=int(item_claim["state_version"]),
                )
            except Exception as exc:  # noqa: BLE001 - each target fails closed independently
                payload = EvidenceReport(
                    resource_code=key[0], award_name=target.award_name, year=key[1],
                    verdict="无法核对", confidence="low", source_urls=target.urls,
                    submitted_count=target.submitted_count,
                    evidence=list(getattr(exc, "evidence", []) or []),
                    found_assets=list(getattr(exc, "found_assets", []) or []),
                    reason_codes=["verify_exception"],
                    notes=f"核对异常，已转 M5 补证：{type(exc).__name__}",
                ).model_dump(mode="json")
                result_id = store.add_audit_results(prepared.batch_id, [payload])[0]
                store.finish_stage_item(
                    prepared.batch_id, key[0], key[1], status="failed",
                    current_result_id=result_id, worker=worker,
                    expected_version=int(item_claim["state_version"]),
                    error_code=f"VERIFY_{type(exc).__name__.upper()}",
                    error_message="联网核对异常，已保存证据并转 M5 补证",
                )
                failed_or_skipped += 1
            reports.append(payload)
            if on_report is not None:
                on_report(payload)

        case_reports = [
            report
            for report in reports
            if (
                _normalized_code(report.get("resource_code")),
                str(report.get("year", "")).strip(),
            ) not in handoff_keys
        ]
        bridge = queue_prepared_review_cases(
            store,
            prepared,
            search_handoffs=precheck.search_handoffs,
            audit_reports=case_reports,
            tool_limits=tool_limits,
            require_m4_binding=True,
        )
        status = "partial" if failed_or_skipped else "done"
        store.finish_batch_stage(
            prepared.batch_id, "m4", status, worker=worker,
            expected_version=expected_batch_version,
        )
        return AuditStageOutcome(
            precheck=precheck, reports=tuple(reports), bridge=bridge, status=status
        )
    except Exception as exc:
        current = store.get_batch_stage_run(prepared.batch_id, "m4")
        if current is not None and current["status"] == "running":
            store.finish_batch_stage(
                prepared.batch_id, "m4", "failed", worker=worker,
                expected_version=int(current["state_version"]),
                error_code=f"M4_{type(exc).__name__.upper()}",
                error_message="M4 阶段未能收口",
            )
        raise


def default_harness_factory(store: Store, roots: list[Path]) -> EvidenceHarness:
    """Legacy injection point retained for isolated compatibility tests."""

    allowed_roots: list[str | Path] = list(roots)
    return build_default_harness(store, allowed_roots=allowed_roots)


def default_semantic_runner_factory(
    store: Store,
    roots: list[Path],
) -> SemanticReviewRunner:
    """Build the production M5 path from an LLM and M4-bounded evidence roots."""

    repository = CaseRepository(store)
    review_llm = LlmClient()
    memory_service = CaseMemoryService(store)
    investigation_limits = ToolBudgetLimits(
        max_ocr_pages=100,
        max_vision_pages=100,
        wall_time_seconds=30 * 60,
    )

    def retrieve_active_memory(case_id: int) -> list[dict[str, object]]:
        state = repository.load(case_id)
        return [
            hit.model_dump(mode="json")
            for hit in memory_service.retrieve_for_case(state, limit=3)
        ]

    def checkpoint_graph_node(event: dict[str, object]) -> None:
        """Persist each graph transition before the following node can run."""

        case_id = int(event["case_id"])
        state = repository.load(case_id)
        trace = ToolObservation(
            call_id=(
                f"langgraph-{case_id}-{str(event.get('node', 'node'))}-"
                f"{int(event.get('step_count', 0) or 0)}"
            ),
            tool_name=f"langgraph:{str(event.get('node', 'unknown'))}",
            started_at=str(event.get("started_at", utc_now())),
            finished_at=str(event.get("finished_at", utc_now())),
            duration_ms=int(event.get("duration_ms", 0) or 0),
            input_summary={"step_count": int(event.get("step_count", 0) or 0)},
            output_summary={
                "transition_reason": str(event.get("transition_reason", "")),
                "checkpoint": True,
            },
            ok=True,
        )
        state.tool_trace.append(trace)
        state.step_count += 1
        repository.save(state, traces=[trace])

    return SemanticReviewRunner(
        repository,
        review_llm=review_llm,
        investigation_agent=InvestigationAgent(
            review_llm,
            build_default_registry(),
            allowed_roots=roots,
            memory_lookup=retrieve_active_memory,
            node_event_sink=checkpoint_graph_node,
            planner_tool_names=(
                "fetch_web_page",
                "search_official_award",
                "extract_search_document",
                "download_evidence",
                "parse_spreadsheet",
                "inspect_pdf",
                "extract_pdf_text",
                "render_pdf_pages",
                "ocr_image",
                "vision_extract_roster",
                "compare_roster",
            ),
            limits=investigation_limits,
            max_steps=12,
        ),
        allowed_roots=roots,
        tool_limits=investigation_limits,
    )


def execution_error_summary(exc: Exception) -> str:
    if isinstance(exc, PermissionError):
        return "运行环境配置或证据目录不可访问，请检查服务进程权限。"
    if isinstance(exc, RuntimeError):
        return "案件执行器发生运行时错误，已有案件互相隔离，请检查服务日志。"
    if isinstance(exc, ValueError):
        return "案件输入或工具参数不符合约束，请检查案件上下文。"
    return "案件执行器发生未分类错误，请检查服务日志。"


def run_review_case(
    db_path: str | Path,
    evidence_roots: Sequence[str | Path],
    case_id: int,
    harness_factory: HarnessFactory | None = None,
    *,
    semantic_runner_factory: SemanticRunnerFactory | None = None,
) -> dict[str, object]:
    database = str(db_path)
    roots = [Path(root).resolve(strict=False) for root in evidence_roots]
    store = Store(database)
    try:
        if harness_factory is not None:
            outcome = harness_factory(store, roots).run(case_id)
        else:
            semantic_factory = semantic_runner_factory or default_semantic_runner_factory
            outcome = semantic_factory(store, roots).run(case_id)
        attempts = store.list_audit_attempts(case_id)
        attempt = attempts[-1] if attempts else {
            "attempt_id": 0,
            "status": "incomplete",
            "conclusion_readiness": "incomplete",
            "verifier_status": "missing",
            "blockers": ["execution_attempt_missing", "verifier_missing"],
        }
        raw_attempt_id = attempt.get("attempt_id")
        selected_attempt_id = (
            int(raw_attempt_id)
            if isinstance(raw_attempt_id, (str, int, float)) and raw_attempt_id
            else None
        )
        workflow = store.evidence_workflow_summary(
            case_id, attempt_id=selected_attempt_id
        )
        attempt_blockers = attempt.get("blockers", [])
        workflow_blockers = workflow.get("blockers", [])
        if not isinstance(attempt_blockers, list):
            attempt_blockers = []
        if not isinstance(workflow_blockers, list):
            workflow_blockers = []
        return {
            "case_id": case_id,
            "status": outcome.state.status,
            "stopped_reason": outcome.stopped_reason,
            "attempt_id": attempt["attempt_id"],
            "attempt_status": attempt["status"],
            "conclusion_readiness": attempt["conclusion_readiness"],
            "verifier_status": attempt["verifier_status"],
            "blockers": list(dict.fromkeys([
                *(str(item) for item in attempt_blockers),
                *(str(item) for item in workflow_blockers),
            ])),
        }
    finally:
        store.close()


def mark_review_case_for_human(
    db_path: str | Path,
    case_id: int,
    error_code: str,
    error_summary: str,
) -> dict[str, object]:
    store = Store(db_path)
    try:
        repository = CaseRepository(store)
        state = repository.load(case_id)
        if state.status != "completed":
            state.status = "waiting_human"
            state.last_error = error_code[:100]
            state.recommendation = f"{error_summary} 已隔离并转人工复核。"
            if "job_handler_error" not in state.reason_codes:
                state.reason_codes.append("job_handler_error")
            verification = deterministic_verify(build_evidence_snapshot(state, [])).model_copy(
                update={
                    "recommended_action": "manual",
                    "reason_codes": ["job_handler_error", "deterministic_terminal_verification"],
                }
            )
            state.latest_verification = verification
            repository.record_comparison(state, [], verification)
            repository.save(state, verifications=[verification])
            repository.finish_attempt(
                state, stopped_reason="job_handler_error", failed=True
            )
        attempts = store.list_audit_attempts(case_id)
        latest = attempts[-1] if attempts else {}
        return {
            "case_id": case_id,
            "status": state.status,
            "stopped_reason": "job_handler_error",
            "attempt_id": latest.get("attempt_id", 0),
            "attempt_status": latest.get("status", "failed"),
            "conclusion_readiness": latest.get("conclusion_readiness", "incomplete"),
            "verifier_status": latest.get("verifier_status", "missing"),
            "blockers": latest.get("blockers", ["job_handler_error"]),
        }
    finally:
        store.close()


def run_queued_review_cases(
    db_path: str | Path,
    batch_id: int,
    *,
    evidence_roots: Sequence[str | Path],
    harness_factory: HarnessFactory | None = None,
    semantic_runner_factory: SemanticRunnerFactory | None = None,
    progress: CaseProgress | None = None,
) -> list[dict[str, object]]:
    """Run queued M5 cases only after M4 closure, under a durable batch claim."""

    worker = f"m5-{uuid.uuid4().hex[:12]}"
    store = Store(db_path)
    try:
        m4 = store.get_batch_stage_run(batch_id, "m4")
        if m4 is None or str(m4["status"]) not in {"done", "partial"}:
            raise RuntimeError("M4 阶段尚未收口，不能启动 M5")
        stage_claim = store.claim_batch_stage(
            batch_id, "m5", worker=worker, lease_seconds=3600
        )
        if stage_claim is None:
            raise RuntimeError("M5 阶段正在运行，不能重复启动")
        expected_version = int(stage_claim["state_version"])
        rows = store.list_audit_cases(batch_id=batch_id, status="queued")
        case_ids = [int(row["id"]) for row in rows]
        try:
            for case_id in case_ids:
                store.validate_audit_case_m4_binding(case_id)
        except Exception as exc:
            store.finish_batch_stage(
                batch_id,
                "m5",
                "failed",
                worker=worker,
                expected_version=expected_version,
                error_code="M5_M4_BINDING_INVALID",
                error_message=str(exc)[:500],
            )
            raise
    finally:
        store.close()
    results: list[dict[str, object]] = []
    try:
        for index, case_id in enumerate(case_ids, start=1):
            if progress is not None:
                progress(index, len(case_ids), case_id)
            try:
                result = run_review_case(
                    db_path,
                    evidence_roots,
                    case_id,
                    harness_factory,
                    semantic_runner_factory=semantic_runner_factory,
                )
            except Exception as exc:  # noqa: BLE001 - each case fails closed in isolation
                result = mark_review_case_for_human(
                    db_path,
                    case_id,
                    f"JOB_{type(exc).__name__.upper()}",
                    execution_error_summary(exc),
                )
                result["execution_error_type"] = type(exc).__name__
                result["execution_error_detail"] = (
                    " ".join(str(exc).split())[:500]
                )
            results.append(result)
        final_status = (
            "done"
            if results and all(
                item.get("conclusion_readiness") == "ready_for_human"
                and item.get("verifier_status") == "persisted"
                for item in results
            )
            else "partial"
        )
        final_store = Store(db_path)
        try:
            final_store.finish_batch_stage(
                batch_id, "m5", final_status, worker=worker,
                expected_version=expected_version,
            )
        finally:
            final_store.close()
        return results
    except Exception as exc:
        failed_store = Store(db_path)
        try:
            current = failed_store.get_batch_stage_run(batch_id, "m5")
            if current is not None and current["status"] == "running":
                failed_store.finish_batch_stage(
                    batch_id, "m5", "failed", worker=worker,
                    expected_version=int(current["state_version"]),
                    error_code=f"M5_{type(exc).__name__.upper()}",
                    error_message="M5 阶段未能收口",
                )
        finally:
            failed_store.close()
        raise
