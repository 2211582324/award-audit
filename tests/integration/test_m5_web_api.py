"""M5.6 FastAPI P6 vertical and security boundary tests."""

from __future__ import annotations

import time
from io import BytesIO
from pathlib import Path
from urllib.parse import quote

from fastapi.testclient import TestClient
from openpyxl import Workbook

from award_audit.agent import review_workflow
from award_audit.agent.harness.models import CaseSeed
from award_audit.agent.harness.persistence import CaseRepository
from award_audit.agent.memory import CaseMemoryService
from award_audit.agent.toolkit import EvidenceArtifact, ToolObservation
from award_audit.core.models.record import ImportedFile
from award_audit.core.pipeline.ingest import ingest_batch
from award_audit.core.pipeline.store import Store
from award_audit.core.reference.ledger import LedgerEntry
from award_audit.core.reference.resource_map import ResourceMapEntry
from award_audit.core.reference.template_registry import build_template_spec
from award_audit.web import app as web_app
from award_audit.web.app import create_app


def _xlsx_bytes() -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["ZYLBM", "JLMC", "HJDJ", "XM", "NF"])
    sheet.append(["资源项码", "奖励名称", "获奖等级", "姓名", "年份"])
    sheet.append(["04050014", "上传测试奖", "一等奖", "张三", "2026"])
    buffer = BytesIO()
    workbook.save(buffer)
    workbook.close()
    return buffer.getvalue()


def test_browser_upload_creates_managed_batch_without_path_leak(tmp_path: Path) -> None:
    db_path = tmp_path / "upload.db"
    import_root = tmp_path / "managed-imports"
    app = create_app(
        db_path,
        evidence_roots=[tmp_path / "evidence"],
        import_roots=[import_root],
        start_worker=False,
        environment="acceptance",
    )

    with TestClient(app) as client:
        response = client.post(
            "/api/batches/upload",
            headers={"X-Reviewer": "upload-reviewer"},
            files=[(
                "files",
                ("CON_TEST-上传测试奖-2026.xlsx", _xlsx_bytes(),
                 "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
            )],
        )
        health = client.get("/api/health")

    assert response.status_code == 202
    payload = response.json()
    assert payload["upload"]["file_count"] == 1
    assert payload["job"]["input"]["folder"] == payload["upload"]["batch_name"]
    assert str(tmp_path) not in str(payload)
    uploaded = list(import_root.glob("*/CON_TEST-上传测试奖-2026.xlsx"))
    assert len(uploaded) == 1
    assert health.json()["environment"] == "acceptance"
    assert health.json()["database"] == "upload.db"


def test_browser_upload_rejects_non_xlsx_files(tmp_path: Path) -> None:
    app = create_app(
        tmp_path / "upload.db",
        evidence_roots=[tmp_path / "evidence"],
        import_roots=[tmp_path / "managed-imports"],
        start_worker=False,
    )

    with TestClient(app) as client:
        response = client.post(
            "/api/batches/upload",
            headers={"X-Reviewer": "upload-reviewer"},
            files=[("files", ("notes.txt", b"not a workbook", "text/plain"))],
        )

    assert response.status_code == 422
    assert not list((tmp_path / "managed-imports").glob("upload-*"))


def _seed_database(db_path: Path, evidence_root: Path) -> dict[str, int]:
    store = Store(db_path)
    batch_id = store.create_batch("Web 复核样例")
    store.update_batch_counts(batch_id, 1, 1)
    store.add_staging(batch_id, [{
        "file": "A-示例奖-2025.xlsx",
        "sheet": "A",
        "row_no": 3,
        "table_code": "A",
        "resource_code": "04050014",
        "dedup_key": "d1",
        "data": {"ZYLBM": "04050014"},
        "check_status": "warn",
        "issues": [{
            "rule_id": "L5S-01",
            "severity": "review",
            "message": "姓名字段疑似混入机构",
            "field_code": "ZZXM",
            "suggestion": "人工核验",
        }],
    }])
    m4 = store.claim_batch_stage(batch_id, "m4", worker="seed-m4")
    assert m4 is not None
    store.finish_batch_stage(
        batch_id,
        "m4",
        "done",
        worker="seed-m4",
        expected_version=int(m4["state_version"]),
    )
    repository = CaseRepository(store)
    review_case, _ = repository.create_or_get(CaseSeed(
        batch_id=batch_id,
        resource_code="04050014",
        award_name="示例奖",
        year="2025",
        trigger_codes=["EVIDENCE_CONFLICT"],
        objective="核验证据冲突",
    ))
    review_case.status = "waiting_human"
    review_case.recommendation = "两个来源需要人工选择"
    review_case.submitted_summary = {
        "submission_file": str(evidence_root / "private-submission.xlsx"),
        "submitted_rows": 20,
        "reference_rows": 50,
    }
    repository.save(review_case, traces=[ToolObservation(
        call_id="web-source-check",
        tool_name="fetch_web_page",
        started_at="2026-07-25T00:00:00Z",
        finished_at="2026-07-25T00:00:01Z",
        duration_ms=1000,
        input_summary={
            "url": "https://example.gov.cn/award/2025",
            "submitted_path": str(evidence_root / "private-submission.xlsx"),
        },
        output_summary={
            "source_url": "https://example.gov.cn/award/2025",
            "verification_facts": {
                "award_name_match": True,
                "year_match": True,
                "coverage_complete": False,
                "observed_count": 20,
                "expected_count": 50,
            },
        },
        ok=True,
    )])

    evidence_root.mkdir(parents=True, exist_ok=True)
    pdf_path = evidence_root / "evidence.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n%%EOF\n")
    artifact = EvidenceArtifact(
        kind="pdf",
        source_url="https://example.gov.cn/list.pdf",
        local_path=str(pdf_path),
        content_type="application/pdf",
        sha256="a" * 64,
        size_bytes=pdf_path.stat().st_size,
        fetched_at="2026-07-25T00:00:00Z",
    )
    repository.save(review_case, artifacts=[artifact])

    memory_case, _ = repository.create_or_get(CaseSeed(
        batch_id=batch_id,
        resource_code="04050015",
        award_name="示例奖",
        year="2025",
        trigger_codes=["SOFT_RULE_SUSPECT"],
        objective="核验字段语义",
        submitted_summary={"resource_type": "JXCG", "field_code": "ZZXM"},
        open_questions=["姓名字段混入机构"],
    ))
    memory_case.status = "waiting_human"
    repository.save(memory_case)
    completed = repository.finalize(
        memory_case.case_id,
        "accepted",
        "人工确认姓名字段只填写姓名",
        "reviewer-a",
        expected_version=memory_case.state_version,
    )
    candidate = CaseMemoryService(store).propose_from_case(completed)
    assert candidate is not None
    store.close()
    return {
        "batch_id": batch_id,
        "review_case_id": review_case.case_id,
        "review_case_version": review_case.state_version,
        "memory_id": candidate.memory_id,
        "memory_version": candidate.state_version,
    }


def _seed_preview_batch(
    db_path: Path, root: Path, monkeypatch
) -> tuple[int, Path]:  # noqa: ANN001
    folder = root / "preview-source"
    folder.mkdir()
    file_path = folder / "CON_GG_XK_RCPY_GXDJSCGR-预览奖-2026.xlsx"
    codes = ["ZYLBM", "ZYLB", "XMMC", "XRYXM", "ND"]
    names = ["资源项码", "资源项", "项目名称", "获奖人", "年度"]
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "数据"
    sheet.append(codes)
    sheet.append(names)
    sheet.append(["04050014", "预览奖", "年度人物", "张三", "2026"])
    workbook.save(file_path)
    imported = ImportedFile(
        batch=folder.name,
        path=str(file_path),
        file_name=file_path.name,
        claimed_table_code="CON_GG_XK_RCPY_GXDJSCGR",
        award_name="预览奖",
        year="2026",
        sheet_name="数据",
        header_codes=codes,
        header_names=names,
        rows=[["04050014", "预览奖", "年度人物", "张三", "2026"]],
    )
    spec = build_template_spec(
        imported.claimed_table_code, imported.sheet_name, codes, names
    )
    registry = {imported.claimed_table_code: spec}
    resource_map = {"04050014": ResourceMapEntry(
        resource_code="04050014",
        resource_name="预览奖",
        table_code=imported.claimed_table_code,
    )}
    ledger = {"04050014": LedgerEntry(
        resource_code="04050014",
        resource_name="预览奖",
        expected_count=1,
        collect_url="https://official.example/preview-award",
    )}
    store = Store(db_path)
    batch_id, _ = ingest_batch(
        folder,
        store,
        registry=registry,
        resource_map=resource_map,
        ledger=ledger,
        files=[imported],
    )
    store.close()
    monkeypatch.setattr(review_workflow, "load_template_registry", lambda: registry)
    monkeypatch.setattr(review_workflow, "load_ledger", lambda: ledger)
    monkeypatch.setattr(web_app, "load_template_registry", lambda: registry)
    monkeypatch.setattr(web_app, "load_ledger", lambda: ledger)
    return batch_id, file_path


def test_batch_m4_results_expose_only_current_result_and_case_binding(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "m4-results.db"
    store = Store(db_path)
    batch_id = store.create_batch("M4 result visibility")
    batch_stage = store.claim_batch_stage(batch_id, "m4", worker="m4-worker")
    assert batch_stage is not None
    stage_item = store.claim_stage_item(
        batch_id, "04030052", "2025", worker="m4-worker"
    )
    assert stage_item is not None
    old_result_id, current_result_id = store.add_audit_results(batch_id, [
        {
            "resource_code": "04030052",
            "award_name": "Fish challenge",
            "year": "2025",
            "verdict": "old historical result",
            "confidence": "low",
            "source_url": "https://official.example/old",
        },
        {
            "resource_code": "04030052",
            "award_name": "Fish challenge",
            "year": "2025",
            "verdict": "manual review required",
            "confidence": "medium",
            "source_kind": "pdf",
            "source_url": "https://official.example/2025",
            "source_urls": ["https://official.example/2025"],
            "found_assets": ["https://official.example/roster.pdf"],
            "page_year": "2025",
            "extracted_count": 93,
            "submitted_count": 93,
            "missing": ["team-a", "team-b"],
            "extra": ["team-c", "team-d"],
            "reason_codes": ["partial_overlap"],
            "notes": "91 of 93 matched",
        },
    ])
    store.finish_stage_item(
        batch_id,
        "04030052",
        "2025",
        status="done",
        worker="m4-worker",
        expected_version=int(stage_item["state_version"]),
        current_result_id=current_result_id,
    )
    store.finish_batch_stage(
        batch_id,
        "m4",
        "done",
        worker="m4-worker",
        expected_version=int(batch_stage["state_version"]),
    )
    case, _created = CaseRepository(store).create_or_get(CaseSeed(
        batch_id=batch_id,
        origin_m4_result_id=current_result_id,
        resource_code="04030052",
        award_name="Fish challenge",
        year="2025",
        trigger_codes=["COVERAGE_UNKNOWN"],
        objective="Review the two differences",
    ))
    store.close()

    app = create_app(
        db_path=db_path,
        evidence_roots=[tmp_path],
        import_roots=[tmp_path],
        start_worker=False,
    )
    with TestClient(app) as client:
        response = client.get(f"/api/batches/{batch_id}/audit-results")

    assert response.status_code == 200
    payload = response.json()
    assert payload["history_count"] == 2
    assert len(payload["items"]) == 1
    item = payload["items"][0]
    assert item["current_result_id"] == current_result_id
    assert item["current_result_id"] != old_result_id
    assert item["verdict"] == "manual review required"
    assert item["source_urls"] == ["https://official.example/2025"]
    assert item["found_assets"] == ["https://official.example/roster.pdf"]
    assert item["extracted_count"] == 93
    assert item["submitted_count"] == 93
    assert item["binding"] == {
        "case_id": case.case_id,
        "case_status": "queued",
        "origin_m4_result_id": current_result_id,
        "is_current": True,
    }


def test_batch_audit_preview_digest_and_promotion_fail_closed(
    tmp_path: Path, monkeypatch
) -> None:  # noqa: ANN001
    db_path = tmp_path / "preview.db"
    batch_id, source_file = _seed_preview_batch(db_path, tmp_path, monkeypatch)
    app = create_app(
        db_path,
        evidence_roots=[tmp_path],
        import_roots=[tmp_path],
        start_worker=False,
    )
    headers = {"X-Reviewer": "preview-reviewer"}
    with TestClient(app) as client:
        preview_response = client.post(
            f"/api/batches/{batch_id}/audit/preview", headers=headers
        )
        assert preview_response.status_code == 200
        preview = preview_response.json()
        assert preview["probe_status"] == "not_checked"
        assert preview["candidate_targets"][0]["probe_status"] == "not_checked"
        assert "passable_targets" not in preview
        assert len(preview["preview_digest"]) == 64

        stale = client.post(
            f"/api/batches/{batch_id}/audit",
            headers=headers,
            json={"preview_digest": "0" * 64},
        )
        assert stale.status_code == 409
        assert stale.json()["error"] == "STATE_CONFLICT"

        first = client.post(
            f"/api/batches/{batch_id}/audit",
            headers=headers,
            json={"preview_digest": preview["preview_digest"]},
        )
        second = client.post(
            f"/api/batches/{batch_id}/audit",
            headers=headers,
            json={"preview_digest": preview["preview_digest"]},
        )
        assert first.status_code == second.status_code == 202
        assert first.json()["job"]["job_id"] == second.json()["job"]["job_id"]
        assert first.json()["job"]["kind"] == "audit_batch"

        before = client.get(f"/api/batches/{batch_id}").json()["batch"]
        blocked = client.post(
            f"/api/batches/{batch_id}/promote",
            headers=headers,
            json={"expected_status": before["status"]},
        )
        assert blocked.status_code == 409
        assert blocked.json()["error"] == "PROMOTE_BLOCKED"
        after = client.get(f"/api/batches/{batch_id}").json()
        assert after["batch"]["status"] == before["status"]
        assert after["promotion_readiness"]["can_promote"] is False

        source_file.write_bytes(source_file.read_bytes() + b"changed")
        drifted = client.post(
            f"/api/batches/{batch_id}/audit/preview", headers=headers
        )
        assert drifted.status_code == 409
        assert drifted.json()["error"] == "IMPORT_CONTEXT_INVALID"

    verify = Store(db_path)
    assert verify.current_keys() == set()
    job_count = verify.conn.execute(
        "SELECT COUNT(*) FROM audit_job WHERE batch_id=?", (batch_id,)
    ).fetchone()[0]
    assert job_count == 1
    verify.close()


def test_api_views_artifact_human_actions_and_conflicts(tmp_path) -> None:  # noqa: ANN001
    db_path = tmp_path / "web.db"
    evidence_root = tmp_path / "evidence"
    ids = _seed_database(db_path, evidence_root)
    app = create_app(
        db_path,
        evidence_roots=[evidence_root],
        import_roots=[tmp_path],
        start_worker=False,
    )
    with TestClient(app) as client:
        response = client.get("/api/batches")
        assert response.status_code == 200
        assert response.headers["x-content-type-options"] == "nosniff"
        assert "default-src 'self'" in response.headers["content-security-policy"]
        batch = response.json()["batches"][0]
        assert batch["issue_counts"]["review"] == 1
        assert batch["case_counts"]["waiting_human"] == 1
        assert batch["stages"]["m4"]["status"] == "done"
        assert batch["stages"]["m4"]["item_counts"] == {}
        assert batch["stages"]["m5"]["required"] is True
        assert batch["stages"]["m5"]["case_counts"]["waiting_human"] == 1
        assert "import_context" not in batch

        issues = client.get("/api/issues", params={"severity": "review"}).json()["issues"]
        assert issues[0]["field_code"] == "ZZXM"
        detail = client.get(f"/api/audit-cases/{ids['review_case_id']}").json()["case"]
        encoded = str(detail)
        assert "local_path" not in encoded and str(evidence_root) not in encoded
        assert all(
            isinstance(scope["semantic_identity_decisions"], list)
            for scope in detail["scope_comparisons"]
        )
        assert detail["submitted_summary"]["submission_file"] == "private-submission.xlsx"
        assert detail["tool_trace"][0]["input_summary"]["submitted_path"] == (
            "private-submission.xlsx"
        )
        assert detail["tool_trace"][0]["input_summary"]["url"] == (
            "https://example.gov.cn/award/2025"
        )
        assert detail["tool_trace"][0]["output_summary"]["verification_facts"][
            "coverage_complete"
        ] is False
        artifact = detail["artifacts"][0]
        preview = client.get(artifact["preview_url"])
        assert preview.status_code == 200
        assert preview.headers["content-type"].startswith("application/pdf")
        assert preview.headers["content-disposition"].startswith("inline")

        missing_reviewer = client.post(
            f"/api/audit-cases/{ids['review_case_id']}/review",
            json={
                "decision": "accepted",
                "summary": "人工确认",
                "expected_version": detail["state_version"],
            },
        )
        assert missing_reviewer.status_code == 422
        headers = {"X-Reviewer": quote("复核员甲")}
        payload = {
            "decision": "rejected",
            "summary": "人工确认两个来源冲突，打回补充材料",
            "expected_version": detail["state_version"],
        }
        reviewed = client.post(
            f"/api/audit-cases/{ids['review_case_id']}/review",
            json=payload,
            headers=headers,
        )
        assert reviewed.status_code == 200
        reviewed_list = client.get("/api/audit-cases").json()["cases"]
        reviewed_item = next(
            item for item in reviewed_list if item["case_id"] == ids["review_case_id"]
        )
        assert reviewed_item["human_decision"] == "rejected"
        assert reviewed_item["reviewed_by"] == "复核员甲"
        assert reviewed_item["reviewed_at"]
        conflict = client.post(
            f"/api/audit-cases/{ids['review_case_id']}/review",
            json=payload,
            headers=headers,
        )
        assert conflict.status_code == 409
        assert conflict.json()["error"] == "STATE_CONFLICT"

        approved = client.post(
            f"/api/memories/{ids['memory_id']}/approve",
            json={"expected_version": ids["memory_version"]},
            headers=headers,
        )
        assert approved.status_code == 200
        repeated = client.post(
            f"/api/memories/{ids['memory_id']}/approve",
            json={"expected_version": ids["memory_version"]},
            headers=headers,
        )
        assert repeated.status_code in {409, 422}
        assert client.get("/api/memories", params={"status": "active"}).json()[
            "memories"
        ][0]["approved_by"] == "复核员甲"

        event_text = client.get("/api/events", params={"once": "true"}).text
        assert "human.action" in event_text

    reopened = create_app(
        db_path,
        evidence_roots=[evidence_root],
        start_worker=False,
    )
    with TestClient(reopened) as client:
        assert client.get(f"/api/audit-cases/{ids['review_case_id']}").json()["case"][
            "status"
        ] == "completed"


def test_p6_job_progress_sse_artifact_and_browser_reopen(tmp_path) -> None:  # noqa: ANN001
    db_path = tmp_path / "p6.db"
    evidence_root = tmp_path / "evidence"
    ids = _seed_database(db_path, evidence_root)

    async def fake_review(context):  # noqa: ANN001, ANN202
        await context.progress(20, "Fake Agent 开始")
        await context.progress(80, "证据已持久化")
        return {"case_id": ids["review_case_id"], "artifact_ready": True}

    app = create_app(
        db_path,
        evidence_roots=[evidence_root],
        job_handlers={"review_batch": fake_review},
        start_worker=True,
    )
    with TestClient(app) as client:
        queued = client.post(
            f"/api/batches/{ids['batch_id']}/review",
            headers={"X-Reviewer": quote("任务发起人")},
        )
        assert queued.status_code == 202
        job_id = queued.json()["job"]["job_id"]
        for _ in range(100):
            job = client.get(f"/api/jobs/{job_id}").json()["job"]
            if job["status"] == "completed":
                break
            time.sleep(0.02)
        assert job["status"] == "completed"
        assert job["result"]["artifact_ready"] is True
        events = client.get("/api/events", params={"once": "true"}).text
        assert "job.progress" in events and "job.completed" in events

    reopened = create_app(
        db_path,
        evidence_roots=[evidence_root],
        start_worker=False,
    )
    with TestClient(reopened) as client:
        persisted = client.get(f"/api/jobs/{job_id}").json()["job"]
        assert persisted["status"] == "completed"
        case = client.get(f"/api/audit-cases/{ids['review_case_id']}").json()["case"]
        assert case["artifacts"][0]["sha256"] == "a" * 64


def test_supplement_reopens_m5_and_allows_review_job(tmp_path) -> None:  # noqa: ANN001
    db_path = tmp_path / "supplement-rerun.db"
    evidence_root = tmp_path / "evidence"
    ids = _seed_database(db_path, evidence_root)
    store = Store(db_path)
    claimed = store.claim_batch_stage(ids["batch_id"], "m5", worker="initial-m5")
    assert claimed is not None
    store.finish_batch_stage(
        ids["batch_id"],
        "m5",
        "done",
        worker="initial-m5",
        expected_version=int(claimed["state_version"]),
    )
    store.close()
    app = create_app(db_path, evidence_roots=[evidence_root], start_worker=False)

    with TestClient(app) as client:
        supplemented = client.post(
            f"/api/audit-cases/{ids['review_case_id']}/supplement",
            headers={"X-Reviewer": "reviewer"},
            json={
                "request": "按最新名单差异规则重新核验",
                "expected_version": ids["review_case_version"],
            },
        )
        assert supplemented.status_code == 200
        batch = client.get("/api/batches").json()["batches"][0]
        assert batch["case_counts"]["queued"] == 1
        assert batch["stages"]["m5"]["status"] == "pending"

        started = client.post(
            f"/api/batches/{ids['batch_id']}/review",
            headers={"X-Reviewer": "reviewer"},
        )
        assert started.status_code == 202
        assert started.json()["job"]["kind"] == "review_batch"


def test_artifact_outside_root_is_denied_without_path_leak(tmp_path) -> None:  # noqa: ANN001
    db_path = tmp_path / "outside.db"
    evidence_root = tmp_path / "allowed"
    ids = _seed_database(db_path, evidence_root)
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(b"%PDF-1.4\n%%EOF")
    store = Store(db_path)
    store.conn.execute(
        "UPDATE evidence_artifact SET local_path=? WHERE case_id=?",
        (str(outside), ids["review_case_id"]),
    )
    store.conn.commit()
    store.close()
    app = create_app(db_path, evidence_roots=[evidence_root], start_worker=False)
    with TestClient(app) as client:
        detail = client.get(f"/api/audit-cases/{ids['review_case_id']}").json()["case"]
        response = client.get(detail["artifacts"][0]["preview_url"])
        assert response.status_code == 403
        assert str(outside) not in response.text


def test_job_cancel_requires_reviewer_and_optimistic_version(tmp_path) -> None:  # noqa: ANN001
    db_path = tmp_path / "cancel-api.db"
    evidence_root = tmp_path / "evidence"
    ids = _seed_database(db_path, evidence_root)
    app = create_app(db_path, evidence_roots=[evidence_root], start_worker=False)
    with TestClient(app) as client:
        queued = client.post(
            f"/api/batches/{ids['batch_id']}/review",
            headers={"X-Reviewer": "creator"},
        ).json()["job"]
        missing = client.post(
            f"/api/jobs/{queued['job_id']}/cancel",
            json={"expected_version": queued["state_version"]},
        )
        assert missing.status_code == 422
        cancelled = client.post(
            f"/api/jobs/{queued['job_id']}/cancel",
            json={"expected_version": queued["state_version"]},
            headers={"X-Reviewer": "reviewer"},
        )
        assert cancelled.status_code == 200
        assert cancelled.json()["job"]["status"] == "cancelled"
        stale = client.post(
            f"/api/jobs/{queued['job_id']}/cancel",
            json={"expected_version": queued["state_version"]},
            headers={"X-Reviewer": "reviewer"},
        )
        assert stale.status_code == 409
