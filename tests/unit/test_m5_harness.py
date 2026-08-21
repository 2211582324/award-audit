"""M5.4 contracts, clients, case persistence and Harness stop tests."""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from award_audit.agent.harness.client import (
    AgentClientError,
    AgentOutputError,
    FakeAgentClient,
    FallbackAgentClient,
    NativeToolCallingUnavailable,
    OpenAINativeAgentClient,
    StructuredActionClient,
)
from award_audit.agent.harness.models import (
    AgentDecision,
    AgentTurnContext,
    CaseSeed,
    EvidenceCandidate,
    HarnessLimits,
    LlmTurnUsage,
    M4EvidenceBundle,
    NextAction,
)
from award_audit.agent.harness.persistence import CaseRepository
from award_audit.agent.harness.runner import (
    EvidenceHarness,
    _annotate_m5_artifact,
    _calibrate_media_wall_time,
    _collect_arguments,
    _filter_unacquired_asset_urls,
    _hydrate_m4_evidence_progress,
    _image_roster_arguments,
    _next_unattempted_known_url,
    _queue_search_candidates,
    _route_image_result_to_scopes,
    _route_pdf_result_to_scopes,
    _route_web_result_to_scopes,
    _tool_schemas_for_state,
    _turn_context,
    _update_attachment_queue_after_collection,
    _update_media_queue_after_verification,
    build_default_harness,
)
from award_audit.agent.harness.seeds import (
    seed_from_search_handoff,
    seed_from_soft_rule,
    seeds_from_file_issues,
)
from award_audit.agent.toolkit import (
    EvidenceArtifact,
    EvidenceAssetRecord,
    EvidenceFact,
    ToolBudgetLimits,
    ToolRegistry,
    ToolResult,
    build_default_registry,
)
from award_audit.agent.toolkit.testing import register_fake_tool
from award_audit.agent.verification import EvidenceVerifier, VerifierCallUsage
from award_audit.core.models.issue import make_issue
from award_audit.core.models.record import ImportedFile
from award_audit.core.pipeline.checks.l5_precheck import SearchHandoff
from award_audit.core.pipeline.store import StateConflictError, Store


def _seed(batch_id: int) -> CaseSeed:
    return CaseSeed(
        batch_id=batch_id,
        resource_code="04050014",
        award_name="示例奖",
        year="2025",
        trigger_codes=["SOURCE_URL_MISSING"],
        objective="查找并核验官方获奖名单",
    )


def test_page_revisit_does_not_requeue_acquired_attachments() -> None:
    from award_audit.agent.harness.models import AuditCaseState
    from award_audit.agent.toolkit.contracts import ToolBudgetState

    existing = "https://official.example.cn/list-1.pdf"
    new = "https://official.example.cn/list-2.pdf"
    state = AuditCaseState.from_seed(CaseSeed(
        batch_id=1,
        resource_code="06020007",
        award_name="中国专利奖",
        year="2025",
        trigger_codes=["COVERAGE_UNKNOWN"],
        objective="核验多个名单附件",
    ), ToolBudgetState())
    state.artifacts.append(EvidenceArtifact(
        kind="pdf",
        source_url=existing,
        local_path="list-1.pdf",
        content_type="application/pdf",
        sha256="1" * 64,
        size_bytes=100,
        fetched_at="2026-08-04T00:00:00Z",
    ))

    assert _filter_unacquired_asset_urls(state, [existing, new, existing]) == [new]


def test_candidate_page_revisit_does_not_recollect_acquired_attachment(
    tmp_path,
) -> None:  # noqa: ANN001
    _store, repository, batch_id = _repository(tmp_path)
    page_url = "https://official.example.cn/award-page"
    attachment_url = "https://official.example.cn/list.pdf"
    artifact_path = tmp_path / "list.pdf"
    artifact_path.write_bytes(b"offline pdf")
    state, _created = repository.create_or_get(_seed(batch_id).model_copy(update={
        "trigger_codes": ["COVERAGE_UNKNOWN"],
    }))
    artifact = EvidenceArtifact(
        kind="pdf",
        source_url=attachment_url,
        local_path=str(artifact_path),
        content_type="application/pdf",
        sha256="2" * 64,
        size_bytes=artifact_path.stat().st_size,
        fetched_at="2026-08-04T00:00:00Z",
    )
    state.artifacts.append(artifact)
    state.evidence_progress.candidates = [EvidenceCandidate(
        url=page_url,
        source_level="official_secondary",
        status="pending",
    )]
    state.evidence_progress.phase = "candidate_recovery"
    repository.save(state, artifacts=[artifact])

    registry = ToolRegistry()
    fetched = register_fake_tool(registry, "fetch_web_page", [ToolResult(
        ok=True,
        source_url=page_url,
        data={
            "next_evidence_stage": "spreadsheet_processing",
            "candidate_attachment_urls": [attachment_url],
        },
    )])
    collected = register_fake_tool(
        registry,
        "collect_spreadsheet_attachments",
        [ToolResult(ok=True)],
    )

    outcome = EvidenceHarness(
        repository=repository,
        registry=registry,
        agent_client=FakeAgentClient([NextAction(
            action="manual",
            reason_summary="已取得附件，无需重复下载",
        )]),
        allowed_roots=[tmp_path],
    ).run(state.case_id)

    assert len(fetched.calls) == 1
    assert collected.calls == []
    assert outcome.stopped_reason == "agent_requested_manual"


def _repository(tmp_path) -> tuple[Store, CaseRepository, int]:  # noqa: ANN001
    store = Store(tmp_path / "harness.db")
    batch_id = store.create_batch("m5.4-test")
    return store, CaseRepository(store), batch_id


def _harness(
    tmp_path,  # noqa: ANN001
    repository: CaseRepository,
    client: FakeAgentClient,
    registry: ToolRegistry,
    *,
    limits: HarnessLimits | None = None,
) -> EvidenceHarness:
    return EvidenceHarness(
        repository=repository,
        registry=registry,
        agent_client=client,
        allowed_roots=[tmp_path],
        limits=limits,
    )


def test_next_action_contract_is_fail_closed() -> None:
    action = NextAction(action="call_tool", tool_name="search_tool", arguments={"q": "x"})
    assert action.tool_name == "search_tool"
    with pytest.raises(ValidationError):
        NextAction(action="call_tool")
    with pytest.raises(ValidationError):
        NextAction(action="finish", tool_name="search_tool")
    with pytest.raises(ValidationError):
        NextAction(action="manual", arguments={"path": "x"})


def test_agent_turn_context_includes_bound_m4_evidence(tmp_path) -> None:  # noqa: ANN001
    store, repository, batch_id = _repository(tmp_path)
    state, _ = repository.create_or_get(_seed(batch_id))
    state.m4_evidence = M4EvidenceBundle(
        result_id=7,
        resource_code=state.resource_code,
        year=state.year,
        verdict="无法核对",
        confidence="low",
        submitted_count=10,
        extracted_count=8,
        source_urls=["https://official.example/page"],
        found_assets=["https://official.example/list.pdf"],
        reason_codes=["coverage_unknown"],
    )

    context = _turn_context(state, [], max_observation_chars=8_000)

    assert context.case["m4_evidence"]["result_id"] == 7
    assert context.case["m4_evidence"]["source_urls"] == [
        "https://official.example/page"
    ]
    store.close()


def test_bound_m4_assets_hydrate_m5_evidence_queues_once(tmp_path) -> None:  # noqa: ANN001
    store, repository, batch_id = _repository(tmp_path)
    page_url = "https://example.gov.cn/notice"
    pdf_url = "https://example.gov.cn/files/list.pdf"
    excel_url = "https://example.gov.cn/files/list.xlsx"
    image_url = "https://example.gov.cn/files/list-1.png"
    state, _ = repository.create_or_get(_seed(batch_id).model_copy(update={
        "known_urls": [page_url, pdf_url, excel_url, image_url],
    }))
    state.m4_evidence = M4EvidenceBundle(
        result_id=8,
        resource_code=state.resource_code,
        award_name=state.award_name,
        year=state.year,
        source_urls=[page_url],
        found_assets=[pdf_url, excel_url, image_url],
    )

    assert _hydrate_m4_evidence_progress(state) is True
    assert state.evidence_progress.pending_attachment_page_urls == [page_url]
    assert state.evidence_progress.pending_attachment_urls == [pdf_url, excel_url]
    assert state.evidence_progress.pending_media_source_url == page_url
    assert state.evidence_progress.pending_media_urls == [image_url]
    assert state.evidence_progress.phase == "spreadsheet_processing"
    assert "bound_m4_assets_queued" in state.reason_codes
    assert state.submitted_summary["official_domains"] == ["example.gov.cn"]

    assert _hydrate_m4_evidence_progress(state) is False
    assert state.evidence_progress.pending_attachment_urls == [pdf_url, excel_url]
    arguments = _collect_arguments(state, {})
    assert arguments["page_urls"] == [page_url]
    assert arguments["attachment_urls"] == [pdf_url, excel_url]
    store.close()


def test_bound_m4_assets_keep_each_attachment_parent_page(tmp_path) -> None:  # noqa: ANN001
    store, repository, batch_id = _repository(tmp_path)
    page_a = "https://example.gov.cn/notice-a"
    page_b = "https://example.gov.cn/notice-b"
    pdf_url = "https://example.gov.cn/files/a.pdf"
    excel_url = "https://example.gov.cn/files/b.xlsx"
    state, _ = repository.create_or_get(_seed(batch_id))
    state.m4_evidence = M4EvidenceBundle(
        result_id=9,
        resource_code=state.resource_code,
        award_name=state.award_name,
        year=state.year,
        source_urls=[page_a, page_b],
        assets=[
            EvidenceAssetRecord(url=pdf_url, parent_url=page_a, kind="pdf"),
            EvidenceAssetRecord(url=excel_url, parent_url=page_b, kind="xlsx"),
        ],
    )

    assert _hydrate_m4_evidence_progress(state) is True
    arguments = _collect_arguments(state, {})
    assert arguments["attachment_parent_urls"] == {
        pdf_url: page_a,
        excel_url: page_b,
    }
    store.close()


def test_bound_m4_local_asset_is_reused_only_after_path_and_hash_validation(
    tmp_path,
) -> None:  # noqa: ANN001
    store, repository, batch_id = _repository(tmp_path)
    local = tmp_path / "bound.pdf"
    payload = b"%PDF-1.4\n% bounded test evidence\n"
    local.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    page_url = "https://example.gov.cn/notice"
    pdf_url = "https://example.gov.cn/files/list.pdf"
    state, _ = repository.create_or_get(_seed(batch_id))
    state.m4_evidence = M4EvidenceBundle(
        result_id=10,
        resource_code=state.resource_code,
        award_name=state.award_name,
        year=state.year,
        source_urls=[page_url],
        assets=[EvidenceAssetRecord(
            url=pdf_url,
            parent_url=page_url,
            kind="pdf",
            status="parsed",
            content_type="application/pdf",
            sha256=digest,
            size_bytes=len(payload),
            fetched_at="2026-08-01T00:00:00+00:00",
            local_path=str(local),
            extraction_method="pdf_text",
        )],
    )

    assert _hydrate_m4_evidence_progress(state, allowed_roots=[tmp_path]) is True
    assert state.evidence_progress.pending_attachment_urls == []
    assert len(state.artifacts) == 1
    assert state.artifacts[0].sha256 == digest
    assert state.artifacts[0].metadata["origin"] == "m4_current_result"
    assert state.artifacts[0].metadata["page_url"] == page_url
    store.close()


def test_attachment_collection_keeps_unprocessed_urls_for_next_run(tmp_path) -> None:  # noqa: ANN001
    from award_audit.agent.harness import runner as runner_module

    store, repository, batch_id = _repository(tmp_path)
    page_a = "https://example.gov.cn/a"
    page_b = "https://example.gov.cn/b"
    attachment_a = "https://example.gov.cn/a.xlsx"
    attachment_b = "https://example.gov.cn/b.xlsx"
    state, _ = repository.create_or_get(_seed(batch_id))
    state.evidence_progress.pending_attachment_page_urls = [page_a, page_b]
    state.evidence_progress.pending_attachment_urls = [attachment_a, attachment_b]
    state.evidence_progress.pending_attachment_parent_urls = {
        attachment_a: page_a,
        attachment_b: page_b,
    }
    result = ToolResult(ok=True, data={
        "all_attachments_processed": False,
        "processed_attachment_urls": [attachment_a],
        "unprocessed_attachment_urls": [attachment_b],
        "failed_attachment_urls": [],
    })

    runner_module._update_attachment_queue_after_collection(state, result)

    assert state.evidence_progress.pending_attachment_urls == [attachment_b]
    assert state.evidence_progress.pending_attachment_page_urls == [page_b]
    assert state.evidence_progress.pending_attachment_parent_urls == {
        attachment_b: page_b,
    }
    store.close()


def test_attachment_collection_closes_failed_urls_for_current_attempt(tmp_path) -> None:  # noqa: ANN001
    store, repository, batch_id = _repository(tmp_path)
    page = "https://example.gov.cn/results"
    failed = "https://example.gov.cn/blocked.pdf"
    state, _ = repository.create_or_get(_seed(batch_id))
    state.evidence_progress.pending_attachment_page_urls = [page]
    state.evidence_progress.pending_attachment_urls = [failed]
    state.evidence_progress.pending_attachment_parent_urls = {failed: page}

    _update_attachment_queue_after_collection(
        state,
        ToolResult(ok=False, error_code="ATTACHMENT_FAILED", data={
            "processed_attachment_urls": [],
            "unprocessed_attachment_urls": [],
            "failed_attachment_urls": [failed],
        }),
        [failed],
    )

    assert state.evidence_progress.pending_attachment_urls == []
    assert state.evidence_progress.failed_attachment_urls == [failed]
    repository.start_attempt(state, kind="initial", supplement_request="")
    repository.save(state)
    assert store.evidence_workflow_summary(state.case_id)["assets"]["failed"] == 1
    store.close()


def test_m5_artifact_records_relationship_to_bound_m4_evidence(tmp_path) -> None:  # noqa: ANN001
    store, repository, batch_id = _repository(tmp_path)
    bound_url = "https://example.gov.cn/list.pdf"
    state, _ = repository.create_or_get(_seed(batch_id))
    state.m4_evidence = M4EvidenceBundle(
        result_id=11,
        resource_code=state.resource_code,
        award_name=state.award_name,
        year=state.year,
        assets=[EvidenceAssetRecord(
            url=bound_url,
            kind="pdf",
            status="parsed",
            sha256="a" * 64,
        )],
    )
    replacement = EvidenceArtifact(
        kind="pdf",
        source_url=bound_url,
        local_path=str(tmp_path / "new.pdf"),
        content_type="application/pdf",
        sha256="b" * 64,
        size_bytes=10,
        fetched_at="2026-08-03T00:00:00Z",
    )
    supplemental = replacement.model_copy(update={
        "source_url": "https://example.gov.cn/other.pdf",
        "sha256": "c" * 64,
    })

    replacement = _annotate_m5_artifact(state, replacement)
    supplemental = _annotate_m5_artifact(state, supplemental)

    assert replacement.metadata["origin"] == "m5_supplement"
    assert replacement.metadata["m4_relationship"] == "replacement_candidate"
    assert replacement.metadata["m4_result_id"] == 11
    assert supplemental.metadata["m4_relationship"] == "supplemental"
    store.close()


class _StructuredLlm:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.calls: list[tuple[str, str, int]] = []

    def json_call(self, system: str, user: str, *, max_tokens: int) -> dict[str, Any]:
        self.calls.append((system, user, max_tokens))
        return self.payload


def test_structured_client_and_fallback_validate_one_action() -> None:
    context = AgentTurnContext(case={"case_id": 1})
    llm = _StructuredLlm({"action": "manual", "reason_summary": "证据不足"})
    structured = StructuredActionClient(lambda: llm)
    decision = structured.next_action(context, [])
    assert decision.action.action == "manual" and decision.route == "structured"
    assert "untrusted" in llm.calls[0][0].lower()

    primary = FakeAgentClient([NativeToolCallingUnavailable("unsupported")])
    fallback = FakeAgentClient([NextAction(action="finish", reason_summary="done")])
    decision = FallbackAgentClient(primary, fallback).next_action(context, [])
    assert decision.route == "fake"
    assert decision.warnings == ["fallback_from_native:NATIVE_TOOL_CALLING_UNAVAILABLE"]

    invalid = StructuredActionClient(lambda: _StructuredLlm({"action": "finish", "x": 1}))
    with pytest.raises(AgentOutputError):
        invalid.next_action(context, [])


class _CompletionEndpoint:
    def __init__(self, response: Any) -> None:
        self.response = response
        self.kwargs: dict[str, Any] = {}

    def create(self, **kwargs: Any) -> Any:
        self.kwargs = kwargs
        return self.response


class _NativeLlm:
    provider = "openai"
    model = "fake-model"

    def __init__(self, endpoint: _CompletionEndpoint) -> None:
        self.endpoint = endpoint

    def _sdk(self) -> Any:
        return SimpleNamespace(chat=SimpleNamespace(completions=self.endpoint))


def _native_tool_schema(name: str) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": "fake",
            "parameters": {"type": "object", "properties": {}},
        },
    }


def test_native_client_parses_one_registered_tool_call(monkeypatch) -> None:  # noqa: ANN001
    import award_audit.agent.llm as llm_module

    monkeypatch.setattr(llm_module, "_max_retries", lambda: 1)
    tool_call = SimpleNamespace(
        function=SimpleNamespace(name="search_official_award", arguments='{"award_name":"A"}')
    )
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(tool_calls=[tool_call], content=""))],
        usage=SimpleNamespace(
            total_tokens=17,
            prompt_tokens=12,
            completion_tokens=5,
            prompt_tokens_details=SimpleNamespace(cached_tokens=8),
            completion_tokens_details=SimpleNamespace(reasoning_tokens=3),
        ),
    )
    endpoint = _CompletionEndpoint(response)
    client = OpenAINativeAgentClient(lambda: _NativeLlm(endpoint))
    decision = client.next_action(
        AgentTurnContext(case={"case_id": 1}),
        [_native_tool_schema("search_official_award")],
    )
    assert decision.action.tool_name == "search_official_award"
    assert decision.action.arguments == {"award_name": "A"}
    assert decision.token_used == 17 and decision.route == "native"
    assert decision.usage.input_tokens == 12
    assert decision.usage.cached_input_tokens == 8
    assert decision.usage.reasoning_output_tokens == 3
    assert decision.usage.cache_detail_reported is True
    assert decision.usage.provider_usage_reported is True
    assert decision.usage.prompt_chars > 0 and decision.usage.tool_schema_chars > 2
    assert endpoint.kwargs["tool_choice"] == "required"
    assert {item["function"]["name"] for item in endpoint.kwargs["tools"]} == {
        "search_official_award",
        "finish_evidence_review",
        "request_manual_review",
    }


def test_native_client_serializes_multiple_tool_calls(monkeypatch) -> None:  # noqa: ANN001
    import award_audit.agent.llm as llm_module

    monkeypatch.setattr(llm_module, "_max_retries", lambda: 1)
    first = SimpleNamespace(function=SimpleNamespace(
        name="fetch_web_page",
        arguments='{"url":"https://example.cn/first"}',
    ))
    second = SimpleNamespace(function=SimpleNamespace(
        name="search_official_award",
        arguments='{"award_name":"示例奖"}',
    ))
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(
            tool_calls=[first, second], content=""
        ))],
        usage=SimpleNamespace(total_tokens=21, prompt_tokens=16, completion_tokens=5),
    )
    endpoint = _CompletionEndpoint(response)
    schemas = [
        _native_tool_schema("fetch_web_page"),
        _native_tool_schema("search_official_award"),
    ]

    decision = OpenAINativeAgentClient(lambda: _NativeLlm(endpoint)).next_action(
        AgentTurnContext(case={"case_id": 1}), schemas
    )

    assert decision.action.tool_name == "fetch_web_page"
    assert decision.action.arguments == {"url": "https://example.cn/first"}
    assert decision.warnings == ["native_multiple_function_calls_first_only"]


@pytest.mark.parametrize(
    ("function_name", "expected_action"),
    [
        ("finish_evidence_review", "finish"),
        ("request_manual_review", "manual"),
    ],
)
def test_native_control_functions_map_to_non_executable_actions(
    monkeypatch, function_name: str, expected_action: str  # noqa: ANN001
) -> None:
    import award_audit.agent.llm as llm_module

    monkeypatch.setattr(llm_module, "_max_retries", lambda: 1)
    tool_call = SimpleNamespace(function=SimpleNamespace(
        name=function_name,
        arguments='{"reason_summary":"bounded result"}',
    ))
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(tool_calls=[tool_call]))],
        usage=SimpleNamespace(total_tokens=9, prompt_tokens=7, completion_tokens=2),
    )
    endpoint = _CompletionEndpoint(response)
    decision = OpenAINativeAgentClient(lambda: _NativeLlm(endpoint)).next_action(
        AgentTurnContext(case={"case_id": 1}), []
    )
    assert decision.action.action == expected_action
    assert decision.action.tool_name == "" and decision.action.arguments == {}


def test_native_invalid_output_is_fail_closed_and_keeps_usage(monkeypatch) -> None:  # noqa: ANN001
    import award_audit.agent.llm as llm_module

    monkeypatch.setattr(llm_module, "_max_retries", lambda: 1)
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(tool_calls=[], content="plain text"))],
        usage=SimpleNamespace(total_tokens=13, prompt_tokens=10, completion_tokens=3),
    )
    endpoint = _CompletionEndpoint(response)
    native = OpenAINativeAgentClient(lambda: _NativeLlm(endpoint))
    fallback = FakeAgentClient([NextAction(action="manual")])
    with pytest.raises(AgentOutputError) as caught:
        FallbackAgentClient(native, fallback).next_action(
            AgentTurnContext(case={"case_id": 1}), []
        )
    assert caught.value.safe_detail == "native_missing_required_function_call"
    assert caught.value.usages[0].total_tokens == 13
    assert caught.value.usages[0].outcome == "failed"
    assert fallback.calls == []


def test_clients_and_default_harness_are_lazy(tmp_path) -> None:  # noqa: ANN001
    calls = 0

    def factory() -> Any:
        nonlocal calls
        calls += 1
        raise AssertionError("must remain lazy")

    OpenAINativeAgentClient(factory)
    StructuredActionClient(factory)
    store = Store(tmp_path / "lazy.db")
    build_default_harness(store, allowed_roots=[tmp_path])
    assert calls == 0


def test_default_agent_tools_expand_only_after_verified_media_artifacts(tmp_path) -> None:  # noqa: ANN001
    _store, repository, batch_id = _repository(tmp_path)
    state, _created = repository.create_or_get(_seed(batch_id))
    registry = build_default_registry()

    initial = {
        item["function"]["name"] for item in _tool_schemas_for_state(registry, state)
    }
    assert initial == {
        "fetch_web_page",
        "download_evidence",
        "collect_spreadsheet_attachments",
        "search_official_award",
    }

    state.artifacts.append(EvidenceArtifact(
        kind="pdf",
        source_url="https://example.gov.cn/list.pdf",
        local_path=str(tmp_path / "list.pdf"),
        content_type="application/pdf",
        sha256="a" * 64,
        size_bytes=100,
        fetched_at="2026-01-01T00:00:00Z",
    ))
    pdf_tools = {
        item["function"]["name"] for item in _tool_schemas_for_state(registry, state)
    }
    assert {"inspect_pdf", "extract_pdf_text", "render_pdf_pages"} <= pdf_tools
    assert "ocr_image" not in pdf_tools

    state.evidence_progress.pending_media_source_url = "https://example.gov.cn/list"
    state.evidence_progress.pending_media_page_title = "2025年示例奖名单"
    state.evidence_progress.pending_media_urls = [
        "https://example.gov.cn/list-1.png"
    ]
    pending_media_tools = {
        item["function"]["name"] for item in _tool_schemas_for_state(registry, state)
    }
    assert pending_media_tools == {"verify_page_image_roster"}


def test_supplied_urls_are_attempted_before_search_is_exposed(tmp_path) -> None:  # noqa: ANN001
    _store, repository, batch_id = _repository(tmp_path)
    seed = _seed(batch_id).model_copy(update={
        "trigger_codes": ["COVERAGE_UNKNOWN"],
        "known_urls": [
            "https://news.eol.cn/old-year",
            "https://news.eol.cn/target-year",
        ],
    })
    state, _created = repository.create_or_get(seed)
    registry = build_default_registry()
    initial = {
        item["function"]["name"] for item in _tool_schemas_for_state(registry, state)
    }
    assert "fetch_web_page" in initial
    assert "search_official_award" not in initial
    assert _next_unattempted_known_url(state) == "https://news.eol.cn/old-year"

    state.tool_trace.append(SimpleNamespace(tool_name="fetch_web_page"))
    after_fetch = {
        item["function"]["name"] for item in _tool_schemas_for_state(registry, state)
    }
    assert "search_official_award" in after_fetch


def test_search_excludes_urls_already_attempted_by_the_case(tmp_path) -> None:  # noqa: ANN001
    _store, repository, batch_id = _repository(tmp_path)
    known_url = "https://example.gov.cn/stale.pdf"
    state, _created = repository.create_or_get(CaseSeed(
        batch_id=batch_id,
        resource_code="03020004",
        award_name="示例教材奖",
        year="2025",
        trigger_codes=["SOURCE_UNREACHABLE"],
        objective="寻找失效来源的官方替代证据",
        known_urls=[known_url],
    ))
    registry = ToolRegistry()
    register_fake_tool(
        registry,
        "download_evidence",
        [ToolResult.failure("HTTP_ERROR", "HTTP 404")],
    )
    searched = register_fake_tool(
        registry,
        "search_official_award",
        [ToolResult(ok=True, data={"candidate_count": 1, "official_candidate_count": 1})],
    )
    client = FakeAgentClient([
        NextAction(
            action="call_tool",
            tool_name="download_evidence",
            arguments={"url": known_url},
        ),
        NextAction(
            action="call_tool",
            tool_name="search_official_award",
            arguments={"award_name": "示例教材奖"},
        ),
        NextAction(action="manual", reason_summary="候选需人工核验"),
    ])

    outcome = EvidenceHarness(
        repository=repository,
        registry=registry,
        agent_client=client,
        allowed_roots=[tmp_path],
    ).run(state.case_id)

    assert outcome.stopped_reason == "bounded_search_limit_reached"
    assert outcome.state.status == "waiting_human"
    assert searched.calls[0]["exclude_urls"] == [known_url]


def test_complete_secondary_page_runs_broad_then_attachment_search_before_stopping(
    tmp_path,
) -> None:  # noqa: ANN001
    _store, repository, batch_id = _repository(tmp_path)
    known_url = "https://news.eol.cn/example"
    state, _created = repository.create_or_get(CaseSeed(
        batch_id=batch_id,
        resource_code="02050015",
        award_name="最美教师",
        year="2025",
        trigger_codes=["COVERAGE_UNKNOWN"],
        objective="核验已提供名单网页",
        known_urls=[known_url],
        submitted_summary={
            "submission_file": str(tmp_path / "submitted.xlsx"),
            "submission_files": [
                str(tmp_path / "submitted.xlsx"),
                str(tmp_path / "submitted-part-2.xlsx"),
            ],
            "match_fields": ["XRYXM"],
        },
    ))
    registry = ToolRegistry()
    fetched = register_fake_tool(registry, "fetch_web_page", [ToolResult(ok=True, data={
        "observed_award_name": "最美教师",
        "observed_year": "2025",
        "award_name_match": True,
        "year_match": True,
        "source_level": "publisher_secondary",
        "expected_count": 25,
        "observed_count": 25,
        "coverage_complete": True,
    })])
    searched = register_fake_tool(registry, "search_official_award", [
        ToolResult(
            ok=True,
            data={"candidate_count": 0, "official_candidate_count": 0},
            warnings=["search_results_are_leads_not_evidence"],
        ),
        ToolResult(
            ok=True,
            data={"candidate_count": 0, "official_candidate_count": 0},
            warnings=["search_results_are_leads_not_evidence"],
        ),
    ])
    client = FakeAgentClient([
        NextAction(action="call_tool", tool_name="fetch_web_page", arguments={"url": known_url}),
        NextAction(
            action="call_tool",
            tool_name="search_official_award",
            arguments={"award_name": "最美教师"},
        ),
        NextAction(action="finish", reason_summary="第一轮没有候选"),
    ])

    outcome = EvidenceHarness(
        repository=repository,
        registry=registry,
        agent_client=client,
        allowed_roots=[tmp_path],
        verifier=EvidenceVerifier(),
    ).run(state.case_id)

    assert outcome.stopped_reason == "recommendation_ready"
    assert outcome.state.latest_verification.recommended_action == "accept_evidence"
    assert outcome.state.latest_verification.source_authority == "secondary"
    assert len(outcome.state.tool_trace) == 2
    assert fetched.calls[0]["expected_award_name"] == "最美教师"
    assert fetched.calls[0]["submitted_path"] == str(tmp_path / "submitted.xlsx")
    assert fetched.calls[0]["submitted_paths"] == [
        str(tmp_path / "submitted.xlsx"),
        str(tmp_path / "submitted-part-2.xlsx"),
    ]
    assert [call["strategy"] for call in searched.calls] == ["broad"]


def test_pending_candidate_is_processed_without_an_agent_turn(tmp_path) -> None:  # noqa: ANN001
    _store, repository, batch_id = _repository(tmp_path)
    candidate = "https://example.gov.cn/award/result"
    state, _created = repository.create_or_get(_seed(batch_id))
    state.evidence_progress.phase = "candidate_recovery"
    state.evidence_progress.search_round = 1
    state.evidence_progress.candidates = [EvidenceCandidate(
        url=candidate,
        source_level="official_secondary",
        provider="fixture",
        rank=1,
    )]
    repository.save(state)
    registry = ToolRegistry()
    fetched = register_fake_tool(registry, "fetch_web_page", [ToolResult(
        ok=True,
        source_url=candidate,
        evidence_facts=[EvidenceFact(
            status="complete",
            award_name="示例奖",
            year="2025",
            target_match="yes",
            year_match="yes",
            source_url=candidate,
            source_level="official_secondary",
            expected_count=1,
            observed_count=1,
            coverage_complete=True,
        )],
    )])
    client = FakeAgentClient([])

    outcome = EvidenceHarness(
        repository=repository,
        registry=registry,
        agent_client=client,
        allowed_roots=[tmp_path],
        verifier=EvidenceVerifier(),
    ).run(state.case_id)

    assert fetched.calls[0]["url"] == candidate
    assert client.calls == []
    assert outcome.stopped_reason == "recommendation_ready"


def test_search_queue_prioritizes_same_document_id_on_migrated_host(tmp_path) -> None:  # noqa: ANN001
    _store, repository, batch_id = _repository(tmp_path)
    original = (
        "http://www.example.gov.cn/archive/2025/"
        "W020251103413802225668.pdf"
    )
    recovered = (
        "https://files.example.gov.cn/public/2025/"
        "W020251103413802225668.pdf"
    )
    state, _created = repository.create_or_get(_seed(batch_id).model_copy(update={
        "known_urls": [original],
    }))
    result = ToolResult(ok=True, data={
        "provider": "offline",
        "strategy": "site",
        "query": "示例奖 2025 名单",
        "candidates": [
            {
                "url": "https://www.example.gov.cn/other-first.pdf",
                "source_level": "official_secondary",
                "provider": "offline",
                "rank": 1,
                "title": "其他分组名单",
            },
            {
                "url": recovered,
                "source_level": "official_secondary",
                "provider": "offline",
                "rank": 5,
                "title": "原文件迁移地址",
            },
        ],
    })

    _queue_search_candidates(state, result)

    assert state.evidence_progress.pending_urls() == [
        recovered,
        "https://www.example.gov.cn/other-first.pdf",
    ]


def test_same_page_is_not_refetched_when_model_changes_comparison_arguments(
    tmp_path,
) -> None:  # noqa: ANN001
    _store, repository, batch_id = _repository(tmp_path)
    known_url = "https://publisher.example.cn/award"
    state, _created = repository.create_or_get(CaseSeed(
        batch_id=batch_id,
        resource_code="NEW-RESOURCE",
        award_name="示例奖",
        year="2025",
        trigger_codes=["COVERAGE_UNKNOWN"],
        objective="同一网页只抓取一次",
        known_urls=[known_url],
    ))
    registry = ToolRegistry()
    fetched = register_fake_tool(registry, "fetch_web_page", [
        ToolResult(ok=True, source_url=known_url),
        ToolResult(ok=True, source_url=known_url),
    ])
    client = FakeAgentClient([
        NextAction(
            action="call_tool",
            tool_name="fetch_web_page",
            arguments={
                "url": known_url,
                "page_total_count": 50,
                "max_chars": 30000,
            },
        ),
        NextAction(
            action="call_tool",
            tool_name="fetch_web_page",
            arguments={"url": known_url},
        ),
    ])

    outcome = EvidenceHarness(
        repository=repository,
        registry=registry,
        agent_client=client,
        allowed_roots=[tmp_path],
        verifier=EvidenceVerifier(),
    ).run(state.case_id)

    assert len(fetched.calls) == 1
    assert "page_total_count" not in fetched.calls[0]
    assert fetched.calls[0]["max_chars"] == 30000
    assert "repeated_tool_call_blocked" in outcome.state.reason_codes


def test_repeated_call_with_complete_evidence_finishes_normal_verification(
    tmp_path,
) -> None:  # noqa: ANN001
    _store, repository, batch_id = _repository(tmp_path)
    known_url = "https://publisher.example.cn/complete-award"
    state, _created = repository.create_or_get(CaseSeed(
        batch_id=batch_id,
        resource_code="NEW-RESOURCE",
        award_name="示例奖",
        year="2025",
        trigger_codes=["COVERAGE_UNKNOWN"],
        objective="完整证据后阻止重复调用",
        known_urls=[known_url],
    ))
    registry = ToolRegistry()
    fetched = register_fake_tool(registry, "fetch_web_page", [ToolResult(
        ok=True,
        source_url=known_url,
        evidence_facts=[EvidenceFact(
            status="complete",
            award_name="示例奖",
            year="2025",
            target_match="yes",
            year_match="yes",
            source_url=known_url,
            source_level="publisher_secondary",
            expected_count=20,
            observed_count=20,
            submitted_count=20,
            coverage_complete=True,
        )],
    )])
    repeated = NextAction(
        action="call_tool",
        tool_name="fetch_web_page",
        arguments={"url": known_url},
    )

    outcome = EvidenceHarness(
        repository=repository,
        registry=registry,
        agent_client=FakeAgentClient([repeated, repeated]),
        allowed_roots=[tmp_path],
        verifier=EvidenceVerifier(),
    ).run(state.case_id)

    assert len(fetched.calls) == 1
    assert outcome.stopped_reason == "recommendation_ready"
    assert outcome.state.evidence_progress.phase == "waiting_human"
    assert outcome.state.latest_verification is not None
    assert outcome.state.latest_verification.recommended_action == "accept_evidence"
    assert outcome.state.latest_verification.missing_evidence == []


def test_authority_uplift_fetches_only_one_candidate_then_verifies(tmp_path) -> None:  # noqa: ANN001
    _store, repository, batch_id = _repository(tmp_path)
    known_url = "https://news.eol.cn/example"
    state, _created = repository.create_or_get(CaseSeed(
        batch_id=batch_id,
        resource_code="02050015",
        award_name="最美教师",
        year="2025",
        trigger_codes=["COVERAGE_UNKNOWN"],
        objective="核验已提供名单网页",
        known_urls=[known_url],
        submitted_summary={
            "submission_file": str(tmp_path / "submitted.xlsx"),
            "match_fields": ["XRYXM"],
        },
    ))
    complete = {
        "observed_award_name": "最美教师",
        "observed_year": "2025",
        "award_name_match": True,
        "year_match": True,
        "expected_count": 25,
        "observed_count": 25,
        "coverage_complete": True,
    }
    registry = ToolRegistry()
    fetched = register_fake_tool(registry, "fetch_web_page", [
        ToolResult(ok=True, data={**complete, "source_level": "publisher_secondary"}),
        ToolResult(ok=True, data={**complete, "source_level": "official_primary"}),
    ])
    register_fake_tool(registry, "search_official_award", [ToolResult(
        ok=True,
        data={
            "candidate_count": 2,
            "official_candidate_count": 1,
            "candidates": [
                {
                    "url": "https://example.gov.cn/official",
                    "source_level": "official_primary",
                    "provider": "offline",
                    "rank": 1,
                    "title": "示例奖公示",
                    "query": "示例奖 2025",
                },
                {
                    "url": "https://example.gov.cn/second",
                    "source_level": "official_primary",
                    "provider": "offline",
                    "rank": 2,
                    "title": "示例奖名单",
                    "query": "示例奖 2025",
                },
            ],
        },
        warnings=["search_results_are_leads_not_evidence"],
    )])
    client = FakeAgentClient([
        NextAction(action="call_tool", tool_name="fetch_web_page", arguments={"url": known_url}),
    ])

    outcome = EvidenceHarness(
        repository=repository,
        registry=registry,
        agent_client=client,
        allowed_roots=[tmp_path],
        verifier=EvidenceVerifier(),
    ).run(state.case_id)

    assert outcome.stopped_reason == "recommendation_ready"
    assert outcome.state.latest_verification.source_authority == "official"
    assert len(fetched.calls) == 2
    assert len(outcome.state.tool_trace) == 3
    assert len(client.calls) == 0


def test_official_source_with_only_missing_items_runs_one_corroboration_search(
    tmp_path,
) -> None:  # noqa: ANN001
    _store, repository, batch_id = _repository(tmp_path)
    known_url = "https://example.gov.cn/award-2025"
    state, _created = repository.create_or_get(CaseSeed(
        batch_id=batch_id,
        resource_code="04030052",
        award_name="全国研究生渔菁英挑战赛",
        year="2025",
        trigger_codes=["COVERAGE_UNKNOWN"],
        objective="核验官网名单中的逐项差异",
        known_urls=[known_url],
    ))
    state.budget.limits.max_searches = 1
    repository.save(state)
    missing_name = "未在官网名单中找到的参赛队"
    partial_fact = EvidenceFact(
        status="partial",
        award_name=state.award_name,
        year=state.year,
        target_match="yes",
        year_match="yes",
        source_url=known_url,
        source_level="official_primary",
        expected_count=93,
        observed_count=91,
        submitted_count=93,
        coverage_complete=False,
        missing_items=[missing_name],
        missing_item_count=1,
    )
    registry = ToolRegistry()
    fetched = register_fake_tool(registry, "fetch_web_page", [ToolResult(
        ok=True,
        source_url=known_url,
        data={
            "observed_award_name": state.award_name,
            "observed_year": state.year,
            "source_level": "official_primary",
            "expected_count": 93,
            "observed_count": 91,
            "coverage_complete": False,
            "missing_items": [missing_name],
            "missing_item_count": 1,
            "extra_items": [],
            "extra_item_count": 0,
        },
        evidence_facts=[partial_fact],
    )])
    searched = register_fake_tool(registry, "search_official_award", [ToolResult(
        ok=True,
        data={
            "candidate_count": 0,
            "official_candidate_count": 0,
            "candidates": [],
        },
        warnings=["search_results_are_leads_not_evidence"],
    )])

    outcome = EvidenceHarness(
        repository=repository,
        registry=registry,
        agent_client=FakeAgentClient([
            NextAction(action="manual", reason_summary="官网名单存在一项差异"),
        ]),
        allowed_roots=[tmp_path],
        verifier=EvidenceVerifier(),
    ).run(state.case_id)

    assert len(fetched.calls) == 1
    assert len(searched.calls) == 1
    assert searched.calls[0]["award_name"] == state.award_name
    assert searched.calls[0]["year"] == state.year
    assert searched.calls[0]["strategy"] == "broad"
    assert searched.calls[0].get("discrepancy_terms", []) == []
    assert "coverage_discrepancy_recovery_started" in outcome.state.reason_codes
    assert outcome.state.status == "waiting_human"


def test_complete_official_document_with_missing_items_stops_without_search(
    tmp_path,
) -> None:  # noqa: ANN001
    _store, repository, batch_id = _repository(tmp_path)
    known_url = "https://example.gov.cn/award-2025"
    state, _created = repository.create_or_get(CaseSeed(
        batch_id=batch_id,
        resource_code="04030052",
        award_name="全国研究生渔菁英挑战赛",
        year="2025",
        trigger_codes=["COVERAGE_UNKNOWN"],
        objective="核验完整官方名单中的逐项差异",
        known_urls=[known_url],
    ))
    missing_names = ["摸鱼能干队", "青海逐浪"]
    fact = EvidenceFact(
        status="partial", award_name=state.award_name, year=state.year,
        target_match="yes", year_match="yes", source_url=known_url,
        source_level="official_primary", expected_count=93, observed_count=91,
        submitted_count=93, coverage_complete=False, document_complete=True,
        missing_items=missing_names, missing_item_count=2,
    )
    registry = ToolRegistry()
    register_fake_tool(registry, "fetch_web_page", [ToolResult(
        ok=True, source_url=known_url,
        data={"document_complete": True, "coverage_complete": False},
        evidence_facts=[fact],
    )])
    searched = register_fake_tool(
        registry, "search_official_award", [ToolResult(ok=True)]
    )

    outcome = EvidenceHarness(
        repository=repository, registry=registry,
        agent_client=FakeAgentClient([]), allowed_roots=[tmp_path],
        verifier=EvidenceVerifier(),
    ).run(state.case_id)

    assert searched.calls == []
    assert outcome.stopped_reason == "authoritative_document_differences_found"
    assert "authoritative_document_differences_found" in outcome.state.reason_codes


def test_single_unreachable_known_url_runs_bounded_recovery_search(
    tmp_path,
) -> None:  # noqa: ANN001
    _store, repository, batch_id = _repository(tmp_path)
    known_url = "https://example.gov.cn/broken-award-page"
    state, _created = repository.create_or_get(CaseSeed(
        batch_id=batch_id,
        resource_code="04030052",
        award_name="全国研究生渔菁英挑战赛",
        year="2025",
        trigger_codes=["SOURCE_UNREACHABLE"],
        objective="已知来源失效后寻找替代官方名单",
        known_urls=[known_url],
    ))
    state.budget.limits.max_searches = 1
    repository.save(state)
    registry = ToolRegistry()
    fetched = register_fake_tool(registry, "fetch_web_page", [
        ToolResult.failure("SOURCE_UNREACHABLE", "offline fixture"),
    ])
    searched = register_fake_tool(registry, "search_official_award", [ToolResult(
        ok=True,
        data={
            "candidate_count": 0,
            "official_candidate_count": 0,
            "candidates": [],
        },
        warnings=["search_results_are_leads_not_evidence"],
    )])

    outcome = EvidenceHarness(
        repository=repository,
        registry=registry,
        agent_client=FakeAgentClient([
            NextAction(action="manual", reason_summary="替代来源仍未找到"),
        ]),
        allowed_roots=[tmp_path],
        verifier=EvidenceVerifier(),
    ).run(state.case_id)

    assert len(fetched.calls) == 1
    assert len(searched.calls) == 1
    assert searched.calls[0]["award_name"] == state.award_name
    assert searched.calls[0]["year"] == state.year
    assert "known_source_incomplete_recovery_started" in outcome.state.reason_codes
    assert outcome.state.status == "waiting_human"


def test_pdf_attachment_is_processed_before_another_known_url(tmp_path) -> None:  # noqa: ANN001
    _store, repository, batch_id = _repository(tmp_path)
    page_url = "https://publisher.example.cn/award-2025"
    wrong_year_url = "https://publisher.example.cn/award-2023"
    pdf_url = "https://publisher.example.cn/files/award-list.pdf"
    authority_url = "https://example.gov.cn/award-2025"
    second_candidate_url = "https://example.gov.cn/award-2025-detail"
    pdf_path = tmp_path / "award-list.pdf"
    pdf_path.write_bytes(b"offline-pdf-placeholder")
    state, _created = repository.create_or_get(CaseSeed(
        batch_id=batch_id,
        resource_code="NEW-RESOURCE",
        award_name="示例奖",
        year="2025",
        trigger_codes=["COVERAGE_UNKNOWN"],
        objective="先处理当前网页附件，再决定是否需要其他来源",
        known_urls=[page_url, wrong_year_url],
        submitted_summary={
            "submission_file": str(tmp_path / "submitted.xlsx"),
            "submission_files": [str(tmp_path / "submitted.xlsx")],
            "match_fields": ["姓名"],
            "submitted_rows": 325,
        },
    ))
    page_fact = EvidenceFact(
        status="partial",
        award_name="示例奖",
        year="2025",
        target_match="yes",
        year_match="yes",
        source_url=page_url,
        source_level="unknown",
        expected_count=325,
        observed_count=0,
        submitted_count=325,
        coverage_complete=False,
    )
    pdf_fact = EvidenceFact(
        status="complete",
        award_name="示例奖",
        year="2025",
        target_match="yes",
        year_match="yes",
        source_url=pdf_url,
        source_level="unknown",
        expected_count=325,
        observed_count=325,
        submitted_count=325,
        coverage_complete=True,
    )
    artifact = EvidenceArtifact(
        kind="pdf",
        source_url=pdf_url,
        local_path=str(pdf_path),
        content_type="application/pdf",
        sha256="a" * 64,
        size_bytes=pdf_path.stat().st_size,
        fetched_at="2026-07-30T00:00:00Z",
        metadata={
            "page_url": page_url,
            "attachment_linked": True,
            "page_observed_award_name": "示例奖",
            "page_observed_year": "2025",
            "page_source_level": "unknown",
        },
    )
    registry = ToolRegistry()
    fetched = register_fake_tool(registry, "fetch_web_page", [
        ToolResult(
            ok=True,
            source_url=page_url,
            data={
                "next_evidence_stage": "spreadsheet_processing",
                "candidate_attachment_urls": [pdf_url],
            },
            evidence_facts=[page_fact],
        ),
        ToolResult(ok=True, source_url=authority_url),
    ])
    collected = register_fake_tool(
        registry,
        "collect_spreadsheet_attachments",
        [ToolResult(ok=True, source_url=page_url, artifacts=[artifact])],
    )
    inspected = register_fake_tool(registry, "inspect_pdf", [ToolResult(
        ok=True,
        local_path=str(pdf_path),
        data={"page_count": 2, "digital_pages": [1, 2]},
    )])
    extracted = register_fake_tool(registry, "extract_pdf_text", [ToolResult(
        ok=True,
        source_url=pdf_url,
        local_path=str(pdf_path),
        evidence_facts=[pdf_fact],
    )])
    searched = register_fake_tool(registry, "search_official_award", [ToolResult(
        ok=True,
        data={
            "candidate_count": 2,
            "official_candidate_count": 2,
            "candidates": [
                {
                    "url": authority_url,
                    "source_level": "official_primary",
                    "provider": "offline",
                    "rank": 1,
                    "title": "示例奖公示",
                    "query": "示例奖 2025",
                },
                {
                    "url": second_candidate_url,
                    "source_level": "official_primary",
                    "provider": "offline",
                    "rank": 2,
                    "title": "示例奖名单",
                    "query": "示例奖 2025",
                },
            ],
        },
        warnings=["search_results_are_leads_not_evidence"],
    )])

    outcome = EvidenceHarness(
        repository=repository,
        registry=registry,
        agent_client=FakeAgentClient([NextAction(
            action="call_tool",
            tool_name="fetch_web_page",
            arguments={"url": page_url},
        )]),
        allowed_roots=[tmp_path],
        verifier=EvidenceVerifier(),
    ).run(state.case_id)

    assert outcome.stopped_reason == "source_authority_unresolved_after_bounded_search"
    assert len(fetched.calls) == 2
    assert len(collected.calls) == 1
    assert len(inspected.calls) == 1
    assert inspected.calls[0]["path"] == str(pdf_path)
    assert len(extracted.calls) == 1
    assert extracted.calls[0]["pages"] == [1, 2]
    assert len(searched.calls) == 1
    assert all(call.get("url") != wrong_year_url for call in fetched.calls)
    assert all(call.get("url") != second_candidate_url for call in fetched.calls)
    assert outcome.state.latest_verification is not None
    assert outcome.state.latest_verification.coverage_complete == "yes"
    assert outcome.state.latest_verification.missing_evidence == ["来源权威性未确认"]


def test_repeated_authority_search_is_blocked_before_executor(tmp_path) -> None:  # noqa: ANN001
    _store, repository, batch_id = _repository(tmp_path)
    state, _created = repository.create_or_get(_seed(batch_id))
    state.reason_codes = [
        "provided_web_evidence_complete",
        "authority_search_completed",
    ]
    repository.save(state)
    registry = ToolRegistry()
    searched = register_fake_tool(
        registry,
        "search_official_award",
        [ToolResult(ok=True, data={"official_candidate_count": 1})],
    )
    outcome = EvidenceHarness(
        repository=repository,
        registry=registry,
        agent_client=FakeAgentClient([NextAction(
            action="call_tool",
            tool_name="search_official_award",
            arguments={"award_name": "示例奖"},
        )]),
        allowed_roots=[tmp_path],
    ).run(state.case_id)

    assert outcome.stopped_reason == "recommendation_ready"
    assert searched.calls == []
    assert outcome.state.tool_trace == []


def test_mechanical_pdf_pipeline_does_not_consume_agent_step_budget(tmp_path) -> None:  # noqa: ANN001
    _store, repository, batch_id = _repository(tmp_path)
    page_url = "https://official.example/patent-awards"
    pdf_urls = [f"https://official.example/list-{index}.pdf" for index in range(7)]
    pdf_paths = [tmp_path / f"list-{index}.pdf" for index in range(7)]
    for path in pdf_paths:
        path.write_bytes(b"offline fake pdf")
    state, _created = repository.create_or_get(_seed(batch_id).model_copy(update={
        "trigger_codes": ["PDF_ONLY"],
        "known_urls": [page_url],
    }))
    state.evidence_progress.pending_attachment_page_urls = [page_url]
    state.evidence_progress.pending_attachment_urls = pdf_urls
    state.evidence_progress.pending_attachment_parent_urls = {
        url: page_url for url in pdf_urls
    }
    repository.save(state)

    artifacts = [
        EvidenceArtifact(
            kind="pdf",
            source_url=url,
            local_path=str(path),
            content_type="application/pdf",
            sha256=f"{index + 1:064x}",
            size_bytes=path.stat().st_size,
            fetched_at="2026-08-04T00:00:00Z",
            metadata={"page_url": page_url, "attachment_linked": True},
        )
        for index, (url, path) in enumerate(zip(pdf_urls, pdf_paths, strict=True))
    ]
    registry = ToolRegistry()
    register_fake_tool(registry, "collect_spreadsheet_attachments", [ToolResult(
        ok=True,
        source_url=page_url,
        artifacts=artifacts,
        data={
            "all_attachments_processed": True,
            "processed_attachment_urls": pdf_urls,
            "unprocessed_attachment_urls": [],
            "failed_attachment_urls": [],
        },
    )])
    inspected = register_fake_tool(registry, "inspect_pdf", [
        ToolResult(
            ok=True,
            local_path=str(path),
            data={"page_count": 1, "digital_pages": [1]},
        )
        for path in pdf_paths
    ])
    extracted = register_fake_tool(registry, "extract_pdf_text", [
        ToolResult(ok=True, source_url=url, local_path=str(path))
        for url, path in zip(pdf_urls, pdf_paths, strict=True)
    ])

    outcome = EvidenceHarness(
        repository=repository,
        registry=registry,
        agent_client=FakeAgentClient([NextAction(
            action="manual",
            reason_summary="all deterministic PDF work completed",
        )]),
        allowed_roots=[tmp_path],
    ).run(state.case_id)

    assert outcome.stopped_reason == "agent_requested_manual"
    assert len(inspected.calls) == 7
    assert len(extracted.calls) == 7
    assert outcome.state.step_count == 1


def test_image_roster_batches_accumulate_before_final_coverage(tmp_path) -> None:  # noqa: ANN001
    _store, repository, batch_id = _repository(tmp_path)
    submitted = tmp_path / "submitted.xlsx"
    submitted.touch()
    state, _created = repository.create_or_get(_seed(batch_id).model_copy(update={
        "trigger_codes": ["IMAGE_ONLY"],
        "submitted_summary": {
            "submission_files": [str(submitted)],
            "match_fields": ["XMMC"],
            "expected_scope_count": 2,
        },
    }))
    page_url = "https://official.example/image-roster"
    image_urls = [f"https://official.example/page-{index}.jpg" for index in range(8)]
    state.evidence_progress.pending_media_source_url = page_url
    state.evidence_progress.pending_media_urls = image_urls

    first_arguments = _image_roster_arguments(state, {})
    assert first_arguments["image_urls"] == image_urls[:6]
    first_result = ToolResult(ok=True, data={
        "matched_items": ["项目甲"],
        "missing_items": ["项目乙"],
        "extra_items": [],
        "expected_count": 2,
        "observed_count": 1,
        "coverage_complete": False,
        "processed_image_urls": image_urls[:6],
        "failed_image_urls": [],
        "unprocessed_image_urls": [],
        "all_images_processed": True,
    })
    _update_media_queue_after_verification(
        state, first_result, first_arguments["image_urls"]
    )

    assert state.evidence_progress.pending_media_urls == image_urls[6:]
    assert first_result.data["missing_items"] == []
    assert first_result.data["unresolved_items"] == ["项目乙"]
    assert first_result.data["all_images_processed"] is False

    second_arguments = _image_roster_arguments(state, {})
    assert second_arguments["image_urls"] == image_urls[6:]
    second_result = ToolResult(ok=True, data={
        "matched_items": ["项目乙"],
        "missing_items": ["项目甲"],
        "extra_items": [],
        "expected_count": 2,
        "observed_count": 1,
        "coverage_complete": False,
        "processed_image_urls": image_urls[6:],
        "failed_image_urls": [],
        "unprocessed_image_urls": [],
        "all_images_processed": True,
    })
    _update_media_queue_after_verification(
        state, second_result, second_arguments["image_urls"]
    )

    assert state.evidence_progress.pending_media_urls == []
    assert second_result.data["matched_items"] == ["项目甲", "项目乙"]
    assert second_result.data["missing_items"] == []
    assert second_result.data["unresolved_items"] == []
    assert second_result.data["coverage_complete"] is True
    assert second_result.data["all_images_processed"] is True
    assert second_result.data["document_complete"] is True
    assert second_result.data["evidence_group"] == page_url
    assert second_result.data["cumulative_identity_count"] == 2
    assert second_result.data["batch_new_identity_count"] == 1


def test_spreadsheet_assets_are_queued_by_routed_scope(tmp_path) -> None:  # noqa: ANN001
    _store, repository, batch_id = _repository(tmp_path)
    submitted = tmp_path / "submitted.xlsx"
    submitted.touch()
    state, _created = repository.create_or_get(_seed(batch_id).model_copy(update={
        "submitted_summary": {
            "submission_files": [str(submitted)],
            "role_scopes": [
                {
                    "scope_id": 11,
                    "role_type": "team",
                    "submitted_identity_count": 279,
                    "business_scope": {"XMLB": "获奖队伍"},
                    "profile": {"primary_alternatives": [["ZPMC"]]},
                },
                {
                    "scope_id": 12,
                    "role_type": "organization",
                    "submitted_identity_count": 28,
                    "business_scope": {"XMLB": "优秀组织单位"},
                    "profile": {"primary_alternatives": [["XCSDW"]]},
                },
            ],
        },
    }))
    page_url = "https://official.example/roster"
    team_url = "https://official.example/team.xlsx"
    organization_url = "https://official.example/organization.xlsx"
    for url, label in (
        (team_url, "获奖队伍名单"),
        (organization_url, "优秀组织单位名单"),
    ):
        local_path = tmp_path / url.rsplit("/", 1)[-1]
        local_path.write_bytes(b"fixture")
        state.artifacts.append(EvidenceArtifact(
            kind="xlsx",
            source_url=url,
            local_path=str(local_path),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            sha256="1" * 64,
            size_bytes=local_path.stat().st_size,
            fetched_at="2026-08-05T00:00:00Z",
            metadata={"label": label},
        ))
    progress = state.evidence_progress
    progress.pending_attachment_urls = [team_url, organization_url]
    progress.pending_attachment_page_urls = [page_url]
    progress.pending_attachment_parent_urls = {
        team_url: page_url,
        organization_url: page_url,
    }

    first = _collect_arguments(state, {})
    assert first["scope_id"] == 11
    assert first["expected_scope_count"] == 279
    assert first["match_fields"] == ["ZPMC"]
    assert first["attachment_urls"] == [team_url]
    _update_attachment_queue_after_collection(
        state,
        ToolResult(ok=True, data={
            "processed_attachment_urls": [team_url],
            "unprocessed_attachment_urls": [],
            "failed_attachment_urls": [],
        }),
        [team_url],
    )

    second = _collect_arguments(state, {})
    assert second["scope_id"] == 12
    assert second["expected_scope_count"] == 28
    assert second["match_fields"] == ["XCSDW"]
    assert second["attachment_urls"] == [organization_url]


def test_adjacent_web_role_headers_share_parallel_table_section(tmp_path) -> None:  # noqa: ANN001
    _store, repository, batch_id = _repository(tmp_path)
    state, _created = repository.create_or_get(_seed(batch_id).model_copy(update={
        "submitted_summary": {"role_scopes": [
            {
                "scope_id": 61, "role_type": "organization",
                "submitted_identities": {"a": "东北大学", "b": "武汉大学"},
                "profile": {"section_include_terms": ["组织单位"]},
            },
            {
                "scope_id": 62, "role_type": "instructor_or_person",
                "submitted_identities": {"c": "教师甲", "d": "教师乙"},
                "profile": {"section_include_terms": ["指导教师"]},
            },
        ]},
    }))
    result = ToolResult(ok=True, source_url="https://official.example/notice", data={
        "text": "组织单位 指导教师 东北大学 教师甲 武汉大学 教师乙",
    }, evidence_facts=[EvidenceFact(
        status="complete", award_name=state.award_name, year=state.year,
        target_match="yes", year_match="yes", source_level="official_primary",
        document_complete=True, coverage_complete=True,
    )])
    state.evidence_progress.pending_media_source_url = result.source_url
    state.evidence_progress.pending_media_urls = [
        "https://official.example/unneeded-roster-image.png"
    ]
    state.m4_evidence = M4EvidenceBundle(
        result_id=1,
        resource_code=state.resource_code,
        assets=[EvidenceAssetRecord(
            url=state.evidence_progress.pending_media_urls[0],
            parent_url=result.source_url,
            kind="image",
        )],
    )

    _route_web_result_to_scopes(state, result)

    by_scope = {fact.scope_id: fact for fact in result.evidence_facts}
    assert by_scope[61].matched_items == ["东北大学", "武汉大学"]
    assert by_scope[62].matched_items == ["教师甲", "教师乙"]
    assert all(fact.document_complete for fact in by_scope.values())
    assert result.data["all_required_scopes_complete"] is True
    assert state.evidence_progress.pending_media_urls == []
    assert state.m4_evidence.assets[0].status == "skipped"
    assert state.m4_evidence.assets[0].metadata["routes"][0]["route_status"] == "excluded"


def test_image_records_route_by_section_and_identity_without_casewide_denominator(
    tmp_path,
) -> None:  # noqa: ANN001
    _store, repository, batch_id = _repository(tmp_path)
    state, _created = repository.create_or_get(_seed(batch_id).model_copy(update={
        "submitted_summary": {
            "role_scopes": [
                {
                    "scope_id": 21, "role_type": "work_or_project",
                    "business_scope": {"XMLB": "舞台剧项目"},
                    "submitted_identities": {"a": "作品甲", "b": "作品乙"},
                },
                {
                    "scope_id": 22, "role_type": "work_or_project",
                    "business_scope": {"XMLB": "传播推广项目"},
                    "submitted_identities": {"c": "展览甲"},
                },
            ],
        },
    }))
    image_a = "https://official.example/stage.png"
    image_b = "https://official.example/exhibition.png"
    result = ToolResult(ok=True, source_url="https://official.example/notice", data={
        "scope_id": 0,
        "processed_image_urls": [image_a, image_b],
        "failed_image_urls": [],
        "identity_records": [
            {"source_url": image_a, "section_title": "舞台剧项目",
             "name": "作品甲", "org": "甲单位"},
            {"source_url": image_a, "section_title": "舞台剧项目",
             "name": "作品乙", "org": "乙单位"},
            {"source_url": image_b, "section_title": "传播推广项目",
             "name": "展览甲", "org": "丙单位"},
        ],
    })

    _route_image_result_to_scopes(state, result, [image_a, image_b])

    assert {fact.scope_id for fact in result.evidence_facts} == {21, 22}
    facts = {fact.scope_id: fact for fact in result.evidence_facts}
    assert facts[21].expected_count == 2
    assert facts[21].matched_items == ["作品甲", "作品乙"]
    assert facts[22].expected_count == 1
    assert result.data["image_scope_routes"][image_a][0]["scope_id"] == 21
    assert result.data["image_scope_routes"][image_b][0]["scope_id"] == 22


def test_image_records_match_composite_submitted_identity_with_secondary_column(
    tmp_path,
) -> None:  # noqa: ANN001
    _store, repository, batch_id = _repository(tmp_path)
    state, _created = repository.create_or_get(_seed(batch_id).model_copy(update={
        "submitted_summary": {"role_scopes": [{
            "scope_id": 23,
            "role_type": "work_or_project",
            "business_scope": {"XMLB": "fine art individual"},
            "submitted_identities": {
                "spring-rain\x1fdiscriminator:owner=Li": "Spring Rain",
                "spring-rain\x1fdiscriminator:owner=Wang": "Spring Rain",
            },
        }]},
    }))
    image_url = "https://official.example/fine-art.png"
    result = ToolResult(ok=True, source_url="https://official.example/notice", data={
        "scope_id": 0,
        "processed_image_urls": [image_url],
        "failed_image_urls": [],
        "identity_records": [{
            "source_url": image_url,
            "section_title": "",
            "name": "Spring Rain",
            "org": "Li",
        }],
    })

    _route_image_result_to_scopes(state, result, [image_url])

    assert result.evidence_facts[0].scope_id == 23
    assert result.evidence_facts[0].matched_items == ["Spring Rain"]
    assert result.evidence_facts[0].missing_items == ["Spring Rain"]
    assert result.data["image_scope_routes"][image_url][0]["route_status"] == "routed"


def test_unrouted_roster_image_is_ambiguous_instead_of_excluded(tmp_path) -> None:  # noqa: ANN001
    _store, repository, batch_id = _repository(tmp_path)
    state, _created = repository.create_or_get(_seed(batch_id).model_copy(update={
        "submitted_summary": {"role_scopes": [{
            "scope_id": 24,
            "role_type": "work_or_project",
            "business_scope": {"XMLB": "known category"},
            "submitted_identities": {"known": "Known Work"},
        }]},
    }))
    image_url = "https://official.example/unrouted-roster.png"
    result = ToolResult(ok=True, source_url="https://official.example/notice", data={
        "scope_id": 0,
        "processed_image_urls": [image_url],
        "failed_image_urls": [],
        "identity_records": [{
            "source_url": image_url,
            "section_title": "",
            "name": "Different Work",
            "org": "Different Owner",
        }],
    })

    _route_image_result_to_scopes(state, result, [image_url])

    route = result.data["image_scope_routes"][image_url][0]
    assert route["route_status"] == "ambiguous"
    assert route["confidence"] == 0.0


def test_image_records_route_when_vision_reverses_primary_and_secondary_columns(
    tmp_path,
) -> None:  # noqa: ANN001
    _store, repository, batch_id = _repository(tmp_path)
    state, _created = repository.create_or_get(_seed(batch_id).model_copy(update={
        "submitted_summary": {"role_scopes": [{
            "scope_id": 25,
            "role_type": "work_or_project",
            "business_scope": {"XMLB": "fine art individual"},
            "submitted_identities": {
                "chinese-painting-spring\x1fdiscriminator:owner=Li":
                    "Chinese Painting Spring",
            },
        }]},
    }))
    image_url = "https://official.example/reversed-columns.png"
    result = ToolResult(ok=True, source_url="https://official.example/notice", data={
        "scope_id": 0,
        "processed_image_urls": [image_url],
        "failed_image_urls": [],
        "identity_records": [{
            "source_url": image_url,
            "section_title": "",
            "name": "Li",
            "org": "Chinese Painting Spring",
        }, {
            "source_url": image_url,
            "section_title": "",
            "name": "Unknown Artist",
            "org": "Unknown Work",
        }],
    })

    _route_image_result_to_scopes(state, result, [image_url])

    fact = result.evidence_facts[0]
    assert fact.scope_id == 25
    assert fact.matched_items == ["Chinese Painting Spring"]
    assert fact.extra_items == ["Unknown Work"]
    assert result.data["image_scope_routes"][image_url][0]["route_status"] == "routed"


def test_mixed_image_page_is_extracted_once_without_first_scope_filter(tmp_path) -> None:  # noqa: ANN001
    _store, repository, batch_id = _repository(tmp_path)
    page_url = "https://official.example/notice"
    image_urls = [
        "https://official.example/part-a.png",
        "https://official.example/part-b.png",
    ]
    state, _created = repository.create_or_get(_seed(batch_id).model_copy(update={
        "known_urls": [page_url, *image_urls],
        "submitted_summary": {
            "submission_files": [str(tmp_path / "submitted.xlsx")],
            "match_fields": ["XMMC"],
            "expected_scope_count": 3,
            "role_scopes": [
                {
                    "scope_id": 21, "role_type": "work_or_project", "required": True,
                    "business_scope": {"XMLB": "Stage"},
                    "submitted_identity_count": 2,
                    "profile": {"primary_alternatives": [["XMMC"]]},
                },
                {
                    "scope_id": 22, "role_type": "work_or_project", "required": True,
                    "business_scope": {"XMLB": "Exhibition"},
                    "submitted_identity_count": 1,
                    "profile": {"primary_alternatives": [["XMMC"]]},
                },
            ],
        },
    }))
    state.m4_evidence = M4EvidenceBundle(
        result_id=8,
        resource_code=state.resource_code,
        award_name=state.award_name,
        year=state.year,
        source_urls=[page_url],
        assets=[
            EvidenceAssetRecord(url=url, parent_url=page_url, kind="png")
            for url in image_urls
        ],
    )
    state.evidence_progress.pending_media_source_url = page_url
    state.evidence_progress.pending_media_page_title = "Mixed roster"
    state.evidence_progress.pending_media_urls = image_urls

    arguments = _image_roster_arguments(state, {})

    assert arguments["image_urls"] == image_urls
    assert "scope_id" not in arguments
    assert "submitted_scope_filter" not in arguments
    assert "section_keywords" not in arguments


def test_one_pdf_extraction_routes_matches_to_multiple_scopes(tmp_path) -> None:  # noqa: ANN001
    _store, repository, batch_id = _repository(tmp_path)
    page_url = "https://official.example/notice"
    pdf_url = "https://official.example/combined.pdf"
    pdf_path = str(tmp_path / "combined.pdf")
    state, _created = repository.create_or_get(_seed(batch_id).model_copy(update={
        "submitted_summary": {
            "role_scopes": [
                {
                    "scope_id": 31, "role_type": "work_or_project", "required": True,
                    "business_scope": {"XMLB": "Planning"},
                    "submitted_identity_count": 1,
                    "submitted_identities": {"a": "Project A"},
                },
                {
                    "scope_id": 32, "role_type": "work_or_project", "required": True,
                    "business_scope": {"XMLB": "Youth"},
                    "submitted_identity_count": 1,
                    "submitted_identities": {"b": "Project B"},
                },
            ],
        },
    }))
    state.m4_evidence = M4EvidenceBundle(
        result_id=10,
        resource_code=state.resource_code,
        award_name=state.award_name,
        year=state.year,
        source_urls=[page_url],
        assets=[EvidenceAssetRecord(url=pdf_url, parent_url=page_url, kind="pdf")],
    )
    state.artifacts = [EvidenceArtifact(
        kind="pdf", source_url=pdf_url, local_path=pdf_path,
        content_type="application/pdf", sha256="a" * 64, size_bytes=100,
        fetched_at="2026-08-05T00:00:00Z", metadata={"page_url": page_url},
    )]
    result = ToolResult(ok=True, data={
        "scope_id": 0,
        "matched_items": ["Project A"],
        "extra_items": ["Evidence-only project"],
        "pages": [{"page": 1, "text": "Project A\nProject B", "tables": []}],
    }, evidence_facts=[EvidenceFact(
        status="complete", award_name=state.award_name, year=state.year,
        target_match="yes", year_match="yes", document_complete=True,
        coverage_complete=True, matched_items=["Project A"],
    )])

    _route_pdf_result_to_scopes(state, result, local_path=pdf_path)

    assert [(fact.scope_id, fact.matched_items) for fact in result.evidence_facts] == [
        (31, ["Project A"]),
        (32, ["Project B"]),
    ]
    assert all(fact.coverage_complete for fact in result.evidence_facts)
    assert state.artifacts[0].metadata["extracted_count"] == 3
    assert state.artifacts[0].metadata["routed_scope_ids"] == [31, 32]


def test_pdf_route_matches_duplicate_titles_by_discriminator_text(tmp_path) -> None:  # noqa: ANN001
    _store, repository, batch_id = _repository(tmp_path)
    page_url = "https://official.example/notice"
    pdf_url = "https://official.example/combined.pdf"
    pdf_path = str(tmp_path / "combined.pdf")
    title = "生成式人工智能对大学生就业的影响及对策研究"
    displays = [
        f"{title};高天琦;东北农业大学",
        f"{title};王渤洋;南开大学",
        f"{title};钱婷婷;上海应用技术大学",
        f"{title};史耀媛;西安电子科技大学",
    ]
    state, _created = repository.create_or_get(_seed(batch_id).model_copy(update={
        "submitted_summary": {"role_scopes": [
            {
                "scope_id": 41, "role_type": "work_or_project", "required": True,
                "business_scope": {"XMLB": "青年基金项目"},
                "submitted_identity_count": 4,
                "submitted_identities": {
                    f"identity-{index}": display
                    for index, display in enumerate(displays)
                },
            },
            {
                "scope_id": 42, "role_type": "organization", "required": True,
                "business_scope": {"XMLB": "组织奖"},
                "submitted_identity_count": 1,
                "submitted_identities": {"org": "示例大学"},
            },
        ]},
    }))
    state.m4_evidence = M4EvidenceBundle(
        result_id=10, resource_code=state.resource_code,
        award_name=state.award_name, year=state.year,
        source_urls=[page_url],
        assets=[EvidenceAssetRecord(url=pdf_url, parent_url=page_url, kind="pdf")],
    )
    state.artifacts = [EvidenceArtifact(
        kind="pdf", source_url=pdf_url, local_path=pdf_path,
        content_type="application/pdf", sha256="a" * 64, size_bytes=100,
        fetched_at="2026-08-05T00:00:00Z", metadata={"page_url": page_url},
    )]
    page_text = "\n".join([
        f"{title} {person} {organization}"
        for person, organization in (
            ("高天琦", "东北农业大学"),
            ("王渤洋", "南开大学"),
            ("钱婷婷", "上海应用技术大学"),
            ("史耀媛", "西安电子科技大学"),
        )
    ])
    result = ToolResult(ok=True, data={
        "scope_id": 0, "matched_items": [title],
        "pages": [{"page": 1, "text": page_text, "tables": []}],
    }, evidence_facts=[EvidenceFact(
        status="complete", award_name=state.award_name, year=state.year,
        target_match="yes", year_match="yes", document_complete=True,
        coverage_complete=False, matched_items=[title],
    )])

    _route_pdf_result_to_scopes(state, result, local_path=pdf_path)

    project_fact = next(fact for fact in result.evidence_facts if fact.scope_id == 41)
    assert project_fact.matched_items == displays
    assert project_fact.coverage_complete is True


def test_single_scope_pdf_route_applies_discriminator_identity_contract(tmp_path) -> None:  # noqa: ANN001
    _store, repository, batch_id = _repository(tmp_path)
    page_url = "https://official.example/notice"
    pdf_url = "https://official.example/youth.pdf"
    pdf_path = str(tmp_path / "youth.pdf")
    title = "生成式人工智能对大学生就业的影响及对策研究"
    displays = [
        f"{title};高天琦;东北农业大学",
        f"{title};王渤洋;南开大学",
    ]
    state, _created = repository.create_or_get(_seed(batch_id).model_copy(update={
        "submitted_summary": {
            "official_domains": ["official.example"],
            "role_scopes": [{
                "scope_id": 51, "role_type": "work_or_project", "required": True,
                "business_scope": {"XMLB": "青年基金项目"},
                "submitted_identity_count": 2,
                "submitted_identities": {"one": displays[0], "two": displays[1]},
            }],
        },
    }))
    state.m4_evidence = M4EvidenceBundle(
        result_id=12, resource_code=state.resource_code,
        award_name=state.award_name, year=state.year,
        source_urls=[page_url],
        assets=[EvidenceAssetRecord(url=pdf_url, parent_url=page_url, kind="pdf")],
    )
    state.artifacts = [EvidenceArtifact(
        kind="pdf", source_url=pdf_url, local_path=pdf_path,
        content_type="application/pdf", sha256="b" * 64, size_bytes=100,
        fetched_at="2026-08-05T00:00:00Z", metadata={"page_url": page_url},
    )]
    result = ToolResult(ok=True, data={
        "scope_id": 51, "matched_items": [title],
        "pages": [{
            "page": 1,
            "text": (
                f"{title} 高天琦 东北农业大学\n"
                f"{title} 王渤洋 南开大学"
            ),
            "tables": [],
        }],
    }, evidence_facts=[EvidenceFact(
        scope_id=51, status="partial", award_name=state.award_name,
        year=state.year, target_match="yes", year_match="yes",
        document_complete=True, coverage_complete=False, matched_items=[title],
    )])

    _route_pdf_result_to_scopes(state, result, local_path=pdf_path)

    assert len(result.evidence_facts) == 1
    assert result.evidence_facts[0].matched_items == displays
    assert result.evidence_facts[0].coverage_complete is True
    assert result.evidence_facts[0].source_level == "official_primary"


def test_downloaded_m4_asset_keeps_verified_parent_for_multi_scope_routes(
    tmp_path,
) -> None:  # noqa: ANN001
    store, repository, batch_id = _repository(tmp_path)
    page_url = "https://official.example/notice"
    pdf_url = "https://official.example/list.pdf"
    state, _created = repository.create_or_get(_seed(batch_id))
    state.submitted_summary["role_scopes"] = [
        {
            "scope_key": "work:planning", "role_type": "work_or_project",
            "role_label": "Planning", "required": True,
            "business_scope": {"XMLB": "Planning"},
            "submitted_row_count": 1, "submitted_identity_count": 1,
            "submitted_identities": {"a": "Project A"},
        },
        {
            "scope_key": "work:youth", "role_type": "work_or_project",
            "role_label": "Youth", "required": True,
            "business_scope": {"XMLB": "Youth"},
            "submitted_row_count": 1, "submitted_identity_count": 1,
            "submitted_identities": {"b": "Project B"},
        },
    ]
    state.m4_evidence = M4EvidenceBundle(
        result_id=9,
        resource_code=state.resource_code,
        award_name=state.award_name,
        year=state.year,
        source_urls=[page_url],
        assets=[EvidenceAssetRecord(url=pdf_url, parent_url=page_url, kind="pdf")],
    )
    state.artifacts = [EvidenceArtifact(
        kind="pdf",
        source_url=pdf_url,
        local_path=str(tmp_path / "list.pdf"),
        content_type="application/pdf",
        sha256="a" * 64,
        size_bytes=100,
        fetched_at="2026-08-05T00:00:00Z",
        metadata={"page_url": page_url},
    )]
    repository.start_attempt(state, kind="initial", supplement_request="")
    repository.save(state)

    routes = store.list_evidence_asset_routes(state.case_id)
    assert {route["scope_key"] for route in routes} == {
        "work:planning", "work:youth",
    }
    assert all(route["route_status"] == "routed" for route in routes)


def test_search_candidates_exclude_non_roster_notices_and_bound_queue(tmp_path) -> None:  # noqa: ANN001
    _store, repository, batch_id = _repository(tmp_path)
    state, _created = repository.create_or_get(_seed(batch_id).model_copy(update={
        "award_name": "全国研究生渔菁英挑战赛",
        "year": "2025",
        "submitted_summary": {"role_scopes": [{
            "role_type": "team", "required": True,
        }]},
    }))
    result = ToolResult(ok=True, data={"candidates": [
        {"url": "https://example.cn/test", "title": "2025年决赛试题通知"},
        {"url": "https://example.cn/host", "title": "征集承办单位邀请函"},
        {"url": "https://example.cn/org", "title": "优秀组织奖获奖名单"},
        {"url": "https://example.cn/result-1", "title": "2025年获奖名单公示"},
        {"url": "https://example.cn/result-2", "title": "2025年决赛结果"},
        {"url": "https://example.cn/result-3", "title": "2025年拟获奖名单"},
        {"url": "https://example.cn/result-4", "title": "2025年入选名单"},
    ]})

    _queue_search_candidates(state, result)

    assert len(state.evidence_progress.pending_urls()) <= 3
    assert all(
        candidate.status == "skipped"
        for candidate in state.evidence_progress.candidates
        if candidate.url.endswith(("/test", "/host", "/org"))
    )


def test_agent_context_does_not_embed_full_role_identity_sets(tmp_path) -> None:  # noqa: ANN001
    _store, repository, batch_id = _repository(tmp_path)
    identities = {f"key-{index}": f"身份-{index}" for index in range(3270)}
    state, _created = repository.create_or_get(_seed(batch_id).model_copy(update={
        "submitted_summary": {
            "role_scopes": [{
                "scope_key": "team:2025", "role_type": "team",
                "role_label": "参赛队伍", "required": True,
                "submitted_row_count": 5109, "submitted_identity_count": 3270,
                "submitted_identities": identities,
            }],
            "unresolved_items": list(identities.values()),
        },
    }))

    context = _turn_context(state, [], max_observation_chars=8_000)
    payload = json.dumps(context.model_dump(mode="json"), ensure_ascii=False)

    assert len(payload) < 20_000
    assert "身份-3269" not in payload
    assert context.case["submitted_summary"]["role_scopes"][0][
        "submitted_identity_count"
    ] == 3270


def test_assets_share_parent_group_and_budget_stop_cannot_succeed(tmp_path) -> None:  # noqa: ANN001
    store, repository, batch_id = _repository(tmp_path)
    state, _created = repository.create_or_get(_seed(batch_id).model_copy(update={
        "submitted_summary": {"role_scopes": [{
            "scope_key": "team:2025", "role_type": "team", "role_label": "队伍",
            "required": True, "submitted_row_count": 2,
            "submitted_identity_count": 2,
            "submitted_identities": {"a": "队伍甲", "b": "队伍乙"},
        }]},
    }))
    repository.start_attempt(state, kind="initial", supplement_request="")
    parent = "https://official.example.cn/notice"
    store.sync_evidence_ledger(
        state.case_id,
        state.active_attempt_id,
        known_urls=[parent],
        candidates=[],
        asset_records=[
            {"url": f"{parent}/1.png", "parent_url": parent, "kind": "image", "status": "processed"},
            {"url": f"{parent}/2.png", "parent_url": parent, "kind": "image", "status": "processed"},
        ],
        artifacts=[],
        scope={"year": "2025"},
    )

    groups = store.list_evidence_groups(state.case_id)
    assert len(groups) == 1
    assert groups[0]["parent_url"] == parent
    assert groups[0]["expected_assets"] == 2
    assert groups[0]["terminal_assets"] == 2
    assert {
        row["parent_url"]
        for row in store.conn.execute(
            "SELECT parent_url FROM evidence_asset_task WHERE case_id=?",
            (state.case_id,),
        )
    } == {parent}

    repository.finish_attempt(
        state, stopped_reason="agent_token_budget_exhausted"
    )
    attempt = store.list_audit_attempts(state.case_id)[-1]
    assert attempt["status"] == "incomplete"
    assert attempt["conclusion_readiness"] == "incomplete"
    assert "agent_token_budget_exhausted" in attempt["blockers"]


def test_large_image_queue_gets_bounded_wall_time_margin(tmp_path) -> None:  # noqa: ANN001
    _store, repository, batch_id = _repository(tmp_path)
    state, _created = repository.create_or_get(_seed(batch_id))
    original = state.budget.limits.wall_time_seconds

    state.evidence_progress.pending_media_urls = [
        f"https://official.example/page-{index}.jpg" for index in range(60)
    ]
    _calibrate_media_wall_time(state)

    assert original == 8 * 60
    assert state.budget.limits.wall_time_seconds == 14 * 60
    assert state.budget.limits.max_vision_pages == 80
    assert state.budget.limits.max_calls == 24


def test_official_candidates_hide_search_from_the_next_agent_turn(tmp_path) -> None:  # noqa: ANN001
    _store, repository, batch_id = _repository(tmp_path)
    state, _created = repository.create_or_get(_seed(batch_id))
    registry = ToolRegistry()
    register_fake_tool(
        registry,
        "search_official_award",
        [ToolResult(ok=True, data={"candidate_count": 2, "official_candidate_count": 1})],
    )
    client = FakeAgentClient([
        NextAction(
            action="call_tool",
            tool_name="search_official_award",
            arguments={"award_name": "示例奖"},
        ),
        NextAction(action="manual", reason_summary="候选需人工核验"),
    ])

    outcome = EvidenceHarness(
        repository=repository,
        registry=registry,
        agent_client=client,
        allowed_roots=[tmp_path],
    ).run(state.case_id)

    assert outcome.stopped_reason == "agent_requested_manual"
    assert "official_search_candidates_ready" in outcome.state.reason_codes
    assert client.calls[0]["tool_names"] == ["search_official_award"]
    assert client.calls[1]["tool_names"] == []


def test_repeated_search_after_official_candidates_is_blocked_before_executor(
    tmp_path,
) -> None:  # noqa: ANN001
    _store, repository, batch_id = _repository(tmp_path)
    state, _created = repository.create_or_get(_seed(batch_id))
    registry = ToolRegistry()
    searched = register_fake_tool(
        registry,
        "search_official_award",
        [ToolResult(ok=True, data={"candidate_count": 2, "official_candidate_count": 1})],
    )
    repeated = NextAction(
        action="call_tool",
        tool_name="search_official_award",
        arguments={"award_name": "示例奖"},
    )

    outcome = EvidenceHarness(
        repository=repository,
        registry=registry,
        agent_client=FakeAgentClient([repeated, repeated]),
        allowed_roots=[tmp_path],
    ).run(state.case_id)

    assert outcome.stopped_reason == "bounded_search_limit_reached"
    assert len(searched.calls) == 1
    assert len(outcome.state.tool_trace) == 1


def test_case_seed_bridges_only_l5p_and_l5s_review_issues() -> None:
    handoff = SearchHandoff(
        resource_code="04050014",
        award_name="示例奖",
        year="2025",
        trigger_code="SOURCE_UNREACHABLE",
        objective="寻找官方替代页",
        known_urls=["https://example.gov.cn/old"],
    )
    assert seed_from_search_handoff(2, handoff).trigger_codes == ["SOURCE_UNREACHABLE"]

    imported = ImportedFile(
        batch="b",
        path="x.xlsx",
        file_name="A-示例奖-2025.xlsx",
        claimed_table_code="A",
        award_name="示例奖",
        year="2025",
        sheet_name="A",
        header_codes=["ZYLBM"],
        header_names=["资源项码"],
        rows=[["04050014"]],
    )
    l5s = make_issue(
        "L5S-01", batch="b", file="x.xlsx", message="疑似姓名语义异常"
    )
    assert seed_from_soft_rule(2, l5s, imported).trigger_codes == ["SOFT_RULE_SUSPECT"]
    l1 = make_issue("L1-10", batch="b", file="x.xlsx", message="需人工复核")
    assert seed_from_soft_rule(2, l1, imported) is None
    blocker = make_issue("L1-01", batch="b", file="x.xlsx", message="资源项码缺失")
    assert seeds_from_file_issues(2, imported, [l5s, blocker]) == []
    assert seeds_from_file_issues(2, imported, [l1]) == []


def test_migration_repository_idempotency_conflict_and_supplement(tmp_path) -> None:  # noqa: ANN001
    store, repository, batch_id = _repository(tmp_path)
    tables = {
        row[0]
        for row in store.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"schema_migration", "audit_case", "tool_trace", "evidence_artifact"} <= tables
    migration_versions = {
        str(row[0]) for row in store.conn.execute("SELECT version FROM schema_migration")
    }
    assert "0001_m5_harness" in migration_versions

    state, created = repository.create_or_get(_seed(batch_id))
    same, created_again = repository.create_or_get(_seed(batch_id))
    assert created and not created_again and state.case_id == same.case_id

    stale = repository.load(state.case_id)
    state.status = "waiting_human"
    repository.save(state)
    with pytest.raises(StateConflictError):
        repository.save(stale)

    queued = repository.request_supplement(
        state.case_id, " 请补查主管单位附件 ", expected_version=state.state_version
    )
    assert queued.status == "queued"
    assert queued.pending_supplement == "请补查主管单位附件"

    store.close()
    reopened = Store(tmp_path / "harness.db")
    reopened_versions = {
        str(row[0]) for row in reopened.conn.execute("SELECT version FROM schema_migration")
    }
    assert reopened_versions == migration_versions
    assert CaseRepository(reopened).load(state.case_id).pending_supplement


def test_active_case_identity_includes_year(tmp_path) -> None:  # noqa: ANN001
    store, repository, batch_id = _repository(tmp_path)
    seed_2025 = _seed(batch_id)
    seed_2023 = seed_2025.model_copy(update={"year": "2023"})

    case_2025, created_2025 = repository.create_or_get(seed_2025)
    case_2023, created_2023 = repository.create_or_get(seed_2023)
    same_2025, created_again = repository.create_or_get(seed_2025)

    assert created_2025 and created_2023 and not created_again
    assert case_2025.case_id != case_2023.case_id
    assert same_2025.case_id == case_2025.case_id
    index_sql = str(
        store.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' "
            "AND name='uq_audit_case_active'"
        ).fetchone()[0]
    )
    assert "year" in index_sql
    assert "trigger_key" not in index_sql  # P0-10：一 (批,码,年) 一活跃案，不再按 trigger 分


def test_supplement_reopens_completed_m5_batch_stage(tmp_path) -> None:  # noqa: ANN001
    store, repository, batch_id = _repository(tmp_path)
    state, _created = repository.create_or_get(_seed(batch_id))
    state.status = "waiting_human"
    repository.save(state)
    claimed = store.claim_batch_stage(batch_id, "m5", worker="first-m5")
    assert claimed is not None
    store.finish_batch_stage(
        batch_id,
        "m5",
        "done",
        worker="first-m5",
        expected_version=int(claimed["state_version"]),
    )

    queued = repository.request_supplement(
        state.case_id,
        "按最新名单差异规则重新核验",
        expected_version=state.state_version,
    )

    assert queued.status == "queued"
    stage = store.get_batch_stage_run(batch_id, "m5")
    assert stage is not None
    assert str(stage["status"]) == "pending"
    assert int(stage["attempt"]) == 1


def test_harness_persists_trace_artifact_and_redacts_model_observation(tmp_path) -> None:  # noqa: ANN001
    _store, repository, batch_id = _repository(tmp_path)
    state, _ = repository.create_or_get(_seed(batch_id))
    artifact = EvidenceArtifact(
        kind="html",
        source_url="https://example.gov.cn/list",
        local_path=str(tmp_path / "evidence.html"),
        content_type="text/html",
        sha256="a" * 64,
        size_bytes=12,
        fetched_at="2026-07-25T00:00:00Z",
    )
    registry = ToolRegistry()
    fake_tool = register_fake_tool(
        registry,
        "evidence_tool",
        [
            ToolResult(
                ok=True,
                data={
                    "api_key": "must-not-leak",
                    "url": "https://example.gov.cn/list?token=secret&year=2025",
                    "text": "x" * 6000,
                },
                artifacts=[artifact],
            )
        ],
    )
    client = FakeAgentClient(
        [
            NextAction(action="call_tool", tool_name="evidence_tool", arguments={"q": "x"}),
            NextAction(action="finish", reason_summary="证据已收集，待人工复核"),
        ]
    )
    outcome = _harness(tmp_path, repository, client, registry).run(state.case_id)
    assert outcome.stopped_reason == "recommendation_ready"
    assert outcome.state.status == "waiting_human" and outcome.state.confidence == "medium"
    assert fake_tool.calls == [{"q": "x"}]

    reloaded = repository.load(state.case_id)
    assert len(reloaded.tool_trace) == 1 and len(reloaded.artifacts) == 1
    observation = json.dumps(client.calls[1]["context"]["observations"], ensure_ascii=False)
    assert "must-not-leak" not in observation and "secret" not in observation
    assert "[REDACTED]" in observation
    assert len(observation) < 5000

    second = _harness(tmp_path, repository, FakeAgentClient([]), registry).run(state.case_id)
    assert second.stopped_reason == "awaiting_human_action"


@pytest.mark.parametrize(
    ("limits", "tool_limits", "decision", "expected"),
    [
        (
            HarnessLimits(max_steps=1),
            ToolBudgetLimits(),
            AgentDecision(
                action=NextAction(action="call_tool", tool_name="ok_tool"), route="fake"
            ),
            "agent_step_budget_exhausted",
        ),
        (
            HarnessLimits(max_tokens=5),
            ToolBudgetLimits(),
            AgentDecision(
                action=NextAction(action="finish"), token_used=6, route="fake"
            ),
            "agent_token_budget_exhausted",
        ),
        (
            HarnessLimits(),
            ToolBudgetLimits(max_calls=1),
            AgentDecision(
                action=NextAction(action="call_tool", tool_name="ok_tool"), route="fake"
            ),
            "tool_call_budget_exhausted",
        ),
    ],
)
def test_harness_budget_stops_are_waiting_human(
    tmp_path,  # noqa: ANN001
    limits: HarnessLimits,
    tool_limits: ToolBudgetLimits,
    decision: AgentDecision,
    expected: str,
) -> None:
    _store, repository, batch_id = _repository(tmp_path)
    state, _ = repository.create_or_get(_seed(batch_id), tool_limits=tool_limits)
    registry = ToolRegistry()
    register_fake_tool(registry, "ok_tool", [ToolResult(ok=True)])
    outcome = _harness(
        tmp_path,
        repository,
        FakeAgentClient([decision]),
        registry,
        limits=limits,
    ).run(state.case_id)
    assert outcome.stopped_reason == expected
    assert outcome.state.status == "waiting_human" and outcome.state.confidence == "low"


class _RequiredInput(BaseModel):
    value: str


def test_harness_contains_bad_inputs_unregistered_tools_and_failures(tmp_path) -> None:  # noqa: ANN001
    _store, repository, batch_id = _repository(tmp_path)
    state, _ = repository.create_or_get(_seed(batch_id))
    registry = ToolRegistry()
    register_fake_tool(
        registry,
        "required_tool",
        [ToolResult(ok=True)],
        input_model=_RequiredInput,
    )
    client = FakeAgentClient(
        [
            NextAction(action="call_tool", tool_name="required_tool"),
            NextAction(action="call_tool", tool_name="unknown_tool"),
        ]
    )
    outcome = _harness(tmp_path, repository, client, registry).run(state.case_id)
    assert outcome.stopped_reason == "consecutive_tool_failures"
    assert outcome.state.last_error == "TOOL_NOT_REGISTERED"
    assert [item.error_code for item in repository.load(state.case_id).tool_trace] == [
        "TOOL_INPUT_INVALID",
        "TOOL_NOT_REGISTERED",
    ]


def test_harness_wall_time_budget_stops_before_agent_call(tmp_path) -> None:  # noqa: ANN001
    _store, repository, batch_id = _repository(tmp_path)
    state, _ = repository.create_or_get(
        _seed(batch_id), tool_limits=ToolBudgetLimits(wall_time_seconds=0.001)
    )
    state.elapsed_ms = 1
    repository.save(state)
    client = FakeAgentClient([])
    outcome = _harness(tmp_path, repository, client, ToolRegistry()).run(state.case_id)
    assert outcome.stopped_reason == "wall_time_budget_exhausted"
    assert outcome.state.status == "waiting_human" and client.calls == []


def test_harness_client_error_and_manual_action_fail_closed(tmp_path) -> None:  # noqa: ANN001
    _store, repository, batch_id = _repository(tmp_path)
    state, _ = repository.create_or_get(_seed(batch_id))
    failed = _harness(
        tmp_path,
        repository,
        FakeAgentClient([AgentClientError("offline")]),
        ToolRegistry(),
    ).run(state.case_id)
    assert failed.state.status == "waiting_human"
    assert failed.stopped_reason == "agent_client_error"

    repository.request_supplement(
        state.case_id, "人工确认后重试", expected_version=failed.state.state_version
    )
    manual = _harness(
        tmp_path,
        repository,
        FakeAgentClient([NextAction(action="manual", reason_summary="需人工判定")]),
        ToolRegistry(),
    ).run(state.case_id)
    assert manual.stopped_reason == "agent_requested_manual"
    assert "人工补证要求" in manual.state.open_questions[-1]


def test_harness_accounts_failed_agent_usage_and_safe_detail(tmp_path) -> None:  # noqa: ANN001
    _store, repository, batch_id = _repository(tmp_path)
    state, _ = repository.create_or_get(_seed(batch_id))
    error = AgentOutputError(
        "raw model output must not persist",
        usages=[LlmTurnUsage(
            route="native",
            outcome="failed",
            provider_usage_reported=True,
            total_tokens=21,
            input_tokens=18,
            output_tokens=3,
        )],
        safe_detail="native_missing_required_function_call",
    )
    outcome = _harness(
        tmp_path,
        repository,
        FakeAgentClient([error]),
        ToolRegistry(),
    ).run(state.case_id)
    assert outcome.stopped_reason == "agent_output_invalid"
    assert outcome.state.token_used == 21
    assert outcome.state.llm_usage[0].outcome == "failed"
    assert outcome.state.last_error_detail == "native_missing_required_function_call"
    assert "raw model output" not in outcome.state.model_dump_json()


def test_repository_persists_agent_and_verifier_telemetry(tmp_path) -> None:  # noqa: ANN001
    _store, repository, batch_id = _repository(tmp_path)
    state, _ = repository.create_or_get(_seed(batch_id))
    state.llm_usage = [LlmTurnUsage(
        step=1,
        route="native",
        provider_usage_reported=True,
        total_tokens=11,
    )]
    state.verifier_llm_usage = [VerifierCallUsage(
        route="native",
        provider_usage_reported=True,
        total_tokens=7,
    )]
    state.last_error_detail = "safe_stage"
    repository.save(state)
    restored = repository.load(state.case_id)
    assert restored.llm_usage[0].total_tokens == 11
    assert restored.verifier_llm_usage[0].total_tokens == 7
    assert restored.last_error_detail == "safe_stage"
