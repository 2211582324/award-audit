"""Adapters from existing M4/M5 facts into a bounded review-agent case packet."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from hashlib import sha256
from pathlib import Path
from typing import Any

from award_audit.agent.integration import AuditCaseInput
from award_audit.agent.review_agent.models import (
    ParsedAsset,
    ReviewCasePacket,
    ScopeCandidate,
    SourceCandidate,
    SubmissionSummary,
)
from award_audit.agent.toolkit.contracts import EvidenceAssetRecord

_ROLE_MAP = {
    "work_or_project": "project",
    "team": "team",
    "instructor_or_person": "person",
    "organization": "organization",
    "ranking_or_special": "special",
}


def _asset_id(asset: EvidenceAssetRecord) -> str:
    if asset.sha256:
        return f"sha256:{asset.sha256}"
    return f"url:{sha256(asset.url.encode('utf-8')).hexdigest()}"


def asset_packet_key(asset: EvidenceAssetRecord) -> tuple[str, str]:
    """Stable physical-asset key used by both the packet and route persistence."""

    metadata = asset.metadata if isinstance(asset.metadata, dict) else {}
    subunit_id = str(metadata.get("subunit_id", "document"))[:300] or "document"
    return _asset_id(asset), subunit_id


def _string_list(value: object, *, limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item)[:500] for item in value[:limit] if str(item).strip()]


def _asset_summary(asset: EvidenceAssetRecord) -> str:
    metadata = asset.metadata if isinstance(asset.metadata, dict) else {}
    values = [
        str(metadata.get(key, "")).strip()
        for key in ("title", "summary", "ocr_summary", "text_summary")
    ]
    return "\n".join(value for value in values if value)[:4000]


def _resolved_asset_kind(asset: EvidenceAssetRecord) -> str:
    """Prefer M4's type, with a bounded fallback for anonymous download URLs."""

    kind = asset.kind.strip().lower()
    if kind and kind != "unknown":
        return kind
    content_type = asset.content_type.casefold()
    if "spreadsheetml.sheet" in content_type:
        return "xlsx"
    if content_type == "application/vnd.ms-excel":
        return "xls"
    suffix = Path(asset.local_path).suffix.casefold()
    if suffix in {".xlsx", ".xls"}:
        return suffix[1:]
    return kind or "unknown"


def _parsed_asset(asset: EvidenceAssetRecord) -> ParsedAsset:
    metadata = asset.metadata if isinstance(asset.metadata, dict) else {}
    status = asset.status
    blockers = _string_list(metadata.get("blockers"), limit=20)
    if asset.error_code:
        blockers.append(asset.error_code)
    if asset.error_message:
        blockers.append(asset.error_message[:300])
    return ParsedAsset(
        asset_id=asset_packet_key(asset)[0],
        subunit_id=asset_packet_key(asset)[1],
        source_url=asset.url,
        parent_url=asset.parent_url,
        kind=_resolved_asset_kind(asset),
        status=status,
        label=asset.label,
        title=str(metadata.get("title", ""))[:500],
        summary=_asset_summary(asset),
        sample_rows=[
            [str(cell)[:500] for cell in row[:30]]
            for row in metadata.get("sample_rows", [])[:10]
            if isinstance(row, list)
        ],
        anchors=_string_list(metadata.get("anchors"), limit=50),
        document_complete=(
            None if asset.truncated else status == "parsed"
        ),
        sha256=asset.sha256,
        blockers=list(dict.fromkeys(blockers)),
    )


def _asset_detail_score(asset: EvidenceAssetRecord) -> tuple[int, int, int, int]:
    metadata = asset.metadata if isinstance(asset.metadata, dict) else {}
    sample_rows = metadata.get("sample_rows", [])
    return (
        int(asset.status == "parsed"),
        int(bool(asset.local_path)),
        len(_asset_summary(asset)),
        len(sample_rows) if isinstance(sample_rows, list) else 0,
    )


def _packet_assets(assets: Sequence[EvidenceAssetRecord]) -> list[EvidenceAssetRecord]:
    """Collapse duplicate M4 discovery records only for bounded Agent input."""

    selected: dict[tuple[str, str], EvidenceAssetRecord] = {}
    for asset in assets:
        key = asset_packet_key(asset)
        current = selected.get(key)
        if current is None or _asset_detail_score(asset) > _asset_detail_score(current):
            selected[key] = asset
    return list(selected.values())


def _scope_candidates(
    raw_scopes: Sequence[Mapping[str, Any]],
    scope_ids_by_key: Mapping[str, int],
) -> list[ScopeCandidate]:
    scopes: list[ScopeCandidate] = []
    for raw in raw_scopes:
        scope_key = str(raw.get("scope_key", "")).strip()
        scope_id = int(scope_ids_by_key.get(scope_key, raw.get("scope_id", 0)) or 0)
        role_type = str(raw.get("role_type", "")).strip()
        role = _ROLE_MAP.get(role_type)
        if not scope_key or not scope_id or role is None:
            raise ValueError(f"cannot build review scope from {scope_key or role_type or 'empty'}")
        business_scope = raw.get("business_scope", {})
        scopes.append(ScopeCandidate(
            scope_id=scope_id,
            scope_key=scope_key,
            source_role_type=role_type,
            role=role,
            role_label=str(raw.get("role_label", "") or role_type)[:200],
            required=bool(raw.get("required", True)),
            business_scope={
                str(key): str(value)[:500]
                for key, value in business_scope.items()
                if str(key).strip() and str(value).strip()
            } if isinstance(business_scope, dict) else {},
            submitted_row_count=int(raw.get("submitted_row_count", 0) or 0),
            submitted_identity_count=int(raw.get("submitted_identity_count", 0) or 0),
        ))
    return scopes


def build_review_case_packet(
    *,
    case_id: int,
    context: AuditCaseInput,
    scope_ids_by_key: Mapping[str, int],
    assets: Sequence[EvidenceAssetRecord] = (),
    source_candidates: Sequence[Mapping[str, Any]] = (),
) -> ReviewCasePacket:
    """Build the new Agent input from persisted M4/M5 facts without raw DB access."""

    known_urls = list(context.known_urls)
    sources = [
        SourceCandidate(
            original_url=str(item.get("original_url", item.get("url", ""))).strip(),
            normalized_url=str(item.get("normalized_url", "")).strip(),
            redirect_chain=_string_list(item.get("redirect_chain"), limit=10),
            title=str(item.get("title", ""))[:500],
            source_level=str(item.get("source_level", "unknown"))[:80],
        )
        for item in source_candidates
        if str(item.get("original_url", item.get("url", ""))).strip()
    ]
    known_originals = {source.original_url for source in sources}
    sources.extend(
        SourceCandidate(original_url=url, normalized_url=url)
        for url in known_urls
        if url and url not in known_originals
    )
    raw_scopes = [item for item in context.role_scopes if isinstance(item, dict)]
    if not raw_scopes:
        raise ValueError("review case packet requires persisted role scopes")
    local_issues = [
        f"{item.rule_id}: {item.message}"
        for item in context.local_issues
    ]
    return ReviewCasePacket(
        case_id=case_id,
        resource_code=context.resource_code,
        award_name=context.award_name,
        year=context.year,
        submission_summary=SubmissionSummary(
            submission_files=context.submission_files,
            submitted_rows=context.submitted_rows,
            expected_scope_count=context.expected_scope_count,
            identity_version=context.identity_version,
            match_profile=context.match_profile,
            match_fields=context.match_fields,
            row_conservation=context.row_conservation,
            identity_samples=context.submitted_only_items[:10],
        ),
        scopes=_scope_candidates(raw_scopes, scope_ids_by_key),
        known_urls=sources,
        assets=[_parsed_asset(asset) for asset in _packet_assets(assets)],
        local_issues=local_issues,
    )
