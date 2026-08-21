from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi.testclient import TestClient

from award_audit.agent.harness.models import CaseSeed, HarnessOutcome
from award_audit.agent.harness.persistence import CaseRepository
from award_audit.core.models.issue import make_issue
from award_audit.core.models.record import ImportedFile
from award_audit.core.pipeline.engine import BatchResult, FileResult
from award_audit.core.pipeline.store import Store
from award_audit.core.reference.ledger import LedgerEntry
from award_audit.core.reference.resource_map import ResourceMapEntry
from award_audit.core.reference.template_registry import build_template_spec
from award_audit.web import review_jobs
from award_audit.web.app import create_app
from award_audit.web.jobs import JobContext, JobRepository
from award_audit.web.review_jobs import (
    build_import_batch_handler,
    build_review_batch_handler,
)


def test_review_handler_isolates_case_failure_and_keeps_human_destination(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "review-jobs.db"
    store = Store(db_path)
    batch_id = store.create_batch("M5.7 handler")
    origins: dict[str, int] = {}
    for code, name in (("04050014", "正常案件"), ("04050015", "异常案件")):
        result_id = store.add_audit_results(batch_id, [{
            "resource_code": code,
            "award_name": name,
            "year": "",
            "verdict": "无法核对",
            "confidence": "low",
            "source_kind": "none",
            "submitted_count": 1,
            "extracted_count": 0,
        }])[0]
        item = store.claim_stage_item(batch_id, code, "", worker="seed-m4")
        assert item is not None
        store.finish_stage_item(
            batch_id,
            code,
            "",
            status="failed",
            current_result_id=result_id,
            error_code="TEST_HANDOFF",
            worker="seed-m4",
            expected_version=int(item["state_version"]),
        )
        origins[code] = result_id
    repository = CaseRepository(store)
    good, _ = repository.create_or_get(CaseSeed(
        batch_id=batch_id,
        resource_code="04050014",
        origin_m4_result_id=origins["04050014"],
        award_name="正常案件",
        trigger_codes=["COVERAGE_UNKNOWN"],
        objective="核验覆盖",
    ))
    bad, _ = repository.create_or_get(CaseSeed(
        batch_id=batch_id,
        resource_code="04050015",
        origin_m4_result_id=origins["04050015"],
        award_name="异常案件",
        trigger_codes=["PDF_ONLY"],
        objective="核验 PDF",
    ))
    m4 = store.claim_batch_stage(batch_id, "m4", worker="seed-m4")
    assert m4 is not None
    store.finish_batch_stage(
        batch_id,
        "m4",
        "done",
        worker="seed-m4",
        expected_version=int(m4["state_version"]),
    )
    calls: list[int] = []

    class FakeHarness:
        def __init__(self, current_store: Store) -> None:
            self.repository = CaseRepository(current_store)

        def run(self, case_id: int) -> HarnessOutcome:
            calls.append(case_id)
            if case_id == bad.case_id:
                raise RuntimeError("fake isolated failure")
            state = self.repository.load(case_id)
            state.status = "waiting_human"
            state.recommendation = "Fake Harness 已完成，等待人工。"
            self.repository.save(state)
            return HarnessOutcome(state=state, stopped_reason="recommendation_ready")

    def factory(current_store: Store, _roots: list[Path]) -> FakeHarness:
        return FakeHarness(current_store)

    jobs = JobRepository(store)
    job = jobs.enqueue("review_batch", {}, created_by="reviewer", batch_id=batch_id)
    claimed = jobs.claim_next("worker-one")
    assert claimed is not None and claimed.job_id == job.job_id
    handler = build_review_batch_handler(
        db_path,
        evidence_roots=[tmp_path],
        harness_factory=factory,  # type: ignore[arg-type]
    )
    context = JobContext(jobs, claimed, "worker-one")
    outcome = asyncio.run(handler(context))
    assert outcome.status == "waiting_human"
    assert calls == [good.case_id, bad.case_id]
    results = outcome.result["cases"]
    assert isinstance(results, list) and len(results) == 2
    assert CaseRepository(store).load(good.case_id).status == "waiting_human"
    failed_case = CaseRepository(store).load(bad.case_id)
    assert failed_case.status == "waiting_human"
    assert failed_case.last_error == "JOB_RUNTIMEERROR"
    assert "运行时错误" in failed_case.recommendation
    assert "fake isolated failure" not in failed_case.recommendation
    assert "job_handler_error" in failed_case.reason_codes


def test_application_default_handlers_are_lazy(tmp_path: Path) -> None:
    app = create_app(
        tmp_path / "lazy-web.db",
        evidence_roots=[tmp_path],
        import_roots=[tmp_path],
        start_worker=False,
    )
    with TestClient(app) as client:
        assert client.get("/api/health").status_code == 200
        assert set(client.app.state.worker.handlers) == {
            "audit_batch", "review_batch", "import_batch",
        }


def test_web_import_does_not_skip_m4_by_precreating_review_case(
    tmp_path: Path,
    monkeypatch,
) -> None:
    folder = tmp_path / "new-business-batch"
    folder.mkdir()
    submission = folder / "CON_GG_XK_RCPY_GXDJSCGR-新奖项-2026.xlsx"
    submission.touch()
    imported = ImportedFile(
        batch=folder.name,
        path=str(submission),
        file_name=submission.name,
        claimed_table_code="CON_GG_XK_RCPY_GXDJSCGR",
        award_name="新奖项",
        year="2026",
        sheet_name="数据",
        header_codes=["ZYLBM", "ZYLB", "XMMC", "XRYXM", "ND"],
        header_names=["资源项码", "资源项", "项目名称", "获奖人", "年度"],
        rows=[["04050014", "新奖项", "年度人物", "张三", "2026"]],
    )
    spec = build_template_spec(
        imported.claimed_table_code,
        imported.sheet_name,
        imported.header_codes,
        imported.header_names,
    )
    resource = ResourceMapEntry(
        resource_code="04050014",
        resource_name="新奖项",
        table_code=imported.claimed_table_code,
    )
    ledger = LedgerEntry(
        resource_code="04050014",
        resource_name="新奖项",
        expected_count=1,
        collect_url="https://official.example/new-award",
    )
    monkeypatch.setattr(
        review_jobs, "import_files", lambda _folder: [imported], raising=False
    )
    monkeypatch.setattr(
        review_jobs,
        "load_template_registry",
        lambda: {imported.claimed_table_code: spec},
        raising=False,
    )
    monkeypatch.setattr(
        review_jobs,
        "load_resource_map",
        lambda: {resource.resource_code: resource},
        raising=False,
    )
    monkeypatch.setattr(
        review_jobs,
        "load_ledger",
        lambda: {ledger.resource_code: ledger},
        raising=False,
    )

    def fake_ingest(folder_path: Path, store: Store, **_kwargs):
        batch_id = store.create_batch(folder_path.name)
        return batch_id, BatchResult(
            batch=folder_path.name,
            files=[FileResult(
                file=imported.file_name,
                claimed_table_code=imported.claimed_table_code,
                n_rows=1,
                issues=[make_issue(
                    "L3-01",
                    batch=folder_path.name,
                    file=imported.file_name,
                    message="本地应采数量与提交数量不一致，必须继续核验来源范围",
                )],
            )],
        )

    monkeypatch.setattr(review_jobs, "ingest_batch", fake_ingest)
    db_path = tmp_path / "web-import.db"
    store = Store(db_path)
    jobs = JobRepository(store)
    job = jobs.enqueue("import_batch", {"folder": str(folder)}, created_by="reviewer")
    claimed = jobs.claim_next("worker")
    assert claimed is not None and claimed.job_id == job.job_id
    handler = build_import_batch_handler(db_path, import_roots=[tmp_path])

    outcome = asyncio.run(handler(JobContext(jobs, claimed, "worker")))

    batch_id = int(outcome.result["batch_id"])
    assert "cases_created" not in outcome.result
    assert store.list_audit_cases(batch_id=batch_id) == []
