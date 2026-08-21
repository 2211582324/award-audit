"""Offline M5.3 chain: search leads remain separate from fetched evidence."""

from __future__ import annotations

from award_audit.agent.toolkit import (
    FakeSearchProvider,
    SafeToolExecutor,
    SearchHit,
    SearchResponse,
    ToolExecutionContext,
    build_default_registry,
    web,
)


def test_search_lead_then_fetch_verified_page(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    provider = FakeSearchProvider([SearchResponse(
        provider="fake",
        query="fixture",
        hits=[SearchHit(
            title="教育部公示",
            url="https://www.moe.gov.cn/notice/2024",
            snippet="2024 年某竞赛获奖名单",
            rank=1,
        )],
    )])

    def fake_fetch(url: str, timeout: float = 15.0) -> web.PageContent:
        assert timeout == 15.0
        return web.PageContent(
            url=url,
            status=200,
            title="教育部正式公示",
            text="2024 年某竞赛完整获奖名单",
        )

    monkeypatch.setattr(web, "fetch_page", fake_fetch)
    registry = build_default_registry(search_provider_factory=lambda: provider)
    executor = SafeToolExecutor(registry)
    context = ToolExecutionContext.create([tmp_path])

    search = executor.execute("search_official_award", {
        "award_name": "某竞赛",
        "year": "2024",
        "organizer": "教育部",
        "official_domains": ["moe.gov.cn"],
    }, context)
    lead = search.data["candidates"][0]
    fetched = executor.execute("fetch_web_page", {"url": lead["url"]}, context)

    assert search.ok and search.artifacts == [] and lead["is_evidence"] is False
    assert fetched.ok and fetched.source_url == lead["url"]
    assert fetched.data["title"] == "教育部正式公示"
    assert context.budget.searches == 1 and context.budget.candidate_urls == 1
