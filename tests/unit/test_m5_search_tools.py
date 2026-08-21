"""M5.3 provider, provenance, Registry and privacy-boundary tests."""

from __future__ import annotations

import json
from typing import Any

from award_audit.agent.toolkit import (
    AnySearchProvider,
    BingHtmlSearchProvider,
    FakeSearchProvider,
    FallbackSearchProvider,
    SafeToolExecutor,
    SearchHit,
    SearchResponse,
    ToolBudgetLimits,
    ToolExecutionContext,
    build_default_registry,
)
from award_audit.agent.toolkit.provenance import (
    canonicalize_candidate_url,
    classify_source,
)
from award_audit.agent.toolkit.search import (
    SearchQuotaError,
    SearchUnavailableError,
)
from award_audit.agent.toolkit.registry import (
    SearchOfficialAwardInput,
    _official_search_query,
)


class _Response:
    def __init__(
        self,
        *,
        status: int = 200,
        payload: dict[str, Any] | None = None,
        text: str = "",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status
        self._payload = payload or {}
        self.text = text
        self.headers = headers or {}

    def json(self) -> dict[str, Any]:
        return self._payload


class _Client:
    def __init__(self, response: _Response | list[_Response]) -> None:
        self.responses = response if isinstance(response, list) else [response]
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def _next_response(self) -> _Response:
        if len(self.responses) > 1:
            return self.responses.pop(0)
        return self.responses[0]

    def __enter__(self) -> _Client:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def post(self, url: str, **kwargs: Any) -> _Response:
        self.calls.append(("post", url, kwargs))
        return self._next_response()

    def get(self, url: str, **kwargs: Any) -> _Response:
        self.calls.append(("get", url, kwargs))
        return self._next_response()


class _Factory:
    def __init__(self, response: _Response | list[_Response]) -> None:
        self.client = _Client(response)
        self.kwargs: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> _Client:
        self.kwargs.append(kwargs)
        return self.client


def _search_response(*hits: SearchHit, provider: str = "fake") -> SearchResponse:
    return SearchResponse(provider=provider, query="fixture", hits=list(hits))


def _mcp_text(text: str, *, request_id: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {"content": [{"type": "text", "text": text}]},
    }


def _context(tmp_path, **limits):  # noqa: ANN001, ANN003
    return ToolExecutionContext.create([tmp_path], ToolBudgetLimits(**limits))


def test_anysearch_anonymous_and_lazy_environment_key(monkeypatch) -> None:  # noqa: ANN001
    payload = _mcp_text(
        """## Search Results (1 results, 12ms)

### 1. 中国专利奖公示
- **URL**: https://www.cnipa.gov.cn/art/1
- 2025 年获奖名单
""",
        request_id="req-1",
    )
    monkeypatch.delenv("ANYSEARCH_API_KEY", raising=False)
    anonymous = _Factory(_Response(payload=payload))
    result = AnySearchProvider(client_factory=anonymous).search("中国专利奖 2025", max_results=1)
    assert result.request_id == "req-1" and result.hits[0].title == "中国专利奖公示"
    assert anonymous.client.calls[0][1] == "https://api.anysearch.com/mcp"
    assert anonymous.client.calls[0][2]["json"] == {
        "jsonrpc": "2.0",
        "id": "award-audit-search",
        "method": "tools/call",
        "params": {
            "name": "search",
            "arguments": {"query": "中国专利奖 2025", "max_results": 1},
        },
    }
    assert "Authorization" not in anonymous.kwargs[0]["headers"]
    assert not hasattr(result.hits[0], "content")

    monkeypatch.setenv("ANYSEARCH_API_KEY", "runtime-only-secret")
    authenticated = _Factory(_Response(payload=payload))
    AnySearchProvider(client_factory=authenticated).search("中国专利奖 2025", max_results=1)
    assert authenticated.kwargs[0]["headers"]["Authorization"] == (
        "Bearer runtime-only-secret"
    )


def test_anysearch_quota_error_does_not_expose_or_save_auto_key(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.delenv("ANYSEARCH_API_KEY", raising=False)
    factory = _Factory(_Response(
        status=402,
        payload={"data": {"api_key": "must-not-be-exposed", "password": "secret"}},
        headers={"x-request-id": "quota-1"},
    ))
    provider = AnySearchProvider(client_factory=factory)
    try:
        provider.search("某奖 2024")
    except SearchQuotaError as exc:
        assert "must-not-be-exposed" not in str(exc)
        assert "quota-1" in str(exc)
    else:
        raise AssertionError("HTTP 402 must be a structured quota error")


def test_anysearch_extract_falls_back_to_same_page_id_indexed_content() -> None:
    target = (
        "http://www.moe.gov.cn/jyb_xxgk/s5743/s5744/A10/202509/"
        "t20250905_1411955.html"
    )
    initial_extract = _Response(payload=_mcp_text(
        "extract unavailable", request_id="award-audit-extract"
    ))
    indexed_search = _Response(payload=_mcp_text(
        """## Search Results (3 results, 10ms)

### 1. 原失效地址
- **URL**: http://www.moe.gov.cn/jyb_xxgk/s5743/s5744/A10/202509/t20250905_1411955.html
- 原路径仍在索引中

### 2. 其他政府站点同名页面
- **URL**: https://evil.gov.cn/other/t20250905_1411955.html
- 无关

### 3. 教育部门户备用子域中的同一名单
- **URL**: https://hudong.moe.gov.cn/jyb_xxgk/s5743/s5744/202509/t20250905_1411955.html
- 同一页面的无 A10 变体
""",
        request_id="search-fallback",
    ))
    recovered_extract = _Response(payload=_mcp_text(
        "全国高校黄大年式教师团队 2025 入围名单 " * 2_000,
        request_id="award-audit-recovered-extract",
    ))
    factory = _Factory([initial_extract, indexed_search, recovered_extract])

    result = AnySearchProvider(client_factory=factory).extract(
        target,
        query_hint=(
            "site:moe.gov.cn 全国高校黄大年式教师团队 2025 第三批 认定名单 "
            "第四批 入围名单"
        ),
    )

    assert result.url == (
        "https://hudong.moe.gov.cn/jyb_xxgk/s5743/s5744/202509/"
        "t20250905_1411955.html"
    )
    assert result.text.startswith("全国高校黄大年式教师团队")
    assert len(result.text) > 30_000
    assert result.is_truncated is False
    assert "wrong content" not in result.text
    assert [call[1] for call in factory.client.calls] == [
        "https://api.anysearch.com/mcp",
        "https://api.anysearch.com/mcp",
        "https://api.anysearch.com/mcp",
    ]
    assert [
        call[2]["json"]["params"]["name"] for call in factory.client.calls
    ] == ["extract", "search", "extract"]


def test_anysearch_recovers_exact_page_between_trusted_education_hosts() -> None:
    page_id = "t20250905_2688410.shtml"
    target = f"https://www.edu.cn/news/{page_id}"
    recovered = f"https://www.cernet.edu.cn/archive/{page_id}"
    factory = _Factory([
        _Response(payload=_mcp_text("unavailable", request_id="extract-1")),
        _Response(payload=_mcp_text(
            f"""## Search Results (1 results, 5ms)

### 1. 全国高校黄大年式教师团队认定名单
- **URL**: {recovered}
- 2025年第三批认定名单
""",
            request_id="search-1",
        )),
        _Response(payload=_mcp_text(
            "全国高校黄大年式教师团队 2025 第三批认定名单 " * 2_000,
            request_id="extract-2",
        )),
    ])

    result = AnySearchProvider(client_factory=factory).extract(target)

    assert result.url == recovered
    assert len(result.text) > 30_000


def test_attachment_search_query_is_result_neutral() -> None:
    query = _official_search_query(SearchOfficialAwardInput(
        award_name="全国高校黄大年式教师团队",
        year="2025",
        strategy="attachment",
    ))

    assert "名单" in query
    assert "结果" not in query and "附件" not in query
    assert "候选人" not in query and "xlsx" not in query


def test_bing_html_provider_uses_same_contract() -> None:
    html = """
    <ol><li class="b_algo"><h2><a href="https://www.moe.gov.cn/a">教育部公示</a></h2>
    <div><p>2024 年获奖名单</p></div></li></ol>
    """
    response = BingHtmlSearchProvider(
        client_factory=_Factory(_Response(text=html))
    ).search("某竞赛 2024", max_results=3)
    assert response.provider == "bing"
    assert response.hits[0].url == "https://www.moe.gov.cn/a"
    assert response.hits[0].snippet == "2024 年获奖名单"


def test_fallback_and_fake_provider_record_calls() -> None:
    primary = FakeSearchProvider([SearchUnavailableError("down")])
    secondary = FakeSearchProvider([_search_response(
        SearchHit(title="官网", url="https://a.gov.cn/x", rank=1), provider="bing"
    )])
    response = FallbackSearchProvider(primary, secondary).search("某奖 2024")
    assert response.provider == "bing"
    assert response.warnings == ["fallback_from:fake:SEARCH_PROVIDER_UNAVAILABLE"]
    assert primary.calls[0]["operation"] == secondary.calls[0]["operation"] == "search"


def test_url_canonicalization_and_source_levels() -> None:
    canonical = canonicalize_candidate_url(
        "HTTPS://WWW.CNIPA.GOV.CN:443/a?utm_source=x&id=2#part"
    )
    assert canonical == "https://www.cnipa.gov.cn/a?id=2"
    primary = classify_source(
        "sub.acm.org", official_domains=["acm.org"], official_secondary_domains=[]
    )
    government = classify_source(
        "www.moe.gov.cn", official_domains=["acm.org"], official_secondary_domains=[]
    )
    school = classify_source(
        "news.example.edu.cn", official_domains=[], official_secondary_domains=[]
    )
    media = classify_source(
        "news.eol.cn", official_domains=[], official_secondary_domains=[]
    )
    vocational_publisher = classify_source(
        "www.chinazy.org", official_domains=[], official_secondary_domains=[]
    )
    broadcaster = classify_source(
        "edu.cnr.cn", official_domains=[], official_secondary_domains=[]
    )
    public_account = classify_source(
        "mp.weixin.qq.com", official_domains=[], official_secondary_domains=[]
    )
    aggregator = classify_source(
        "baidu.com", official_domains=[], official_secondary_domains=[]
    )
    assert primary.level == "official_primary"
    assert government.level == "official_secondary"
    assert school.level == "institutional_secondary"
    assert (
        media.level
        == vocational_publisher.level
        == broadcaster.level
        == public_account.level
        == "publisher_secondary"
    )
    assert aggregator.level == "media_or_aggregator"


def test_registry_search_deduplicates_rejects_unsafe_and_never_returns_evidence(tmp_path) -> None:
    provider = FakeSearchProvider([_search_response(
        SearchHit(
            title="中国专利奖 2025 公示",
            url="https://www.cnipa.gov.cn/a?utm_source=x&id=1",
            snippet="国家知识产权局获奖名单",
            rank=1,
        ),
        SearchHit(
            title="duplicate",
            url="https://www.cnipa.gov.cn/a?id=1#top",
            rank=2,
        ),
        SearchHit(title="unsafe", url="http://127.0.0.1/admin", rank=3),
    )])
    registry = build_default_registry(search_provider_factory=lambda: provider)
    result = SafeToolExecutor(registry).execute(
        "search_official_award",
        {
            "award_name": "中国专利奖",
            "year": "2025",
            "organizer": "国家知识产权局",
            "official_domains": ["cnipa.gov.cn"],
            "strategy": "broad",
        },
        _context(tmp_path),
    )
    assert result.ok and result.artifacts == []
    assert result.data["candidate_count"] == 1
    candidate = result.data["candidates"][0]
    assert candidate["source_level"] == "official_primary"
    assert candidate["is_evidence"] is False
    assert candidate["requires_fetch_verification"] is True
    assert "unsafe_or_invalid_candidates_rejected:1" in result.warnings


def test_search_input_privacy_manual_gate_budget_and_trace(tmp_path) -> None:
    response = _search_response(SearchHit(
        title="王某获奖新闻",
        url="https://school.example.edu.cn/news/1",
        snippet="学校转载名单",
        rank=1,
    ))
    provider = FakeSearchProvider([response])
    context = _context(tmp_path, max_calls=3, max_searches=3, max_candidate_urls=1)
    executor = SafeToolExecutor(build_default_registry(search_provider_factory=lambda: provider))

    rejected = executor.execute(
        "search_official_award",
        {"award_name": "某奖", "submitted_names": ["王某"]},
        context,
    )
    first = executor.execute("search_official_award", {"award_name": "某奖"}, context)
    second = executor.execute("search_official_award", {"award_name": "某奖"}, context)

    assert rejected.error_code == "TOOL_INPUT_INVALID"
    assert first.ok and first.data["manual_required"] is True
    assert second.error_code == "TOOL_BUDGET_EXCEEDED"
    assert len(provider.calls) == 1
    trace = json.dumps([item.model_dump() for item in context.trace], ensure_ascii=False)
    assert "王某获奖新闻" not in trace and "学校转载名单" not in trace
    assert first.data["query"] and len(first.data["query"]) <= 100


def test_search_excludes_previously_attempted_candidates_before_counting(tmp_path) -> None:
    stale_url = "https://www.moe.gov.cn/old/list.pdf"
    fresh_url = "https://www.moe.gov.cn/new/list.pdf"
    provider = FakeSearchProvider([_search_response(
        SearchHit(title="旧名单", url=stale_url, snippet="旧链接", rank=1),
        SearchHit(title="新名单", url=fresh_url, snippet="新链接", rank=2),
    )])
    executor = SafeToolExecutor(
        build_default_registry(search_provider_factory=lambda: provider)
    )

    result = executor.execute(
        "search_official_award",
        {"award_name": "某奖", "exclude_urls": [stale_url]},
        _context(tmp_path),
    )

    assert result.ok
    assert result.data["candidate_count"] == 1
    assert result.data["candidates"][0]["url"] == fresh_url
    assert "previously_attempted_candidates_excluded:1" in result.warnings


def test_search_rejects_only_explicitly_conflicting_candidate_years(
    tmp_path,
) -> None:  # noqa: ANN001
    provider = FakeSearchProvider([_search_response(
        SearchHit(
            title="首届全国教材建设奖名单",
            url="https://www.moe.gov.cn/2021-award.html",
            snippet="2021年奖励名单",
            rank=1,
        ),
        SearchHit(
            title="全国教材建设奖名单",
            url="https://www.moe.gov.cn/award.html",
            snippet="官方名单附件",
            rank=2,
        ),
        SearchHit(
            title="第二届全国教材建设奖名单",
            url="https://www.moe.gov.cn/2025-award.html",
            snippet="2025年拟奖励名单",
            rank=3,
        ),
        SearchHit(
            title="2025年教材管理工作通知",
            url="https://www.moe.gov.cn/2025-unrelated.html",
            snippet="2025年教育部通知",
            rank=4,
        ),
    )])
    executor = SafeToolExecutor(
        build_default_registry(search_provider_factory=lambda: provider)
    )

    result = executor.execute("search_official_award", {
        "award_name": "全国教材建设奖",
        "year": "2025",
        "strategy": "site",
        "official_domains": ["moe.gov.cn"],
        "require_award_name_match": True,
    }, _context(tmp_path))

    assert result.ok
    assert result.data["candidate_count"] == 2
    assert result.data["official_candidate_count"] == 2
    assert result.data["year_conflict_count"] == 1
    assert result.data["year_conflict_candidates"] == [{
        "title": "首届全国教材建设奖名单",
        "url": "https://www.moe.gov.cn/2021-award.html",
        "observed_years": ["2021"],
    }]
    assert result.data["unqualified_candidate_count"] == 1
    assert result.data["unqualified_candidates"][0]["url"] == (
        "https://www.moe.gov.cn/2025-unrelated.html"
    )
    assert "explicit_year_conflicts_rejected:1" in result.warnings
    assert "award_name_unmatched_candidates_rejected:1" in result.warnings


def test_strict_search_rejects_application_stage_for_any_award(tmp_path) -> None:
    application_url = "https://example.gov.cn/award/apply"
    result_url = "https://example.gov.cn/award/result"
    provider = FakeSearchProvider([_search_response(
        SearchHit(
            title="关于开展2025年全国青年公益创新竞赛创建活动的通知",
            url=application_url,
            snippet="请各单位填写申报表和推荐汇总表",
            rank=1,
        ),
        SearchHit(
            title="2025年全国青年公益创新竞赛认定名单公布",
            url=result_url,
            snippet="现将认定结果名单予以公布",
            rank=2,
        ),
    )])
    executor = SafeToolExecutor(
        build_default_registry(search_provider_factory=lambda: provider)
    )

    result = executor.execute("search_official_award", {
        "award_name": "全国青年公益创新竞赛",
        "year": "2025",
        "strategy": "site",
        "site_domains": ["example.gov.cn"],
        "require_award_name_match": True,
    }, _context(tmp_path))

    assert result.ok
    assert [item["url"] for item in result.data["candidates"]] == [result_url]
    assert result.data["result_stage_mismatch_count"] == 1
    assert result.data["result_stage_mismatch_candidates"][0]["url"] == application_url
    assert "result_stage_candidates_rejected:1" in result.warnings


def test_recovery_keeps_same_document_id_but_rejects_other_application_page(
    tmp_path,
) -> None:  # noqa: ANN001
    document_id = "t20250905_1411955.html"
    recovered_url = f"https://hudong.moe.gov.cn/archive/{document_id}"
    application_url = "https://www.moe.gov.cn/apply/t20250707_1196832.html"
    other_result_url = "https://www.moe.gov.cn/result/t20250819_1407464.html"
    provider = FakeSearchProvider([_search_response(
        SearchHit(
            title="第三批教师团队创建示范活动",
            url=recovered_url,
            snippet="组织开展第三批认定和第四批创建工作",
            rank=1,
        ),
        SearchHit(
            title="关于开展第四批教师团队创建活动的通知",
            url=application_url,
            snippet="组织认定、推荐名额、申报表、网上提交和材料报送",
            rank=2,
        ),
        SearchHit(
            title="第四批教师团队拟入围名单公示",
            url=other_result_url,
            snippet="经评审拟确定第四批教师团队，现予以公示",
            rank=3,
        ),
    )])
    executor = SafeToolExecutor(
        build_default_registry(search_provider_factory=lambda: provider)
    )

    result = executor.execute("search_official_award", {
        "award_name": "教师团队",
        "year": "2025",
        "strategy": "site",
        "site_domains": ["moe.gov.cn"],
        "recovery_terms": [document_id],
        "require_award_name_match": True,
    }, _context(tmp_path))

    assert result.ok
    assert [item["url"] for item in result.data["candidates"]] == [recovered_url]
    assert "document_id_match" in result.data["candidates"][0]["match_reasons"]
    assert result.data["result_stage_mismatch_candidates"][0]["url"] == application_url
    assert result.data["deferred_recovery_candidate_count"] == 1
    assert "nonmatching_recovery_candidates_deferred:1" in result.warnings


def test_unrelated_government_result_is_not_a_qualified_official_candidate(tmp_path) -> None:
    provider = FakeSearchProvider([_search_response(SearchHit(
        title="其他事项通知",
        url="https://www.moe.gov.cn/unrelated",
        snippet="与目标奖项无关",
        rank=1,
    ))])
    executor = SafeToolExecutor(build_default_registry(search_provider_factory=lambda: provider))
    result = executor.execute(
        "search_official_award",
        {"award_name": "中国专利奖", "year": "2025"},
        _context(tmp_path),
    )
    assert result.ok
    assert result.data["candidates"][0]["source_level"] == "official_secondary"
    assert result.data["official_candidate_count"] == 0
    assert result.data["manual_required"] is True


def test_site_and_international_queries_are_deterministic(tmp_path) -> None:
    provider = FakeSearchProvider([
        _search_response(),
        _search_response(),
    ])
    executor = SafeToolExecutor(build_default_registry(search_provider_factory=lambda: provider))
    context = _context(tmp_path, max_searches=3)
    site = executor.execute("search_official_award", {
        "award_name": "中国专利奖",
        "year": "2025",
        "strategy": "site",
        "official_domains": ["cnipa.gov.cn"],
    }, context)
    international = executor.execute("search_official_award", {
        "award_name": "图灵奖",
        "english_name": "ACM A.M. Turing Award",
        "year": "2024",
        "strategy": "international",
        "official_domains": ["acm.org"],
    }, context)
    assert site.ok and international.ok
    assert provider.calls[0]["query"].startswith("site:cnipa.gov.cn")
    assert provider.calls[1]["query"] == "ACM A.M. Turing Award 2024 winners official"


def test_discrepancy_query_uses_bounded_public_relationship_terms(tmp_path) -> None:
    relationship_url = "https://news.example.cn/relationship"
    provider = FakeSearchProvider([_search_response(SearchHit(
        title="最美教师群体代表扎根边疆的育人事迹",
        url=relationship_url,
        snippet="李某与王某共同作为群体代表接受采访",
        rank=1,
    ))])
    executor = SafeToolExecutor(build_default_registry(search_provider_factory=lambda: provider))

    result = executor.execute("search_official_award", {
        "award_name": "最美教师",
        "year": "2025",
        "strategy": "discrepancy",
        "require_award_name_match": True,
        "discrepancy_terms": [
            "李桂枝",
            "王伟江",
            "保定学院毕业生赴疆任教群体代表",
        ],
    }, _context(tmp_path, max_searches=3))

    assert result.ok
    assert provider.calls[0]["query"] == (
        "最美教师 2025 李桂枝 王伟江 保定学院毕业生赴疆任教群体代表 对应关系"
    )
    assert [item["url"] for item in result.data["candidates"]] == [relationship_url]
    assert result.data["result_stage_mismatch_count"] == 0


def test_attachment_query_requests_public_result_roster_without_format_bias(tmp_path) -> None:
    provider = FakeSearchProvider([_search_response()])
    executor = SafeToolExecutor(
        build_default_registry(search_provider_factory=lambda: provider)
    )

    result = executor.execute("search_official_award", {
        "award_name": "全国高校辅导员年度人物",
        "year": "2023",
        "strategy": "attachment",
    }, _context(tmp_path))

    assert result.ok
    assert provider.calls[0]["query"] == (
        "全国高校辅导员年度人物 2023 名单"
    )


def test_site_recovery_query_includes_failed_public_document_id(tmp_path) -> None:
    provider = FakeSearchProvider([_search_response()])
    executor = SafeToolExecutor(
        build_default_registry(search_provider_factory=lambda: provider)
    )

    result = executor.execute("search_official_award", {
        "award_name": "全国教材建设奖",
        "year": "2025",
        "strategy": "site",
        "site_domains": ["moe.gov.cn"],
        "recovery_terms": ["W020251103413802225668.pdf"],
    }, _context(tmp_path))

    assert result.ok
    assert provider.calls[0]["query"] == (
        "site:moe.gov.cn 全国教材建设奖 2025 "
        "W020251103413802225668.pdf 获奖名单 公示"
    )


def test_semantic_award_variant_is_candidate_but_not_promoted_to_evidence(
    tmp_path,
) -> None:  # noqa: ANN001
    related_url = "https://www.chinazy.org/info/1014/15997.htm"
    provider = FakeSearchProvider([_search_response(SearchHit(
        title="2023年‘最美大学生’‘最美高校辅导员’候选人公示",
        url=related_url,
        snippet="公示页面包含名单附件",
        rank=1,
    ))])
    executor = SafeToolExecutor(
        build_default_registry(search_provider_factory=lambda: provider)
    )

    result = executor.execute("search_official_award", {
        "award_name": "全国高校辅导员年度人物",
        "year": "2023",
        "strategy": "attachment",
        "require_award_name_match": True,
    }, _context(tmp_path))

    assert result.ok
    assert result.data["candidate_count"] == 1
    assert result.data["related_candidate_count"] == 0
    assert result.data["official_candidate_count"] == 0
    candidate = result.data["candidates"][0]
    assert candidate["url"] == related_url
    assert "award_name_match" in candidate["match_reasons"]
    assert candidate["is_evidence"] is False
    assert candidate["requires_fetch_verification"] is True
    assert result.data["next_action"] == "fetch_and_verify"
