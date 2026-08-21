"""M5.5 deterministic/model Verifier and one-Reflection Harness tests."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from award_audit.agent.harness.client import FakeAgentClient
from award_audit.agent.harness.models import CaseSeed, M4EvidenceBundle, NextAction
from award_audit.agent.harness.persistence import CaseRepository
from award_audit.agent.harness.runner import EvidenceHarness
from award_audit.agent.toolkit import ToolBudgetLimits, ToolRegistry, ToolResult
from award_audit.agent.toolkit.testing import register_fake_tool
from award_audit.agent.verification import (
    EvidenceSnapshot,
    EvidenceVerifier,
    FakeVerifierClient,
    StructuredVerifierClient,
    VerificationReport,
    VerifierCallUsage,
    VerifierError,
    build_evidence_snapshot,
    deterministic_verify,
)
from award_audit.core.pipeline.store import Store


def _complete_snapshot() -> EvidenceSnapshot:
    return EvidenceSnapshot(
        expected_award_name="某竞赛",
        expected_year="2024",
        observed_award_names=["某竞赛"],
        observed_years=["2024"],
        source_levels=["official_primary"],
        expected_count=10,
        observed_count=10,
        total_pages=2,
        processed_pages=2,
        sequence_complete=True,
    )


def _report(action: str, **changes: Any) -> VerificationReport:
    payload: dict[str, Any] = {
        "target_match": "yes",
        "year_match": "yes",
        "source_authority": "official",
        "coverage_complete": "yes",
        "contradictions": [],
        "missing_evidence": [],
        "recommended_action": action,
        "reason_codes": [],
        "deterministic_action": action,
        "model_used": True,
    }
    payload.update(changes)
    return VerificationReport.model_validate(payload)


def _case(tmp_path) -> tuple[CaseRepository, int]:  # noqa: ANN001
    store = Store(tmp_path / "verify.db")
    batch_id = store.create_batch("m5.5-verify")
    repository = CaseRepository(store)
    state, _ = repository.create_or_get(CaseSeed(
        batch_id=batch_id,
        resource_code="04050014",
        award_name="某竞赛",
        year="2024",
        trigger_codes=["SOURCE_URL_MISSING"],
        objective="核验官方完整名单",
    ))
    return repository, state.case_id


def test_verification_report_forbids_approval_and_extra_fields() -> None:
    with pytest.raises(ValidationError):
        VerificationReport.model_validate({
            **_report("accept_evidence").model_dump(),
            "recommended_action": "approve_ingestion",
        })


def test_verifier_snapshot_reuses_bound_m4_evidence(tmp_path) -> None:  # noqa: ANN001
    repository, case_id = _case(tmp_path)
    state = repository.load(case_id)
    state.m4_evidence = M4EvidenceBundle(
        result_id=11,
        resource_code=state.resource_code,
        award_name=state.award_name,
        year=state.year,
        page_year="2024",
        verdict="疑似缺漏",
        confidence="medium",
        submitted_count=10,
        extracted_count=8,
        missing=["名单甲", "名单乙"],
        source_urls=["https://official.example/list"],
    )

    snapshot = build_evidence_snapshot(state, [])

    assert snapshot.expected_count == 10
    assert snapshot.observed_count == 8
    assert snapshot.observed_years == ["2024"]
    assert snapshot.missing_evidence == ["名单甲", "名单乙"]
    with pytest.raises(ValidationError):
        VerificationReport.model_validate({
            **_report("manual").model_dump(),
            "hidden_reasoning": "not allowed",
        })


@pytest.mark.parametrize(
    ("snapshot", "action", "reason"),
    [
        (_complete_snapshot(), "accept_evidence", ""),
        (
            _complete_snapshot().model_copy(update={"observed_years": ["2023"]}),
            "manual",
            "year_mismatch",
        ),
        (
            _complete_snapshot().model_copy(update={"source_levels": []}),
            "supplement",
            "source_authority_unknown",
        ),
        (
            _complete_snapshot().model_copy(update={"sequence_complete": False}),
            "supplement",
            "coverage_incomplete",
        ),
        (
            _complete_snapshot().model_copy(update={"contradictions": ["两个官网结果冲突"]}),
            "manual",
            "evidence_conflict",
        ),
    ],
)
def test_deterministic_verification_matrix(
    snapshot: EvidenceSnapshot, action: str, reason: str
) -> None:
    report = deterministic_verify(snapshot)
    assert report.recommended_action == action
    if reason:
        assert reason in report.reason_codes


def test_model_can_only_keep_or_lower_deterministic_result() -> None:
    incomplete = _complete_snapshot().model_copy(update={"source_levels": []})
    optimistic = _report("accept_evidence")
    merged = EvidenceVerifier(FakeVerifierClient([optimistic])).verify(incomplete)
    assert merged.recommended_action == "supplement"
    assert merged.source_authority == "unknown" and merged.model_used

    pessimistic = _report(
        "manual",
        contradictions=["模型识别到发布状态冲突"],
        reason_codes=["model_source_conflict"],
    )
    downgraded = EvidenceVerifier(FakeVerifierClient([pessimistic])).verify(
        _complete_snapshot()
    )
    assert downgraded.recommended_action == "manual"
    assert "model_source_conflict" in downgraded.reason_codes


def test_verified_media_is_secondary_evidence_but_search_leads_are_excluded(tmp_path) -> None:  # noqa: ANN001
    secondary = _complete_snapshot().model_copy(
        update={"source_levels": ["publisher_secondary"]}
    )
    report = deterministic_verify(secondary)
    assert report.source_authority == "secondary"
    assert report.recommended_action == "accept_evidence"
    assert "secondary_source_only" in report.reason_codes
    aggregator = deterministic_verify(secondary.model_copy(
        update={"source_levels": ["media_or_aggregator"]}
    ))
    assert aggregator.source_authority == "unknown"
    assert aggregator.recommended_action == "supplement"

    repository, case_id = _case(tmp_path)
    state = repository.load(case_id)
    snapshot = build_evidence_snapshot(state, [
        ToolResult(
            ok=True,
            data={"candidates": [{"source_level": "official_primary"}]},
            warnings=["search_results_are_leads_not_evidence"],
        ),
        ToolResult(
            ok=True,
            data={
                "observed_award_name": "某竞赛",
                "observed_year": "2024",
                "source_level": "publisher_secondary",
                "expected_count": 10,
                "observed_count": 10,
                "coverage_complete": True,
            },
            source_url="https://news.eol.cn/example",
        ),
    ])
    assert snapshot.source_levels == ["publisher_secondary"]


class _VerifierLlm:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.calls = 0

    def json_call(self, _system: str, _user: str, *, max_tokens: int) -> dict[str, Any]:
        assert max_tokens == 1200
        self.calls += 1
        return self.payload


class _VerifierEndpoint:
    def __init__(self, response: Any) -> None:
        self.response = response
        self.kwargs: dict[str, Any] = {}

    def create(self, **kwargs: Any) -> Any:
        self.kwargs = kwargs
        return self.response


class _NativeVerifierLlm:
    provider = "openai"
    model = "fake-model"

    def __init__(self, endpoint: _VerifierEndpoint) -> None:
        self.endpoint = endpoint

    def _sdk(self) -> Any:
        return SimpleNamespace(chat=SimpleNamespace(completions=self.endpoint))


def test_native_verifier_requires_one_report_function_and_records_usage(
    monkeypatch,  # noqa: ANN001
) -> None:
    import award_audit.agent.llm as llm_module

    monkeypatch.setattr(llm_module, "_max_retries", lambda: 1)
    call = SimpleNamespace(function=SimpleNamespace(
        name="submit_verification_report",
        arguments=json.dumps(_report("manual").model_dump(mode="json")),
    ))
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(tool_calls=[call]))],
        usage=SimpleNamespace(
            total_tokens=31,
            prompt_tokens=25,
            completion_tokens=6,
            prompt_tokens_details=SimpleNamespace(cached_tokens=16),
        ),
    )
    endpoint = _VerifierEndpoint(response)
    client = StructuredVerifierClient(lambda: _NativeVerifierLlm(endpoint))
    report = client.verify(_complete_snapshot(), deterministic_verify(_complete_snapshot()))
    assert report.recommended_action == "manual"
    assert endpoint.kwargs["tool_choice"] == "required"
    assert endpoint.kwargs["tools"][0]["function"]["name"] == (
        "submit_verification_report"
    )
    assert client.last_usage is not None
    assert client.last_usage.total_tokens == 31
    assert client.last_usage.cached_input_tokens == 16


def test_native_verifier_invalid_function_count_keeps_safe_failure_usage(
    monkeypatch,  # noqa: ANN001
) -> None:
    import award_audit.agent.llm as llm_module

    monkeypatch.setattr(llm_module, "_max_retries", lambda: 1)
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(tool_calls=[]))],
        usage=SimpleNamespace(total_tokens=19, prompt_tokens=17, completion_tokens=2),
    )
    client = StructuredVerifierClient(
        lambda: _NativeVerifierLlm(_VerifierEndpoint(response))
    )
    with pytest.raises(VerifierError) as caught:
        client.verify(_complete_snapshot(), deterministic_verify(_complete_snapshot()))
    assert caught.value.safe_detail == "verifier_native_function_count_invalid"
    assert caught.value.usage is not None
    assert caught.value.usage.total_tokens == 19
    assert caught.value.usage.outcome == "failed"


def test_structured_verifier_is_lazy_and_rejects_bad_output() -> None:
    created = 0
    llm = _VerifierLlm({"recommended_action": "approve_ingestion"})

    def factory() -> _VerifierLlm:
        nonlocal created
        created += 1
        return llm

    client = StructuredVerifierClient(factory)
    assert created == 0
    with pytest.raises(VerifierError):
        client.verify(_complete_snapshot(), deterministic_verify(_complete_snapshot()))
    assert created == 1 and llm.calls == 1


def test_snapshot_uses_evidence_facts_not_submitted_identity(tmp_path) -> None:  # noqa: ANN001
    repository, case_id = _case(tmp_path)
    state = repository.load(case_id)
    state.submitted_summary = {"award_name": "某竞赛", "submitted_count": 10}
    snapshot = build_evidence_snapshot(state, [ToolResult(ok=True, data={
        "observed_award_name": "另一竞赛",
        "observed_year": "2024",
        "source_level": "official_primary",
        "extracted_count": 10,
        "coverage_complete": True,
    })])
    assert snapshot.observed_award_names == ["另一竞赛"]
    assert snapshot.expected_count == 10 and snapshot.observed_count == 10
    assert deterministic_verify(snapshot).recommended_action == "manual"


def test_snapshot_prefers_complete_attachment_coverage_over_partial_page(
    tmp_path,
) -> None:  # noqa: ANN001
    repository, case_id = _case(tmp_path)
    state = repository.load(case_id)
    snapshot = build_evidence_snapshot(state, [
        ToolResult(ok=True, data={
            "expected_count": 307,
            "observed_count": 13,
            "coverage_complete": False,
        }),
        ToolResult(ok=True, data={
            "expected_count": 307,
            "observed_count": 307,
            "coverage_complete": True,
        }),
    ])

    assert snapshot.expected_count == 307
    assert snapshot.observed_count == 307
    assert snapshot.explicit_coverage_complete is True
    report = deterministic_verify(snapshot)
    assert report.coverage_complete == "yes"
    assert "coverage_incomplete" not in report.reason_codes


def test_harness_allows_exactly_one_reflection_and_persists_reports(tmp_path) -> None:  # noqa: ANN001
    repository, case_id = _case(tmp_path)
    registry = ToolRegistry()
    register_fake_tool(registry, "evidence_tool", [ToolResult(ok=True, data={
        "observed_award_name": "某竞赛",
        "observed_year": "2024",
        "source_level": "official_primary",
        "expected_count": 10,
        "extracted_count": 10,
        "total_pages": 2,
        "processed_pages": 2,
        "sequence_complete": True,
    })])
    client = FakeAgentClient([
        NextAction(action="finish", reason_summary="先验证"),
        NextAction(action="call_tool", tool_name="evidence_tool"),
        NextAction(action="finish", reason_summary="补证完成"),
    ])
    outcome = EvidenceHarness(
        repository=repository,
        registry=registry,
        agent_client=client,
        allowed_roots=[tmp_path],
        verifier=EvidenceVerifier(),
    ).run(case_id)
    assert outcome.stopped_reason == "recommendation_ready"
    assert outcome.state.reflection_count == 1
    assert outcome.state.latest_verification.recommended_action == "accept_evidence"
    assert any("Verifier 补证项" in item for item in outcome.state.open_questions)
    count = repository.store.conn.execute(
        "SELECT COUNT(*) FROM verification_report WHERE case_id=?", (case_id,)
    ).fetchone()[0]
    assert count == 2
    restored = repository.load(case_id)
    assert restored.latest_verification.model_used is False


def test_agent_manual_with_tool_results_produces_a_forced_manual_report(tmp_path) -> None:  # noqa: ANN001
    repository, case_id = _case(tmp_path)
    registry = ToolRegistry()
    register_fake_tool(
        registry,
        "search_official_award",
        [ToolResult(ok=True, data={"candidate_count": 1, "official_candidate_count": 1})],
    )
    optimistic = _report("accept_evidence")

    outcome = EvidenceHarness(
        repository=repository,
        registry=registry,
        agent_client=FakeAgentClient([
            NextAction(
                action="call_tool",
                tool_name="search_official_award",
                arguments={"award_name": "某竞赛"},
            ),
            NextAction(action="manual", reason_summary="候选不可访问，需人工补证"),
        ]),
        allowed_roots=[tmp_path],
        verifier=EvidenceVerifier(FakeVerifierClient([optimistic])),
    ).run(case_id)

    assert outcome.stopped_reason == "agent_requested_manual"
    assert outcome.state.latest_verification.recommended_action == "manual"
    assert "agent_requested_manual" in outcome.state.latest_verification.reason_codes
    count = repository.store.conn.execute(
        "SELECT COUNT(*) FROM verification_report WHERE case_id=?", (case_id,)
    ).fetchone()[0]
    assert count == 1
    assert repository.store.list_audit_attempts(case_id)[-1]["verifier_status"] == "persisted"


def test_repeated_failed_url_is_blocked_and_formally_verified(tmp_path) -> None:  # noqa: ANN001
    repository, case_id = _case(tmp_path)
    failed_url = "https://example.gov.cn/stale.pdf"
    registry = ToolRegistry()
    downloaded = register_fake_tool(
        registry,
        "download_evidence",
        [ToolResult.failure("HTTP_ERROR", "HTTP 404")],
    )
    fetched = register_fake_tool(
        registry,
        "fetch_web_page",
        [ToolResult(ok=True, data={"coverage_complete": True})],
    )

    outcome = EvidenceHarness(
        repository=repository,
        registry=registry,
        agent_client=FakeAgentClient([
            NextAction(
                action="call_tool",
                tool_name="download_evidence",
                arguments={"url": failed_url},
            ),
            NextAction(
                action="call_tool",
                tool_name="fetch_web_page",
                arguments={"url": failed_url},
            ),
        ]),
        allowed_roots=[tmp_path],
        verifier=EvidenceVerifier(FakeVerifierClient([_report("accept_evidence")])),
    ).run(case_id)

    assert outcome.stopped_reason == "agent_requested_manual"
    assert len(downloaded.calls) == 1 and fetched.calls == []
    assert len(outcome.state.tool_trace) == 1
    assert "repeated_failed_url_blocked" in outcome.state.reason_codes
    assert outcome.state.latest_verification.recommended_action == "manual"
    assert repository.store.list_audit_attempts(case_id)[-1]["verifier_status"] == "persisted"


def test_repeated_exact_tool_call_is_blocked_and_formally_verified(tmp_path) -> None:  # noqa: ANN001
    repository, case_id = _case(tmp_path)
    registry = ToolRegistry()
    parsed = register_fake_tool(
        registry,
        "parse_spreadsheet",
        [ToolResult(ok=True, data={"observed_count": 10})],
    )
    action = NextAction(
        action="call_tool",
        tool_name="parse_spreadsheet",
        arguments={"path": str(tmp_path / "submitted.xlsx")},
    )

    outcome = EvidenceHarness(
        repository=repository,
        registry=registry,
        agent_client=FakeAgentClient([action, action]),
        allowed_roots=[tmp_path],
        verifier=EvidenceVerifier(FakeVerifierClient([_report("accept_evidence")])),
    ).run(case_id)

    assert outcome.stopped_reason == "repeated_tool_call_blocked"
    assert len(parsed.calls) == 1 and len(outcome.state.tool_trace) == 1
    assert "repeated_tool_call_blocked" in outcome.state.reason_codes
    assert "repeated_tool_call_blocked" in outcome.state.latest_verification.reason_codes
    assert repository.store.list_audit_attempts(case_id)[-1]["verifier_status"] == "persisted"


def test_tool_call_budget_stop_with_results_is_formally_verified(tmp_path) -> None:  # noqa: ANN001
    repository, case_id = _case(tmp_path)
    state = repository.load(case_id)
    state.budget.limits = ToolBudgetLimits(max_calls=1)
    repository.save(state)
    registry = ToolRegistry()
    register_fake_tool(registry, "evidence_tool", [ToolResult(ok=True)])

    outcome = EvidenceHarness(
        repository=repository,
        registry=registry,
        agent_client=FakeAgentClient([
            NextAction(action="call_tool", tool_name="evidence_tool"),
        ]),
        allowed_roots=[tmp_path],
        verifier=EvidenceVerifier(FakeVerifierClient([_report("accept_evidence")])),
    ).run(case_id)

    assert outcome.stopped_reason == "tool_call_budget_exhausted"
    assert "tool_call_budget_exhausted" in outcome.state.reason_codes
    assert "tool_call_budget_exhausted" in outcome.state.latest_verification.reason_codes
    assert repository.store.conn.execute(
        "SELECT COUNT(*) FROM verification_report WHERE case_id=?", (case_id,)
    ).fetchone()[0] == 1


def test_consecutive_tool_failures_are_formally_verified(tmp_path) -> None:  # noqa: ANN001
    repository, case_id = _case(tmp_path)
    registry = ToolRegistry()
    failed = register_fake_tool(
        registry,
        "failing_tool",
        [
            ToolResult.failure("HTTP_ERROR", "first"),
            ToolResult.failure("HTTP_ERROR", "second"),
        ],
    )

    outcome = EvidenceHarness(
        repository=repository,
        registry=registry,
        agent_client=FakeAgentClient([
            NextAction(
                action="call_tool", tool_name="failing_tool", arguments={"attempt": 1}
            ),
            NextAction(
                action="call_tool", tool_name="failing_tool", arguments={"attempt": 2}
            ),
        ]),
        allowed_roots=[tmp_path],
        verifier=EvidenceVerifier(FakeVerifierClient([_report("accept_evidence")])),
    ).run(case_id)

    assert outcome.stopped_reason == "consecutive_tool_failures"
    assert len(failed.calls) == 2 and outcome.state.last_error == "HTTP_ERROR"
    assert "consecutive_tool_failures" in outcome.state.reason_codes
    assert "consecutive_tool_failures" in outcome.state.latest_verification.reason_codes
    assert repository.store.list_audit_attempts(case_id)[-1]["verifier_status"] == "persisted"


def test_second_supplement_and_verifier_failure_stop_for_human(tmp_path) -> None:  # noqa: ANN001
    repository, case_id = _case(tmp_path)
    exhausted = EvidenceHarness(
        repository=repository,
        registry=ToolRegistry(),
        agent_client=FakeAgentClient([
            NextAction(action="finish"),
            NextAction(action="finish"),
        ]),
        allowed_roots=[tmp_path],
        verifier=EvidenceVerifier(),
    ).run(case_id)
    assert exhausted.stopped_reason == "reflection_exhausted"
    assert exhausted.state.status == "waiting_human" and exhausted.state.reflection_count == 1

    repository.request_supplement(
        case_id, "重跑验证器", expected_version=exhausted.state.state_version
    )
    failed = EvidenceHarness(
        repository=repository,
        registry=ToolRegistry(),
        agent_client=FakeAgentClient([NextAction(action="finish")]),
        allowed_roots=[tmp_path],
        verifier=EvidenceVerifier(FakeVerifierClient([VerifierError(
            "offline",
            safe_detail="verifier_native_request_failed",
            usage=VerifierCallUsage(
                route="native",
                outcome="failed",
                provider_usage_reported=True,
                total_tokens=23,
            ),
        )])),
    ).run(case_id)
    assert failed.stopped_reason == "verifier_error"
    assert failed.state.status == "waiting_human"
    assert failed.state.last_error_detail == "verifier_native_request_failed"
    assert failed.state.verifier_llm_usage[0].total_tokens == 23
