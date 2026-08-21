"""M5.5 human-gated Case Memory lifecycle and retrieval tests."""

from __future__ import annotations

from datetime import date

import pytest

from award_audit.agent.harness.models import CaseSeed
from award_audit.agent.harness.persistence import CaseRepository
from award_audit.agent.memory import CaseMemoryService
from award_audit.core.pipeline.store import StateConflictError, Store


def _setup(tmp_path) -> tuple[Store, CaseRepository, CaseMemoryService, int]:  # noqa: ANN001
    store = Store(tmp_path / "memory.db")
    batch_id = store.create_batch("m5.5-memory")
    return store, CaseRepository(store), CaseMemoryService(store), batch_id


def _finalized(
    repository: CaseRepository,
    batch_id: int,
    *,
    trigger: str = "SOFT_RULE_SUSPECT",
    decision: str = "accepted",
    symptom: str = "推荐单位列混入专家姓名",
    resource_code: str = "04050014",
):  # noqa: ANN201
    state, _ = repository.create_or_get(CaseSeed.model_validate({
        "batch_id": batch_id,
        "resource_code": resource_code,
        "award_name": "示例奖",
        "year": "2025",
        "trigger_codes": [trigger],
        "objective": "核验字段语义",
        "submitted_summary": {"resource_type": "JXCG", "field_code": "TJDW"},
        "open_questions": [symptom],
    }))
    state.status = "waiting_human"
    state.recommendation = "已找到可复核证据"
    repository.save(state)
    return repository.finalize(
        state.case_id,
        decision,
        "人工确认该字段应只填写推荐单位",
        "reviewer-a",
        expected_version=state.state_version,
    )


def test_taxonomy_candidate_policy_is_locked(tmp_path) -> None:  # noqa: ANN001
    _store, _repository, service, _batch_id = _setup(tmp_path)
    taxonomy = {item.code: item for item in service.repository.list_taxonomy()}
    assert len(taxonomy) == 8
    assert taxonomy["FIELD_SEMANTICS"].candidate_eligible
    assert not taxonomy["EVIDENCE_CONFLICT"].candidate_eligible
    assert not taxonomy["OTHER"].candidate_eligible


def test_finalization_and_candidate_require_human_gate(tmp_path) -> None:  # noqa: ANN001
    _store, repository, service, batch_id = _setup(tmp_path)
    queued, _ = repository.create_or_get(CaseSeed(
        batch_id=batch_id,
        resource_code="04050014",
        trigger_codes=["SOFT_RULE_SUSPECT"],
        objective="核验字段",
    ))
    assert service.propose_from_case(queued) is None
    with pytest.raises(StateConflictError):
        repository.finalize(
            queued.case_id,
            "accepted",
            "结论",
            "reviewer",
            expected_version=queued.state_version,
        )

    insufficient = _finalized(
        repository,
        batch_id,
        decision="insufficient",
        resource_code="04050015",
    )
    assert service.propose_from_case(insufficient) is None
    conflict = _finalized(
        repository,
        batch_id,
        trigger="EVIDENCE_CONFLICT",
        resource_code="04050016",
    )
    assert service.propose_from_case(conflict) is None


def test_candidate_is_not_retrieved_until_approved_and_can_expire(tmp_path) -> None:  # noqa: ANN001
    _store, repository, service, batch_id = _setup(tmp_path)
    state = _finalized(repository, batch_id)
    candidate = service.propose_from_case(
        state,
        applicable_from="2025-01-01",
        applicable_to="2027-12-31",
    )
    assert candidate is not None and candidate.status == "candidate"
    query, _ = repository.create_or_get(CaseSeed(
        batch_id=batch_id,
        resource_code="04050099",
        trigger_codes=["SOFT_RULE_SUSPECT"],
        objective="推荐单位列出现人名，核验字段语义",
        submitted_summary={"resource_type": "JXCG", "field_code": "TJDW"},
    ))
    assert service.retrieve_for_case(query, on_date=date(2026, 7, 25)) == []
    active = service.repository.transition(
        candidate.memory_id,
        "active",
        "memory-approver",
        expected_version=candidate.state_version,
    )
    hits = service.retrieve_for_case(query, on_date=date(2026, 7, 25))
    assert hits[0].memory_id == active.memory_id
    assert hits[0].warning == "历史案例不是当前事实，必须重新核验证据。"
    assert service.retrieve_for_case(query, on_date=date(2028, 1, 1)) == []


def test_fingerprint_deduplicates_and_tracks_all_source_cases(tmp_path) -> None:  # noqa: ANN001
    _store, repository, service, batch_id = _setup(tmp_path)
    first = _finalized(repository, batch_id, resource_code="04050014")
    memory1 = service.propose_from_case(first, symptom_text="推荐单位列混入专家姓名")
    second = _finalized(repository, batch_id, resource_code="04050015")
    memory2 = service.propose_from_case(second, symptom_text="推荐单位列混入专家姓名")
    assert memory1 is not None and memory2 is not None
    assert memory1.memory_id == memory2.memory_id
    assert memory2.occurrence_count == 2
    assert memory2.source_case_ids == [first.case_id, second.case_id]


def test_memory_transitions_use_optimistic_lock_and_governance(tmp_path) -> None:  # noqa: ANN001
    _store, repository, service, batch_id = _setup(tmp_path)
    state = _finalized(repository, batch_id)
    candidate = service.propose_from_case(state)
    assert candidate is not None
    stale = service.repository.get(candidate.memory_id)
    active = service.repository.transition(
        candidate.memory_id,
        "active",
        "approver",
        expected_version=candidate.state_version,
    )
    with pytest.raises(StateConflictError):
        service.repository.transition(
            stale.memory_id,
            "deprecated",
            "approver",
            expected_version=stale.state_version,
        )
    deprecated = service.repository.transition(
        active.memory_id,
        "deprecated",
        "approver",
        expected_version=active.state_version,
    )
    assert deprecated.status == "deprecated"
    with pytest.raises(ValueError):
        service.repository.transition(
            deprecated.memory_id,
            "active",
            "approver",
            expected_version=deprecated.state_version,
        )


def test_merged_memory_is_excluded_and_top3_is_bounded(tmp_path) -> None:  # noqa: ANN001
    _store, repository, service, batch_id = _setup(tmp_path)
    active_memories = []
    patterns = ["姓名错列", "单位缩写", "多值混填", "角色前缀", "中英混排"]
    for index, pattern in enumerate(patterns):
        state = _finalized(
            repository,
            batch_id,
            symptom=f"推荐单位列异常 {pattern}",
            resource_code=f"040501{index:02d}",
        )
        candidate = service.propose_from_case(
            state, symptom_text=f"推荐单位列异常 {pattern}"
        )
        assert candidate is not None
        active_memories.append(service.repository.transition(
            candidate.memory_id,
            "active",
            "approver",
            expected_version=candidate.state_version,
        ))
    merged = service.repository.transition(
        active_memories[4].memory_id,
        "merged",
        "approver",
        expected_version=active_memories[4].state_version,
        merged_into_id=active_memories[0].memory_id,
    )
    assert merged.status == "merged" and merged.merged_into_id == active_memories[0].memory_id

    query, _ = repository.create_or_get(CaseSeed(
        batch_id=batch_id,
        resource_code="04050999",
        trigger_codes=["SOFT_RULE_SUSPECT"],
        objective="推荐单位列混入专家姓名",
        submitted_summary={"resource_type": "JXCG", "field_code": "TJDW"},
    ))
    hits = service.retrieve_for_case(query)
    assert len(hits) == 3
    assert merged.memory_id not in {item.memory_id for item in hits}
