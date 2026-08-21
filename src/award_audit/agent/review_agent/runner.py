"""Thin, fail-closed orchestration for case-level semantic review.

This runner deliberately separates semantic asset routing from deterministic
identity comparison.  It persists an incomplete attempt until selected assets
have produced per-scope comparison facts.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections import Counter
from collections.abc import Mapping, Sequence
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from award_audit.agent.harness.models import (
    AuditCaseState,
    HarnessOutcome,
    ToolBudgetState,
)
from award_audit.agent.harness.persistence import CaseRepository
from award_audit.agent.integration import AuditCaseInput
from award_audit.agent.investigation import (
    InvestigationAgent,
    InvestigationResult,
    InvestigationStageHooks,
)
from award_audit.agent.memory.service import CaseMemoryService
from award_audit.agent.review_agent.models import (
    IdentityAdjudicationBatch,
    ReviewOutcome,
)
from award_audit.agent.review_agent.packet import asset_packet_key, build_review_case_packet
from award_audit.agent.review_agent.readers import M4AssetReader
from award_audit.agent.review_agent.service import (
    ReviewAgent,
    ReviewAgentRun,
    ReviewAgentTrace,
    ReviewLlm,
)
from award_audit.agent.toolkit import pdf as pdf_tools
from award_audit.agent.toolkit.contracts import (
    EvidenceAssetRecord,
    EvidenceFact,
    ToolBudgetLimits,
    ToolObservation,
    ToolResult,
    utc_now,
)
from award_audit.agent.toolkit.safety import inspect_evidence_file, validate_local_path
from award_audit.agent.toolkit.spreadsheet import (
    extract_semantic_roster_records,
    parse_award_excel,
)
from award_audit.agent.verification.service import (
    build_evidence_snapshot,
    deterministic_verify,
)
from award_audit.core.identity import normalize_comparison_identity, normalize_identity


def _context_from_state(state: AuditCaseState) -> AuditCaseInput:
    allowed = set(AuditCaseInput.model_fields)
    payload = {
        key: value for key, value in state.submitted_summary.items() if key in allowed
    }
    payload.update({
        "resource_code": state.resource_code,
        "award_name": state.award_name,
        "year": state.year,
        "known_urls": state.known_urls,
    })
    return AuditCaseInput.model_validate(payload)


def _scope_ids_by_key(
    state: AuditCaseState,
    persisted_scopes: Sequence[Mapping[str, Any]] = (),
) -> dict[str, int]:
    """Use ledger scope IDs; snapshots are descriptive and need not contain them."""

    persisted = {
        str(scope.get("scope_key", "")): int(scope.get("scope_id", 0) or 0)
        for scope in persisted_scopes
        if isinstance(scope, Mapping)
        and str(scope.get("scope_key", "")).strip()
        and int(scope.get("scope_id", 0) or 0) > 0
    }
    raw_scopes = state.submitted_summary.get("role_scopes", [])
    if not isinstance(raw_scopes, list):
        return persisted
    return {
        str(scope.get("scope_key", "")): persisted.get(
            str(scope.get("scope_key", "")), int(scope.get("scope_id", 0) or 0)
        )
        for scope in raw_scopes
        if isinstance(scope, Mapping)
        and str(scope.get("scope_key", "")).strip()
        and persisted.get(
            str(scope.get("scope_key", "")), int(scope.get("scope_id", 0) or 0)
        ) > 0
    }


def _semantic_routes(outcome: ReviewOutcome) -> dict[tuple[str, str], list[dict[str, Any]]]:
    routes: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for assessment in outcome.assessments:
        key = (assessment.asset_id, assessment.subunit_id)
        selector = {
            "material_relation": assessment.material_relation,
            "version_relation": assessment.version_relation,
            "roster_contribution": assessment.roster_contribution,
            "requires_human_confirmation": assessment.requires_human_confirmation,
        }
        if assessment.roster_contribution == "include":
            routes[key] = [{
                "scope_id": scope_id,
                "subunit_type": assessment.subunit_id,
                "selector": selector,
                "route_source": "llm",
                "confidence": assessment.confidence,
                "route_status": "routed",
                "reason": assessment.reason,
            } for scope_id in assessment.scope_ids]
        elif assessment.roster_contribution == "cross_scope":
            routes[key] = [{
                "scope_id": scope_id,
                "subunit_type": assessment.subunit_id,
                "selector": selector,
                "route_source": "llm",
                "confidence": assessment.confidence,
                "route_status": "out_of_scope",
                "reason": assessment.reason,
            } for scope_id in assessment.scope_ids]
        elif assessment.roster_contribution == "exclude":
            routes[key] = [{
                "scope_id": None,
                "subunit_type": assessment.subunit_id,
                "selector": selector,
                "route_source": "llm",
                "confidence": assessment.confidence,
                "route_status": "excluded",
                "reason": assessment.reason,
            }]
        else:
            routes[key] = [{
                "scope_id": None,
                "subunit_type": assessment.subunit_id,
                "selector": selector,
                "route_source": "llm",
                "confidence": assessment.confidence,
                "route_status": "ambiguous",
                "reason": assessment.reason,
                "blockers": ["review_agent_requires_human_confirmation"],
            }]
    return routes


def _review_trace(run: ReviewAgentRun, *, packet: Any) -> ToolObservation:
    packet_payload = packet.model_dump(mode="json")
    packet_digest = hashlib.sha256(
        json.dumps(packet_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    outcome_payload = run.outcome.model_dump(mode="json")
    digest = hashlib.sha256(
        json.dumps(outcome_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    now = utc_now()
    return ToolObservation(
        call_id=f"review-agent-{digest[:16]}",
        tool_name="review_asset_relations",
        started_at=now,
        finished_at=now,
        duration_ms=0,
        input_summary={
            "prompt_version": run.trace.prompt_version,
            "case_packet_sha256": packet_digest,
            "asset_count": len(packet.assets),
            "scope_count": len(packet.scopes),
            "model_call_count": run.trace.model_call_count,
            "request_count": run.trace.request_count,
        },
        output_summary={
            "validation_status": run.trace.validation_status,
            "case_recommendation": run.outcome.case_recommendation,
            "assessment_count": len(run.outcome.assessments),
            "selected_asset_count": len(run.outcome.selected_assets),
            "outcome_sha256": digest,
            "raw_json_summary": {
                "selected_assets": run.outcome.selected_assets[:20],
                "assessment_asset_ids": [
                    item.asset_id for item in run.outcome.assessments[:20]
                ],
                "version_group_count": len(run.outcome.version_groups),
                "unresolved_question_count": len(run.outcome.unresolved_questions),
            },
            "blockers": run.trace.blockers,
        },
        ok=run.trace.validation_status == "accepted",
        error_code=(
            "REVIEW_AGENT_EVIDENCE_INSUFFICIENT"
            if run.outcome.case_recommendation == "evidence_insufficient" else ""
        ),
    )


def _investigation_node_traces(result: InvestigationResult) -> list[ToolObservation]:
    """Expose the actual LangGraph nodes through the existing append-only trace ledger."""

    traces: list[ToolObservation] = []
    for index, event in enumerate(result.node_events):
        traces.append(ToolObservation(
            call_id=f"langgraph-{index}-{str(event.get('node', 'node'))}",
            tool_name=f"langgraph:{str(event.get('node', 'unknown'))}",
            started_at=str(event.get("started_at", utc_now())),
            finished_at=str(event.get("finished_at", utc_now())),
            duration_ms=int(event.get("duration_ms", 0) or 0),
            input_summary={"step_count": int(event.get("step_count", 0) or 0)},
            output_summary={
                "transition_reason": str(event.get("transition_reason", "")),
                "graph_status": result.status,
            },
            ok=result.status not in {"protocol_error"},
            error_code="LANGGRAPH_PROTOCOL_ERROR" if result.status == "protocol_error" else "",
        ))
    return traces


_AGENT_TO_SOURCE_ROLE = {
    "project": "work_or_project",
    "team": "team",
    "person": "instructor_or_person",
    "organization": "organization",
    "special": "ranking_or_special",
}

_SOURCE_FIELD_HEADERS = {
    "XMMC": ("项目名称", "项目名", "作品名称", "作品名", "成果名称", "专利名称"),
    "XMBH": ("项目编号", "项目代码", "编号", "专利号", "专利申请号", "申请号", "专利编号"),
    "ZPMC": (
        "项目名称", "项目名", "作品名称", "作品名", "成果名称", "专利名称",
        "团队名称", "队伍名称", "参赛队伍名称", "参赛队伍",
    ),
    "LWTM": ("论文题目", "论文名称", "题目"),
    "XRYXM": (
        "获奖人", "获奖人姓名", "获奖者", "姓名", "主要完成人",
        "指导老师", "指导教师", "教师",
    ),
    "FZRXM": (
        "负责人", "项目负责人", "主持人", "申请人",
        "指导老师", "指导教师", "教师",
    ),
    "XFZRXM": ("申请人", "项目申请人", "项目负责人", "负责人", "主持人", "申报人"),
    "ZZXM": ("作者", "作者姓名", "获奖人", "获奖人姓名", "主要完成人"),
    "DSXM": ("导师", "导师姓名", "指导教师", "指导教师姓名"),
    "XDWMC": ("单位名称", "学校名称", "参赛单位", "获奖单位", "组织单位", "单位"),
    "XCSDW": ("单位名称", "学校名称", "参赛单位", "获奖单位", "组织单位", "单位"),
    "CSDWMC": ("单位名称", "学校", "学校名称", "参赛单位", "获奖单位", "组织单位", "单位"),
    "TDM": ("团队名称", "队伍名称", "参赛队伍名称", "参赛队伍"),
}


def _primary_identity(
    value: str, width: int, *, role_type: str = "instructor_or_person"
) -> str:
    primary = ";".join(value.split(";")[:width])
    return normalize_comparison_identity(primary, role_type=role_type)


def _source_identity_for_scope(
    record: Mapping[str, object],
    primary_fields: Sequence[object],
) -> str:
    """Build a complete source identity from one spreadsheet row only."""

    raw_values = record.get("row_values", {})
    if not isinstance(raw_values, Mapping) or not primary_fields:
        return ""
    normalized = {
        str(header).strip().casefold(): str(value or "").strip()
        for header, value in raw_values.items()
        if str(header).strip()
    }
    values: list[str] = []
    for field in primary_fields:
        aliases = tuple(dict.fromkeys((
            str(field), *_SOURCE_FIELD_HEADERS.get(str(field), ()),
        )))
        value = next((
            normalized.get(alias.casefold(), "")
            for alias in aliases
            if normalized.get(alias.casefold(), "")
        ), "")
        if not value:
            if str(record.get("role_type", "")) == "unclassified":
                value = str(record.get("identity", "")).strip()
            if not value:
                return ""
        values.append(value)
    return ";".join(values)


def _hydrate_graph_image_assets(
    assets: Sequence[EvidenceAssetRecord],
    graph_state: Mapping[str, Any],
    scope_labels: Sequence[str] = (),
) -> tuple[list[EvidenceAssetRecord], EvidenceAssetRecord | None, set[tuple[str, str]]]:
    """Convert OCR/vision observations into physical pages and one logical roster."""

    ocr_by_page: dict[int, Mapping[str, Any]] = {}
    vision_by_sha: dict[str, dict[str, Any]] = {}
    vision_errors: dict[int, Mapping[str, Any]] = {}
    for observation in graph_state.get("observations", []):
        if not isinstance(observation, Mapping):
            continue
        data = observation.get("summary", {}).get("data", {})
        if not isinstance(data, Mapping):
            continue
        if observation.get("tool_name") == "ocr_image":
            for page in data.get("pages", []):
                if isinstance(page, Mapping):
                    ocr_by_page[int(page.get("page", 0) or 0)] = page
        elif observation.get("tool_name") == "vision_extract_roster":
            for page in data.get("pages", []):
                if isinstance(page, Mapping) and str(page.get("image_sha256", "")):
                    vision_by_sha[str(page["image_sha256"])] = dict(page)
            for error in data.get("errors", []):
                if isinstance(error, Mapping):
                    vision_errors[int(error.get("page", 0) or 0)] = error

    updated: list[EvidenceAssetRecord] = []
    roster_keys: set[tuple[str, str]] = set()
    roster_pages: list[dict[str, Any]] = []
    image_sources: list[dict[str, Any]] = []
    for asset in assets:
        if asset.kind != "image":
            updated.append(asset)
            continue
        metadata = dict(asset.metadata)
        page_number = int(metadata.get("page", 0) or 0)
        image_sources.append({
            "page": page_number,
            "url": asset.url,
            "sha256": asset.sha256,
            "local_path": asset.local_path,
        })
        vision_page = vision_by_sha.get(asset.sha256)
        ocr_page = ocr_by_page.get(page_number)
        if vision_page is not None:
            entries = vision_page.get("entries", [])
            is_roster = bool(vision_page.get("is_roster_page", True) and entries)
            metadata.update({
                "vision_roster_page": vision_page,
                "ocr_summary": str((ocr_page or {}).get("text", ""))[:1000],
                "summary": (
                    f"Vision extracted image page {page_number}; "
                    f"section={vision_page.get('section_title', '')}; "
                    f"records={len(entries) if isinstance(entries, list) else 0}."
                ),
                "sample_rows": [
                    [
                        str(entry.get("no", "")),
                        str(entry.get("name", "")),
                        str(entry.get("org", "")),
                        str(entry.get("level", "")),
                    ]
                    for entry in entries[:10]
                    if isinstance(entry, Mapping)
                ] if isinstance(entries, list) else [],
                "anchors": [f"image:{page_number}"],
                "image_is_roster_page": is_roster,
            })
            hydrated = asset.model_copy(update={
                "status": "parsed",
                "truncated": bool(vision_page.get("truncated", False)),
                "extraction_method": "langgraph_vision_roster",
                "metadata": metadata,
            })
            if is_roster:
                roster_keys.add(asset_packet_key(hydrated))
                roster_pages.append(vision_page)
            updated.append(hydrated)
            continue
        if ocr_page is not None and len(str(ocr_page.get("text", "")).strip()) < 80:
            metadata.update({
                "ocr_summary": str(ocr_page.get("text", ""))[:1000],
                "summary": "Local OCR classified this page as non-roster decoration.",
                "anchors": [f"image:{page_number}"],
                "image_is_roster_page": False,
            })
            updated.append(asset.model_copy(update={
                "status": "parsed",
                "extraction_method": "langgraph_ocr_non_roster",
                "metadata": metadata,
            }))
            continue
        if page_number in vision_errors:
            error = vision_errors[page_number]
            updated.append(asset.model_copy(update={
                "status": "failed",
                "error_code": str(error.get("error_code", "VISION_PAGE_FAILED"))[:100],
                "error_message": str(error.get("error_message", ""))[:500],
                "metadata": {
                    **metadata,
                    "blockers": ["vision_page_extraction_failed"],
                },
            }))
            continue
        updated.append(asset)

    if not roster_pages:
        return updated, None, roster_keys
    roster_pages.sort(key=lambda item: int(item.get("page", 0) or 0))
    previous_section = ""
    previous_last: int | None = None
    normalized_scope_labels = {
        normalize_identity(str(label)): str(label).strip()
        for label in scope_labels
        if normalize_identity(str(label))
    }

    def top_level_scope_label(value: str) -> str:
        normalized = normalize_identity(value)
        matches = [
            label for key, label in normalized_scope_labels.items()
            if key in normalized
        ]
        return max(matches, key=len, default="")

    declared_segments: list[tuple[str, int, int]] = []
    seen_declared_labels: set[str] = set()
    for page_number, ocr_page in sorted(ocr_by_page.items()):
        raw_lines = ocr_page.get("lines", [])
        lines = [
            str(line.get("text", "")) for line in raw_lines
            if isinstance(line, Mapping)
        ] if isinstance(raw_lines, list) else []
        for line in lines:
            label = top_level_scope_label(line)
            if not label or label in seen_declared_labels:
                continue
            count_match = re.search(r"[（(]\s*(\d{1,4})\s*项\s*[）)]", line)
            if count_match:
                declared_segments.append((label, int(count_match.group(1)), page_number))
                seen_declared_labels.add(label)

    declared_total = sum(count for _label, count, _page in declared_segments)
    declared_segmentation_complete = bool(
        normalized_scope_labels
        and seen_declared_labels == set(normalized_scope_labels.values())
    )
    declared_boundaries: list[tuple[int, str]] = []
    cumulative = 0
    for label, count, _page in declared_segments:
        cumulative += count
        declared_boundaries.append((cumulative, label))

    current_top_level = ""
    records: list[dict[str, Any]] = []
    for page in roster_pages:
        section = str(page.get("section_title", "")).strip()
        first_no = int(page.get("first_no", 0) or 0) or None
        if not section and previous_section and previous_last and first_no == previous_last + 1:
            section = previous_section
            page["inherited_section_title"] = section
        if section:
            previous_section = section
        page_top_level = top_level_scope_label(section)
        if page_top_level:
            current_top_level = page_top_level
        previous_last = int(page.get("last_no", 0) or 0) or previous_last
        for row_number, entry in enumerate(page.get("entries", []), start=1):
            if not isinstance(entry, Mapping):
                continue
            name = str(entry.get("name", "")).strip()
            org = str(entry.get("org", "")).strip()
            level = str(entry.get("level", "")).strip()
            row_section = str(entry.get("section_title", "")).strip() or section
            row_top_level = top_level_scope_label(row_section)
            if row_top_level:
                current_top_level = row_top_level
            record_position = len(records) + 1
            declared_section = next((
                label for boundary, label in declared_boundaries
                if record_position <= boundary
            ), "") if declared_segmentation_complete else ""
            business_section = declared_section or current_top_level or row_section
            records.append({
                "sheet": f"Image page {page.get('page', 0)}",
                "row_number": row_number,
                "role_type": "unclassified",
                "identity": name,
                "identity_field": "XMMC",
                "row_values": {
                    "XMMC": name,
                    "XRYXM": name,
                    "姓名": name,
                    "项目名称": name,
                    "XDWMC": org,
                    "学校": org,
                    "申报主体名称": org,
                    "XMLB": business_section,
                    "项目类别": business_section,
                    "奖项等级": level,
                },
                "title": business_section,
                "source_section_title": row_section,
                "category_values": [
                    value for value in (business_section, level) if value
                ],
                "level_values": [level] if level else [],
                "document_complete": not bool(page.get("truncated", False)),
                "source_anchor": f"image:{page.get('page', 0)}:row:{row_number}",
            })
    manifest = hashlib.sha256(
        "\n".join(item["sha256"] for item in image_sources).encode("ascii")
    ).hexdigest()
    parent_url = next((asset.parent_url for asset in updated if asset.kind == "image"), "")
    aggregate = EvidenceAssetRecord(
        url=parent_url or next(asset.url for asset in updated if asset.kind == "image"),
        parent_url=parent_url,
        label="LangGraph multi-image roster",
        kind="image_collection",
        status="parsed",
        sha256=manifest,
        fetched_at=utc_now(),
        extraction_method="langgraph_multi_image_roster",
        truncated=any(bool(page.get("truncated", False)) for page in roster_pages),
        metadata={
            "summary": (
                f"A verified ordered collection of {len(image_sources)} page images; "
                f"{len(roster_pages)} roster pages yielded {len(records)} records. "
                "Visible sections: "
                + "; ".join(dict.fromkeys(
                    str(record.get("title", ""))
                    for record in records
                    if str(record.get("title", "")).strip()
                ))[:2500]
            ),
            "anchors": [f"image:{page.get('page', 0)}" for page in roster_pages],
            "sample_rows": [
                [
                    str(record["row_values"].get("项目名称", "")),
                    str(record["row_values"].get("申报主体名称", "")),
                    str(record["row_values"].get("项目类别", "")),
                ]
                for record in records[:10]
            ],
            "vision_pages": roster_pages,
            "semantic_records": records,
            "image_sources": image_sources,
            "logical_manifest_sha256": manifest,
            "declared_scope_segments": [
                {"scope_label": label, "declared_count": count, "page": page}
                for label, count, page in declared_segments
            ],
            "declared_scope_total": declared_total,
            "scope_segmentation_complete": (
                len(normalized_scope_labels) <= 1
                or (
                    declared_segmentation_complete
                    and declared_total == len(records)
                )
            ),
        },
    )
    return updated, aggregate, roster_keys


def _hydrate_graph_pdf_assets(
    assets: Sequence[EvidenceAssetRecord],
    graph_state: Mapping[str, Any],
    scope_labels: Sequence[str] = (),
) -> tuple[list[EvidenceAssetRecord], set[tuple[str, str]]]:
    """Attach rendered-page OCR/vision records to their source PDF asset."""

    by_sha = {asset.sha256: asset for asset in assets if asset.kind == "pdf"}
    replacements: dict[str, EvidenceAssetRecord] = {}
    hydrated_keys: set[tuple[str, str]] = set()
    for observation in graph_state.get("observations", []):
        if not isinstance(observation, Mapping) or observation.get("tool_name") != "render_pdf_pages":
            continue
        summary = observation.get("summary", {})
        data = summary.get("data", {}) if isinstance(summary, Mapping) else {}
        pdf_sha = str(summary.get("sha256", ""))
        pdf_asset = by_sha.get(pdf_sha)
        rendered_pages = data.get("pages", []) if isinstance(data, Mapping) else []
        if pdf_asset is None or not isinstance(rendered_pages, list):
            continue
        total_pages = int(pdf_asset.metadata.get("page_count", 0) or 0)
        virtual_pages = [EvidenceAssetRecord(
            url=f"{pdf_asset.url}#page={int(page.get('page', 0) or 0)}",
            parent_url=pdf_asset.url,
            label=f"{pdf_asset.label} page {int(page.get('page', 0) or 0)}",
            kind="image",
            status="downloaded",
            sha256=str(page.get("sha256", "")),
            local_path=str(page.get("path", "")),
            fetched_at=pdf_asset.fetched_at or utc_now(),
            metadata={
                "page": int(page.get("page", 0) or 0),
                "total_pages": total_pages,
                "derived_from_sha256": pdf_sha,
            },
        ) for page in rendered_pages
            if isinstance(page, Mapping)
            and str(page.get("sha256", ""))
            and str(page.get("path", ""))]
        page_hashes = {page.sha256 for page in virtual_pages}
        filtered_observations: list[dict[str, Any]] = []
        for item in graph_state.get("observations", []):
            if not isinstance(item, Mapping) or item.get("tool_name") not in {
                "ocr_image", "vision_extract_roster"
            }:
                continue
            item_summary = item.get("summary", {})
            item_data = item_summary.get("data", {}) if isinstance(item_summary, Mapping) else {}
            pages = [
                page for page in item_data.get("pages", [])
                if isinstance(page, Mapping)
                and str(page.get("image_sha256", "")) in page_hashes
            ] if isinstance(item_data, Mapping) else []
            if pages:
                filtered_observations.append({
                    **item,
                    "summary": {**item_summary, "data": {**item_data, "pages": pages}},
                })
        _physical, aggregate, _keys = _hydrate_graph_image_assets(
            virtual_pages,
            {"observations": filtered_observations},
            scope_labels,
        )
        if aggregate is None:
            continue
        metadata = {
            **pdf_asset.metadata,
            **aggregate.metadata,
            "summary": (
                f"Scanned PDF pages were rendered, OCR-checked, and vision-structured; "
                f"{len(aggregate.metadata.get('semantic_records', []))} records recovered."
            ),
            "rendered_page_sources": aggregate.metadata.get("image_sources", []),
        }
        hydrated = pdf_asset.model_copy(update={
            "status": "parsed",
            "extraction_method": "langgraph_pdf_ocr_vision",
            "truncated": aggregate.truncated,
            "metadata": metadata,
        })
        replacements[pdf_sha] = hydrated
        hydrated_keys.add(asset_packet_key(hydrated))
    return [replacements.get(asset.sha256, asset) for asset in assets], hydrated_keys


def _source_comparison_identity(
    record: Mapping[str, object],
    *,
    primary_fields: Sequence[object],
    discriminator_fields: Sequence[object],
    supplemental_fields: Sequence[object] = (),
    ambiguous_primaries: set[str],
    role_type: str = "",
) -> tuple[str, str]:
    """Retain source-side fallback identity fields for later conflict detection."""

    primary = _source_identity_for_scope(record, primary_fields)
    if not primary:
        return primary, ""
    # Prefer a template's alternative primary identity (for example, project
    # name after patent number) before generic applicant/unit discriminators.
    # The composite is retained even for a source-only number: it lets the
    # comparison layer expose a same-name/different-code conflict.
    for fields in (supplemental_fields, discriminator_fields):
        discriminator = _source_identity_for_scope(record, fields)
        if discriminator and normalize_identity(discriminator) != normalize_identity(primary):
            return f"{primary};{discriminator}", ""
    if normalize_comparison_identity(primary, role_type=role_type) not in ambiguous_primaries:
        return primary, ""
    return primary, (
        "source row cannot disambiguate a duplicated submitted primary identity: "
        f"{primary}"
    )


def _identity_part(
    value: str, width: int, *, role_type: str = ""
) -> tuple[str, str]:
    """Return normalized primary and secondary portions of a stored identity."""

    parts = [part.strip() for part in value.split(";") if part.strip()]
    primary = ";".join(parts[:width])
    secondary = ";".join(parts[width:])
    return (
        normalize_comparison_identity(primary, role_type=role_type),
        normalize_comparison_identity(secondary, role_type=role_type),
    )


def _compare_source_identities(
    submitted: set[str],
    source_values: Sequence[str],
    *,
    primary_width: int,
    primary_fields: Sequence[object],
    role_type: str = "",
) -> tuple[list[str], list[str], list[dict[str, str]]]:
    """Match exact composites and isolate same-name/different-code conflicts.

    A code is still the first identity field, but duplicated codes must not
    consume every submitted row.  If both sides expose a secondary identity
    (normally a project/result name), equality requires the complete pair.
    A unique equal secondary value with a different primary is evidence of a
    field conflict, not a missing item plus an unrelated extra item.
    """

    submitted_by_primary: dict[str, list[str]] = {}
    submitted_by_secondary: dict[str, list[str]] = {}
    for value in submitted:
        primary, secondary = _identity_part(
            value, primary_width, role_type=role_type
        )
        if primary:
            submitted_by_primary.setdefault(primary, []).append(value)
        if secondary:
            submitted_by_secondary.setdefault(secondary, []).append(value)

    matched: set[str] = set()
    extra: set[str] = set()
    conflicts: list[dict[str, str]] = []
    primary_label = "+".join(str(field) for field in primary_fields) or "primary"
    for source in source_values:
        source_primary, source_secondary = _identity_part(
            source, primary_width, role_type=role_type
        )
        primary_candidates = submitted_by_primary.get(source_primary, [])
        if len(primary_candidates) == 1:
            candidate = primary_candidates[0]
            _candidate_primary, candidate_secondary = _identity_part(
                candidate, primary_width, role_type=role_type
            )
            if source_secondary and candidate_secondary and source_secondary != candidate_secondary:
                conflicts.append({
                    "submitted": candidate,
                    "source": source,
                    "fields": primary_label,
                    "reason": "same_primary_different_secondary",
                })
            else:
                matched.add(candidate)
            continue
        if len(primary_candidates) > 1:
            exact = [
                candidate for candidate in primary_candidates
                if normalize_identity(candidate) == normalize_identity(source)
            ]
            if len(exact) == 1:
                matched.add(exact[0])
            else:
                for candidate in primary_candidates:
                    conflicts.append({
                        "submitted": candidate,
                        "source": source,
                        "fields": primary_label,
                        "reason": "duplicated_primary_not_disambiguated",
                    })
            continue
        secondary_candidates = submitted_by_secondary.get(source_secondary, [])
        if source_secondary and len(secondary_candidates) == 1:
            conflicts.append({
                "submitted": secondary_candidates[0],
                "source": source,
                "fields": primary_label,
                "reason": "same_secondary_different_primary",
            })
        else:
            extra.add(source)
    return sorted(matched), sorted(extra), conflicts


def _scope_discriminator_keys(
    scopes: Sequence[Mapping[str, Any]],
    expected_role: str,
) -> set[str]:
    """Return business fields that actually distinguish scopes of one role."""

    ignored = {"ZYLBM", "year", "LXNF", "HJNF"}
    values_by_key: dict[str, set[str]] = {}
    for scope in scopes:
        if str(scope.get("role_type", "")) != expected_role:
            continue
        business_scope = scope.get("business_scope", {})
        if not isinstance(business_scope, Mapping):
            continue
        for key, value in business_scope.items():
            normalized = normalize_identity(str(value))
            if str(key) not in ignored and normalized:
                values_by_key.setdefault(str(key), set()).add(normalized)
    return {key for key, values in values_by_key.items() if len(values) > 1}


def _record_matches_scope(
    record: Mapping[str, object],
    scope: Mapping[str, Any],
    discriminator_keys: set[str],
) -> bool:
    business_scope = scope.get("business_scope", {})
    if not isinstance(business_scope, Mapping) or not discriminator_keys:
        return True
    row_values = record.get("row_values", {})
    row_cells = row_values.values() if isinstance(row_values, Mapping) else ()
    context = normalize_identity(" ".join(
        str(value) for value in (
            record.get("title", ""),
            *record.get("category_values", []),
            *record.get("level_values", []),
            *row_cells,
        ) if str(value).strip()
    ))
    return all(
        normalize_identity(str(business_scope.get(key, ""))) in context
        for key in discriminator_keys
    )


def _records_for_assessment_scope(
    records: Sequence[Mapping[str, object]],
    *,
    scope: Mapping[str, object],
    expected_role: str,
    discriminator_keys: set[str],
    document_routed_scope_count: int,
) -> list[Mapping[str, object]]:
    """Use document routing for title-only scope dimensions, not row matching.

    A selected PDF title can establish a parent category while its table rows
    only repeat a child category.  Retain every discriminator that actually
    occurs in the table; omit only title-level dimensions.  If no table-level
    discriminator remains, use all rows only for a uniquely routed document.
    """

    role_records = [
        record
        for record in records
        if str(record.get("role_type", "")) == expected_role
    ]
    present_discriminator_keys = {
        key for key in discriminator_keys
        if any(_record_matches_scope(record, {
            "business_scope": {key: scope.get("business_scope", {}).get(key, "")},
        }, {key}) for record in role_records)
    }
    matching_records = [
        record
        for record in role_records
        if _record_matches_scope(record, scope, present_discriminator_keys)
    ]
    if present_discriminator_keys:
        return matching_records
    if document_routed_scope_count != 1:
        return []
    # The bounded Agent route establishes the document-level scope. Some
    # attachment tables repeat row identities but not their full scope label.
    return role_records


_PDF_LIST_SECTION = re.compile(r"^\s*[一二三四五六七八九十]+、\s*(.+?)\s*$")
_PDF_LIST_ITEM = re.compile(r"^\s*\d+[\.．、]\s*(.+?)\s*$")


def _pdf_numbered_list_records(
    pages: Sequence[pdf_tools.PdfTextPage],
) -> list[dict[str, object]]:
    """Adapt a headed, numbered PDF roster when no extractable table exists."""

    records: list[dict[str, object]] = []
    section = ""
    for page in pages:
        for line_number, raw_line in enumerate(page.text.splitlines(), start=1):
            line = " ".join(raw_line.split())
            if not line:
                continue
            section_match = _PDF_LIST_SECTION.match(line)
            if section_match:
                section = section_match.group(1).strip()
                continue
            item_match = _PDF_LIST_ITEM.match(line)
            if not item_match or not section:
                continue
            identity = item_match.group(1).strip()
            if not identity:
                continue
            records.append({
                "sheet": f"PDF page {page.page}",
                "row_number": len(records) + 1,
                "role_type": "unclassified",
                "identity": identity,
                "identity_field": "名单项",
                "row_values": {"名单项": identity},
                "title": section,
                "category_values": [section],
                "level_values": [],
                "document_complete": not page.is_truncated,
                "source_anchor": f"page:{page.page}:line:{line_number}",
            })
    return records


_PDF_TABLE_IDENTITY_HEADERS = (
    "参赛队伍", "参赛队伍名称", "队伍名称", "团队名称",
    "作品名称", "项目名称",
)


def _pdf_whitespace_table_records(
    pages: Sequence[pdf_tools.PdfTextPage],
) -> tuple[list[dict[str, object]], bool]:
    """Parse text PDFs whose visual columns are separated by repeated spaces."""

    records: list[dict[str, object]] = []
    headers: list[str] = []
    sequence: list[int] = []
    for page in pages:
        for raw_line in page.text.splitlines():
            cells = [cell.strip() for cell in re.split(r"\s{2,}", raw_line.strip()) if cell.strip()]
            if len(cells) >= 3 and cells[0] == "序号":
                headers = cells
                continue
            if not headers or not cells or not cells[0].isdigit():
                continue
            if len(cells) != len(headers):
                continue
            row_values = dict(zip(headers, cells, strict=True))
            identity_header = next((
                header for header in _PDF_TABLE_IDENTITY_HEADERS
                if str(row_values.get(header, "")).strip()
            ), "")
            if not identity_header:
                continue
            number = int(cells[0])
            identity = str(row_values[identity_header]).strip()
            sequence.append(number)
            records.append({
                "sheet": f"PDF page {page.page}",
                "row_number": number,
                "role_type": "team" if "队伍" in identity_header or "团队" in identity_header else "unclassified",
                "identity": identity,
                "identity_field": identity_header,
                "row_values": row_values,
                "title": headers[0] + "/" + identity_header,
                "category_values": [],
                "level_values": [str(row_values.get("奖项", "")).strip()]
                if str(row_values.get("奖项", "")).strip() else [],
                "document_complete": not page.is_truncated,
                "source_anchor": f"page:{page.page}:row:{number}",
            })
    sequence_complete = bool(sequence) and sequence == list(range(1, len(sequence) + 1))
    return records, sequence_complete


def _pdf_semantic_roster_records(
    path: Path,
    metadata: Mapping[str, object],
) -> tuple[list[dict[str, object]], bool]:
    """Adapt verified PDF tables into the same bounded records as spreadsheets."""

    page_count = int(metadata.get("page_count", 0) or 0)
    if not 1 <= page_count <= pdf_tools.MAX_PDF_PAGES:
        raise ValueError("M4 PDF page count is missing or outside the allowed limit")
    pages = pdf_tools.extract_pdf_text(
        path,
        list(range(1, page_count + 1)),
        max_pages=pdf_tools.MAX_PDF_PAGES,
    )
    if len(pages) != page_count:
        raise ValueError("PDF extraction did not return every M4-verified page")
    sheet_grids: list[dict[str, object]] = []
    continuation_headers: list[str] = []
    previous_data_row: list[object] | None = None
    numbered_table_rows = 0
    for page in pages:
        for table in page.tables:
            if not table.rows:
                continue
            rows = [list(row) for row in table.rows]
            header = next((
                [str(cell or "").strip() for cell in row]
                for row in rows[:30]
                if row and str(row[0] or "").strip() == "序号"
            ), [])
            if header:
                continuation_headers = header
            elif (
                continuation_headers
                and rows[0]
                and str(rows[0][0] or "").strip().isdigit()
                and len(rows[0]) == len(continuation_headers)
            ):
                rows.insert(0, continuation_headers)
            normalized_rows: list[list[object]] = []
            active_headers = header or continuation_headers
            for row in rows:
                if not row:
                    continue
                first_cell = str(row[0] or "").strip()
                if first_cell == "序号":
                    normalized_rows.append(row)
                    continue
                if not active_headers or len(row) != len(active_headers):
                    normalized_rows.append(row)
                    previous_data_row = None
                    continue
                if first_cell.isdigit():
                    numbered_table_rows += 1
                    expected = numbered_table_rows
                    expected_text = str(expected)
                    # Some PDF extractors append a one-digit footnote marker to
                    # large sequence values (for example 606 + superscript 1).
                    if (
                        expected >= 100
                        and len(first_cell) == len(expected_text) + 1
                        and first_cell.startswith(expected_text)
                    ):
                        row[0] = expected_text
                    normalized_rows.append(row)
                    previous_data_row = row
                    continue
                nonempty_tail = [
                    index for index, cell in enumerate(row[1:], start=1)
                    if str(cell or "").strip()
                ]
                if previous_data_row is not None and nonempty_tail:
                    for index in nonempty_tail:
                        continuation = str(row[index] or "").strip()
                        prior = str(previous_data_row[index] or "").strip()
                        previous_data_row[index] = "".join((prior, continuation))
                    continue
                normalized_rows.append(row)
                previous_data_row = None
            sheet_grids.append({
                "sheet": f"PDF page {page.page}",
                "rows": normalized_rows,
                "truncated": page.is_truncated or table.is_truncated,
            })
    records = extract_semantic_roster_records({"sheet_grids": sheet_grids})
    table_sequence = [
        str(record.get("row_values", {}).get("序号", "")).strip()
        for record in records
        if isinstance(record.get("row_values"), Mapping)
        and str(record.get("row_values", {}).get("序号", "")).strip()
    ]
    sequence_complete = not table_sequence or (
        len(table_sequence) == len(records) == numbered_table_rows
        and all(value.isdigit() for value in table_sequence)
        and [int(value) for value in table_sequence]
        == list(range(1, len(table_sequence) + 1))
    )
    if not records:
        records, sequence_complete = _pdf_whitespace_table_records(pages)
    if not records:
        records = _pdf_numbered_list_records(pages)
        sequence_complete = True
    complete = bool(records) and not any(
        page.is_truncated for page in pages
    ) and sequence_complete
    return records, complete


_HTML_ROSTER_LAYOUTS: tuple[tuple[str, tuple[str, ...], int], ...] = (
    ("team", ("\u5b66\u6821\u540d\u79f0", "\u961f\u4f0d\u540d\u79f0", "\u5956\u9879"), 1),
    ("team", ("\u5b66\u6821", "\u961f\u540d", "\u540d\u6b21"), 1),
    ("instructor_or_person", ("\u5b66\u6821\u540d\u79f0", "\u6307\u5bfc\u8001\u5e08"), 1),
    ("instructor_or_person", ("\u5b66\u6821\u540d\u79f0", "\u6307\u5bfc\u6559\u5e08"), 1),
    ("instructor_or_person", ("\u5b66\u6821", "\u6559\u5e08"), 1),
    ("organization", ("\u5e8f\u53f7", "\u5355\u4f4d"), 1),
    ("organization", ("\u83b7\u5956\u5355\u4f4d",), 0),
)
_HTML_FOOTER_MARKERS = {
    "\u5927\u8d5b\u6982\u51b5", "\u7ec4\u7ec7\u673a\u6784", "\u8054\u7cfb\u6211\u4eec",
    "\u7248\u6743\u6240\u6709", "\u4eacicp", "\u8fd4\u56de\u9996\u9875",
}
_HTML_SECTION_MARKERS = {
    "\u7ec4\u7ec7\u5355\u4f4d", "\u6307\u5bfc\u6559\u5e08", "\u4f18\u79c0\u7ec4\u7ec7\u5355\u4f4d",
}

def _html_semantic_roster_records(
    path: Path,
    *,
    expected_sha256: str,
    document_title: str,
) -> tuple[list[dict[str, object]], bool]:
    """Reconstruct simple, linearly extracted HTML roster tables locally."""

    payload = path.read_bytes()
    if not payload or len(payload) > 512 * 1024:
        raise ValueError("persisted HTML evidence is empty or exceeds the parser limit")
    actual_sha256 = hashlib.sha256(payload).hexdigest()
    if expected_sha256 and actual_sha256 != expected_sha256:
        raise ValueError("M4 HTML hash mismatch")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("persisted HTML evidence is not UTF-8") from exc
    lines = [" ".join(line.split()) for line in text.splitlines() if line.strip()]
    normalized_lines = [normalize_identity(line) for line in lines]
    layouts = [
        (role, headers, identity_index, [normalize_identity(value) for value in headers])
        for role, headers, identity_index in _HTML_ROSTER_LAYOUTS
    ]
    known_headers = {
        value for _role, _headers, _identity_index, normalized_headers in layouts
        for value in normalized_headers
    }

    records: list[dict[str, object]] = []
    for index in range(len(lines)):
        for role_type, headers, identity_index, normalized_headers in layouts:
            width = len(headers)
            if normalized_lines[index:index + width] != normalized_headers:
                continue
            row_index = index + width
            while row_index + width <= len(lines):
                cells = lines[row_index:row_index + width]
                normalized_cells = normalized_lines[row_index:row_index + width]
                if (
                    any(cell in known_headers for cell in normalized_cells)
                    or any(marker in cell for marker in _HTML_SECTION_MARKERS for cell in normalized_cells)
                    or any(marker in cell for marker in _HTML_FOOTER_MARKERS for cell in normalized_cells)
                ):
                    break
                identity = cells[identity_index].strip()
                if not identity:
                    break
                row_values = dict(zip(headers, cells, strict=True))
                level = cells[-1].strip() if width >= 3 else ""
                records.append({
                    "sheet": "HTML body",
                    "row_number": row_index + 1,
                    "role_type": role_type,
                    "identity": identity,
                    "identity_field": headers[identity_index],
                    "row_values": row_values,
                    "title": document_title,
                    "category_values": [],
                    "level_values": [level] if level else [],
                    "document_complete": True,
                })
                row_index += width
    return records, bool(records)


def _expected_scope_role(assessment_role: str, scope: Mapping[str, object]) -> str:
    if assessment_role == "mixed":
        return str(scope.get("role_type", ""))
    return _AGENT_TO_SOURCE_ROLE[assessment_role]


def _mixed_records_for_scope(
    records: Sequence[Mapping[str, object]],
    scope: Mapping[str, object],
) -> list[Mapping[str, object]]:
    """Filter a mixed-role numbered roster by the source section heading."""

    business_scope = scope.get("business_scope", {})
    if not isinstance(business_scope, Mapping):
        return []
    section_keys = {
        str(key)
        for key, value in business_scope.items()
        if str(key) not in {"ZYLBM", "year", "LXNF", "HJNF", "BZ"}
        and normalize_identity(str(value))
    }
    if not section_keys:
        return []
    return [
        record for record in records
        if str(record.get("role_type", "")) == "unclassified"
        and _record_matches_scope(record, scope, section_keys)
    ]


def _spreadsheet_comparison_facts(
    *,
    state: AuditCaseState,
    outcome: ReviewOutcome,
    asset_by_packet_id: Mapping[str, Any],
    scope_ids_by_key: Mapping[str, int],
    allowed_roots: Sequence[str | Path],
) -> list[dict[str, Any]]:
    """Create local facts for included and cross-scope official source assets."""

    raw_scopes = state.submitted_summary.get("role_scopes", [])
    scopes = {
        int(scope.get("scope_id", 0) or scope_ids_by_key.get(
            str(scope.get("scope_key", "")), 0
        )): scope
        for scope in raw_scopes if isinstance(raw_scopes, list)
        if isinstance(scope, Mapping) and int(scope.get("scope_id", 0) or scope_ids_by_key.get(
            str(scope.get("scope_key", "")), 0
        )) > 0
    }
    trusted_urls = {str(url) for url in state.known_urls if str(url)}
    pending_assets = list(asset_by_packet_id.values())
    for _ in range(len(pending_assets) + 1):
        added = False
        for candidate in pending_assets:
            url = str(getattr(candidate, "url", ""))
            parent_url = str(getattr(candidate, "parent_url", ""))
            if url and (url in trusted_urls or parent_url in trusted_urls):
                if url not in trusted_urls:
                    trusted_urls.add(url)
                    added = True
        if not added:
            break
    facts: list[dict[str, Any]] = []
    for assessment in outcome.assessments:
        if assessment.roster_contribution not in {"include", "cross_scope"}:
            continue
        cross_scope = assessment.roster_contribution == "cross_scope"
        asset = asset_by_packet_id.get(assessment.asset_id)
        if asset is None:
            continue
        relationship_fact = {
            "award_name": state.award_name,
            "year": state.year,
            "target_match": "yes",
            "year_match": "yes",
            "source_level": (
                "official_primary" if str(asset.url) in trusted_urls else "unknown"
            ),
            "relationship_terms": [
                assessment.material_relation,
                assessment.version_relation,
            ],
            "relationship_confirmed": True,
            "relationship_summary": assessment.reason[:500],
        }
        if not asset.local_path and asset.kind != "image_collection":
            for scope_id in assessment.scope_ids:
                scope = scopes.get(scope_id, {})
                facts.append({
                    **relationship_fact,
                    "scope_id": scope_id,
                    "role_type": _expected_scope_role(assessment.role, scope),
                    "source_url": asset.url,
                    "status": "unverified",
                    "document_complete": False,
                    "missing_evidence": [
                        "selected asset has no supported local spreadsheet parser"
                    ],
                })
            continue
        try:
            if asset.kind == "image_collection":
                metadata = asset.metadata if isinstance(asset.metadata, Mapping) else {}
                raw_records = metadata.get("semantic_records", [])
                records = [
                    dict(record) for record in raw_records if isinstance(record, Mapping)
                ] if isinstance(raw_records, list) else []
                vision_pages = metadata.get("vision_pages", [])
                document_complete = bool(records) and all(
                    not bool(page.get("truncated", False))
                    and not page.get("unreadable", [])
                    and float(page.get("confidence", 0) or 0) >= 0.85
                    and bool(page.get("all_rows_extracted", True))
                    and int(page.get("visible_row_count", len(page.get("entries", []))) or 0)
                    == len(page.get("entries", []))
                    for page in vision_pages
                    if isinstance(page, Mapping)
                ) and bool(metadata.get("scope_segmentation_complete", True))
                extraction_method = "langgraph_multi_image_roster"
            else:
                path = validate_local_path(asset.local_path, allowed_roots, file_only=True)
            if asset.kind == "html":
                records, document_complete = _html_semantic_roster_records(
                    path,
                    expected_sha256=asset.sha256,
                    document_title=str(asset.metadata.get("title", asset.label)),
                )
                extraction_method = "review_agent_selected_html_roster"
            elif asset.kind == "pdf":
                inspection = inspect_evidence_file(
                    path, max_bytes=20 * 1024 * 1024, allowed_kinds={"pdf"}
                )
                if asset.sha256 and asset.sha256 != inspection.sha256:
                    raise ValueError("M4 asset hash mismatch")
                metadata = asset.metadata if isinstance(asset.metadata, Mapping) else {}
                graph_records = metadata.get("semantic_records", [])
                if isinstance(graph_records, list) and graph_records:
                    records = [
                        dict(record) for record in graph_records
                        if isinstance(record, Mapping)
                    ]
                    document_complete = bool(records) and not asset.truncated and all(
                        bool(record.get("document_complete", True)) for record in records
                    )
                    extraction_method = "review_agent_selected_pdf_ocr_vision"
                else:
                    records, document_complete = _pdf_semantic_roster_records(path, metadata)
                    extraction_method = "review_agent_selected_pdf_table"
            elif asset.kind != "image_collection":
                inspection = inspect_evidence_file(
                    path, max_bytes=20 * 1024 * 1024, allowed_kinds={"xlsx", "xls"}
                )
                if asset.sha256 and asset.sha256 != inspection.sha256:
                    raise ValueError("M4 asset hash mismatch")
                grid = parse_award_excel(path)
                records = extract_semantic_roster_records(grid)
                document_complete = not bool(grid.get("truncated", False))
                extraction_method = "review_agent_selected_spreadsheet"
        except Exception as exc:  # noqa: BLE001 - evidence remains incomplete on adapter failure.
            for scope_id in assessment.scope_ids:
                scope = scopes.get(scope_id, {})
                facts.append({
                    "scope_id": scope_id,
                    "role_type": _expected_scope_role(assessment.role, scope),
                    "source_url": asset.url,
                    "status": "unverified",
                    "document_complete": False,
                    "missing_evidence": [f"local comparison failed: {type(exc).__name__}"],
                })
            continue
        for scope_id in assessment.scope_ids:
            scope = scopes.get(scope_id)
            if scope is None:
                continue
            expected_role = _expected_scope_role(assessment.role, scope)
            role_records = [
                {
                    **record,
                    "role_type": (
                        expected_role
                        if str(record.get("role_type", "")) == "unclassified"
                        else str(record.get("role_type", ""))
                    ),
                }
                for record in records
            ]
            discriminator_keys = _scope_discriminator_keys(
                list(scopes.values()), expected_role
            )
            alternatives = scope.get("profile", {}).get("primary_alternatives", [])
            candidate_alternatives = [
                fields for fields in alternatives if isinstance(fields, list) and fields
            ]
            if assessment.role == "mixed":
                # Section headings are the discriminator for numbered mixed-role
                # rosters. Preserve their unclassified role until this selection
                # and source-identity fallback have both run.
                scoped_records = _mixed_records_for_scope(records, scope)
                if not scoped_records:
                    # HTML pages can expose explicit team/person/organization
                    # table roles in one document.
                    scoped_records = [
                        record for record in records
                        if str(record.get("role_type", "")) == expected_role
                    ]
            elif cross_scope:
                scoped_records = [
                    record for record in role_records
                    if str(record.get("role_type", "")) == expected_role
                ]
            else:
                scoped_records = _records_for_assessment_scope(
                    role_records,
                    scope=scope,
                    expected_role=expected_role,
                    discriminator_keys=discriminator_keys,
                    document_routed_scope_count=(
                        len(assessment.scope_ids)
                        # A workbook may carry the same document-level scope signal
                        # as a PDF.  When its rows do not repeat that scope field,
                        # a unique bounded Agent route is the only valid selector.
                        if assessment.subunit_id == "document"
                        else 0
                    ),
                )
            matching_records = [
                record for record in scoped_records
                if assessment.subunit_id == "document"
                or str(record.get("sheet", "")) == assessment.subunit_id
            ]
            primary_fields = next((
                fields for fields in candidate_alternatives
                if any(_source_identity_for_scope(record, fields) for record in matching_records)
            ), [])
            supplemental_fields = next((
                fields for fields in candidate_alternatives
                if fields != primary_fields
                and any(_source_identity_for_scope(record, fields) for record in matching_records)
            ), [])
            submitted = {
                str(value) for value in scope.get("submitted_identities", {}).values()
                if str(value).strip()
            }
            width = len(primary_fields) or 1
            primary_counts = Counter(
                _primary_identity(value, width, role_type=expected_role)
                for value in submitted
                if _primary_identity(value, width, role_type=expected_role)
            )
            ambiguous_primaries = {
                value for value, count in primary_counts.items() if count > 1
            }
            discriminator_fields = scope.get("profile", {}).get(
                "discriminator_fields", []
            )
            if not isinstance(discriminator_fields, list):
                discriminator_fields = []
            source_values: set[str] = set()
            source_identity_anchors: dict[str, str] = {}
            source_ambiguities: list[str] = []
            for record in matching_records:
                source_value, ambiguity = _source_comparison_identity(
                    record,
                    primary_fields=primary_fields,
                    discriminator_fields=discriminator_fields,
                    supplemental_fields=supplemental_fields,
                    ambiguous_primaries=ambiguous_primaries,
                    role_type=expected_role,
                )
                if source_value:
                    source_values.add(source_value)
                    source_identity_anchors[source_value] = str(
                        record.get("source_anchor", "")
                        or f"{record.get('sheet', '')}:row:{record.get('row_number', '')}"
                    )[:500]
                if ambiguity:
                    source_ambiguities.append(ambiguity)
            source_values = sorted(source_values)
            matched, extra, identity_conflicts = _compare_source_identities(
                submitted,
                source_values,
                primary_width=width,
                primary_fields=primary_fields,
                role_type=expected_role,
            )
            complete = bool(source_values) and document_complete
            cross_scope_matches = [
                {
                    "identity": value,
                    "source_url": asset.url,
                    "source_label": asset.label,
                    "reason": assessment.reason,
                }
                for value in matched
            ] if cross_scope else []
            facts.append({
                **relationship_fact,
                "scope_id": scope_id,
                "role_type": expected_role,
                "source_url": asset.url,
                "status": "out_of_scope" if cross_scope else (
                    "complete" if complete else "partial"
                ),
                "document_complete": complete,
                "coverage_complete": complete,
                "contributes_to_scope": not cross_scope,
                "expected_count": len(submitted),
                "observed_count": len(source_values),
                "submitted_items": sorted(submitted),
                "source_identity_anchors": source_identity_anchors,
                "matched_items": [] if cross_scope else matched,
                "extra_items": [] if cross_scope else extra,
                "related_out_of_scope": cross_scope_matches,
                "identity_conflicts": [] if cross_scope else identity_conflicts,
                "contradictions": list(dict.fromkeys(source_ambiguities)),
                "missing_evidence": (
                    [] if complete else ["no readable source identities for selected scope"]
                ),
                "extraction_method": extraction_method,
            })
    return facts


def _graph_web_comparison_facts(
    graph_state: Mapping[str, Any],
    packet: Any,
) -> list[dict[str, Any]]:
    """Adapt one complete, scope-unambiguous web extraction for the Verifier."""

    required_scopes = [scope for scope in packet.scopes if scope.required]
    if len(required_scopes) != 1:
        return []
    scope = required_scopes[0]
    facts_by_url: dict[str, dict[str, Any]] = {}
    for observation in graph_state.get("observations", []):
        if not isinstance(observation, Mapping) or not observation.get("ok"):
            continue
        tool_name = str(observation.get("tool_name", ""))
        if tool_name not in {"fetch_web_page", "extract_search_document"}:
            continue
        summary = observation.get("summary", {})
        data = summary.get("data", {}) if isinstance(summary, Mapping) else {}
        if (
            not isinstance(data, Mapping)
            or data.get("coverage_complete") is not True
            or data.get("award_name_match") is False
            or data.get("year_match") is False
        ):
            continue
        source_url = str(
            summary.get("source_url", "") or data.get("source_url", "")
        ).strip()
        if not source_url:
            continue
        matched_items = [str(item) for item in data.get("matched_items", []) if str(item)]
        missing_items = [str(item) for item in data.get("missing_items", []) if str(item)]
        extra_items = [str(item) for item in data.get("extra_items", []) if str(item)]
        observed_count = data.get("observed_count")
        expected_count = data.get("expected_count")
        facts_by_url[source_url] = {
            "scope_id": scope.scope_id,
            "role_type": scope.source_role_type,
            "source_url": source_url,
            "status": "complete",
            "award_name": str(data.get("observed_award_name", "") or packet.award_name),
            "year": str(data.get("observed_year", "") or packet.year),
            "target_match": "yes",
            "year_match": "yes",
            "source_level": str(data.get("source_level", "unknown")),
            "expected_count": expected_count,
            "observed_count": observed_count,
            "submitted_count": packet.submission_summary.submitted_rows,
            "reference_count": data.get("page_total_count") or expected_count,
            "coverage_complete": True,
            "document_complete": True,
            "contributes_to_scope": True,
            "comparison_scope": "submitted_roster",
            "extraction_method": f"langgraph_{tool_name}",
            "matched_items": matched_items,
            "split_matched_items": [
                str(item) for item in data.get("split_matched_items", []) if str(item)
            ],
            "missing_items": missing_items,
            "extra_items": extra_items,
            "missing_item_count": len(missing_items),
            "extra_item_count": len(extra_items),
            "missing_evidence": [],
            "relationship_terms": [
                str(item) for item in data.get("relationship_terms", []) if str(item)
            ][:8],
            "relationship_confirmed": data.get("relationship_confirmed"),
            "relationship_summary": str(data.get("relationship_summary", ""))[:500],
        }
    return list(facts_by_url.values())


def _merge_complete_graph_web_facts(
    facts: Sequence[Mapping[str, Any]],
    web_facts: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Use one coherent complete web source instead of mixing earlier partial facts."""

    complete_scope_ids = {
        int(fact.get("scope_id", 0) or 0)
        for fact in web_facts
        if fact.get("coverage_complete") is True
    }
    retained = [
        dict(fact) for fact in facts
        if int(fact.get("scope_id", 0) or 0) not in complete_scope_ids
    ]
    return [*retained, *[dict(fact) for fact in web_facts]]


def _identity_candidates_from_facts(
    facts: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Generate bounded person candidates without declaring semantic equality."""

    candidates: list[dict[str, Any]] = []
    for fact in facts:
        if (
            str(fact.get("role_type", "")) != "instructor_or_person"
            or fact.get("contributes_to_scope", True) is False
        ):
            continue
        submitted = {str(value) for value in fact.get("submitted_items", []) if str(value)}
        matched = {str(value) for value in fact.get("matched_items", []) if str(value)}
        conflicted = {
            str(item.get("submitted", ""))
            for item in fact.get("identity_conflicts", [])
            if isinstance(item, Mapping) and str(item.get("submitted", ""))
        }
        missing = sorted(submitted - matched - conflicted)
        extras = [str(value) for value in fact.get("extra_items", []) if str(value)]
        anchors = fact.get("source_identity_anchors", {})
        anchors = anchors if isinstance(anchors, Mapping) else {}
        for submitted_value in missing:
            normalized_submitted = normalize_identity(submitted_value)
            ranked: list[tuple[float, str]] = []
            for source_value in extras:
                normalized_source = normalize_identity(source_value)
                if not normalized_submitted or not normalized_source:
                    continue
                score = SequenceMatcher(
                    None, normalized_submitted, normalized_source
                ).ratio()
                if (
                    normalized_submitted in normalized_source
                    or normalized_source in normalized_submitted
                ):
                    score = max(score, 0.9)
                if score >= 0.55:
                    ranked.append((score, source_value))
            for score, source_value in sorted(ranked, reverse=True)[:3]:
                fingerprint = "\n".join([
                    str(fact.get("scope_id", 0)), submitted_value, source_value
                ])
                candidates.append({
                    "candidate_id": "identity:" + hashlib.sha256(
                        fingerprint.encode("utf-8")
                    ).hexdigest()[:24],
                    "scope_id": int(fact.get("scope_id", 0) or 0),
                    "role_type": "instructor_or_person",
                    "submitted": submitted_value,
                    "source": source_value,
                    "source_url": str(fact.get("source_url", "")),
                    "source_anchor": str(anchors.get(source_value, "")),
                    "local_similarity": round(score, 4),
                })
    if len(candidates) > 100:
        raise ValueError("semantic identity candidate budget exceeded")
    return candidates


def _apply_identity_adjudications(
    facts: list[dict[str, Any]],
    candidates: Sequence[Mapping[str, Any]],
    raw_response: object,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Validate model decisions and apply only one-to-one accepted identities."""

    batch = IdentityAdjudicationBatch.model_validate(raw_response)
    candidate_by_id = {
        str(candidate["candidate_id"]): dict(candidate) for candidate in candidates
    }
    decision_ids = {item.candidate_id for item in batch.decisions}
    if decision_ids != set(candidate_by_id):
        raise ValueError("identity adjudication must cover exactly the supplied candidates")

    accepted = [
        (item, candidate_by_id[item.candidate_id])
        for item in batch.decisions
        if item.decision == "same_identity"
    ]
    submitted_keys = [
        (int(candidate["scope_id"]), str(candidate["submitted"]))
        for _item, candidate in accepted
    ]
    source_keys = [
        (int(candidate["scope_id"]), str(candidate["source"]))
        for _item, candidate in accepted
    ]
    if len(submitted_keys) != len(set(submitted_keys)) or len(source_keys) != len(set(source_keys)):
        raise ValueError("accepted identity decisions violate one-to-one matching")

    safe_decisions: list[dict[str, Any]] = []
    for item in batch.decisions:
        candidate = candidate_by_id[item.candidate_id]
        safe = {
            **candidate,
            "decision": item.decision,
            "confidence": item.confidence,
            "reason": item.reason,
        }
        safe_decisions.append(safe)
        fact = next((
            value for value in facts
            if int(value.get("scope_id", 0) or 0) == int(candidate["scope_id"])
            and str(candidate["source"]) in value.get("source_identity_anchors", {})
        ), None)
        if fact is None:
            raise ValueError("identity decision references no live unmatched source fact")
        fact.setdefault("semantic_identity_decisions", []).append(safe)
        if item.decision == "same_identity":
            if str(candidate["source"]) not in fact.get("extra_items", []):
                raise ValueError("accepted identity source was already consumed")
            fact.setdefault("matched_items", []).append(str(candidate["submitted"]))
            fact["matched_items"] = sorted(set(fact["matched_items"]))
            fact["extra_items"] = [
                value for value in fact.get("extra_items", [])
                if str(value) != str(candidate["source"])
            ]
        elif item.decision == "field_conflict":
            fact.setdefault("identity_conflicts", []).append({
                "submitted": str(candidate["submitted"]),
                "source": str(candidate["source"]),
                "fields": "identity",
                "reason": "semantic_field_conflict: " + item.reason,
                "source_url": str(candidate.get("source_url", "")),
                "source_anchor": str(candidate.get("source_anchor", "")),
            })
    return facts, safe_decisions


def _verification_results_from_comparison_facts(
    facts: Sequence[Mapping[str, Any]],
) -> list[ToolResult]:
    """Adapt deterministic comparison facts to the Verifier's public protocol."""

    supported_statuses = {"complete", "partial", "missing", "conflict", "unverified"}
    evidence_facts = [
        EvidenceFact.model_validate({
            key: value
            for key, value in fact.items()
            if key in EvidenceFact.model_fields
        } | {
            "status": (
                str(fact.get("status", "unverified"))
                if str(fact.get("status", "unverified")) in supported_statuses
                else "unverified"
            ),
        })
        for fact in facts
        if fact.get("contributes_to_scope", True) is not False
    ]
    return [ToolResult(ok=True, evidence_facts=evidence_facts)] if evidence_facts else []


class SemanticReviewRunner:
    """Persist the semantic decision before local comparison is attempted."""

    def __init__(
        self,
        repository: CaseRepository,
        review_agent: ReviewAgent | None = None,
        *,
        review_llm: ReviewLlm | None = None,
        investigation_agent: InvestigationAgent | None = None,
        allowed_roots: Sequence[str | Path] = (),
        tool_limits: ToolBudgetLimits | None = None,
    ) -> None:
        if review_agent is None and review_llm is None:
            raise ValueError("SemanticReviewRunner requires review_agent or review_llm")
        self._repository = repository
        self._review_agent = review_agent
        self._review_llm = review_llm
        self._investigation_agent = investigation_agent
        self._allowed_roots = tuple(allowed_roots)
        self._tool_limits = tool_limits

    def _run_langgraph_case(
        self,
        *,
        state: AuditCaseState,
        started_at: float,
        packet: Any,
        assets: Sequence[Any],
        packet_asset_records: Mapping[tuple[str, str], Any],
        scope_ids_by_key: Mapping[str, int],
    ) -> HarnessOutcome:
        """Execute routing, matching, verification and persistence inside LangGraph."""

        if self._investigation_agent is None:
            raise RuntimeError("LangGraph runner requires an investigation agent")
        workflow: dict[str, Any] = {}
        working_assets = list(assets)
        asset_by_packet_id = {
            parsed.asset_id: packet_asset_records[(parsed.asset_id, parsed.subunit_id)]
            for parsed in packet.assets
        }

        def semantic_route_assets(graph_state: Mapping[str, Any]) -> Mapping[str, Any]:
            nonlocal packet, working_assets
            current = self._repository.load(state.case_id)
            scope_labels = [
                str(scope.business_scope.get("XMLB", "")).strip()
                for scope in packet.scopes
                if str(scope.business_scope.get("XMLB", "")).strip()
            ]
            working_assets, image_collection, roster_image_keys = _hydrate_graph_image_assets(
                working_assets,
                graph_state,
                scope_labels,
            )
            working_assets, _hydrated_pdf_keys = _hydrate_graph_pdf_assets(
                working_assets,
                graph_state,
                scope_labels,
            )
            routing_assets = list(working_assets)
            if image_collection is not None:
                image_collection = image_collection.model_copy(update={
                    "label": f"{packet.award_name} {packet.year} multi-image roster",
                    "metadata": {
                        **image_collection.metadata,
                        "summary": (
                            f"Candidate roster discovered on the registered parent page for "
                            f"{packet.award_name} {packet.year}. The submitted baseline expects "
                            f"{packet.submission_summary.submitted_rows} records across "
                            f"{len(packet.scopes)} scopes: {'; '.join(scope_labels)}. "
                            + str(image_collection.metadata.get("summary", ""))
                        )[:4000],
                        "expected_record_count": packet.submission_summary.submitted_rows,
                        "expected_scope_labels": scope_labels,
                    },
                })
                routing_assets = [
                    asset for asset in working_assets if asset.kind != "image"
                ] + [image_collection]
            packet = build_review_case_packet(
                case_id=state.case_id,
                context=_context_from_state(current),
                scope_ids_by_key=scope_ids_by_key,
                assets=routing_assets,
                source_candidates=[{
                    "url": url,
                    "title": "",
                    "source_level": "unknown",
                } for url in current.known_urls],
            )
            asset_by_packet_id.clear()
            asset_by_packet_id.update({
                asset_packet_key(asset)[0]: asset for asset in routing_assets
            })
            plan_calls = len(graph_state.get("actions", []))
            current.budget.calls += plan_calls
            remaining_agent_calls = current.budget.limits.max_calls - current.budget.calls
            remaining_asset_reads = (
                current.budget.limits.max_asset_calls - current.budget.asset_calls
            )
            call_budget_exhausted = (
                remaining_agent_calls < 3 or remaining_asset_reads < 0
            )
            if call_budget_exhausted:
                run = ReviewAgentRun(
                    outcome=ReviewOutcome(
                        case_recommendation="evidence_insufficient",
                        reason="语义审核预算已耗尽，未执行新的模型调用。",
                    ),
                    trace=ReviewAgentTrace(blockers=["semantic_agent_budget_exhausted"]),
                )
            else:
                agent = self._review_agent or ReviewAgent(
                    self._review_llm,
                    M4AssetReader(asset_by_packet_id, allowed_roots=self._allowed_roots),
                    max_material_requests=min(10, remaining_asset_reads),
                )
                run = agent.run(packet)
                current.budget.calls += run.trace.model_call_count
                current.budget.asset_calls += run.trace.request_count
            route_by_asset = _semantic_routes(run.outcome)
            image_collection_routes = (
                route_by_asset.get(asset_packet_key(image_collection), [])
                if image_collection is not None else []
            )
            updated_assets = [
                asset.model_copy(update={
                    "metadata": {
                        **asset.metadata,
                        "routes": (
                            image_collection_routes
                            if asset.kind == "image"
                            and asset_packet_key(asset) in roster_image_keys
                            else ([{
                                "scope_id": None,
                                "subunit_type": "document",
                                "selector": {
                                    "material_relation": "supplement",
                                    "version_relation": "same",
                                    "roster_contribution": "exclude",
                                    "requires_human_confirmation": False,
                                },
                                "route_source": "llm",
                                "confidence": 0.99,
                                "route_status": "excluded",
                                "reason": (
                                    "The Graph OCR quality stage classified this physical "
                                    "page as non-roster decoration within the reviewed image collection."
                                ),
                            }] if asset.kind == "image" else
                                route_by_asset.get(asset_packet_key(asset), []))
                        ),
                        "review_agent_outcome": run.outcome.case_recommendation,
                    },
                })
                for asset in working_assets
            ]
            working_assets = updated_assets
            if current.m4_evidence is not None:
                current.m4_evidence = current.m4_evidence.model_copy(
                    update={"assets": updated_assets}
                )
            trace = _review_trace(run, packet=packet)
            current.tool_trace.append(trace)
            current.status = "waiting_human"
            current.evidence_progress.phase = "fail_closed"
            current.confidence = "low"
            current.recommendation = (
                "案件语义关系已由 LangGraph 内的 ReviewAgent 节点记录；"
                "比较与 Verifier 尚在本次图执行中。"
            )
            current.reason_codes = list(dict.fromkeys([
                *current.reason_codes,
                "langgraph_investigation_executed",
                "review_agent_semantic_routing_recorded",
                *run.trace.blockers,
            ]))[:50]
            self._repository.save(current, traces=[trace])
            workflow["run"] = run
            workflow["budget_exhausted"] = call_budget_exhausted or any(
                blocker == "semantic_asset_budget_exhausted"
                for blocker in run.trace.blockers
            )
            return {
                "ok": True,
                "transition_reason": (
                    "strict ReviewAgent asset routing completed inside LangGraph"
                ),
                "assessment_count": len(run.outcome.assessments),
                "case_recommendation": run.outcome.case_recommendation,
            }

        def build_exact_matches(graph_state: Mapping[str, Any]) -> Mapping[str, Any]:
            current = self._repository.load(state.case_id)
            run = workflow.get("run")
            if not isinstance(run, ReviewAgentRun):
                return {
                    "ok": False,
                    "transition_reason": "semantic routing result is unavailable",
                }
            facts = _spreadsheet_comparison_facts(
                state=current,
                outcome=run.outcome,
                asset_by_packet_id=asset_by_packet_id,
                scope_ids_by_key=scope_ids_by_key,
                allowed_roots=self._allowed_roots,
            )
            web_facts = _graph_web_comparison_facts(graph_state, packet)
            if web_facts:
                facts = _merge_complete_graph_web_facts(facts, web_facts)
            if facts:
                current.evidence_progress.phase = "evidence_ready"
            self._repository.save(current)
            workflow["facts"] = facts
            return {
                "ok": True,
                "transition_reason": "deterministic exact matching facts were built",
                "fact_count": len(facts),
            }

        def adjudicate_identities(_graph_state: Mapping[str, Any]) -> Mapping[str, Any]:
            current = self._repository.load(state.case_id)
            facts = workflow.get("facts", [])
            candidates = _identity_candidates_from_facts(facts)
            workflow["identity_candidates"] = candidates
            if not candidates:
                return {
                    "ok": True,
                    "transition_reason": "no unresolved semantic identity candidates",
                    "candidate_count": 0,
                    "decision_count": 0,
                }
            identity_llm = self._review_llm or (
                self._review_agent.llm if self._review_agent is not None else None
            )
            if identity_llm is None:
                return {
                    "ok": False,
                    "transition_reason": "semantic identity LLM is unavailable",
                }
            if current.budget.calls >= current.budget.limits.max_calls:
                return {
                    "ok": False,
                    "transition_reason": "semantic identity call budget exhausted",
                }
            payload = json.dumps({"candidates": candidates}, ensure_ascii=False)
            raw = identity_llm.json_call(
                """You adjudicate bounded identity candidates for an auditable award review.
Return JSON with exactly one decision for every supplied candidate_id. Allowed decisions are
same_identity, field_conflict, different, uncertain. A title or honorific may support
same_identity, but must never be removed by a deterministic rule. Use only the supplied pair,
role, scope and source anchor. Do not invent people, fields, sources or candidate IDs.
Use same_identity only at confidence >= 0.9; use uncertain whenever the pair is ambiguous.
The only valid top-level shape is:
{"decisions":[{"candidate_id":"copy the exact supplied id","decision":"same_identity",
"confidence":0.98,"reason":"concise evidence-based reason"}]}
Every decision object must contain exactly candidate_id, decision, confidence, and reason.
Confidence is a JSON number from 0 to 1. Reason must be a non-empty string.""",
                payload,
                max_tokens=4000,
            )
            try:
                updated_facts, decisions = _apply_identity_adjudications(
                    facts, candidates, raw
                )
            except (ValidationError, ValueError) as exc:
                if isinstance(exc, ValidationError):
                    validation_errors = [
                        {
                            "location": [str(part) for part in error.get("loc", ())],
                            "type": str(error.get("type", "validation_error")),
                        }
                        for error in exc.errors(include_url=False)
                    ][:20]
                else:
                    validation_errors = [{"location": [], "type": type(exc).__name__}]
                current.budget.calls += 1
                trace = ToolObservation(
                    call_id=f"identity-adjudication:{current.active_attempt_id}:failed",
                    tool_name="review_identity_candidates",
                    started_at=utc_now(),
                    finished_at=utc_now(),
                    duration_ms=0,
                    input_summary={
                        "candidate_count": len(candidates),
                        "candidate_set_sha256": hashlib.sha256(
                            payload.encode("utf-8")
                        ).hexdigest(),
                    },
                    output_summary={
                        "validation_status": "failed",
                        "validation_errors": validation_errors,
                    },
                    ok=False,
                    error_code="identity_protocol_invalid",
                )
                current.tool_trace.append(trace)
                self._repository.save(current, traces=[trace])
                return {
                    "ok": False,
                    "transition_reason": "semantic identity response failed strict validation",
                    "candidate_count": len(candidates),
                    "validation_errors": validation_errors,
                }
            current.budget.calls += 1
            decision_counts = dict(Counter(
                str(item["decision"]) for item in decisions
            ))
            trace = ToolObservation(
                call_id=f"identity-adjudication:{current.active_attempt_id}",
                tool_name="review_identity_candidates",
                started_at=utc_now(),
                finished_at=utc_now(),
                duration_ms=0,
                input_summary={
                    "candidate_count": len(candidates),
                    "candidate_set_sha256": hashlib.sha256(
                        payload.encode("utf-8")
                    ).hexdigest(),
                },
                output_summary={
                    "validation_status": "accepted",
                    "decision_count": len(decisions),
                    "decision_counts": decision_counts,
                },
                ok=True,
            )
            current.tool_trace.append(trace)
            self._repository.save(current, traces=[trace])
            workflow["facts"] = updated_facts
            workflow["identity_decisions"] = decisions
            return {
                "ok": True,
                "transition_reason": "semantic identity candidates were adjudicated and verified",
                "candidate_count": len(candidates),
                "decision_count": len(decisions),
                "decision_counts": decision_counts,
            }

        def verify(_graph_state: Mapping[str, Any]) -> Mapping[str, Any]:
            current = self._repository.load(state.case_id)
            verification = deterministic_verify(
                build_evidence_snapshot(
                    current,
                    _verification_results_from_comparison_facts(
                        workflow.get("facts", [])
                    ),
                )
            )
            current.latest_verification = verification
            self._repository.save(current, verifications=[verification])
            workflow["verification"] = verification
            return {
                "ok": True,
                "transition_reason": "deterministic verifier completed",
                "recommended_action": verification.recommended_action,
            }

        def persist(_graph_state: Mapping[str, Any]) -> Mapping[str, Any]:
            current = self._repository.load(state.case_id)
            facts = workflow.get("facts", [])
            verification = workflow.get("verification")
            if verification is None:
                return {
                    "ok": False,
                    "transition_reason": "verification result is unavailable",
                }
            self._repository.store.record_evidence_comparison(
                current.case_id,
                current.active_attempt_id,
                facts=facts,
                fallback_missing=verification.missing_evidence,
                fallback_contradictions=verification.contradictions,
            )
            self._repository.store.record_scope_comparisons(
                current.case_id,
                current.active_attempt_id,
                facts=facts,
                verifier=verification.model_dump(mode="json"),
            )
            comparisons = self._repository.store.list_scope_comparisons(
                current.case_id, attempt_id=current.active_attempt_id
            )
            comparison_complete = bool(comparisons) and all(
                item["status"] == "complete" for item in comparisons
            )
            stopped_reason = (
                "tool_budget_exhausted" if workflow.get("budget_exhausted")
                else "review_agent_comparison_complete" if comparison_complete
                else "comparison_pending_after_semantic_routing"
            )
            if comparison_complete:
                current.recommendation = (
                    "LangGraph 内的语义路由、本地逐 scope 比较和 Verifier 已完成，"
                    "等待人工复核业务差异。"
                )
                current.reason_codes = [
                    code for code in current.reason_codes
                    if code != "comparison_pending_after_semantic_routing"
                ]
                current.reason_codes.append("review_agent_comparison_complete")
            else:
                current.reason_codes = list(dict.fromkeys([
                    *current.reason_codes,
                    "comparison_pending_after_semantic_routing",
                ]))[:50]
            self._repository.save(current)
            workflow["stopped_reason"] = stopped_reason
            workflow["comparison_complete"] = comparison_complete
            return {
                "ok": True,
                "transition_reason": "comparison and verifier records were persisted",
                "comparison_count": len(comparisons),
                "comparison_complete": comparison_complete,
            }

        asset_index = []
        for parsed in packet.assets:
            source_asset = packet_asset_records[(parsed.asset_id, parsed.subunit_id)]
            source_metadata = (
                source_asset.metadata
                if isinstance(getattr(source_asset, "metadata", None), Mapping)
                else {}
            )
            asset_index.append({
                "asset_id": parsed.asset_id,
                "kind": parsed.kind,
                "subunit_id": parsed.subunit_id,
                "source_url": parsed.source_url,
                "local_path": str(getattr(source_asset, "local_path", "")),
                "sha256": parsed.sha256,
                "readable": bool(
                    getattr(source_asset, "local_path", "") and parsed.sha256
                ),
                "document_complete": parsed.document_complete,
                "label": parsed.label,
                "page": int(source_metadata.get("page", 0) or 0),
                "total_pages": int(source_metadata.get("total_pages", 0) or 0),
                "page_count": int(source_metadata.get("page_count", 0) or 0),
                "parent_roster_complete": bool(
                    source_metadata.get("m4_html_parent_roster_complete", False)
                ),
            })
        required_scopes = [scope for scope in packet.scopes if scope.required]
        investigation = self._investigation_agent.run(
            case_id=state.case_id,
            objective=(
                f"Review {state.award_name} {state.year}; decide whether bounded M4 "
                "evidence is sufficient and complete the governed review stages. "
                f"The downstream ReviewCasePacket contains {packet.submission_summary.submitted_rows} "
                f"submitted rows across {len(packet.scopes)} persisted scopes."
            ),
            known_urls=state.known_urls,
            asset_index=asset_index,
            expected_record_count=packet.submission_summary.submitted_rows,
            comparison_context={
                "expected_award_name": packet.award_name,
                "expected_year": packet.year,
                "submitted_paths": packet.submission_summary.submission_files,
                "submitted_path": (
                    packet.submission_summary.submission_files[0]
                    if packet.submission_summary.submission_files else ""
                ),
                "match_fields": packet.submission_summary.match_fields,
                "match_combine": str(state.submitted_summary.get("match_combine", "first")),
                "expected_scope_count": (
                    packet.submission_summary.expected_scope_count
                    or packet.submission_summary.submitted_rows
                ),
                "page_total_count": packet.submission_summary.submitted_rows,
                "official_domains": list(
                    state.submitted_summary.get("official_domains", [])
                ),
                "official_secondary_domains": list(
                    state.submitted_summary.get("official_secondary_domains", [])
                ),
                "section_keywords": (
                    [packet.award_name]
                    if len(required_scopes) == 1 and packet.award_name else []
                ),
            },
            stage_hooks=InvestigationStageHooks(
                semantic_route_assets=semantic_route_assets,
                build_exact_matches_and_candidates=build_exact_matches,
                semantic_adjudicate_identities=adjudicate_identities,
                deterministic_verify=verify,
                persist=persist,
            ),
        )
        current = self._repository.load(state.case_id)
        graph_traces = _investigation_node_traces(investigation)
        if not self._investigation_agent.persists_node_events:
            current.tool_trace.extend(graph_traces)
            current.step_count += max(1, len(investigation.node_events))
            self._repository.save(current, traces=graph_traces)
        tool_traces = [
            ToolObservation.model_validate(item) for item in investigation.tool_trace
        ]
        if tool_traces:
            current = self._repository.load(state.case_id)
            current.tool_trace.extend(tool_traces)
            self._repository.save(current, traces=tool_traces)
        current = self._repository.load(state.case_id)
        if "run" not in workflow:
            current.budget.calls += len(investigation.actions)
            current.status = "waiting_human"
            current.evidence_progress.phase = "fail_closed"
            current.recommendation = (
                f"LangGraph 调查在语义路由前停止：{investigation.reason}"
            )
            current.reason_codes = list(dict.fromkeys([
                *current.reason_codes,
                "langgraph_investigation_executed",
                f"langgraph_terminal_{investigation.status}",
            ]))[:50]
            self._repository.save(current)
        stopped_reason = str(workflow.get(
            "stopped_reason", f"langgraph_{investigation.status}"
        ))
        current.elapsed_ms += max(1, round((time.monotonic() - started_at) * 1000))
        self._repository.save(current)
        self._repository.finish_attempt(current, stopped_reason=stopped_reason)
        return HarnessOutcome(state=current, stopped_reason=stopped_reason)

    def run(self, case_id: int) -> HarnessOutcome:
        state = self._repository.load(case_id)
        if state.status == "completed":
            return HarnessOutcome(state=state, stopped_reason="already_completed")
        if state.status == "waiting_human" and not state.pending_supplement:
            return HarnessOutcome(state=state, stopped_reason="awaiting_human_action")

        supplement_request = state.pending_supplement
        limits = (
            self._tool_limits.model_copy(deep=True)
            if self._tool_limits is not None
            else state.budget.limits.model_copy(deep=True)
        )
        # Attempts retain durable history in their own tables. Runtime counters
        # and bounded traces belong to the new attempt and must start fresh.
        state.budget = ToolBudgetState(limits=limits)
        state.step_count = 0
        state.token_used = 0
        state.elapsed_ms = 0
        state.llm_usage = []
        state.verifier_llm_usage = []
        state.reflection_count = 0
        state.latest_verification = None
        state.tool_trace = []

        started_at = time.monotonic()
        self._repository.start_attempt(
            state,
            kind="supplement" if supplement_request else "initial",
            supplement_request=supplement_request,
        )
        if supplement_request:
            question = f"人工补证要求：{supplement_request}"
            if question not in state.open_questions:
                state.open_questions.append(question)
            state.pending_supplement = ""
        # A graph event may be checkpointed before the first model call, so its
        # attempt ID and optimistic-lock version must already be durable.
        self._repository.save(state)
        context = _context_from_state(state)
        m4_evidence = state.m4_evidence
        assets = list(m4_evidence.assets) if m4_evidence is not None else []
        sources = [{
            "url": url,
            "title": "",
            "source_level": "unknown",
        } for url in state.known_urls]
        scope_ids_by_key = _scope_ids_by_key(
            state,
            self._repository.store.list_audit_scopes(state.case_id),
        )
        packet = build_review_case_packet(
            case_id=state.case_id,
            context=context,
            scope_ids_by_key=scope_ids_by_key,
            assets=assets,
            source_candidates=sources,
        )
        packet_asset_records = {
            asset_packet_key(asset): asset
            for asset in assets
        }
        if self._investigation_agent is not None:
            return self._run_langgraph_case(
                state=state,
                started_at=started_at,
                packet=packet,
                assets=assets,
                packet_asset_records=packet_asset_records,
                scope_ids_by_key=scope_ids_by_key,
            )
        investigation: InvestigationResult | None = None
        remaining_agent_calls = state.budget.limits.max_calls - state.budget.calls
        remaining_asset_reads = (
            state.budget.limits.max_asset_calls - state.budget.asset_calls
        )
        # Reserve one bounded protocol-correction retry in addition to the
        # planning and final semantic calls; never exceed the case budget.
        call_budget_exhausted = remaining_agent_calls < 3 or remaining_asset_reads < 0
        if investigation is not None and investigation.status != "compare":
            reason = investigation.reason
            run = ReviewAgentRun(
                outcome=ReviewOutcome(
                    case_recommendation="manual",
                    reason=f"LangGraph investigation stopped safely: {reason}",
                ),
                trace=ReviewAgentTrace(
                    blockers=[f"langgraph_{investigation.status}"]
                ),
            )
        elif call_budget_exhausted:
            run = ReviewAgentRun(
                outcome=ReviewOutcome(
                    case_recommendation="evidence_insufficient",
                    reason="语义审核预算已耗尽，未执行新的模型调用。",
                ),
                trace=ReviewAgentTrace(blockers=["semantic_agent_budget_exhausted"]),
            )
        else:
            agent = self._review_agent or ReviewAgent(
                self._review_llm,
                M4AssetReader(
                    {
                        parsed.asset_id: packet_asset_records[(
                            parsed.asset_id, parsed.subunit_id
                        )]
                        for parsed in packet.assets
                    },
                    allowed_roots=self._allowed_roots,
                ),
                max_material_requests=min(10, remaining_asset_reads),
            )
            run = agent.run(packet)
        budget_exhausted = call_budget_exhausted or any(
            blocker == "semantic_asset_budget_exhausted"
            for blocker in run.trace.blockers
        )
        if not call_budget_exhausted:
            state.budget.calls += run.trace.model_call_count
            state.budget.asset_calls += run.trace.request_count
        state.step_count += 1
        route_by_asset = _semantic_routes(run.outcome)
        updated_assets = []
        for asset in assets:
            routes = route_by_asset.get(asset_packet_key(asset), [])
            updated_assets.append(asset.model_copy(update={
                "metadata": {
                    **asset.metadata,
                    "routes": routes,
                    "review_agent_outcome": run.outcome.case_recommendation,
                },
            }))
        if state.m4_evidence is not None:
            state.m4_evidence = state.m4_evidence.model_copy(update={"assets": updated_assets})

        trace = _review_trace(
            run,
            packet=packet,
        )
        state.tool_trace.append(trace)
        state.status = "waiting_human"
        state.evidence_progress.phase = "fail_closed"
        state.confidence = "low"
        state.recommendation = (
            "案件语义关系已由 ReviewAgent 记录；选中资产尚未完成本地身份比较，"
            "当前不得形成取证完成结论。"
        )
        state.reason_codes = list(dict.fromkeys([
            *state.reason_codes,
            "review_agent_semantic_routing_recorded",
            "comparison_pending_after_semantic_routing",
            *run.trace.blockers,
        ]))[:50]
        # Sync M4 assets and their semantic routes before the Verifier reads ledger gates.
        self._repository.save(state, traces=[trace])
        facts = _spreadsheet_comparison_facts(
            state=state,
            outcome=run.outcome,
            asset_by_packet_id={
                parsed.asset_id: packet_asset_records[(
                    parsed.asset_id, parsed.subunit_id
                )]
                for parsed in packet.assets
            },
            scope_ids_by_key=scope_ids_by_key,
            allowed_roots=self._allowed_roots,
        )
        if facts:
            state.evidence_progress.phase = "evidence_ready"
        verification = deterministic_verify(build_evidence_snapshot(
            state, _verification_results_from_comparison_facts(facts)
        ))
        state.latest_verification = verification
        self._repository.store.record_evidence_comparison(
            state.case_id,
            state.active_attempt_id,
            facts=facts,
            fallback_missing=verification.missing_evidence,
            fallback_contradictions=verification.contradictions,
        )
        self._repository.store.record_scope_comparisons(
            state.case_id,
            state.active_attempt_id,
            facts=facts,
            verifier=verification.model_dump(mode="json"),
        )
        comparisons = self._repository.store.list_scope_comparisons(
            state.case_id,
            attempt_id=state.active_attempt_id,
        )
        comparison_complete = bool(comparisons) and all(
            comparison["status"] == "complete" for comparison in comparisons
        )
        stopped_reason = (
            "tool_budget_exhausted" if budget_exhausted
            else "review_agent_comparison_complete" if comparison_complete
            else "comparison_pending_after_semantic_routing"
        )
        if comparison_complete:
            state.recommendation = (
                "案件语义关系和本地逐 scope 比较已完成，等待人工复核业务差异。"
            )
            state.reason_codes = [
                code for code in state.reason_codes
                if code != "comparison_pending_after_semantic_routing"
            ]
            state.reason_codes.append("review_agent_comparison_complete")
        state.elapsed_ms += max(1, round((time.monotonic() - started_at) * 1000))
        self._repository.save(state, verifications=[verification])
        self._repository.finish_attempt(
            state,
            stopped_reason=stopped_reason,
        )
        return HarnessOutcome(
            state=state,
            stopped_reason=stopped_reason,
        )
