"""Offline M5.5 vertical: Reflection, human gate, active memory and next-case reuse."""

from __future__ import annotations

from award_audit.agent.harness.client import FakeAgentClient
from award_audit.agent.harness.models import CaseSeed, NextAction
from award_audit.agent.harness.persistence import CaseRepository
from award_audit.agent.harness.runner import EvidenceHarness
from award_audit.agent.memory import CaseMemoryService
from award_audit.agent.toolkit import ToolRegistry, ToolResult
from award_audit.agent.toolkit.testing import register_fake_tool
from award_audit.agent.verification import EvidenceVerifier
from award_audit.core.pipeline.store import Store


def test_reflect_finalize_approve_and_retrieve_in_next_case(tmp_path) -> None:  # noqa: ANN001
    store = Store(tmp_path / "m5-memory-toolchain.db")
    batch_id = store.create_batch("m5.5-toolchain")
    repository = CaseRepository(store)
    memory_service = CaseMemoryService(store)
    first, _ = repository.create_or_get(CaseSeed(
        batch_id=batch_id,
        resource_code="04050014",
        award_name="某竞赛",
        year="2024",
        trigger_codes=["SOURCE_URL_MISSING"],
        objective="定位并核验官网最终名单入口",
        submitted_summary={"resource_type": "JXCG"},
    ))
    registry = ToolRegistry()
    register_fake_tool(registry, "evidence_tool", [ToolResult(ok=True, data={
        "observed_award_name": "某竞赛",
        "observed_year": "2024",
        "source_level": "official_primary",
        "expected_count": 20,
        "extracted_count": 20,
        "total_pages": 2,
        "processed_pages": 2,
        "sequence_complete": True,
    })])
    first_client = FakeAgentClient([
        NextAction(action="finish"),
        NextAction(action="call_tool", tool_name="evidence_tool"),
        NextAction(action="finish", reason_summary="官网名单完整，等待人工核验"),
    ])
    first_outcome = EvidenceHarness(
        repository=repository,
        registry=registry,
        agent_client=first_client,
        allowed_roots=[tmp_path],
        verifier=EvidenceVerifier(),
        memory_service=memory_service,
    ).run(first.case_id)
    assert first_outcome.state.reflection_count == 1
    assert first_outcome.stopped_reason == "recommendation_ready"

    completed = repository.finalize(
        first.case_id,
        "accepted",
        "人工确认最终名单入口位于主管方年度公示栏目附件",
        "reviewer-a",
        expected_version=first_outcome.state.state_version,
    )
    candidate = memory_service.propose_from_case(
        completed,
        symptom_text="官网首页未直接给出名单入口，需要定位年度公示栏目附件",
        resolution="优先检查主管方年度公示栏目及其最终名单附件",
    )
    assert candidate is not None and candidate.status == "candidate"
    active = memory_service.repository.transition(
        candidate.memory_id,
        "active",
        "memory-approver",
        expected_version=candidate.state_version,
    )

    second, _ = repository.create_or_get(CaseSeed(
        batch_id=batch_id,
        resource_code="04050015",
        award_name="另一竞赛",
        year="2025",
        trigger_codes=["SOURCE_URL_MISSING"],
        objective="官网首页没有名单入口，查找年度公示栏目附件",
        submitted_summary={"resource_type": "JXCG"},
    ))
    second_client = FakeAgentClient([
        NextAction(action="manual", reason_summary="仅凭历史路径不能认定当前事实"),
    ])
    second_outcome = EvidenceHarness(
        repository=repository,
        registry=ToolRegistry(),
        agent_client=second_client,
        allowed_roots=[tmp_path],
        memory_service=memory_service,
    ).run(second.case_id)
    assert second_outcome.stopped_reason == "agent_requested_manual"
    memories = second_client.calls[0]["context"]["case"]["retrieved_memories"]
    assert memories[0]["memory_id"] == active.memory_id
    assert "年度公示栏目" in memories[0]["resolution"]
    assert memories[0]["warning"] == "历史案例不是当前事实，必须重新核验证据。"
    assert repository.load(second.case_id).retrieved_memories == memories
