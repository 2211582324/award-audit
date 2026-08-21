from __future__ import annotations

import pytest
from pydantic import ValidationError

from award_audit.agent.integration import AuditCaseInput, LocalIssueSummary
from award_audit.agent.review_agent.models import (
    AssetAssessment,
    IdentityAdjudication,
    ParsedAsset,
    ReviewCasePacket,
    ReviewOutcome,
    ReviewProtocolError,
    ScopeCandidate,
    SubmissionSummary,
    validate_review_outcome,
)
from award_audit.agent.review_agent.packet import build_review_case_packet
from award_audit.agent.toolkit.contracts import EvidenceAssetRecord


def test_identity_adjudication_requires_high_confidence_to_match() -> None:
    with pytest.raises(ValidationError):
        IdentityAdjudication(
            candidate_id="identity:1",
            decision="same_identity",
            confidence=0.89,
            reason="insufficient confidence",
        )


def _packet() -> ReviewCasePacket:
    return ReviewCasePacket(
        case_id=1,
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
        assets=[ParsedAsset(
            asset_id="asset-1",
            source_url="https://official.example/list.xlsx",
            kind="xlsx",
            status="parsed",
        )],
    )


def _included_assessment(**overrides: object) -> AssetAssessment:
    payload: dict[str, object] = {
        "asset_id": "asset-1",
        "role": "team",
        "scope_ids": [12],
        "material_relation": "primary",
        "version_relation": "same",
        "roster_contribution": "include",
        "confidence": 0.92,
        "reason": "页面附件明确标注为 2026 年获奖队伍名单。",
    }
    payload.update(overrides)
    return AssetAssessment(**payload)


def test_valid_compare_outcome_is_bound_to_packet() -> None:
    packet = _packet()
    outcome = ReviewOutcome(
        case_recommendation="compare",
        assessments=[_included_assessment()],
        selected_assets=["asset-1"],
        version_groups=[{
            "key": "2026-team-final",
            "asset_ids": ["asset-1"],
            "merge_allowed": True,
            "reason": "唯一主名单。",
        }],
        reason="有一份完整的主名单附件。",
    )

    assert validate_review_outcome(packet, outcome) is outcome


def test_mixed_role_assessment_is_bound_to_multiple_packet_roles() -> None:
    packet = ReviewCasePacket(
        case_id=1,
        resource_code="06020007",
        submission_summary=SubmissionSummary(submitted_rows=2),
        scopes=[
            ScopeCandidate(
                scope_id=1,
                scope_key="organization:best",
                source_role_type="organization",
                role="organization",
                role_label="organization",
            ),
            ScopeCandidate(
                scope_id=2,
                scope_key="person:recommendation",
                source_role_type="instructor_or_person",
                role="person",
                role_label="person",
            ),
        ],
        assets=[ParsedAsset(
            asset_id="asset-1",
            source_url="https://official.example/mixed.pdf",
            kind="pdf",
            status="parsed",
        )],
    )
    outcome = ReviewOutcome(
        case_recommendation="compare",
        assessments=[AssetAssessment(
            asset_id="asset-1",
            scope_ids=[1, 2],
            role="mixed",
            material_relation="primary",
            version_relation="same",
            roster_contribution="include",
            confidence=0.95,
            reason="A single official roster has separate organization and person sections.",
        )],
        selected_assets=["asset-1"],
        version_groups=[{
            "key": "mixed-current",
            "asset_ids": ["asset-1"],
            "merge_allowed": True,
            "reason": "single official roster",
        }],
        reason="compare each headed section with its bound scope",
    )

    assert validate_review_outcome(packet, outcome) is outcome


@pytest.mark.parametrize(
    "overrides",
    [
        {"confidence": 0.84},
        {"version_relation": "old"},
        {"scope_ids": []},
    ],
)
def test_invalid_included_relationships_fail_closed(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _included_assessment(**overrides)


def test_low_confidence_requires_manual_human_confirmation() -> None:
    assessment = AssetAssessment(
        asset_id="asset-1",
        role="team",
        material_relation="supplement",
        version_relation="same",
        roster_contribution="manual",
        confidence=0.5,
        reason="标题与目标接近，但页面缺少届次。",
        requires_human_confirmation=True,
    )

    assert assessment.roster_contribution == "manual"


def test_related_out_of_scope_requires_cross_scope_targets() -> None:
    assessment = AssetAssessment(
        asset_id="asset-1",
        role="team",
        scope_ids=[12],
        material_relation="related_out_of_scope",
        version_relation="independent",
        roster_contribution="cross_scope",
        confidence=0.95,
        reason="Official roster is for a different category in the same award year.",
    )

    assert assessment.roster_contribution == "cross_scope"

    with pytest.raises(ValidationError, match="related out-of-scope"):
        AssetAssessment(
            asset_id="asset-1",
            role="team",
            material_relation="related_out_of_scope",
            version_relation="independent",
            roster_contribution="exclude",
            confidence=0.95,
            reason="This would discard relevant official evidence.",
        )


def test_packet_rejects_unknown_scope_or_asset_references() -> None:
    packet = _packet()
    unknown_scope = ReviewOutcome(
        case_recommendation="manual",
        assessments=[_included_assessment(scope_ids=[99], roster_contribution="manual",
                                           confidence=0.9, requires_human_confirmation=True,
                                           version_relation="unknown")],
        reason="需要人工确认。",
    )
    with pytest.raises(ReviewProtocolError, match="unknown scope"):
        validate_review_outcome(packet, unknown_scope)

    unknown_asset = ReviewOutcome(
        case_recommendation="manual",
        assessments=[AssetAssessment(
            asset_id="asset-unknown",
            role="team",
            material_relation="supplement",
            version_relation="same",
            roster_contribution="manual",
            confidence=0.9,
            reason="待人工。",
            requires_human_confirmation=True,
        )],
        reason="需要人工确认。",
    )
    with pytest.raises(ReviewProtocolError, match="unknown asset"):
        validate_review_outcome(packet, unknown_asset)


def test_compare_cannot_hide_manual_asset() -> None:
    with pytest.raises(ValidationError, match="compare cannot contain"):
        ReviewOutcome(
            case_recommendation="compare",
            assessments=[
                _included_assessment(),
                AssetAssessment(
                    asset_id="asset-2",
                    role="team",
                    material_relation="supplement",
                    version_relation="same",
                    roster_contribution="manual",
                    confidence=0.9,
                    reason="待确认。",
                    requires_human_confirmation=True,
                ),
            ],
            selected_assets=["asset-1"],
            reason="存在未决资产。",
        )


def test_packet_adapter_maps_existing_case_context_and_assets() -> None:
    context = AuditCaseInput(
        resource_code="04050014",
        award_name="示例奖",
        year="2026",
        submission_files=["submitted.xlsx"],
        submitted_rows=2,
        known_urls=["https://official.example/notice"],
        role_scopes=[{
            "scope_key": "instructor_or_person:final",
            "role_type": "instructor_or_person",
            "role_label": "指导教师/个人奖",
            "required": False,
            "business_scope": {"year": "2026", "group": "教师"},
            "submitted_row_count": 2,
            "submitted_identity_count": 2,
        }],
        local_issues=[LocalIssueSummary(rule_id="L5P-05", message="来源待确认")],
    )
    asset = EvidenceAssetRecord(
        url="https://official.example/list.xlsx",
        parent_url="https://official.example/notice",
        label="获奖名单",
        kind="xlsx",
        status="parsed",
        sha256="a" * 64,
        metadata={
            "title": "2026 年获奖名单",
            "sample_rows": [["姓名", "单位"], ["张三", "示例大学"]],
            "anchors": ["Sheet1!A1:B2"],
        },
    )

    packet = build_review_case_packet(
        case_id=7,
        context=context,
        scope_ids_by_key={"instructor_or_person:final": 9},
        assets=[asset],
    )

    assert packet.scopes[0].role == "person"
    assert packet.scopes[0].source_role_type == "instructor_or_person"
    assert packet.known_urls[0].original_url == "https://official.example/notice"
    assert packet.assets[0].asset_id == f"sha256:{'a' * 64}"
    assert packet.assets[0].sample_rows[1] == ["张三", "示例大学"]
    assert packet.local_issues == ["L5P-05: 来源待确认"]


def test_packet_collapses_duplicate_m4_discovery_records() -> None:
    context = AuditCaseInput(
        resource_code="04050014",
        role_scopes=[{
            "scope_key": "organization:final",
            "role_type": "organization",
            "submitted_row_count": 1,
            "submitted_identity_count": 1,
        }],
    )
    first = EvidenceAssetRecord(
        url="https://official.example/list.xlsx",
        kind="xlsx",
        status="discovered",
        sha256="b" * 64,
    )
    parsed_duplicate = first.model_copy(update={
        "status": "parsed",
        "local_path": "/tmp/list.xlsx",
        "metadata": {"summary": "完整名单", "sample_rows": [["单位"]]},
    })

    packet = build_review_case_packet(
        case_id=8,
        context=context,
        scope_ids_by_key={"organization:final": 10},
        assets=[first, parsed_duplicate],
    )

    assert len(packet.assets) == 1
    assert packet.assets[0].status == "parsed"
    assert packet.scopes[0].role == "organization"


def test_packet_keeps_html_child_image_for_semantic_routing() -> None:
    context = AuditCaseInput(
        resource_code="04030061",
        role_scopes=[{
            "scope_key": "team:final",
            "role_type": "team",
            "submitted_row_count": 1,
            "submitted_identity_count": 1,
        }],
    )
    image = EvidenceAssetRecord(
        url="https://official.example/list-01.jpg",
        parent_url="https://official.example/notice",
        kind="image",
        status="discovered",
        metadata={"m4_html_parent_roster_complete": True},
    )

    packet = build_review_case_packet(
        case_id=9,
        context=context,
        scope_ids_by_key={"team:final": 11},
        assets=[image],
    )

    assert len(packet.assets) == 1
    assert packet.assets[0].kind == "image"
    assert packet.assets[0].status == "discovered"
