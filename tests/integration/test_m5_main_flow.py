"""Offline M5.7 bridge-to-job-to-evidence integration path."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from pathlib import Path
from typing import cast

from fastapi.testclient import TestClient

from award_audit.agent.harness.client import FakeAgentClient
from award_audit.agent.harness.models import NextAction
from award_audit.agent.harness.persistence import CaseRepository
from award_audit.agent.harness.runner import EvidenceHarness
from award_audit.agent.integration import case_report_rows, ensure_review_cases
from award_audit.agent.toolkit import EvidenceArtifact, ToolRegistry, ToolResult
from award_audit.agent.toolkit.testing import register_fake_tool
from award_audit.core.pipeline.store import Store
from award_audit.web.app import create_app
from award_audit.web.jobs import JobContext, JobRepository
from award_audit.web.models import JobOutcome
from award_audit.web.review_jobs import build_review_batch_handler


def test_bridge_fake_harness_evidence_report_and_api(tmp_path: Path) -> None:
    db_path = tmp_path / "m5-main-flow.db"
    evidence_path = tmp_path / "official.html"
    evidence_path.write_text("official evidence", encoding="utf-8")
    artifact = EvidenceArtifact(
        kind="html",
        source_url="https://example.gov.cn/official",
        local_path=str(evidence_path),
        content_type="text/html",
        sha256="a" * 64,
        size_bytes=evidence_path.stat().st_size,
        fetched_at="2026-07-25T00:00:00Z",
    )
    store = Store(db_path)
    batch_id = store.create_batch("M5.7 main flow")
    report = {
        "resource_code": "04050014",
        "award_name": "示例奖",
        "year": "2026",
        "verdict": "无法核对",
        "confidence": "low",
        "source_kind": "web",
        "source_url": "https://example.gov.cn/official",
        "source_urls": ["https://example.gov.cn/official"],
        "submitted_count": 3,
        "extracted_count": 0,
        "missing": [],
        "extra": [],
        "reason_codes": ["coverage_unknown"],
    }
    result_id = store.add_audit_results(batch_id, [report])[0]
    item = store.claim_stage_item(batch_id, "04050014", "2026", worker="seed-m4")
    assert item is not None
    store.finish_stage_item(
        batch_id,
        "04050014",
        "2026",
        status="failed",
        current_result_id=result_id,
        error_code="TEST_HANDOFF",
        worker="seed-m4",
        expected_version=int(item["state_version"]),
    )
    bridged = ensure_review_cases(
        store,
        batch_id,
        audit_reports=[report],
        require_m4_binding=True,
    )
    assert bridged.created == 1
    m4 = store.claim_batch_stage(batch_id, "m4", worker="seed-m4")
    assert m4 is not None
    store.finish_batch_stage(
        batch_id,
        "m4",
        "done",
        worker="seed-m4",
        expected_version=int(m4["state_version"]),
    )

    def factory(current_store: Store, roots: list[Path]) -> EvidenceHarness:
        registry = ToolRegistry()
        register_fake_tool(
            registry,
            "official_evidence",
            [ToolResult(ok=True, artifacts=[artifact])],
        )
        allowed_roots: list[str | Path] = list(roots)
        return EvidenceHarness(
            repository=CaseRepository(current_store),
            registry=registry,
            agent_client=FakeAgentClient(
                [
                    NextAction(action="call_tool", tool_name="official_evidence"),
                    NextAction(action="finish", reason_summary="证据已就绪，等待人工复核"),
                ]
            ),
            allowed_roots=allowed_roots,
        )

    jobs = JobRepository(store)
    job = jobs.enqueue("review_batch", {}, created_by="reviewer", batch_id=batch_id)
    claimed = jobs.claim_next("m5-worker")
    assert claimed is not None and claimed.job_id == job.job_id
    handler = build_review_batch_handler(
        db_path,
        evidence_roots=[tmp_path],
        harness_factory=factory,
    )
    async def execute() -> JobOutcome:
        pending = handler(JobContext(jobs, claimed, "m5-worker"))
        return await cast(Awaitable[JobOutcome], pending)

    outcome = asyncio.run(execute())
    finished = jobs.finish(job.job_id, "m5-worker", outcome)
    assert finished.status == "waiting_human"

    rows = case_report_rows(store, batch_id)
    assert rows[0]["status"] == "waiting_human"
    assert rows[0]["evidence_sources"] == ["https://example.gov.cn/official"]
    assert rows[0]["evidence_hashes"] == ["a" * 64]
    case_id = int(rows[0]["case_id"])
    store.close()

    app = create_app(
        db_path,
        evidence_roots=[tmp_path],
        job_handlers={},
        start_worker=False,
    )
    with TestClient(app) as client:
        response = client.get(f"/api/audit-cases/{case_id}")
        assert response.status_code == 200
        case = response.json()["case"]
        assert case["status"] == "waiting_human"
        assert case["artifacts"][0]["source_url"] == "https://example.gov.cn/official"
        assert case["artifacts"][0]["sha256"] == "a" * 64
