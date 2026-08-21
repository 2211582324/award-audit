from __future__ import annotations

from collections import deque
from typing import Any

from award_audit.agent.review_agent.models import (
    ParsedAsset,
    ReviewCasePacket,
    ScopeCandidate,
    SubmissionSummary,
)
from award_audit.agent.review_agent.service import ReviewAgent


class FakeLlm:
    def __init__(self, responses: list[object]) -> None:
        self.responses = deque(responses)
        self.calls: list[dict[str, object]] = []

    def json_call(self, system: str, user: str, *, max_tokens: int) -> Any:
        self.calls.append({"system": system, "user": user, "max_tokens": max_tokens})
        return self.responses.popleft()


def _packet() -> ReviewCasePacket:
    return ReviewCasePacket(
        case_id=7,
        resource_code="04050014",
        award_name="示例奖",
        year="2026",
        submission_summary=SubmissionSummary(submitted_rows=2),
        scopes=[ScopeCandidate(
            scope_id=12,
            scope_key="team:final",
            source_role_type="team",
            role="team",
            role_label="参赛队伍",
            submitted_row_count=2,
            submitted_identity_count=2,
        )],
        assets=[
            ParsedAsset(
                asset_id="main",
                source_url="https://official.example/main.xlsx",
                kind="xlsx",
                status="parsed",
                summary="附件标题：2026 年获奖队伍名单。",
                sample_rows=[["作品", "单位"], ["甲", "甲大学"]],
                anchors=["Sheet1!A1:B2"],
            ),
            ParsedAsset(
                asset_id="old",
                source_url="https://official.example/old.pdf",
                kind="pdf",
                status="parsed",
                summary="标题：2025 年公示名单。",
            ),
            ParsedAsset(
                asset_id="supplement",
                source_url="https://official.example/supplement.pdf",
                kind="pdf",
                status="parsed",
                summary="标题：2026 年获奖队伍补充名单。",
            ),
        ],
    )


def _compare_outcome() -> dict[str, object]:
    return {
        "case_recommendation": "compare",
        "assessments": [
            {
                "asset_id": "main",
                "scope_ids": [12],
                "role": "team",
                "material_relation": "primary",
                "version_relation": "same",
                "roster_contribution": "include",
                "confidence": 0.95,
                "reason": "附件标题、年份和样例均指向目标获奖队伍名单。",
            },
            {
                "asset_id": "old",
                "scope_ids": [],
                "role": "team",
                "material_relation": "unrelated",
                "version_relation": "old",
                "roster_contribution": "exclude",
                "confidence": 0.99,
                "reason": "材料明确属于 2025 年，不是目标年度。",
            },
            {
                "asset_id": "supplement",
                "scope_ids": [],
                "role": "team",
                "material_relation": "unrelated",
                "version_relation": "independent",
                "roster_contribution": "exclude",
                "confidence": 0.99,
                "reason": "默认案例不使用该额外材料。",
            },
        ],
        "selected_assets": ["main"],
        "excluded_assets": {
            "old": "目标年度不符的旧版。",
            "supplement": "默认案例不使用该额外材料。",
        },
        "version_groups": [{
            "key": "2026-team-final",
            "asset_ids": ["main"],
            "merge_allowed": True,
            "reason": "唯一主名单。",
        }],
        "reason": "可用来源只包含目标年度主名单。",
    }


def test_agent_requests_bounded_material_then_returns_review_outcome() -> None:
    llm = FakeLlm([
        {
            "requests": [{
                "asset_id": "main",
                "subunit_id": "document",
                "content_kind": "spreadsheet_sheet",
                "reason": "确认表头和样例行是否为名单。",
            }],
            "reason": "主附件需要确认表格内容。",
        },
        _compare_outcome(),
    ])

    run = ReviewAgent(llm).run(_packet())

    assert run.outcome.case_recommendation == "compare"
    assert run.outcome.selected_assets == ["main"]
    assert run.trace.request_count == 1 and run.trace.supplement_rounds == 1
    assert run.excerpts[0].anchors == ["Sheet1!A1:B2"]
    assert len(llm.calls) == 2


def test_agent_provider_error_trace_is_truncated_and_redacted() -> None:
    class FailingLlm:
        def json_call(self, system: str, user: str, *, max_tokens: int) -> Any:
            del system, user, max_tokens
            raise RuntimeError("503 upstream api_key=secret-value " + "x" * 400)

    run = ReviewAgent(FailingLlm()).run(_packet())

    assert run.outcome.case_recommendation == "evidence_insufficient"
    assert "RuntimeError:503 upstream" in run.trace.blockers[0]
    assert "secret-value" not in run.trace.blockers[0]
    assert "api_key=[redacted]" in run.trace.blockers[0]
    assert len(run.trace.blockers[0]) <= 300


def test_unknown_asset_request_fails_closed_without_final_model_call() -> None:
    llm = FakeLlm([{
        "requests": [{
            "asset_id": "unknown",
            "subunit_id": "document",
            "content_kind": "pdf_section",
            "reason": "需要读取。",
        }],
        "reason": "需要材料。",
    }])

    run = ReviewAgent(llm).run(_packet())

    assert run.outcome.case_recommendation == "evidence_insufficient"
    assert "unknown asset" in run.trace.blockers[0]
    assert len(llm.calls) == 1


def test_case_outcome_json_is_not_accepted_as_a_review_plan() -> None:
    packet = _packet()
    agent = ReviewAgent(FakeLlm([{
        "case_id": packet.case_id,
        "resource_code": packet.resource_code,
        "content_requests": [],
    }]))

    run = agent.run(packet)

    assert run.outcome.case_recommendation == "evidence_insufficient"
    assert run.trace.blockers == [
        "review_plan_validation_failed:reason:missing:Field required,case_id:extra_forbidden:Extra inputs are not permitted,resource_code:extra_forbidden:Extra inputs are not permitted,content_requests:extra_forbidden:Extra inputs are not permitted"
    ]


def test_alternate_case_review_json_is_not_accepted_as_an_outcome() -> None:
    llm = FakeLlm([
        {"requests": [], "reason": "资产索引足够。"},
        {
            "overall_assessment": "可比较",
            "asset_reviews": [],
            "scope_reviews": [],
        },
    ])

    run = ReviewAgent(llm).run(_packet())

    assert run.outcome.case_recommendation == "evidence_insufficient"
    assert run.trace.blockers == [
        "review_outcome_validation_failed:case_recommendation:missing:Field required,reason:missing:Field required,overall_assessment:extra_forbidden:Extra inputs are not permitted,asset_reviews:extra_forbidden:Extra inputs are not permitted,scope_reviews:extra_forbidden:Extra inputs are not permitted"
    ]


def test_mismatched_content_kind_request_fails_closed() -> None:
    llm = FakeLlm([{
        "requests": [{
            "asset_id": "main",
            "subunit_id": "document",
            "content_kind": "pdf_section",
            "reason": "错误地要求按 PDF 读取 XLSX。",
        }],
        "reason": "需要材料。",
    }])

    run = ReviewAgent(llm).run(_packet())

    assert run.outcome.case_recommendation == "evidence_insufficient"
    assert "content kind does not match" in run.trace.blockers[0]
    assert len(llm.calls) == 1


def test_agent_fails_closed_when_semantic_asset_budget_is_exhausted() -> None:
    llm = FakeLlm([{
        "requests": [{
            "asset_id": "main",
            "subunit_id": "document",
            "content_kind": "spreadsheet_sheet",
            "reason": "需要确认表头。",
        }],
        "reason": "需要材料。",
    }])

    run = ReviewAgent(llm, max_material_requests=0).run(_packet())

    assert run.outcome.case_recommendation == "evidence_insufficient"
    assert run.trace.blockers == ["semantic_asset_budget_exhausted"]
    assert len(llm.calls) == 1


def test_invalid_low_confidence_include_fails_closed() -> None:
    invalid = _compare_outcome()
    assessments = invalid["assessments"]
    assert isinstance(assessments, list)
    assessments[0]["confidence"] = 0.5
    llm = FakeLlm([{"requests": [], "reason": "索引足够。"}, invalid])

    run = ReviewAgent(llm).run(_packet())

    assert run.outcome.case_recommendation == "evidence_insufficient"
    assert run.trace.blockers


def test_failed_asset_cannot_contribute_even_if_model_selects_it() -> None:
    packet = _packet()
    packet.assets[0].status = "failed"
    llm = FakeLlm([{"requests": [], "reason": "索引足够。"}, _compare_outcome()])

    run = ReviewAgent(llm).run(packet)

    assert run.outcome.case_recommendation == "evidence_insufficient"
    assert "included evidence must be parsed" in run.trace.blockers[0]


def test_url_migration_can_contribute_when_scope_and_version_are_clear() -> None:
    outcome = _compare_outcome()
    assessments = outcome["assessments"]
    assert isinstance(assessments, list)
    assessments[0]["material_relation"] = "url_migration"
    llm = FakeLlm([{"requests": [], "reason": "索引足够。"}, outcome])

    run = ReviewAgent(llm).run(_packet())

    assert run.outcome.case_recommendation == "compare"
    assert run.outcome.assessments[0].material_relation == "url_migration"


def test_primary_and_supplement_can_merge_within_one_version_group() -> None:
    outcome = _compare_outcome()
    assessments = outcome["assessments"]
    assert isinstance(assessments, list)
    supplement = assessments[2]
    assert isinstance(supplement, dict)
    supplement.update({
        "scope_ids": [12],
        "material_relation": "supplement",
        "version_relation": "revision",
        "roster_contribution": "include",
        "reason": "标题表明这是目标年度同类别的补充名单。",
    })
    outcome["selected_assets"] = ["main", "supplement"]
    outcome["excluded_assets"] = {"old": "目标年度不符的旧版。"}
    groups = outcome["version_groups"]
    assert isinstance(groups, list)
    groups[0]["asset_ids"] = ["main", "supplement"]

    llm = FakeLlm([{"requests": [], "reason": "索引足够。"}, outcome])
    run = ReviewAgent(llm).run(_packet())

    assert run.outcome.case_recommendation == "compare"
    assert run.outcome.selected_assets == ["main", "supplement"]
