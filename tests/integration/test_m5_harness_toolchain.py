"""Offline M5.4 Agent loop over the real search/fetch Tool Registry."""

from __future__ import annotations

from award_audit.agent.harness.client import FakeAgentClient
from award_audit.agent.harness.models import CaseSeed, NextAction
from award_audit.agent.harness.persistence import CaseRepository
from award_audit.agent.harness.runner import EvidenceHarness
from award_audit.agent.toolkit import (
    FakeSearchProvider,
    SearchHit,
    SearchResponse,
    build_default_registry,
    web,
)
from award_audit.core.pipeline.store import Store


def test_fake_agent_search_fetch_finish_and_restart(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    db_path = tmp_path / "m5-harness.db"
    store = Store(db_path)
    batch_id = store.create_batch("m5.4-integration")
    repository = CaseRepository(store)
    state, _ = repository.create_or_get(CaseSeed(
        batch_id=batch_id,
        resource_code="04050014",
        award_name="某竞赛",
        year="2024",
        trigger_codes=["SOURCE_URL_MISSING"],
        objective="查找并核验主管部门正式公示",
    ))
    official_url = "https://www.moe.gov.cn/notice/2024"
    provider = FakeSearchProvider([SearchResponse(
        provider="fake",
        query="fixture",
        hits=[SearchHit(
            title="教育部公示",
            url=official_url,
            snippet="2024 年某竞赛获奖名单",
            rank=1,
        )],
    )])

    def fake_fetch(url: str, timeout: float = 15.0) -> web.PageContent:
        assert url == official_url and timeout == 15.0
        return web.PageContent(
            url=url,
            status=200,
            title="教育部正式公示",
            text="2024 年某竞赛完整获奖名单",
        )

    monkeypatch.setattr(web, "fetch_page", fake_fetch)
    registry = build_default_registry(search_provider_factory=lambda: provider)
    client = FakeAgentClient([
        NextAction(
            action="call_tool",
            tool_name="search_official_award",
            arguments={
                "award_name": "某竞赛",
                "year": "2024",
                "organizer": "教育部",
                "official_domains": ["moe.gov.cn"],
            },
        ),
        NextAction(action="finish", reason_summary="官方公示可访问，交由人工核对名单"),
    ])
    outcome = EvidenceHarness(
        repository=repository,
        registry=registry,
        agent_client=client,
        allowed_roots=[tmp_path],
    ).run(state.case_id)

    assert outcome.stopped_reason == "bounded_search_candidates_exhausted"
    assert outcome.state.status == "waiting_human"
    assert provider.calls[0]["operation"] == "search"
    assert len(client.calls) == 1
    assert client.calls[0]["tool_names"] == [
        "fetch_web_page",
        "search_official_award",
        "collect_spreadsheet_attachments",
        "download_evidence",
    ]
    store.close()
    reopened = Store(db_path)
    restored = CaseRepository(reopened).load(state.case_id)
    assert restored.status == "waiting_human"
    assert restored.budget.calls == 2 and restored.budget.searches == 1
    assert [item.tool_name for item in restored.tool_trace] == [
        "search_official_award",
        "fetch_web_page",
    ]
