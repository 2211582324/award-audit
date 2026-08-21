from __future__ import annotations

from pathlib import Path

import openpyxl

from award_audit.agent.harness.client import FakeAgentClient
from award_audit.agent.harness.models import CaseSeed, NextAction
from award_audit.agent.harness.persistence import CaseRepository
from award_audit.agent.harness.runner import (
    EvidenceHarness,
    _pending_pdf_inspection,
    _reuse_local_spreadsheet_evidence,
    _route_spreadsheet_result_to_scopes,
)
from award_audit.agent.toolkit.contracts import (
    EvidenceArtifact,
    EvidenceFact,
    ToolBudgetLimits,
    ToolObservation,
    ToolResult,
)
from award_audit.agent.toolkit.registry import ToolRegistry
from award_audit.agent.toolkit.safety import inspect_evidence_file
from award_audit.agent.verification import VerificationReport
from award_audit.core.pipeline.store import Store


def _case(tmp_path: Path) -> tuple[Store, CaseRepository, int]:
    store = Store(tmp_path / "workflow.db")
    batch_id = store.create_batch("workflow")
    repository = CaseRepository(store)
    state, _ = repository.create_or_get(
        CaseSeed(
            batch_id=batch_id,
            resource_code="03020004",
            award_name="示例奖",
            year="2025",
            trigger_codes=["COVERAGE_UNKNOWN"],
            objective="核对完整名单",
        ),
        tool_limits=ToolBudgetLimits(max_calls=24),
    )
    return store, repository, state.case_id


def test_supplement_creates_fresh_attempt_budget_and_keeps_history(tmp_path: Path) -> None:
    store, repository, case_id = _case(tmp_path)
    first = EvidenceHarness(
        repository=repository,
        registry=ToolRegistry(),
        agent_client=FakeAgentClient([NextAction(action="manual")]),
        allowed_roots=[tmp_path],
    ).run(case_id)
    state = repository.load(case_id)
    state.budget.calls = state.budget.limits.max_calls
    state.token_used = 50_000
    state.elapsed_ms = 480_000
    old_trace = ToolObservation(
        call_id="old-attempt-call",
        tool_name="inspect_pdf",
        started_at="2026-08-05T00:00:00Z",
        finished_at="2026-08-05T00:00:01Z",
        duration_ms=1000,
        input_summary={"path": "old.pdf"},
        output_summary={},
        ok=True,
    )
    repository.save(state, traces=[old_trace])
    state = repository.request_supplement(
        case_id,
        "重新核对遗漏附件",
        expected_version=state.state_version,
    )

    second = EvidenceHarness(
        repository=repository,
        registry=ToolRegistry(),
        agent_client=FakeAgentClient([NextAction(action="manual")]),
        allowed_roots=[tmp_path],
    ).run(case_id)

    attempts = store.list_audit_attempts(case_id)
    assert first.stopped_reason == "agent_requested_manual"
    assert second.stopped_reason == "agent_requested_manual"
    assert [item["sequence"] for item in attempts] == [1, 2]
    assert attempts[1]["kind"] == "supplement"
    assert attempts[1]["budget_usage"]["calls"] == 0
    assert attempts[1]["token_used"] < 50_000
    assert all(item["verifier_status"] == "persisted" for item in attempts)
    assert second.state.tool_trace == []


def test_asset_ledger_accounts_for_all_images_without_trace_limits(tmp_path: Path) -> None:
    store, repository, case_id = _case(tmp_path)
    state = repository.load(case_id)
    repository.start_attempt(state, kind="initial", supplement_request="")
    assets = [{
        "url": f"https://example.gov.cn/images/{index}.jpg",
        "parent_url": "https://example.gov.cn/notice/2025",
        "kind": "image",
        "status": "processed" if index < 30 else "discovered",
    } for index in range(60)]
    store.sync_evidence_ledger(
        case_id,
        state.active_attempt_id,
        known_urls=["https://example.gov.cn/notice/2025"],
        candidates=[],
        asset_records=assets,
        artifacts=[],
    )

    summary = store.evidence_workflow_summary(case_id)
    groups = store.list_evidence_groups(case_id)
    assert summary["assets"] == {
        "total": 60,
        "processed": 30,
        "failed": 0,
        "excluded": 0,
        "pending": 30,
    }
    assert summary["ledger_closed"] is False
    assert groups[0]["expected_assets"] == 60
    assert groups[0]["terminal_assets"] == 30


def test_ledger_preserves_four_processed_xlsx_assets_across_weak_resave(
    tmp_path: Path,
) -> None:
    store, repository, case_id = _case(tmp_path)
    state = repository.load(case_id)
    repository.start_attempt(state, kind="initial", supplement_request="")
    urls = [f"https://example.gov.cn/results/list-{index}.xlsx" for index in range(4)]
    parsed_assets = [
        {
            "url": url,
            "parent_url": "https://example.gov.cn/results/2023",
            "label": f"2023 roster {index}",
            "kind": "xlsx",
            "status": "parsed",
            "content_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "sha256": f"{index + 1:064x}",
            "local_path": str(tmp_path / f"list-{index}.xlsx"),
            "extraction_method": "m4_excel_discovery",
            "metadata": {"sample_rows": [["identity", str(index)]]},
        }
        for index, url in enumerate(urls)
    ]
    store.sync_evidence_ledger(
        case_id,
        state.active_attempt_id,
        known_urls=["https://example.gov.cn/results/2023"],
        candidates=[],
        asset_records=parsed_assets,
        artifacts=[],
    )

    store.sync_evidence_ledger(
        case_id,
        state.active_attempt_id,
        known_urls=["https://example.gov.cn/results/2023"],
        candidates=[],
        asset_records=[{"url": url, "kind": "xlsx", "status": "discovered"} for url in urls],
        artifacts=[
            {"source_url": url, "kind": "xlsx", "status": "downloaded", "metadata": {}}
            for url in urls
        ],
    )

    rows = store.conn.execute(
        "SELECT url,status,sha256,local_path,metadata_json FROM evidence_asset_task "
        "WHERE case_id=? ORDER BY url",
        (case_id,),
    ).fetchall()
    assert len(rows) == 4
    assert all(str(row["status"]) == "processed" for row in rows)
    assert all(len(str(row["sha256"])) == 64 for row in rows)
    assert all(str(row["local_path"]).endswith(".xlsx") for row in rows)
    assert all("sample_rows" in str(row["metadata_json"]) for row in rows)


def test_parent_url_correction_excludes_stale_collecting_group(tmp_path: Path) -> None:
    store, repository, case_id = _case(tmp_path)
    state = repository.load(case_id)
    repository.start_attempt(state, kind="initial", supplement_request="")
    asset_url = "https://example.gov.cn/files/roster.pdf"
    store.sync_evidence_ledger(
        case_id,
        state.active_attempt_id,
        known_urls=[],
        candidates=[],
        asset_records=[{
            "url": asset_url,
            "parent_url": asset_url,
            "label": "Roster",
            "kind": "pdf",
            "status": "discovered",
        }],
        artifacts=[],
    )
    assert store.evidence_workflow_summary(case_id)["groups_collecting"] == 1

    store.sync_evidence_ledger(
        case_id,
        state.active_attempt_id,
        known_urls=[],
        candidates=[],
        asset_records=[{
            "url": asset_url,
            "parent_url": "https://example.gov.cn/notice",
            "label": "Roster",
            "kind": "pdf",
            "status": "processed",
        }],
        artifacts=[],
    )

    groups = store.list_evidence_groups(case_id)
    assert {group["status"] for group in groups} == {"complete", "excluded"}
    assert store.evidence_workflow_summary(case_id)["groups_collecting"] == 0


def test_attempt_summary_ignores_routes_from_older_attempt(tmp_path: Path) -> None:
    store, repository, case_id = _case(tmp_path)
    state = repository.load(case_id)
    repository.start_attempt(state, kind="initial", supplement_request="")
    first_attempt_id = state.active_attempt_id
    store.sync_evidence_ledger(
        case_id,
        first_attempt_id,
        known_urls=[],
        candidates=[],
        asset_records=[{
            "url": "https://example.gov.cn/old-ambiguous.pdf",
            "parent_url": "https://example.gov.cn/old-notice",
            "kind": "pdf",
            "status": "failed",
            "routes": [{
                "scope_id": None,
                "subunit_type": "document",
                "route_source": "exact_rule",
                "confidence": 0.0,
                "route_status": "ambiguous",
                "reason": "fixture unresolved route",
            }],
        }],
        artifacts=[],
    )
    assert store.evidence_workflow_summary(
        case_id, attempt_id=first_attempt_id
    )["routes"]["ambiguous"] == 1
    store.finish_audit_attempt(
        first_attempt_id,
        status="incomplete",
        phase="verification",
        budget_usage={},
        step_count=1,
        token_used=0,
        elapsed_ms=1,
        stop_reason="fixture",
        verifier_status="persisted",
        conclusion_readiness="incomplete",
        blockers=["fixture"],
    )

    second = store.start_audit_attempt(
        case_id,
        kind="supplement",
        supplement_request="retry",
        budget_limits={},
    )
    second_attempt_id = int(second["attempt_id"])
    store.sync_evidence_ledger(
        case_id,
        second_attempt_id,
        known_urls=[],
        candidates=[],
        asset_records=[{
            "url": "https://example.gov.cn/decorative.png",
            "parent_url": "https://example.gov.cn/new-notice",
            "kind": "image",
            "status": "excluded",
            "routes": [{
                "scope_id": None,
                "subunit_type": "document",
                "route_source": "exact_rule",
                "confidence": 1.0,
                "route_status": "excluded",
                "reason": "decorative image",
            }],
        }],
        artifacts=[],
    )

    summary = store.evidence_workflow_summary(case_id, attempt_id=second_attempt_id)
    assert summary["routes"] == {"total": 1, "excluded": 1}
    assert not any("routes are unresolved" in item for item in summary["blockers"])


def test_comparison_ledger_persists_more_than_one_hundred_exact_differences(
    tmp_path: Path,
) -> None:
    store, repository, case_id = _case(tmp_path)
    state = repository.load(case_id)
    repository.start_attempt(state, kind="initial", supplement_request="")
    missing = [f"缺失名单-{index:03d}" for index in range(150)]
    matched = [f"匹配名单-{index:03d}" for index in range(200)]
    store.record_evidence_comparison(
        case_id,
        state.active_attempt_id,
        facts=[{
            "status": "partial",
            "source_url": "https://example.gov.cn/list.pdf",
            "expected_count": 350,
            "observed_count": 200,
            "matched_items": matched,
            "missing_items": missing,
            "missing_item_count": 150,
        }],
        fallback_missing=[],
        fallback_contradictions=[],
    )

    comparison = store.latest_evidence_comparison(case_id)
    assert comparison is not None
    assert comparison["submitted_count"] == 350
    assert comparison["matched_count"] == 200
    assert comparison["missing"] == missing
    assert store.conn.execute(
        "SELECT COUNT(*) FROM evidence_identity WHERE case_id=? AND origin='submitted'",
        (case_id,),
    ).fetchone()[0] == 350


def test_sibling_pdf_results_are_compared_as_one_evidence_group(tmp_path: Path) -> None:
    store, repository, case_id = _case(tmp_path)
    state = repository.load(case_id)
    repository.start_attempt(state, kind="initial", supplement_request="")
    identities = {"id-a": "A", "id-b": "B", "id-c": "C"}
    results = [
        ToolResult(ok=True, data={
            "evidence_group": "https://example.gov.cn/notice/2025",
            "document_complete": True,
            "submitted_identity_items": identities,
            "matched_identity_hashes": ["id-a", "id-b"],
        }),
        ToolResult(ok=True, data={
            "evidence_group": "https://example.gov.cn/notice/2025",
            "document_complete": True,
            "submitted_identity_items": identities,
            "matched_identity_hashes": ["id-c"],
        }),
    ]
    verification = VerificationReport(
        target_match="yes",
        year_match="yes",
        source_authority="official",
        coverage_complete="yes",
        recommended_action="accept_evidence",
        deterministic_action="accept_evidence",
    )

    repository.record_comparison(state, results, verification)

    comparison = store.latest_evidence_comparison(case_id)
    assert comparison is not None
    assert comparison["submitted_count"] == 3
    assert comparison["matched_count"] == 3
    assert comparison["missing"] == []


def test_attempt_without_comparison_cannot_be_ready_for_human(tmp_path: Path) -> None:
    store, repository, case_id = _case(tmp_path)
    state = repository.load(case_id)
    repository.start_attempt(state, kind="initial", supplement_request="")
    state.latest_verification = VerificationReport(
        target_match="yes",
        year_match="yes",
        source_authority="official",
        coverage_complete="yes",
        recommended_action="accept_evidence",
        deterministic_action="accept_evidence",
    )

    repository.finish_attempt(state, stopped_reason="finished")

    attempt = store.list_audit_attempts(case_id)[-1]
    assert attempt["status"] == "incomplete"
    assert attempt["conclusion_readiness"] == "incomplete"
    assert "comparison_missing" in attempt["blockers"]


def test_one_asset_can_route_to_multiple_scopes_and_close_each_group(tmp_path: Path) -> None:
    store, repository, case_id = _case(tmp_path)
    state = repository.load(case_id)
    state.submitted_summary["role_scopes"] = [
        {
            "scope_key": "organization:best", "role_type": "organization",
            "role_label": "最佳组织奖", "required": True,
            "business_scope": {"XMLB": "最佳组织奖"},
            "submitted_row_count": 1, "submitted_identity_count": 1,
            "submitted_identities": {"a": "甲单位"},
        },
        {
            "scope_key": "organization:excellent", "role_type": "organization",
            "role_label": "优秀组织奖", "required": True,
            "business_scope": {"XMLB": "优秀组织奖"},
            "submitted_row_count": 1, "submitted_identity_count": 1,
            "submitted_identities": {"b": "乙单位"},
        },
    ]
    repository.save(state)
    repository.start_attempt(state, kind="initial", supplement_request="")
    store.sync_evidence_ledger(
        case_id,
        state.active_attempt_id,
        known_urls=["https://example.gov.cn/notice"],
        candidates=[],
        asset_records=[{
            "url": "https://example.gov.cn/awards.pdf",
            "parent_url": "https://example.gov.cn/notice",
            "label": "最佳组织奖、优秀组织奖获奖名单",
            "kind": "pdf", "status": "processed",
        }],
        artifacts=[],
    )

    routes = store.list_evidence_asset_routes(case_id)
    assert {route["scope_key"] for route in routes} == {
        "organization:best", "organization:excellent",
    }
    assert all(route["route_status"] == "routed" for route in routes)
    assert len(store.list_evidence_groups(case_id)) == 2
    assert store.evidence_workflow_summary(case_id)["ledger_closed"] is True


def test_asset_title_suffix_variants_route_one_pdf_to_multiple_scopes(
    tmp_path: Path,
) -> None:
    store, repository, case_id = _case(tmp_path)
    state = repository.load(case_id)
    state.submitted_summary["role_scopes"] = [
        {
            "scope_key": "work:planning", "role_type": "work_or_project",
            "role_label": "Planning", "required": True,
            "business_scope": {"XMLB": "规划基金项目"},
            "submitted_row_count": 1, "submitted_identity_count": 1,
            "submitted_identities": {"a": "A"},
        },
        {
            "scope_key": "work:youth", "role_type": "work_or_project",
            "role_label": "Youth", "required": True,
            "business_scope": {"XMLB": "青年基金项目"},
            "submitted_row_count": 1, "submitted_identity_count": 1,
            "submitted_identities": {"b": "B"},
        },
    ]
    repository.save(state)
    repository.start_attempt(state, kind="initial", supplement_request="")
    repository.save(state)
    store.sync_evidence_ledger(
        case_id,
        state.active_attempt_id,
        known_urls=[],
        candidates=[],
        asset_records=[{
            "url": "https://example.gov.cn/combined.pdf",
            "parent_url": "https://example.gov.cn/results",
            "label": "规划基金、青年基金评审结果公示一览表",
            "kind": "pdf",
            "status": "processed",
        }],
        artifacts=[],
    )

    routes = store.list_evidence_asset_routes(case_id)
    assert {route["scope_key"] for route in routes} == {
        "work:planning", "work:youth",
    }


def test_resolved_asset_routes_replace_earlier_ambiguous_placeholder(
    tmp_path: Path,
) -> None:
    store, repository, case_id = _case(tmp_path)
    state = repository.load(case_id)
    repository.start_attempt(state, kind="initial", supplement_request="")
    store.sync_audit_scopes(case_id, [
        {
            "scope_key": "work:silver", "role_type": "work_or_project",
            "role_label": "银奖", "required": True,
            "business_scope": {"XMLB": "银奖"},
            "submitted_row_count": 1, "submitted_identity_count": 1,
            "submitted_identities": {"s": "S"},
        },
        {
            "scope_key": "work:bronze", "role_type": "work_or_project",
            "role_label": "铜奖", "required": True,
            "business_scope": {"XMLB": "铜奖"},
            "submitted_row_count": 1, "submitted_identity_count": 1,
            "submitted_identities": {"b": "B"},
        },
    ], identity_version="identity-v2")
    asset = {
        "url": "https://example.gov.cn/gold.pdf",
        "parent_url": "https://example.gov.cn/notice",
        "label": "金奖名单",
        "kind": "pdf",
        "status": "pending",
    }
    store.sync_evidence_ledger(
        case_id, state.active_attempt_id,
        known_urls=[], candidates=[], asset_records=[asset], artifacts=[],
    )
    assert store.list_evidence_asset_routes(case_id)[0]["route_status"] == "ambiguous"

    store.sync_audit_scopes(case_id, [{
        "scope_key": "work:gold", "role_type": "work_or_project",
        "role_label": "金奖", "required": True,
        "business_scope": {"XMLB": "金奖"},
        "submitted_row_count": 1, "submitted_identity_count": 1,
        "submitted_identities": {"a": "A"},
    }], identity_version="identity-v2")
    store.sync_evidence_ledger(
        case_id, state.active_attempt_id,
        known_urls=[], candidates=[], asset_records=[asset], artifacts=[],
    )

    routes = store.list_evidence_asset_routes(case_id)
    assert len(routes) == 1
    assert routes[0]["route_status"] == "routed"
    assert store.evidence_workflow_summary(case_id)["routes"].get("ambiguous", 0) == 0


def test_exact_routes_replace_earlier_generic_routed_scope(tmp_path: Path) -> None:
    store, repository, case_id = _case(tmp_path)
    state = repository.load(case_id)
    repository.start_attempt(state, kind="initial", supplement_request="")
    store.sync_audit_scopes(case_id, [
        {
            "scope_key": "work:generic", "role_type": "work_or_project",
            "role_label": "Generic", "required": True,
            "business_scope": {"ZYLBM": "05040003"},
            "submitted_row_count": 2, "submitted_identity_count": 2,
            "submitted_identities": {"g1": "G1", "g2": "G2"},
        },
        {
            "scope_key": "work:planning", "role_type": "work_or_project",
            "role_label": "Planning", "required": True,
            "business_scope": {"ZYLBM": "05040003", "XMLB": "Planning"},
            "submitted_row_count": 1, "submitted_identity_count": 1,
            "submitted_identities": {"p": "P"},
        },
        {
            "scope_key": "work:youth", "role_type": "work_or_project",
            "role_label": "Youth", "required": True,
            "business_scope": {"ZYLBM": "05040003", "XMLB": "Youth"},
            "submitted_row_count": 1, "submitted_identity_count": 1,
            "submitted_identities": {"y": "Y"},
        },
    ], identity_version="identity-v2")
    asset_url = "https://example.gov.cn/combined.pdf"
    store.sync_evidence_ledger(
        case_id, state.active_attempt_id,
        known_urls=[], candidates=[], asset_records=[{
            "url": asset_url, "parent_url": "https://example.gov.cn/notice",
            "label": "", "kind": "pdf", "status": "discovered",
            "scope_key": "work:generic",
        }], artifacts=[],
    )
    assert [route["scope_key"] for route in store.list_evidence_asset_routes(case_id)] == [
        "work:generic"
    ]

    store.sync_evidence_ledger(
        case_id, state.active_attempt_id,
        known_urls=[], candidates=[], asset_records=[{
            "url": asset_url, "parent_url": "https://example.gov.cn/notice",
            "label": "Planning and Youth", "kind": "pdf", "status": "processed",
        }], artifacts=[],
    )

    assert {route["scope_key"] for route in store.list_evidence_asset_routes(case_id)} == {
        "work:planning", "work:youth",
    }


def test_explicit_llm_ambiguous_route_replaces_earlier_exact_route(
    tmp_path: Path,
) -> None:
    store, repository, case_id = _case(tmp_path)
    state = repository.load(case_id)
    repository.start_attempt(state, kind="initial", supplement_request="")
    store.sync_audit_scopes(case_id, [{
        "scope_key": "work:generic", "role_type": "work_or_project",
        "role_label": "Generic", "required": True,
        "business_scope": {"ZYLBM": "05060001"},
        "submitted_row_count": 1, "submitted_identity_count": 1,
        "submitted_identities": {"g": "G"},
    }], identity_version="identity-v2")
    asset_url = "https://example.gov.cn/roster-02.png"
    store.sync_evidence_ledger(
        case_id, state.active_attempt_id,
        known_urls=[], candidates=[], asset_records=[{
            "url": asset_url, "parent_url": "https://example.gov.cn/notice",
            "label": "roster", "kind": "image", "status": "processed",
            "scope_key": "work:generic",
        }], artifacts=[],
    )
    first = store.list_evidence_asset_routes(case_id)
    assert len(first) == 1 and first[0]["route_status"] == "routed"

    store.sync_evidence_ledger(
        case_id, state.active_attempt_id,
        known_urls=[], candidates=[], asset_records=[{
            "url": asset_url, "parent_url": "https://example.gov.cn/notice",
            "label": "roster", "kind": "image", "status": "processed",
            "routes": [{
                "scope_id": None,
                "subunit_type": "image_page",
                "route_source": "llm",
                "confidence": 0.6,
                "route_status": "ambiguous",
                "reason": "page spans multiple business scopes",
            }],
        }], artifacts=[],
    )

    routes = store.list_evidence_asset_routes(case_id)
    assert len(routes) == 1
    assert routes[0]["route_status"] == "ambiguous"
    assert routes[0]["route_source"] == "llm"
    assert routes[0]["scope_id"] is None


def test_excluded_route_replaces_earlier_ambiguous_placeholder(tmp_path: Path) -> None:
    store, repository, case_id = _case(tmp_path)
    state = repository.load(case_id)
    repository.start_attempt(state, kind="initial", supplement_request="")
    store.sync_audit_scopes(case_id, [
        {
            "scope_key": "work:a", "role_type": "work_or_project",
            "role_label": "A", "required": True,
            "business_scope": {"XMLB": "A"}, "submitted_row_count": 1,
            "submitted_identity_count": 1, "submitted_identities": {"a": "A"},
        },
        {
            "scope_key": "work:b", "role_type": "work_or_project",
            "role_label": "B", "required": True,
            "business_scope": {"XMLB": "B"}, "submitted_row_count": 1,
            "submitted_identity_count": 1, "submitted_identities": {"b": "B"},
        },
    ], identity_version="identity-v2")
    asset = {
        "url": "https://example.gov.cn/header.png",
        "parent_url": "https://example.gov.cn/notice",
        "label": "", "kind": "image", "status": "discovered",
    }
    store.sync_evidence_ledger(
        case_id, state.active_attempt_id, known_urls=[], candidates=[],
        asset_records=[asset], artifacts=[],
    )
    assert store.list_evidence_asset_routes(case_id)[0]["route_status"] == "ambiguous"

    asset["status"] = "excluded"
    asset["routes"] = [{
        "scope_id": None, "subunit_type": "image_batch",
        "route_source": "exact_rule", "confidence": 1.0,
        "route_status": "excluded", "reason": "decorative image",
    }]
    store.sync_evidence_ledger(
        case_id, state.active_attempt_id, known_urls=[], candidates=[],
        asset_records=[asset], artifacts=[],
    )

    routes = store.list_evidence_asset_routes(case_id)
    assert len(routes) == 1
    assert routes[0]["route_status"] == "excluded"
    assert store.evidence_workflow_summary(case_id)["routes"].get("ambiguous", 0) == 0


def test_deterministically_excluded_asset_closes_route_without_explicit_route(
    tmp_path: Path,
) -> None:
    store, repository, case_id = _case(tmp_path)
    state = repository.load(case_id)
    repository.start_attempt(state, kind="initial", supplement_request="")
    store.sync_audit_scopes(case_id, [
        {
            "scope_key": "team:a", "role_type": "team", "role_label": "A",
            "required": True, "business_scope": {"ZB": "A"},
            "submitted_row_count": 1, "submitted_identity_count": 1,
            "submitted_identities": {"a": "A"},
        },
        {
            "scope_key": "team:b", "role_type": "team", "role_label": "B",
            "required": True, "business_scope": {"ZB": "B"},
            "submitted_row_count": 1, "submitted_identity_count": 1,
            "submitted_identities": {"b": "B"},
        },
    ], identity_version="identity-v2")
    store.sync_evidence_ledger(
        case_id, state.active_attempt_id, known_urls=[], candidates=[],
        asset_records=[{
            "url": "https://example.gov.cn/unrelated.pdf",
            "parent_url": "https://example.gov.cn/notice",
            "kind": "pdf", "status": "failed",
            "error_message": "UnsafeFileError: file type pdf is not allowed here",
        }], artifacts=[],
    )

    row = store.conn.execute(
        "SELECT status FROM evidence_asset_task WHERE case_id=?", (case_id,)
    ).fetchone()
    assert row["status"] == "excluded"
    routes = store.list_evidence_asset_routes(case_id)
    assert len(routes) == 1
    assert routes[0]["route_status"] == "excluded"
    assert not any(
        "routes are unresolved" in item
        for item in store.evidence_workflow_summary(case_id)["blockers"]
    )


def test_manual_agent_route_overrides_pdf_collection_exclusion(tmp_path: Path) -> None:
    store, repository, case_id = _case(tmp_path)
    state = repository.load(case_id)
    repository.start_attempt(state, kind="initial", supplement_request="")
    store.sync_audit_scopes(case_id, [{
        "scope_key": "team:a", "role_type": "team", "role_label": "A",
        "required": True, "business_scope": {"ZB": "A"},
        "submitted_row_count": 1, "submitted_identity_count": 1,
        "submitted_identities": {"a": "A"},
    }], identity_version="identity-v2")
    store.sync_evidence_ledger(
        case_id, state.active_attempt_id, known_urls=[], candidates=[],
        asset_records=[{
            "url": "https://example.gov.cn/unreadable.pdf",
            "parent_url": "https://example.gov.cn/notice",
            "kind": "pdf", "status": "failed",
            "error_message": "UnsafeFileError: file type pdf is not allowed here",
            "routes": [{
                "scope_id": None, "route_source": "llm", "confidence": 0.9,
                "route_status": "ambiguous",
                "selector": {
                    "roster_contribution": "manual",
                    "requires_human_confirmation": True,
                },
                "reason": "source requires human confirmation",
                "blockers": ["review_agent_requires_human_confirmation"],
            }],
        }], artifacts=[],
    )

    row = store.conn.execute(
        "SELECT status FROM evidence_asset_task WHERE case_id=?", (case_id,)
    ).fetchone()
    assert row["status"] == "failed"
    routes = store.list_evidence_asset_routes(case_id)
    assert len(routes) == 1
    assert routes[0]["route_status"] == "ambiguous"
    assert any(
        "routes are unresolved" in blocker
        for blocker in store.evidence_workflow_summary(case_id)["blockers"]
    )


def test_llm_excluded_discovered_asset_becomes_terminal(tmp_path: Path) -> None:
    store, repository, case_id = _case(tmp_path)
    state = repository.load(case_id)
    repository.start_attempt(state, kind="initial", supplement_request="")
    store.sync_evidence_ledger(
        case_id,
        state.active_attempt_id,
        known_urls=[],
        candidates=[],
        asset_records=[{
            "url": "https://example.gov.cn/roster-supplement.jpg",
            "parent_url": "https://example.gov.cn/notice",
            "kind": "image",
            "status": "discovered",
            "routes": [{
                "scope_id": None,
                "route_source": "llm",
                "confidence": 0.9,
                "route_status": "excluded",
                "selector": {
                    "material_relation": "supplement",
                    "roster_contribution": "exclude",
                },
                "reason": "complete parent HTML already supplies the roster",
            }],
        }],
        artifacts=[],
    )

    # A later graph checkpoint resynchronizes the same case after the asset has
    # already become terminal. The explicit LLM route must remain authoritative.
    store.sync_evidence_ledger(
        case_id,
        state.active_attempt_id,
        known_urls=[],
        candidates=[],
        asset_records=[{
            "url": "https://example.gov.cn/roster-supplement.jpg",
            "parent_url": "https://example.gov.cn/notice",
            "kind": "image",
            "status": "discovered",
            "routes": [{
                "scope_id": None,
                "route_source": "llm",
                "confidence": 0.9,
                "route_status": "excluded",
                "selector": {
                    "material_relation": "supplement",
                    "roster_contribution": "exclude",
                },
                "reason": "complete parent HTML already supplies the roster",
            }],
        }],
        artifacts=[],
    )

    row = store.conn.execute(
        "SELECT status FROM evidence_asset_task WHERE case_id=?", (case_id,)
    ).fetchone()
    assert row["status"] == "excluded"
    route = store.list_evidence_asset_routes(case_id)[0]
    assert route["route_source"] == "llm"
    assert route["route_status"] == "excluded"
    assert route["processing_status"] == "excluded"
    assert not any(
        "not terminal" in blocker
        for blocker in store.evidence_workflow_summary(case_id)["blockers"]
    )


def test_access_denied_asset_remains_a_terminal_ledger_record(tmp_path: Path) -> None:
    store, repository, case_id = _case(tmp_path)
    state = repository.load(case_id)
    repository.start_attempt(state, kind="initial", supplement_request="")
    store.sync_audit_scopes(case_id, [{
        "scope_key": "team:a", "role_type": "team", "role_label": "A",
        "required": True, "business_scope": {"ZB": "A"},
        "submitted_row_count": 1, "submitted_identity_count": 1,
        "submitted_identities": {"a": "A"},
    }], identity_version="identity-v2")
    store.sync_evidence_ledger(
        case_id, state.active_attempt_id, known_urls=[], candidates=[],
        asset_records=[{
            "url": "https://example.gov.cn/download?id=blocked",
            "parent_url": "https://example.gov.cn/notice",
            "label": "official roster", "kind": "unknown", "status": "access_denied",
            "error_code": "ATTACHMENT_ACCESS_DENIED",
            "error_message": "RuntimeError: attachment download failed HTTP 403",
            "metadata": {"http_status": 403, "access_status": "denied"},
        }], artifacts=[],
    )

    row = store.conn.execute(
        "SELECT status,error_code,metadata_json FROM evidence_asset_task WHERE case_id=?", (case_id,)
    ).fetchone()
    assert row["status"] == "access_denied"
    assert row["error_code"] == "ATTACHMENT_ACCESS_DENIED"
    assert "403" in row["metadata_json"]
    route = store.list_evidence_asset_routes(case_id)[0]
    assert route["processing_status"] == "access_denied"


def test_scope_comparison_unions_all_complete_facts_for_same_scope(tmp_path: Path) -> None:
    store, repository, case_id = _case(tmp_path)
    state = repository.load(case_id)
    state.submitted_summary["role_scopes"] = [{
        "scope_key": "team:all", "role_type": "team", "role_label": "队伍",
        "required": True, "submitted_row_count": 3, "submitted_identity_count": 3,
        "submitted_identities": {"a": "A", "b": "B", "c": "C"},
    }]
    repository.save(state)
    repository.start_attempt(state, kind="initial", supplement_request="")
    scope_id = store.list_audit_scopes(case_id)[0]["scope_id"]

    store.record_scope_comparisons(
        case_id,
        state.active_attempt_id,
        facts=[
            {"scope_id": scope_id, "document_complete": True,
             "matched_items": ["A"], "missing_items": ["B", "C"],
             "missing_item_count": 2, "observed_count": 1},
            {"scope_id": scope_id, "document_complete": True,
             "matched_items": ["B", "C"], "missing_items": ["A"],
             "missing_item_count": 1, "observed_count": 2},
        ],
        verifier={
            "target_match": "yes",
            "year_match": "yes",
            "source_authority": "official",
            "coverage_complete": "no",
            "missing_evidence": ["stale case-level coverage gap"],
            "supplement_requests": [{"question": "stale request"}],
            "reason_codes": ["stale_case_reason"],
        },
    )

    comparison = store.list_scope_comparisons(case_id)[0]
    assert comparison["submitted_identity_count"] == 3
    assert comparison["matched_count"] == 3
    assert comparison["missing"] == []
    assert comparison["comparison_result"] == "matched"
    assert comparison["verifier"]["scope_id"] == scope_id
    assert comparison["verifier"]["coverage_complete"] == "yes"
    assert comparison["verifier"]["missing_evidence"] == []
    assert comparison["verifier"]["supplement_requests"] == []
    assert comparison["verifier"]["reason_codes"] == [
        "scope_evidence_complete",
        "scope_comparison_matched",
    ]
    scope = store.list_audit_scopes(case_id)[0]
    assert scope["status"] == "complete"
    assert scope["blockers"] == []


def test_scope_comparison_stays_provisional_while_asset_ledger_is_open(
    tmp_path: Path,
) -> None:
    store, repository, case_id = _case(tmp_path)
    state = repository.load(case_id)
    state.submitted_summary["role_scopes"] = [{
        "scope_key": "team:all", "role_type": "team", "role_label": "Teams",
        "required": True, "submitted_row_count": 1, "submitted_identity_count": 1,
        "submitted_identities": {"a": "Team A"},
    }]
    repository.save(state)
    repository.start_attempt(state, kind="initial", supplement_request="")
    scope_id = store.list_audit_scopes(case_id)[0]["scope_id"]
    store.sync_evidence_ledger(
        case_id,
        state.active_attempt_id,
        known_urls=[],
        candidates=[],
        asset_records=[{
            "url": "https://example.gov.cn/failed-page.png",
            "parent_url": "https://example.gov.cn/results",
            "kind": "image",
            "status": "failed",
            "scope_key": "team:all",
        }],
        artifacts=[],
    )

    store.record_scope_comparisons(
        case_id,
        state.active_attempt_id,
        facts=[{
            "scope_id": scope_id,
            "document_complete": True,
            "matched_items": ["Team A"],
        }],
        verifier={"coverage_complete": "yes"},
    )

    comparison = store.list_scope_comparisons(case_id)[0]
    assert comparison["matched_count"] == 1
    assert comparison["status"] == "incomplete"
    assert comparison["evidence_complete"] is False
    assert "1 evidence assets failed" in comparison["blockers"]
    scope = store.list_audit_scopes(case_id)[0]
    assert scope["status"] == "incomplete"
    assert "1 evidence assets failed" in scope["blockers"]


def test_scope_comparison_blocks_duplicate_display_values(tmp_path: Path) -> None:
    store, repository, case_id = _case(tmp_path)
    state = repository.load(case_id)
    state.submitted_summary["role_scopes"] = [{
        "scope_key": "project:all", "role_type": "work_or_project",
        "role_label": "Projects", "required": True,
        "submitted_row_count": 2, "submitted_identity_count": 2,
        "submitted_identities": {"one": "Same title", "two": "Same title"},
    }]
    repository.save(state)
    repository.start_attempt(state, kind="initial", supplement_request="")
    scope_id = store.list_audit_scopes(case_id)[0]["scope_id"]

    store.record_scope_comparisons(
        case_id, state.active_attempt_id,
        facts=[{
            "scope_id": scope_id, "document_complete": True,
            "matched_items": ["Same title"],
        }],
        verifier={"coverage_complete": "yes"},
    )

    comparison = store.list_scope_comparisons(case_id)[0]
    assert comparison["status"] == "incomplete"
    assert "submitted_identity_display_collision" in comparison["blockers"]


def test_later_complete_scope_fact_supersedes_earlier_partial_observation(
    tmp_path: Path,
) -> None:
    store, repository, case_id = _case(tmp_path)
    state = repository.load(case_id)
    state.submitted_summary["role_scopes"] = [{
        "scope_key": "organization:all", "role_type": "organization",
        "role_label": "Organizations", "required": True,
        "submitted_row_count": 2, "submitted_identity_count": 2,
        "submitted_identities": {"a": "Org A", "b": "Org B"},
    }]
    repository.save(state)
    repository.start_attempt(state, kind="initial", supplement_request="")
    scope_id = store.list_audit_scopes(case_id)[0]["scope_id"]

    store.record_scope_comparisons(
        case_id,
        state.active_attempt_id,
        facts=[
            {"scope_id": scope_id, "document_complete": False,
             "status": "partial", "matched_items": ["Org A"]},
            {"scope_id": scope_id, "document_complete": True,
             "status": "complete", "matched_items": ["Org A", "Org B"]},
        ],
        verifier={"coverage_complete": "yes"},
    )

    comparison = store.list_scope_comparisons(case_id)[0]
    assert comparison["status"] == "complete"
    assert comparison["matched_count"] == 2
    assert comparison["missing"] == []


def test_scope_comparison_derives_match_and_extra_from_exact_sets(tmp_path: Path) -> None:
    store, repository, case_id = _case(tmp_path)
    state = repository.load(case_id)
    state.submitted_summary["role_scopes"] = [{
        "scope_key": "team:all", "role_type": "team", "role_label": "队伍",
        "required": True, "submitted_row_count": 3, "submitted_identity_count": 3,
        "submitted_identities": {"a": "A", "b": "B", "c": "C"},
    }]
    repository.save(state)
    repository.start_attempt(state, kind="initial", supplement_request="")
    scope_id = store.list_audit_scopes(case_id)[0]["scope_id"]

    store.record_scope_comparisons(
        case_id,
        state.active_attempt_id,
        facts=[{
            "scope_id": scope_id,
            "document_complete": True,
            "matched_items": ["A", "B", "evidence-only"],
            "semantic_identity_decisions": [{
                "candidate_id": "identity:test",
                "submitted": "A",
                "source": "A教授",
                "decision": "same_identity",
                "confidence": 0.99,
                "reason": "same person with a title",
                "source_url": "https://official.example/people.pdf",
                "source_anchor": "page:1:row:1",
            }],
        }],
        verifier={"coverage_complete": "yes"},
    )

    comparison = store.list_scope_comparisons(case_id)[0]
    assert comparison["matched_count"] == 2
    assert comparison["missing"] == ["C"]
    assert comparison["extra"] == ["evidence-only"]
    assert comparison["evidence_identity_count"] == 3
    assert comparison["verifier"]["semantic_identity_decisions"][0]["decision"] == (
        "same_identity"
    )


def test_scope_comparison_preserves_code_name_pair_as_field_conflict(
    tmp_path: Path,
) -> None:
    store, repository, case_id = _case(tmp_path)
    state = repository.load(case_id)
    submitted_dynamic = "ZL201910243534.X;动态白盒数据处理"
    submitted_air = "ZL201910243534.X;空气交换量测量"
    state.submitted_summary["role_scopes"] = [{
        "scope_key": "patent:excellent", "role_type": "work_or_project",
        "role_label": "Patent excellent", "required": True,
        "profile": {"primary_alternatives": [["XMBH"], ["XMMC"]]},
        "submitted_row_count": 2, "submitted_identity_count": 2,
        "submitted_identities": {
            "dynamic": submitted_dynamic,
            "air": submitted_air,
        },
    }]
    repository.save(state)
    repository.start_attempt(state, kind="initial", supplement_request="")
    scope_id = store.list_audit_scopes(case_id)[0]["scope_id"]
    source_dynamic = "ZL201910245733.4;动态白盒数据处理"

    store.record_scope_comparisons(
        case_id,
        state.active_attempt_id,
        facts=[{
            "scope_id": scope_id,
            "status": "complete",
            "document_complete": True,
            "matched_items": [submitted_air],
            "extra_items": [source_dynamic],
            "identity_conflicts": [{
                "submitted": submitted_dynamic,
                "source": source_dynamic,
                "fields": "XMBH",
                "reason": "same_secondary_different_primary",
                "source_url": "https://official.example/patent-excellent.pdf",
            }],
        }],
        verifier={"coverage_complete": "yes"},
    )

    comparison = store.list_scope_comparisons(case_id)[0]
    assert comparison["matched_count"] == 1
    assert comparison["evidence_identity_count"] == 2
    assert comparison["missing"] == []
    assert comparison["extra"] == []
    assert comparison["comparison_result"] == "conflict"
    assert comparison["conflicts"] == [
        "identity_field_conflict: XMBH differs while a secondary identity matches "
        "| submitted=ZL201910243534.X;动态白盒数据处理 "
        "| source=ZL201910245733.4;动态白盒数据处理"
    ]
    assert comparison["identity_conflicts"] == [{
        "submitted": submitted_dynamic,
        "source": source_dynamic,
        "fields": "XMBH",
        "reason": "same_secondary_different_primary",
        "source_url": "https://official.example/patent-excellent.pdf",
    }]
    assert comparison["comparison_differences"] == [{
        "difference_type": "field_conflict",
        "submitted": submitted_dynamic,
        "source": source_dynamic,
        "fields": "XMBH",
        "reason": "same_secondary_different_primary",
        "source_urls": ["https://official.example/patent-excellent.pdf"],
    }]


def test_scope_comparison_uses_primary_identity_and_preserves_variant_conflict(
    tmp_path: Path,
) -> None:
    store, repository, case_id = _case(tmp_path)
    state = repository.load(case_id)
    state.submitted_summary["role_scopes"] = [{
        "scope_key": "patent:gold",
        "role_type": "work_or_project",
        "role_label": "Patent gold",
        "required": True,
        "profile": {"primary_alternatives": [["XMBH"], ["XMMC"]]},
        "business_scope": {"HJDJ": "Gold"},
        "submitted_row_count": 2,
        "submitted_identity_count": 1,
        "submitted_identities": {"patent-1": "PATENT-1"},
    }]
    repository.save(state)
    repository.start_attempt(state, kind="initial", supplement_request="")
    scope_id = store.list_audit_scopes(case_id)[0]["scope_id"]

    store.record_scope_comparisons(
        case_id,
        state.active_attempt_id,
        facts=[{
            "scope_id": scope_id,
            "status": "complete",
            "document_complete": True,
            "matched_items": ["PATENT-1;Title A", "PATENT-1;Title B"],
        }],
        verifier={},
    )

    comparison = store.list_scope_comparisons(case_id)[0]
    assert comparison["matched_count"] == 1
    assert comparison["missing"] == []
    assert comparison["extra"] == []
    assert comparison["comparison_result"] == "conflict"
    assert comparison["conflicts"] == [
        "multiple evidence variants share submitted primary identity: PATENT-1"
    ]


def test_unscoped_fact_is_not_assigned_to_first_scope(tmp_path: Path) -> None:
    store, repository, case_id = _case(tmp_path)
    state = repository.load(case_id)
    state.submitted_summary["role_scopes"] = [{
        "scope_key": "team:all", "role_type": "team", "role_label": "队伍",
        "required": True, "submitted_row_count": 1, "submitted_identity_count": 1,
        "submitted_identities": {"a": "A"},
    }]
    repository.save(state)
    repository.start_attempt(state, kind="initial", supplement_request="")

    store.record_scope_comparisons(
        case_id,
        state.active_attempt_id,
        facts=[{
            "role_type": "team", "document_complete": True,
            "matched_items": ["A"], "observed_count": 1,
        }],
        verifier={"coverage_complete": "yes"},
    )

    comparison = store.list_scope_comparisons(case_id)[0]
    assert comparison["status"] == "incomplete"
    assert comparison["comparison_result"] == "not_compared"
    assert "scope_evidence_missing" in comparison["blockers"]


def test_wrong_edition_asset_is_excluded_before_scope_comparison(tmp_path: Path) -> None:
    store, repository, case_id = _case(tmp_path)
    state = repository.load(case_id)
    state.submitted_summary["role_scopes"] = [{
        "scope_key": "team:first", "role_type": "team", "role_label": "Teams",
        "required": True, "business_scope": {"BSJS": "第一届"},
        "submitted_row_count": 1, "submitted_identity_count": 1,
        "submitted_identities": {"a": "A"},
    }]
    repository.save(state)
    repository.start_attempt(state, kind="initial", supplement_request="")
    store.sync_evidence_ledger(
        case_id,
        state.active_attempt_id,
        known_urls=[],
        candidates=[],
        asset_records=[{
            "url": "https://example.gov.cn/second.pdf",
            "parent_url": "https://example.gov.cn/notice",
            "label": "第二届大赛获奖名单",
            "kind": "pdf",
            "status": "processed",
        }],
        artifacts=[],
    )

    routes = store.list_evidence_asset_routes(case_id)
    assert len(routes) == 1
    assert routes[0]["route_status"] == "excluded"
    assert "edition conflicts" in routes[0]["reason"]


def test_excluded_pdf_is_not_reprocessed_on_supplement(tmp_path: Path) -> None:
    _store, repository, case_id = _case(tmp_path)
    state = repository.load(case_id)
    state.artifacts = [EvidenceArtifact(
        kind="pdf",
        source_url="https://example.gov.cn/wrong-edition.pdf",
        local_path=str(tmp_path / "wrong-edition.pdf"),
        content_type="application/pdf",
        sha256="a" * 64,
        size_bytes=1,
        fetched_at="2026-08-05T00:00:00Z",
        metadata={"routes": [{"route_status": "excluded"}]},
    )]

    assert _pending_pdf_inspection(state) == ""


def test_spreadsheet_records_route_multiple_workbooks_to_role_scopes(tmp_path: Path) -> None:
    _store, repository, case_id = _case(tmp_path)
    state = repository.load(case_id)
    team_counts = {"技术类": 168, "教育类": 70, "管理类": 41}
    role_scopes = []
    for index, (category, count) in enumerate(team_counts.items(), start=1):
        role_scopes.append({
            "scope_id": index,
            "scope_key": f"team:{category}",
            "role_type": "team",
            "role_label": category,
            "required": True,
            "business_scope": {"BSJS": "第一届", "SDLB": category},
            "submitted_row_count": count,
            "submitted_identity_count": count,
            "submitted_identities": {
                f"{category}-{item}": f"{category}团队{item}" for item in range(count)
            },
        })
    role_scopes.append({
        "scope_id": 4,
        "scope_key": "organization:first",
        "role_type": "organization",
        "role_label": "组织单位",
        "required": True,
        "business_scope": {"BSJS": "第一届"},
        "submitted_row_count": 28,
        "submitted_identity_count": 28,
        "submitted_identities": {f"org-{item}": f"单位{item}" for item in range(28)},
    })
    state.submitted_summary["role_scopes"] = role_scopes

    team_file_counts = [85, 28, 166]
    category_plan = [
        {"技术类": 50, "教育类": 20, "管理类": 15},
        {"技术类": 18, "教育类": 7, "管理类": 3},
        {"技术类": 100, "教育类": 43, "管理类": 23},
    ]
    category_offsets = {category: 0 for category in team_counts}
    records = []
    for file_index, plan in enumerate(category_plan, start=1):
        source_url = f"https://example.edu/team-{file_index}.xlsx"
        for category, count in plan.items():
            offset = category_offsets[category]
            for item in range(offset, offset + count):
                records.append({
                    "source_url": source_url,
                    "parent_url": "https://example.edu/notice",
                    "sheet": "Sheet1",
                    "row_number": item + 3,
                    "role_type": "team",
                    "identity": f"{category}团队{item}",
                    "identity_field": "团队名称",
                    "title": "第一届获奖团队名单",
                    "category_values": [category],
                    "level_values": [],
                    "document_complete": True,
                })
            category_offsets[category] += count
    for item in range(28):
        records.append({
            "source_url": "https://example.edu/organizations.xlsx",
            "parent_url": "https://example.edu/notice",
            "sheet": "Sheet1",
            "row_number": item + 4,
            "role_type": "organization",
            "identity": f"单位{item}",
            "identity_field": "参赛单位",
            "title": "第一届优秀组织单位",
            "category_values": [],
            "level_values": [],
            "document_complete": True,
        })
    result = ToolResult(
        ok=True,
        data={"spreadsheet_identity_records": records},
        evidence_facts=[EvidenceFact(
            target_match="yes", year_match="yes", source_level="official_primary"
        )],
    )

    _route_spreadsheet_result_to_scopes(state, result)

    assert {fact.scope_id: fact.observed_count for fact in result.evidence_facts} == {
        1: 168, 2: 70, 3: 41, 4: 28,
    }
    assert all(fact.coverage_complete is True for fact in result.evidence_facts)
    assert result.data["spreadsheet_asset_identity_counts"] == {
        "https://example.edu/team-1.xlsx": team_file_counts[0],
        "https://example.edu/team-2.xlsx": team_file_counts[1],
        "https://example.edu/team-3.xlsx": team_file_counts[2],
        "https://example.edu/organizations.xlsx": 28,
    }
    assert all(
        route["subunit_type"] == "sheet"
        for routes in result.data["spreadsheet_scope_routes"].values()
        for route in routes
    )


def test_supplement_reuses_local_spreadsheet_for_unresolved_routes(tmp_path: Path) -> None:
    _store, repository, case_id = _case(tmp_path)
    state = repository.load(case_id)
    state.submitted_summary["role_scopes"] = [{
        "scope_id": 1,
        "scope_key": "team:technical",
        "role_type": "team",
        "role_label": "技术类",
        "required": True,
        "business_scope": {"SDLB": "技术类"},
        "submitted_row_count": 2,
        "submitted_identity_count": 2,
        "submitted_identities": {"a": "甲团队", "b": "乙团队"},
    }]
    path = tmp_path / "teams.xlsx"
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.append(["第一届获奖团队名单"])
    sheet.append(["序号", "团队名称", "组别"])
    sheet.append([1, "甲团队", "技术类"])
    sheet.append([2, "乙团队", "技术类"])
    workbook.save(path)
    workbook.close()
    inspection = inspect_evidence_file(
        path, max_bytes=20 * 1024 * 1024, allowed_kinds={"xlsx"}
    )
    state.artifacts = [EvidenceArtifact(
        kind="xlsx",
        source_url="https://example.edu/teams.xlsx",
        local_path=str(path),
        content_type=inspection.content_type,
        sha256=inspection.sha256,
        size_bytes=inspection.size_bytes,
        fetched_at="2026-08-05T00:00:00Z",
        metadata={"page_url": "https://example.edu/notice"},
    )]

    result = _reuse_local_spreadsheet_evidence(state, allowed_roots=[tmp_path])

    assert result is not None
    assert result.evidence_facts[0].observed_count == 2
    assert result.evidence_facts[0].coverage_complete is True
    assert state.artifacts[0].metadata["extracted_count"] == 2
    assert state.artifacts[0].metadata["routes"][0]["subunit_type"] == "sheet"


def test_related_out_of_scope_identity_is_conflict_not_match_or_missing(tmp_path: Path) -> None:
    store, repository, case_id = _case(tmp_path)
    state = repository.load(case_id)
    state.submitted_summary["role_scopes"] = [{
        "scope_key": "work_or_project:planning",
        "role_type": "work_or_project",
        "role_label": "Project",
        "required": True,
        "profile": {"primary_alternatives": [["XMMC"]]},
        "business_scope": {"XMLB": "Planning fund"},
        "submitted_row_count": 2,
        "submitted_identity_count": 2,
        "submitted_identities": {"a": "Primary project", "b": "Xinjiang project"},
    }]
    repository.save(state)
    repository.start_attempt(state, kind="initial", supplement_request="")
    scope_id = store.list_audit_scopes(case_id)[0]["scope_id"]

    store.record_scope_comparisons(
        case_id,
        state.active_attempt_id,
        facts=[
            {
                "scope_id": scope_id,
                "source_url": "https://official.example/planning.pdf",
                "status": "complete",
                "document_complete": True,
                "matched_items": ["Primary project"],
            },
            {
                "scope_id": scope_id,
                "source_url": "https://official.example/xinjiang.pdf",
                "status": "out_of_scope",
                "document_complete": True,
                "contributes_to_scope": False,
                "related_out_of_scope": [{
                    "identity": "Xinjiang project",
                    "source_url": "https://official.example/xinjiang.pdf",
                    "source_label": "Xinjiang projects",
                    "reason": "Official current-year roster for a separate category.",
                }],
            },
        ],
        verifier={"coverage_complete": "yes"},
    )

    comparison = store.list_scope_comparisons(case_id)[0]
    assert comparison["matched_count"] == 1
    assert comparison["missing"] == []
    assert comparison["comparison_result"] == "conflict"
    assert comparison["related_out_of_scope"] == [{
        "identity": "Xinjiang project",
        "source_url": "https://official.example/xinjiang.pdf",
        "source_label": "Xinjiang projects",
        "reason": "Official current-year roster for a separate category.",
    }]
    identities = store.conn.execute(
        "SELECT origin,display_value,source_ref FROM evidence_identity "
        "WHERE case_id=? AND attempt_id=? ORDER BY origin,display_value",
        (case_id, state.active_attempt_id),
    ).fetchall()
    assert any(
        row["origin"] == "related_out_of_scope"
        and row["display_value"] == "Xinjiang project"
        and row["source_ref"] == "https://official.example/xinjiang.pdf"
        for row in identities
    )
