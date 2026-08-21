from __future__ import annotations

from pathlib import Path

from openpyxl import Workbook

from award_audit.agent.harness.models import CaseSeed, HarnessOutcome
from award_audit.agent.harness.persistence import CaseRepository
from award_audit.agent.loop import EvidenceReport
from award_audit.agent.review_workflow import (
    PreparedReviewBatch,
    load_prepared_from_context,
    prepare_review_batch,
    queue_prepared_review_cases,
    run_audit_stage,
    run_queued_review_cases,
    run_review_case,
)
from award_audit.core.models.record import ImportedFile
from award_audit.core.pipeline.engine import BatchResult, FileResult
from award_audit.core.pipeline.store import Store
from award_audit.core.reference.ledger import LedgerEntry
from award_audit.core.reference.resource_map import ResourceMapEntry
from award_audit.core.reference.template_registry import build_template_spec


def _imported_file(folder: Path) -> ImportedFile:
    path = folder / "CON_GG_XK_RCPY_GXDJSCGR-未登记新奖项-2026.xlsx"
    codes = ["ZYLBM", "ZYLB", "XMMC", "XRYXM", "ND"]
    names = ["资源项码", "资源项", "项目名称", "获奖人", "年度"]
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "数据"
    sheet.append(codes)
    sheet.append(names)
    sheet.append(["04059999", "未登记新奖项", "年度人物", "张三", "2026"])
    workbook.save(path)
    return ImportedFile(
        batch=folder.name,
        path=str(path),
        file_name=path.name,
        claimed_table_code="CON_GG_XK_RCPY_GXDJSCGR",
        award_name="未登记新奖项",
        year="2026",
        sheet_name="数据",
        header_codes=codes,
        header_names=names,
        rows=[["04059999", "未登记新奖项", "年度人物", "张三", "2026"]],
    )


def test_load_prepared_from_validated_context_without_rerunning_checks(
    tmp_path: Path,
) -> None:
    folder = tmp_path / "rehydrate"
    folder.mkdir()
    imported = _imported_file(folder)
    spec = build_template_spec(
        imported.claimed_table_code,
        imported.sheet_name,
        imported.header_codes,
        imported.header_names,
    )
    resource_map = {
        "04059999": ResourceMapEntry(
            resource_code="04059999",
            resource_name="未登记新奖项",
            table_code=imported.claimed_table_code,
        )
    }
    ledger = {"04059999": LedgerEntry(
        resource_code="04059999",
        resource_name="未登记新奖项",
        expected_count=1,
        collect_url="https://official.example/award",
    )}
    store = Store(tmp_path / "rehydrate.db")
    prepared = prepare_review_batch(
        folder,
        store,
        imported_files=[imported],
        registry={imported.claimed_table_code: spec},
        resource_map=resource_map,
        ledger=ledger,
    )
    loaded = load_prepared_from_context(
        store,
        prepared.batch_id,
        allowed_roots=[tmp_path],
        registry={imported.claimed_table_code: spec},
        ledger=ledger,
    )
    assert loaded.result.model_dump() == prepared.result.model_dump()
    assert loaded.imported_files[0].rows == imported.rows
    store.close()


def test_cli_and_web_modes_share_prepared_case_context(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from award_audit.agent import review_workflow

    folder = tmp_path / "new-batch"
    folder.mkdir()
    imported = _imported_file(folder)
    spec = build_template_spec(
        imported.claimed_table_code,
        imported.sheet_name,
        imported.header_codes,
        imported.header_names,
    )
    resource_map = {
        "04059999": ResourceMapEntry(
            resource_code="04059999",
            resource_name="未登记新奖项",
            table_code=imported.claimed_table_code,
        )
    }
    ledger = {
        "04059999": LedgerEntry(
            resource_code="04059999",
            resource_name="未登记新奖项",
            expected_count=1,
            collect_url="https://official.example/unregistered-award",
        )
    }
    parse_calls = 0

    def fake_import(_folder: Path) -> list[ImportedFile]:
        nonlocal parse_calls
        parse_calls += 1
        return [imported]

    def fake_ingest(folder_path: Path, store: Store, **kwargs):
        assert kwargs["files"] == [imported]
        return store.create_batch(folder_path.name), BatchResult(
            batch=folder_path.name,
            files=[FileResult(
                file=imported.file_name,
                claimed_table_code=imported.claimed_table_code,
                n_rows=1,
                issues=[],
            )],
        )

    monkeypatch.setattr(review_workflow, "import_files", fake_import)
    monkeypatch.setattr(review_workflow, "ingest_batch", fake_ingest)

    summaries: list[dict[str, object]] = []
    for index, reports in enumerate((None, [{
        "resource_code": "04059999",
        "award_name": "未登记新奖项",
        "year": "2026",
        "verdict": "无法核对",
        "confidence": "low",
        "source_kind": "none",
        "submitted_count": 1,
        "reason_codes": ["coverage_unknown"],
    }])):
        store = Store(tmp_path / f"mode-{index}.db")
        prepared = prepare_review_batch(
            folder,
            store,
            registry={imported.claimed_table_code: spec},
            resource_map=resource_map,
            ledger=ledger,
        )
        bridge = queue_prepared_review_cases(
            store,
            prepared,
            audit_reports=reports,
        )
        assert bridge.created == 1
        state = CaseRepository(store).load(bridge.case_ids[0])
        summaries.append(state.submitted_summary)
        store.close()

    assert parse_calls == 2
    trusted_keys = {
        "submission_files",
        "table_codes",
        "match_profile",
        "match_fields",
        "match_combine",
        "submitted_rows",
        "expected_scope_count",
        "ledger_resource_name",
    }
    assert {key: summaries[0][key] for key in trusted_keys} == {
        key: summaries[1][key] for key in trusted_keys
    }


def test_high_confidence_m4_pass_is_not_requeued_by_generic_fallback(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from award_audit.agent import review_workflow

    folder = tmp_path / "auto-pass-batch"
    folder.mkdir()
    imported = _imported_file(folder)
    spec = build_template_spec(
        imported.claimed_table_code,
        imported.sheet_name,
        imported.header_codes,
        imported.header_names,
    )

    monkeypatch.setattr(review_workflow, "import_files", lambda _folder: [imported])
    monkeypatch.setattr(
        review_workflow,
        "ingest_batch",
        lambda folder_path, store, **_kwargs: (
            store.create_batch(folder_path.name),
            BatchResult(
                batch=folder_path.name,
                files=[FileResult(
                    file=imported.file_name,
                    claimed_table_code=imported.claimed_table_code,
                    n_rows=1,
                    issues=[],
                )],
            ),
        ),
    )
    store = Store(tmp_path / "auto-pass.db")
    prepared = prepare_review_batch(
        folder,
        store,
        registry={imported.claimed_table_code: spec},
        resource_map={},
        ledger={},
    )
    result = queue_prepared_review_cases(store, prepared, audit_reports=[{
        "resource_code": "04059999",
        "award_name": "未登记新奖项",
        "year": "2026",
        "verdict": "一致",
        "confidence": "high",
        "submitted_count": 1,
    }])

    assert result.created == 0
    assert store.list_audit_cases(batch_id=prepared.batch_id) == []
    store.close()


def test_shared_case_runner_isolates_failure_for_cli_and_web(tmp_path: Path) -> None:
    db_path = tmp_path / "shared-runner.db"
    store = Store(db_path)
    batch_id = store.create_batch("shared-runner")
    origin_ids: dict[str, int] = {}
    for code, name in (("new-001", "未登记奖项一"), ("new-002", "未登记奖项二")):
        result_id = store.add_audit_results(batch_id, [{
            "resource_code": code,
            "award_name": name,
            "year": "2026",
            "verdict": "无法核对",
            "confidence": "low",
            "source_kind": "none",
            "submitted_count": 1,
            "extracted_count": 0,
        }])[0]
        item = store.claim_stage_item(batch_id, code, "2026", worker="test")
        assert item is not None
        store.finish_stage_item(
            batch_id,
            code,
            "2026",
            status="failed",
            current_result_id=result_id,
            error_code="TEST_HANDOFF",
            worker="test",
            expected_version=int(item["state_version"]),
        )
        origin_ids[code] = result_id
    stage = store.claim_batch_stage(batch_id, "m4", worker="test")
    assert stage is not None
    store.finish_batch_stage(
        batch_id, "m4", "done", worker="test",
        expected_version=int(stage["state_version"]),
    )
    repository = CaseRepository(store)
    good, _ = repository.create_or_get(CaseSeed(
        batch_id=batch_id,
        resource_code="NEW-001",
        award_name="未登记奖项一",
        year="2026",
        origin_m4_result_id=origin_ids["new-001"],
        trigger_codes=["COVERAGE_UNKNOWN"],
        objective="共享执行器正常案件",
    ))
    bad, _ = repository.create_or_get(CaseSeed(
        batch_id=batch_id,
        resource_code="NEW-002",
        award_name="未登记奖项二",
        year="2026",
        origin_m4_result_id=origin_ids["new-002"],
        trigger_codes=["SOURCE_UNREACHABLE"],
        objective="共享执行器异常隔离",
    ))
    store.close()

    class FakeHarness:
        def run(self, case_id: int) -> HarnessOutcome:
            case_store = Store(db_path)
            try:
                state = CaseRepository(case_store).load(case_id)
                if case_id == bad.case_id:
                    raise RuntimeError("isolated failure")
                state.status = "waiting_human"
                state.recommendation = "证据已整理，等待人工终审。"
                CaseRepository(case_store).save(state)
                return HarnessOutcome(state=state, stopped_reason="recommendation_ready")
            finally:
                case_store.close()

    results = run_queued_review_cases(
        db_path,
        batch_id,
        evidence_roots=[tmp_path],
        harness_factory=lambda _store, _roots: FakeHarness(),  # type: ignore[arg-type]
    )

    assert [item["case_id"] for item in results] == [good.case_id, bad.case_id]
    assert all(item["status"] == "waiting_human" for item in results)
    assert results[0]["stopped_reason"] == "recommendation_ready"
    assert results[1]["stopped_reason"] == "job_handler_error"
    assert results[1]["execution_error_type"] == "RuntimeError"
    assert results[1]["execution_error_detail"] == "isolated failure"
    reopened = Store(db_path)
    try:
        failed = CaseRepository(reopened).load(bad.case_id)
        assert failed.last_error == "JOB_RUNTIMEERROR"
        assert "已隔离并转人工复核" in failed.recommendation
    finally:
        reopened.close()


def test_case_runner_defaults_to_semantic_runner(tmp_path: Path, monkeypatch) -> None:
    from award_audit.agent import review_workflow

    db_path = tmp_path / "semantic-default.db"
    store = Store(db_path)
    batch_id = store.create_batch("semantic-default")
    repository = CaseRepository(store)
    state, _ = repository.create_or_get(CaseSeed(
        batch_id=batch_id,
        resource_code="SEMANTIC-001",
        award_name="默认语义案件",
        year="2026",
        trigger_codes=["COVERAGE_UNKNOWN"],
        objective="验证默认 M5 入口",
    ))
    case_id = state.case_id
    store.close()
    calls: list[tuple[int, tuple[Path, ...]]] = []

    class FakeSemanticRunner:
        def run(self, current_case_id: int) -> HarnessOutcome:
            calls.append((current_case_id, (tmp_path,)))
            runner_store = Store(db_path)
            try:
                current = CaseRepository(runner_store).load(current_case_id)
                current.status = "waiting_human"
                current.recommendation = "语义审核已完成，等待后续比较。"
                CaseRepository(runner_store).save(current)
                return HarnessOutcome(
                    state=current,
                    stopped_reason="semantic_runner_selected",
                )
            finally:
                runner_store.close()

    def fake_factory(_store: Store, roots: list[Path]) -> FakeSemanticRunner:
        assert roots == [tmp_path.resolve()]
        return FakeSemanticRunner()

    monkeypatch.setattr(
        review_workflow,
        "default_semantic_runner_factory",
        fake_factory,
    )
    result = run_review_case(db_path, [tmp_path], case_id)

    assert calls == [(case_id, (tmp_path,))]
    assert result["stopped_reason"] == "semantic_runner_selected"
    assert result["status"] == "waiting_human"


def _prepared_for_stage(tmp_path: Path) -> tuple[Store, PreparedReviewBatch, LedgerEntry]:
    folder = tmp_path / "stage-batch"
    folder.mkdir()
    imported = _imported_file(folder)
    spec = build_template_spec(
        imported.claimed_table_code,
        imported.sheet_name,
        imported.header_codes,
        imported.header_names,
    )
    entry = LedgerEntry(
        resource_code="04059999",
        resource_name="未登记新奖项",
        expected_count=1,
        collect_url="https://official.example/award",
    )
    store = Store(tmp_path / "stage.db")
    bid = store.create_batch(folder.name)
    store.add_staging(bid, [{
        "file": imported.file_name,
        "sheet": imported.sheet_name,
        "row_no": 1,
        "table_code": imported.claimed_table_code,
        "resource_code": imported.first_zylbm,
        "year": imported.year,
        "dedup_key": "K1",
        "data": {code: imported.value(0, code) for code in imported.header_codes},
        "check_status": "pass",
        "issues": [],
    }])
    prepared = PreparedReviewBatch(
        folder=folder,
        batch_id=bid,
        imported_files=(imported,),
        result=BatchResult(batch=folder.name, files=[FileResult(
            file=imported.file_name,
            claimed_table_code=imported.claimed_table_code,
            n_rows=1,
            issues=[],
        )]),
        registry={imported.claimed_table_code: spec},
        ledger={"04059999": entry},
    )
    return store, prepared, entry


def test_run_audit_stage_claims_and_forces_corpus_off(tmp_path: Path) -> None:
    store, prepared, _entry = _prepared_for_stage(tmp_path)
    calls: list[dict[str, object]] = []

    def verify(code, members, urls, spec, llm, workdir, *, use_corpus):  # noqa: ANN001
        calls.append({"code": code, "year": members[0].year, "use_corpus": use_corpus})
        return EvidenceReport(
            resource_code=code,
            award_name=members[0].award_name,
            year=members[0].year,
            verdict="一致",
            confidence="high",
            source_urls=urls,
            submitted_count=1,
            extracted_count=1,
        )

    outcome = run_audit_stage(
        store,
        prepared,
        prober=lambda _url: (200, ""),
        verify=verify,
        llm=object(),
        workdir=tmp_path,
    )
    assert outcome.status == "done" and outcome.bridge.created == 0
    assert calls == [{"code": "04059999", "year": "2026", "use_corpus": False}]
    assert store.get_batch_stage_run(prepared.batch_id, "m4")["status"] == "done"
    item = store.get_stage_item(prepared.batch_id, "04059999", "2026")
    assert item["status"] == "done" and item["current_result_id"] is not None
    store.close()


def test_run_audit_stage_defaults_to_discovery_and_queues_m5(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from award_audit.agent import review_workflow

    store, prepared, _entry = _prepared_for_stage(tmp_path)
    calls: list[object] = []

    def discovery(code, members, urls, spec, llm, workdir, *, use_corpus):  # noqa: ANN001
        calls.append(llm)
        assert code == "04059999" and members and urls
        assert spec is not None and workdir == tmp_path and use_corpus is False
        return EvidenceReport(
            resource_code=code,
            award_name=members[0].award_name,
            year=members[0].year,
            source_urls=urls,
            submitted_count=1,
            reason_codes=["m4_discovery_only"],
        )

    monkeypatch.setattr(review_workflow, "discover_resource", discovery)
    outcome = run_audit_stage(
        store,
        prepared,
        prober=lambda _url: (200, ""),
        workdir=tmp_path,
    )

    assert calls == [None]
    assert outcome.status == "done" and outcome.bridge.created == 1
    cases = store.list_audit_cases(batch_id=prepared.batch_id)
    assert len(cases) == 1
    assert cases[0]["status"] == "queued"
    store.close()


def test_run_audit_stage_retries_m4_after_probe_timeout(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from award_audit.agent import review_workflow

    store, prepared, _entry = _prepared_for_stage(tmp_path)
    calls: list[list[str]] = []

    def discovery(code, members, urls, spec, llm, workdir, *, use_corpus):  # noqa: ANN001
        del code, members, spec, llm, workdir, use_corpus
        calls.append(urls)
        return EvidenceReport(
            resource_code="04059999",
            award_name="timeout retry",
            year="2026",
            source_urls=urls,
            submitted_count=1,
            reason_codes=["m4_discovery_only"],
        )

    monkeypatch.setattr(review_workflow, "discover_resource", discovery)
    outcome = run_audit_stage(
        store,
        prepared,
        prober=lambda _url: (None, "timeout"),
        workdir=tmp_path,
    )

    assert outcome.precheck.passable_targets == []
    assert len(outcome.precheck.retry_targets) == 1
    assert calls == [["https://official.example/award"]]
    assert outcome.status == "done"
    store.close()


def test_run_audit_stage_exception_is_retryable_and_merges_case(tmp_path: Path) -> None:
    store, prepared, _entry = _prepared_for_stage(tmp_path)

    class VerifyFailure(RuntimeError):
        evidence = ["已取得的官方页面标题"]

    def fail_verify(*_args, **_kwargs):  # noqa: ANN002,ANN003,ANN202
        raise VerifyFailure("network failed")

    first = run_audit_stage(
        store,
        prepared,
        prober=lambda _url: (200, ""),
        verify=fail_verify,
        llm=object(),
        workdir=tmp_path,
    )
    assert first.status == "partial"
    item = store.get_stage_item(prepared.batch_id, "04059999", "2026")
    assert item["status"] == "failed" and item["current_result_id"] is not None
    assert len(store.list_audit_cases(batch_id=prepared.batch_id)) == 1
    assert (
        store.list_audit_cases(batch_id=prepared.batch_id)[0]["origin_m4_result_id"]
        == item["current_result_id"]
    )
    saved = store.get_audit_row(int(item["current_result_id"]))
    assert "已取得的官方页面标题" in saved["evidence_json"]

    second = run_audit_stage(
        store,
        prepared,
        prober=lambda _url: (200, ""),
        verify=fail_verify,
        llm=object(),
        workdir=tmp_path,
    )
    assert second.status == "partial"
    assert len(store.list_audit_cases(batch_id=prepared.batch_id)) == 1
    assert len(store.audit_results_of(prepared.batch_id)) == 2
    latest_item = store.get_stage_item(prepared.batch_id, "04059999", "2026")
    assert (
        store.list_audit_cases(batch_id=prepared.batch_id)[0]["origin_m4_result_id"]
        == latest_item["current_result_id"]
    )
    store.close()


def test_run_audit_stage_handoff_persists_result_before_m5_case(tmp_path: Path) -> None:
    store, prepared, _entry = _prepared_for_stage(tmp_path)

    outcome = run_audit_stage(
        store,
        prepared,
        prober=lambda _url: (503, ""),
        llm=object(),
        workdir=tmp_path,
    )

    assert outcome.status == "partial"
    item = store.get_stage_item(prepared.batch_id, "04059999", "2026")
    assert item["status"] == "skipped"
    assert item["current_result_id"] is not None
    saved = store.get_audit_row(int(item["current_result_id"]))
    assert saved is not None
    assert saved["verdict"] == "无法核对"
    cases = store.list_audit_cases(batch_id=prepared.batch_id)
    assert len(cases) == 1
    assert cases[0]["origin_m4_result_id"] == item["current_result_id"]
    store.close()
