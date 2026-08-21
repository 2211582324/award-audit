"""Lightweight, persistent and fail-closed Evidence Harness."""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, Literal
from urllib.parse import unquote, urlsplit

from award_audit.agent.harness.client import (
    AgentClient,
    AgentClientError,
    FallbackAgentClient,
    OpenAINativeAgentClient,
    StructuredActionClient,
)
from award_audit.agent.harness.models import (
    AgentDecision,
    AgentTurnContext,
    AuditCaseState,
    EvidenceCandidate,
    HarnessLimits,
    HarnessOutcome,
    NextAction,
)
from award_audit.agent.harness.persistence import CaseRepository
from award_audit.agent.memory.service import CaseMemoryService
from award_audit.agent.toolkit import image as image_tools
from award_audit.agent.toolkit import spreadsheet as spreadsheet_tools
from award_audit.agent.toolkit.contracts import (
    EvidenceArtifact,
    EvidenceAssetRecord,
    EvidenceFact,
    ToolBudgetState,
    ToolResult,
)
from award_audit.agent.toolkit.provenance import classify_source, normalize_domain
from award_audit.agent.toolkit.registry import (
    CollectSpreadsheetAttachmentsInput,
    ExtractSearchDocumentInput,
    SafeToolExecutor,
    ToolExecutionContext,
    ToolRegistry,
    VerifyPageImageRosterInput,
    build_default_registry,
)
from award_audit.agent.toolkit.safety import inspect_evidence_file, validate_local_path
from award_audit.agent.verification.models import AutoApprovalPolicy, VerificationReport
from award_audit.agent.verification.service import (
    EvidenceVerifier,
    StructuredVerifierClient,
    VerifierError,
    build_evidence_snapshot,
    decide_review_route,
    deterministic_verify,
)
from award_audit.core.identity import normalize_identity, route_text_variants
from award_audit.core.pipeline.store import Store

_SEARCH_TRIGGERS = {
    "SOURCE_URL_MISSING",
    "SOURCE_UNREACHABLE",
    "PAGE_TARGET_UNCERTAIN",
    "EVIDENCE_CONFLICT",
    "COVERAGE_UNKNOWN",
}
_DEFAULT_TOOL_NAMES = {
    "fetch_web_page",
    "download_evidence",
    "verify_page_image_roster",
    "collect_spreadsheet_attachments",
    "parse_spreadsheet",
    "inspect_pdf",
    "extract_pdf_text",
    "render_pdf_pages",
    "ocr_image",
    "vision_extract_roster",
    "compare_roster",
    "search_official_award",
    "extract_search_document",
}
_BASE_AGENT_TOOLS = {
    "fetch_web_page",
    "download_evidence",
    "collect_spreadsheet_attachments",
    "parse_spreadsheet",
    "search_official_award",
    "extract_search_document",
}
_PDF_AGENT_TOOLS = {"inspect_pdf", "extract_pdf_text", "render_pdf_pages"}
_IMAGE_AGENT_TOOLS = {"ocr_image", "vision_extract_roster", "compare_roster"}
_KNOWN_SOURCE_TOOLS = {
    "fetch_web_page",
    "download_evidence",
    "verify_page_image_roster",
    "collect_spreadsheet_attachments",
}
_SKILL_PATH = (
    Path(__file__).resolve().parent.parent
    / "skills"
    / "official_award_search"
    / "SKILL.md"
)
_SENSITIVE_KEY_PARTS = (
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "password",
    "secret",
    "signature",
    "token",
)
_QUERY_SECRET = re.compile(
    r"(?i)([?&](?:api[_-]?key|access[_-]?token|token|signature|sig|secret)=)"
    r"[^&#\"\\\s]+"
)
_PROVIDED_EVIDENCE_COMPLETE = "provided_web_evidence_complete"
_SEARCH_CANDIDATES_READY = "official_search_candidates_ready"
_IMAGE_ASSET_EXTENSIONS = {".gif", ".jpeg", ".jpg", ".png", ".webp"}


def _redact_untrusted(value: Any, *, depth: int = 0) -> Any:
    """Bound nesting and remove common credentials before model exposure."""

    if depth >= 5:
        return "[DEPTH_LIMIT]"
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for raw_key, item in list(value.items())[:50]:
            key = str(raw_key)[:100]
            normalized = key.lower().replace("-", "_")
            if any(part in normalized for part in _SENSITIVE_KEY_PARTS):
                cleaned[key] = "[REDACTED]"
            else:
                cleaned[key] = _redact_untrusted(item, depth=depth + 1)
        return cleaned
    if isinstance(value, (list, tuple)):
        return [_redact_untrusted(item, depth=depth + 1) for item in list(value)[:50]]
    if isinstance(value, str):
        return _QUERY_SECRET.sub(r"\1[REDACTED]", value)[:4000]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:1000]


def _hydrate_m4_evidence_progress(
    state: AuditCaseState,
    *,
    allowed_roots: Iterable[str | Path] = (),
) -> bool:
    """Restore bounded current-M4 assets into the persisted M5 work queues."""

    bundle = state.m4_evidence
    progress = state.evidence_progress
    if bundle is None or progress.m4_result_id == bundle.result_id:
        return False
    progress.m4_result_id = bundle.result_id
    progress.pending_attachment_page_urls = []
    progress.pending_attachment_urls = []
    progress.pending_attachment_parent_urls = {}
    progress.pending_media_source_url = ""
    progress.pending_media_page_title = ""
    progress.pending_media_urls = []
    progress.pending_media_parent_urls = {}

    asset_records = list(bundle.assets)
    if not asset_records:
        fallback_pages = [
            url for url in dict.fromkeys(bundle.source_urls)
            if url.startswith(("http://", "https://"))
            and url not in set(bundle.found_assets)
        ]
        fallback_parent = fallback_pages[0] if len(fallback_pages) == 1 else ""
        asset_records = [
            EvidenceAssetRecord(
                url=url,
                parent_url=fallback_parent,
                kind=(
                    "image"
                    if Path(unquote(urlsplit(url).path)).suffix.casefold()
                    in _IMAGE_ASSET_EXTENSIONS
                    else Path(unquote(urlsplit(url).path)).suffix.casefold().lstrip(".")
                    or "unknown"
                ),
                metadata={"legacy_fallback": True},
            )
            for url in dict.fromkeys(bundle.found_assets)
            if url.startswith(("http://", "https://"))
        ]
    asset_urls = {asset.url for asset in asset_records}
    source_pages = [
        url
        for url in dict.fromkeys([
            *bundle.source_urls,
            *(asset.parent_url for asset in asset_records if asset.parent_url),
        ])
        if url.startswith(("http://", "https://")) and url not in asset_urls
    ]
    if source_pages and not _case_domain_metadata(state, "official_domains"):
        # The current M4 result is a persisted, version-checked binding. Preserve
        # the authority metadata that integration derives from its primary URL,
        # including when M4 itself stopped before copying that field into the seed.
        try:
            primary_host = normalize_domain(urlsplit(source_pages[0]).hostname or "")
        except ValueError:
            primary_host = ""
        if primary_host:
            state.submitted_summary["official_domains"] = [primary_host]
    for url in source_pages:
        if url not in state.known_urls and len(state.known_urls) < 20:
            state.known_urls.append(url)
    if source_pages or asset_records:
        image_urls: list[str] = []
        attachment_urls: list[str] = []
        roots = tuple(allowed_roots)
        for asset in asset_records:
            reused = False
            if (
                roots
                and asset.status in {"downloaded", "parsed"}
                and asset.local_path
                and asset.sha256
                and asset.fetched_at
            ):
                try:
                    local = validate_local_path(
                        asset.local_path, roots, must_exist=True, file_only=True
                    )
                    inspection = inspect_evidence_file(
                        local, max_bytes=state.budget.limits.max_file_bytes
                    )
                    if inspection.sha256 != asset.sha256:
                        raise ValueError("M4 evidence asset hash mismatch")
                    if asset.size_bytes and inspection.size_bytes != asset.size_bytes:
                        raise ValueError("M4 evidence asset size mismatch")
                    artifact = EvidenceArtifact(
                        kind=inspection.kind,
                        source_url=asset.url,
                        local_path=str(local),
                        content_type=inspection.content_type,
                        sha256=inspection.sha256,
                        size_bytes=inspection.size_bytes,
                        fetched_at=asset.fetched_at,
                        metadata={
                            **asset.metadata,
                            "origin": "m4_current_result",
                            "m4_result_id": bundle.result_id,
                            "page_url": asset.parent_url,
                            "m4_status": asset.status,
                            "m4_extraction_method": asset.extraction_method,
                            "truncated": asset.truncated,
                        },
                    )
                    if not any(
                        item.sha256 == artifact.sha256 and item.source_url == artifact.source_url
                        for item in state.artifacts
                    ):
                        state.artifacts.append(artifact)
                    reused = True
                except (OSError, RuntimeError, ValueError):
                    if "bound_m4_artifact_validation_failed" not in state.reason_codes:
                        state.reason_codes.append("bound_m4_artifact_validation_failed")
            if reused:
                continue
            suffix = Path(unquote(urlsplit(asset.url).path)).suffix.casefold()
            is_image = asset.kind == "image" or suffix in _IMAGE_ASSET_EXTENSIONS
            if is_image:
                image_urls.append(asset.url)
                if asset.parent_url:
                    progress.pending_media_parent_urls[asset.url] = asset.parent_url
            else:
                attachment_urls.append(asset.url)
                if asset.parent_url:
                    progress.pending_attachment_parent_urls[asset.url] = asset.parent_url
        progress.pending_attachment_page_urls = source_pages[:20]
        progress.pending_attachment_urls = attachment_urls[:100]
        if image_urls:
            progress.pending_media_source_url = (
                progress.pending_media_parent_urls.get(image_urls[0])
                or (source_pages[0] if source_pages else "")
            )
            progress.pending_media_page_title = bundle.award_name[:500]
            progress.pending_media_urls = image_urls[:100]
        if attachment_urls:
            progress.phase = "spreadsheet_processing"
        elif image_urls:
            progress.phase = "image_processing"
    if state.artifacts and "bound_m4_artifact_reused" not in state.reason_codes:
        state.reason_codes.append("bound_m4_artifact_reused")
    if asset_records and "bound_m4_assets_queued" not in state.reason_codes:
        state.reason_codes.append("bound_m4_assets_queued")
    return True


def _bounded_tool_observation(
    tool_name: str,
    result: ToolResult,
    *,
    max_chars: int,
) -> dict[str, Any]:
    safe_result = _redact_untrusted(result.model_dump(mode="json"))
    payload = json.dumps(safe_result, ensure_ascii=False, default=str)
    truncated = len(payload) > max_chars
    return {
        "tool_name": tool_name,
        "ok": result.ok,
        "error_code": result.error_code,
        "untrusted_tool_result_json": payload[:max_chars],
        "truncated_for_agent": truncated,
    }


def _update_attachment_queue_after_collection(
    state: AuditCaseState,
    result: ToolResult,
    attempted_urls: list[str] | None = None,
) -> None:
    """Close internally retried failures and retain only untouched attachments."""

    progress = state.evidence_progress
    has_manifest = any(
        key in result.data
        for key in (
            "processed_attachment_urls",
            "unprocessed_attachment_urls",
            "failed_attachment_urls",
        )
    )
    if not has_manifest:
        remaining = [
            url for url in progress.pending_attachment_urls
            if attempted_urls is not None and url not in set(attempted_urls)
        ]
    else:
        untouched = [
            url for url in progress.pending_attachment_urls
            if attempted_urls is not None and url not in set(attempted_urls)
        ]
        failed_urls = [
            str(url) for url in result.data.get("failed_attachment_urls", [])
            if isinstance(url, str) and url.startswith(("http://", "https://"))
        ]
        attempted = set(attempted_urls or progress.pending_attachment_urls)
        progress.failed_attachment_urls = list(dict.fromkeys([
            *(url for url in progress.failed_attachment_urls if url not in attempted),
            *failed_urls,
        ]))
        remaining = [
            str(url)
            for url in dict.fromkeys([
                *untouched,
                *result.data.get("unprocessed_attachment_urls", []),
            ])
            if isinstance(url, str) and url.startswith(("http://", "https://"))
        ][:100]
    old_parents = progress.pending_attachment_parent_urls
    progress.pending_attachment_urls = remaining
    retained_parent_urls = list(dict.fromkeys([*remaining, *progress.failed_attachment_urls]))
    progress.pending_attachment_parent_urls = {
        url: old_parents[url]
        for url in retained_parent_urls
        if url in old_parents
    }
    mapped_pages = list(dict.fromkeys(
        progress.pending_attachment_parent_urls[url]
        for url in remaining
        if url in progress.pending_attachment_parent_urls
    ))
    progress.pending_attachment_page_urls = (
        mapped_pages[:20]
        if mapped_pages
        else (progress.pending_attachment_page_urls if remaining else [])
    )


def _annotate_m5_artifact(
    state: AuditCaseState,
    artifact: EvidenceArtifact,
) -> EvidenceArtifact:
    """Record whether an M5 artifact reuses, supplements or may replace M4 evidence."""

    bundle = state.m4_evidence
    matching = [
        asset
        for asset in (bundle.assets if bundle is not None else [])
        if asset.url == artifact.source_url
    ]
    if any(asset.sha256 and asset.sha256 == artifact.sha256 for asset in matching):
        relationship = "same_asset"
    elif any(asset.sha256 for asset in matching):
        relationship = "replacement_candidate"
    elif matching:
        relationship = "bound_url_unverified"
    else:
        relationship = "supplemental"
    return artifact.model_copy(update={
        "metadata": {
            **artifact.metadata,
            "origin": "m5_supplement",
            "m4_relationship": relationship,
            "m4_result_id": bundle.result_id if bundle is not None else 0,
        },
    })


def _load_skill(state: AuditCaseState) -> str:
    triggers = set(state.trigger_codes).intersection(_SEARCH_TRIGGERS)
    if not triggers:
        return ""
    if state.known_urls and triggers == {"COVERAGE_UNKNOWN"}:
        return ""
    return _SKILL_PATH.read_text(encoding="utf-8")[:12_000]


def _turn_context(
    state: AuditCaseState,
    observations: list[dict[str, Any]],
    *,
    max_observation_chars: int,
) -> AgentTurnContext:
    raw_scopes = state.submitted_summary.get("role_scopes", [])
    scope_summaries = []
    if isinstance(raw_scopes, list):
        for scope in raw_scopes[:30]:
            if not isinstance(scope, dict):
                continue
            scope_summaries.append({
                "scope_key": scope.get("scope_key", ""),
                "role_type": scope.get("role_type", ""),
                "role_label": scope.get("role_label", ""),
                "required": scope.get("required", True),
                "business_scope": scope.get("business_scope", {}),
                "submitted_row_count": scope.get("submitted_row_count", 0),
                "submitted_identity_count": scope.get("submitted_identity_count", 0),
            })
    submitted_summary = {
        key: value for key, value in state.submitted_summary.items()
        if key not in {
            "role_scopes", "source_only_items", "submitted_only_items",
            "unresolved_items", "local_issues",
        }
    }
    submitted_summary["role_scopes"] = scope_summaries
    progress = state.evidence_progress
    case = {
        "case_id": state.case_id,
        "resource_code": state.resource_code,
        "award_name": state.award_name,
        "year": state.year,
        "trigger_codes": state.trigger_codes,
        "objective": state.objective,
        "m4_evidence": ({
            "result_id": state.m4_evidence.result_id,
            "source_kind": state.m4_evidence.source_kind,
            "source_urls": state.m4_evidence.source_urls[:5],
            "asset_count": len(state.m4_evidence.assets),
            "submitted_count": state.m4_evidence.submitted_count,
            "extracted_count": state.m4_evidence.extracted_count,
            "missing_sample": state.m4_evidence.missing[:20],
            "extra_sample": state.m4_evidence.extra[:20],
        } if state.m4_evidence is not None else None),
        "submitted_summary": _redact_untrusted(submitted_summary),
        "known_urls": _redact_untrusted(state.known_urls),
        "retrieved_memories": _redact_untrusted(state.retrieved_memories),
        "open_questions": _redact_untrusted(state.open_questions),
        "artifact_count": len(state.artifacts),
        "step_count": state.step_count,
        "token_used": state.token_used,
        "budget": state.budget.model_dump(mode="json"),
        "evidence_progress": {
            "phase": progress.phase,
            "search_round": progress.search_round,
            "source_failures": progress.source_failures,
            "successful_sources": progress.successful_sources,
            "candidate_counts": {
                status: sum(item.status == status for item in progress.candidates)
                for status in ("pending", "succeeded", "failed", "skipped")
            },
            "pending_attachment_count": len(progress.pending_attachment_urls),
            "pending_media_count": len(progress.pending_media_urls),
            "matched_identity_count": len(progress.media_matched_identity_hashes),
            "failed_media_count": len(progress.media_failed_urls),
            "difference_samples": list(progress.media_extra_items[:20]),
        },
    }
    selected: list[dict[str, Any]] = []
    used = 0
    for observation in reversed(observations[-20:]):
        size = len(json.dumps(observation, ensure_ascii=False, default=str))
        if selected and used + size > max_observation_chars:
            break
        selected.append(observation)
        used += size
    selected.reverse()
    return AgentTurnContext(
        case=case,
        observations=selected,
        skill_instructions=_load_skill(state)[:4_000],
    )


def _tool_schemas_for_state(
    registry: ToolRegistry,
    state: AuditCaseState,
) -> list[dict[str, Any]]:
    """Expose only tools relevant to the current evidence stage.

    The executor still owns the complete whitelist. This only reduces repeated
    model context and prevents proposing media work before a verified artifact
    of that type exists. Custom registries retain their complete surface.
    """

    specs = registry.specs()
    registered = {spec.name for spec in specs}
    if not registered or not registered.issubset(_DEFAULT_TOOL_NAMES):
        return [spec.openai_schema() for spec in specs[:4]]
    allowed = set(_BASE_AGENT_TOOLS)
    if state.evidence_progress.has_pending_attachments():
        return [
            spec.openai_schema()
            for spec in specs
            if spec.name == "collect_spreadsheet_attachments"
        ]
    if state.evidence_progress.has_pending_media():
        return [
            spec.openai_schema()
            for spec in specs
            if spec.name == "verify_page_image_roster"
        ]
    known_source_attempted = any(
        trace.tool_name in _KNOWN_SOURCE_TOOLS for trace in state.tool_trace
    )
    if state.known_urls and not known_source_attempted:
        allowed.discard("search_official_award")
    if state.evidence_progress.pending_urls():
        allowed.discard("search_official_award")
    if _SEARCH_CANDIDATES_READY in state.reason_codes:
        allowed.discard("search_official_award")
    if state.evidence_progress.search_round >= state.budget.limits.max_searches:
        allowed.discard("search_official_award")
    if state.evidence_progress.source_failures == 0:
        allowed.discard("extract_search_document")
    artifact_kinds = {artifact.kind.lower() for artifact in state.artifacts}
    if "pdf" in artifact_kinds:
        allowed.update({"extract_pdf_text", "render_pdf_pages"})
        if any(
            artifact.kind.lower() == "pdf"
            and not artifact.metadata.get("automatic_pdf_inspection")
            for artifact in state.artifacts
        ):
            allowed.add("inspect_pdf")
    image_kinds = {"png", "jpeg", "gif", "webp", "pdf_page_image"}
    if artifact_kinds.intersection(image_kinds):
        allowed.update(_IMAGE_AGENT_TOOLS)
    priority = {
        "fetch_web_page": 0,
        "search_official_award": 1,
        "collect_spreadsheet_attachments": 2,
        "download_evidence": 3,
        "parse_spreadsheet": 4,
    }
    if "pdf" in artifact_kinds:
        priority.update({
            "inspect_pdf": -3,
            "extract_pdf_text": -2,
            "render_pdf_pages": -1,
        })
    selected_specs = [spec for spec in specs if spec.name in allowed]
    selected_specs.sort(key=lambda spec: priority.get(spec.name, 20))
    return [spec.openai_schema() for spec in selected_specs[:4]]


def _case_scope_count(state: AuditCaseState) -> int | None:
    role_scopes = state.submitted_summary.get("role_scopes")
    if isinstance(role_scopes, list):
        unique_count = sum(
            int(item.get("submitted_identity_count", 0) or 0)
            for item in role_scopes if isinstance(item, dict) and item.get("required", True)
        )
        if unique_count > 0:
            return unique_count
    for key in (
        "expected_scope_count", "submitted_rows", "submitted_count", "reference_rows"
    ):
        value = state.submitted_summary.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
    return None


def _normalized_route_text(value: object) -> str:
    return re.sub(r"[\W_]+", "", str(value or "")).casefold()


def _artifact_scope_candidates(
    state: AuditCaseState, *, local_path: str = "", source_url: str = ""
) -> list[dict[str, Any]]:
    semantic_parts: list[str] = [source_url]
    resolved_source_urls: set[str] = {source_url} if source_url else set()
    for artifact in state.artifacts:
        if (local_path and artifact.local_path == local_path) or (
            source_url and artifact.source_url == source_url
        ):
            if artifact.source_url:
                resolved_source_urls.add(artifact.source_url)
            semantic_parts.extend([
                artifact.source_url,
                str(artifact.metadata.get("label", "")),
                str(artifact.metadata.get("attachment_label", "")),
                str(artifact.metadata.get("section_title", "")),
            ])
            if state.m4_evidence is not None:
                semantic_parts.extend(
                    asset.label for asset in state.m4_evidence.assets
                    if asset.url == artifact.source_url
                )
    semantic = _normalized_route_text(" ".join(semantic_parts))
    raw_scopes = state.submitted_summary.get("role_scopes", [])
    scopes = [item for item in raw_scopes if isinstance(item, dict)]
    matched: list[dict[str, Any]] = []
    for scope in scopes:
        business_scope = scope.get("business_scope", {})
        if not isinstance(business_scope, dict):
            continue
        values = [
            variant
            for key, value in business_scope.items()
            if key not in {"ZYLBM", "year", "LXNF", "HJNF"}
            and len(str(value).strip()) >= 2
            for variant in route_text_variants(value)
        ]
        if values and any(value and value in semantic for value in values):
            matched.append(scope)
    if matched:
        return matched
    generic_required = [
        scope for scope in scopes
        if scope.get("required", True)
        and not any(
            key not in {"ZYLBM", "year", "LXNF", "HJNF"}
            and str(value).strip()
            for key, value in scope.get("business_scope", {}).items()
        )
    ]
    if len(generic_required) == 1:
        return generic_required
    required = [scope for scope in scopes if scope.get("required", True)]
    is_m4_bound_asset = bool(
        state.m4_evidence is not None
        and resolved_source_urls
        and any(
            asset.url in resolved_source_urls for asset in state.m4_evidence.assets
        )
    )
    if (
        is_m4_bound_asset
        and required
        and len(required) <= 12
    ):
        return required
    return required if len(required) == 1 else []


def _scope_tool_arguments(
    scope: Mapping[str, Any],
    all_scopes: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    profile = scope.get("profile", {})
    business_scope = scope.get("business_scope", {})
    alternatives = profile.get("primary_alternatives", []) if isinstance(profile, dict) else []
    match_fields = list(dict.fromkeys(
        str(field) for alternative in alternatives if isinstance(alternative, list)
        for field in alternative if str(field)
    ))
    scope_filter = {
        str(key): str(value) for key, value in business_scope.items()
        if key not in {"ZYLBM", "year"} and str(value).strip()
    } if isinstance(business_scope, dict) else {}
    scope_exclude: dict[str, list[str]] = {}
    if not scope_filter:
        for sibling in all_scopes:
            if sibling is scope or sibling.get("role_type") != scope.get("role_type"):
                continue
            sibling_business = sibling.get("business_scope", {})
            if not isinstance(sibling_business, dict):
                continue
            for key, value in sibling_business.items():
                if key in {"ZYLBM", "year", "LXNF", "HJNF"} or not str(value).strip():
                    continue
                scope_exclude.setdefault(str(key), []).append(str(value))
        scope_exclude = {
            key: list(dict.fromkeys(values))
            for key, values in scope_exclude.items()
        }
    return {
        "scope_id": int(scope.get("scope_id", 0) or 0),
        "role_type": str(scope.get("role_type", "")),
        "submitted_scope_filter": scope_filter,
        "submitted_scope_exclude": scope_exclude,
        "match_fields": match_fields,
        "match_combine": "first",
        "expected_scope_count": int(scope.get("submitted_identity_count", 0) or 0),
        "section_keywords": list(profile.get("section_include_terms", []))[:8]
        if isinstance(profile, dict) else [],
        "section_exclude_keywords": list(profile.get("section_exclude_terms", []))[:8]
        if isinstance(profile, dict) else [],
    }


def _route_image_result_to_scopes(
    state: AuditCaseState,
    result: ToolResult,
    attempted_urls: list[str],
) -> None:
    """Partition one vision batch by business section without another model call."""

    if int(result.data.get("scope_id", 0) or 0):
        return
    raw_records = result.data.get("identity_records", [])
    if not isinstance(raw_records, list):
        return
    records = [record for record in raw_records if isinstance(record, dict)]
    raw_scopes = state.submitted_summary.get("role_scopes", [])
    scopes = [scope for scope in raw_scopes if isinstance(scope, dict)]
    routed_facts: list[EvidenceFact] = []
    base_fact = next(iter(result.evidence_facts), None)
    routes_by_url: dict[str, list[dict[str, Any]]] = {url: [] for url in attempted_urls}
    processed_urls = {
        str(url) for url in result.data.get("processed_image_urls", [])
        if str(url).strip()
    }
    failed_urls = {
        str(url) for url in result.data.get("failed_image_urls", [])
        if str(url).strip()
    }
    for scope in scopes:
        scope_id = int(scope.get("scope_id", 0) or 0)
        if not scope_id:
            continue
        business_scope = scope.get("business_scope", {})
        category_values = [
            _normalized_route_text(value)
            for key, value in business_scope.items()
            if key not in {"ZYLBM", "year", "LXNF", "HJNF"}
            and str(value).strip()
        ] if isinstance(business_scope, dict) else []
        submitted_raw = scope.get("submitted_identities", {})
        submitted = {
            str(identity_key): str(display)
            for identity_key, display in submitted_raw.items()
            if str(identity_key).strip() and str(display).strip()
        } if isinstance(submitted_raw, dict) else {}
        role_type = str(scope.get("role_type", ""))
        evaluated_records: list[
            tuple[dict[str, Any], str, str, list[str], bool, int]
        ] = []
        for record in records:
            section = _normalized_route_text(record.get("section_title", ""))
            raw_name = str(record.get("name", "")).strip()
            raw_org = str(record.get("org", "")).strip()
            candidates = (
                [(raw_org, raw_name), (raw_name, raw_org)]
                if role_type == "organization"
                else [(raw_name, raw_org), (raw_org, raw_name)]
            )
            display = candidates[0][0]
            secondary_display = candidates[0][1]
            selected_orientation = 0
            matching_keys: list[str] = []
            for orientation, (candidate_display, candidate_secondary) in enumerate(
                candidates
            ):
                normalized = normalize_identity(candidate_display)
                if not normalized:
                    continue
                secondary_values = {
                    normalize_identity(value)
                    for value in (
                        candidate_secondary,
                        str(record.get("level", "")),
                    )
                    if str(value).strip()
                }
                candidate_matches: list[str] = []
                for identity_key, submitted_display in submitted.items():
                    key_parts = identity_key.split("\x1f")
                    primary = normalize_identity(key_parts[0])
                    if normalized not in {
                        primary,
                        normalize_identity(submitted_display),
                    }:
                        continue
                    discriminators = [
                        normalize_identity(part.partition("=")[2])
                        for part in key_parts[1:]
                        if part.startswith("discriminator:")
                        and part.partition("=")[2]
                    ]
                    if discriminators and not all(
                        discriminator in secondary_values
                        for discriminator in discriminators
                    ):
                        continue
                    candidate_matches.append(identity_key)
                if candidate_matches:
                    display = candidate_display
                    secondary_display = candidate_secondary
                    selected_orientation = orientation
                    matching_keys = candidate_matches
                    break
            section_match = bool(
                category_values
                and any(value and value in section for value in category_values)
            )
            if display:
                evaluated_records.append((
                    record, display, secondary_display, matching_keys, section_match,
                    selected_orientation,
                ))
        orientation_by_url: dict[str, int] = {}
        for record, _display, _secondary, matching_keys, _section, orientation in (
            evaluated_records
        ):
            source_url = str(record.get("source_url", ""))
            if source_url and matching_keys:
                orientation_by_url.setdefault(source_url, orientation)
        routed_urls = {
            str(record.get("source_url", ""))
            for record, _display, _secondary, matching_keys, section_match, _orientation
            in evaluated_records
            if (matching_keys or section_match)
            and str(record.get("source_url", ""))
        }
        selected_records: list[tuple[dict[str, Any], str, str, list[str]]] = []
        for record, display, secondary, matching_keys, _section, orientation in (
            evaluated_records
        ):
            source_url = str(record.get("source_url", ""))
            if source_url not in routed_urls:
                continue
            page_orientation = orientation_by_url.get(source_url, orientation)
            if not matching_keys and page_orientation != orientation:
                display, secondary = secondary, display
            selected_records.append((record, display, secondary, matching_keys))
        if not selected_records:
            continue
        official: dict[str, str] = {}
        for record, display, secondary_display, matching_keys in selected_records:
            if matching_keys:
                for identity_key in matching_keys:
                    official.setdefault(identity_key, submitted[identity_key])
                continue
            secondary = normalize_identity(secondary_display)
            evidence_key = normalized = normalize_identity(display)
            if secondary:
                evidence_key = f"{normalized}\x1fsource-secondary={secondary}"
            official.setdefault(evidence_key, display)
        matched_keys = set(submitted) & set(official)
        matched_items = [submitted[key] for key in submitted if key in matched_keys]
        missing_items = [submitted[key] for key in submitted if key not in official]
        extra_items = [official[key] for key in official if key not in submitted]
        scope_urls = list(dict.fromkeys(
            str(record.get("source_url", ""))
            for record, _display, _secondary, _matching_keys in selected_records
            if str(record.get("source_url", ""))
        ))
        for url in scope_urls:
            routes_by_url.setdefault(url, []).append({
                "scope_id": scope_id,
                "subunit_type": "image_batch",
                "selector": {"section": category_values[0] if category_values else ""},
                "route_source": "exact_rule",
                "confidence": 1.0,
                "route_status": "routed",
                "reason": "vision section or identity matches submitted scope",
            })
        batch_complete = bool(scope_urls) and all(
            url in processed_urls and url not in failed_urls for url in scope_urls
        )
        routed_facts.append(EvidenceFact(
            status="complete" if batch_complete else "partial",
            award_name=state.award_name,
            year=state.year,
            target_match="yes",
            year_match="yes",
            source_url=result.source_url,
            source_level=(base_fact.source_level if base_fact is not None else "unknown"),
            expected_count=len(submitted),
            observed_count=len(matched_items),
            submitted_count=len(submitted),
            coverage_complete=batch_complete and not missing_items and not extra_items,
            document_count=len(scope_urls),
            extraction_method="page_image_vision",
            scope_id=scope_id,
            role_type=role_type,
            document_complete=batch_complete,
            matched_items=matched_items,
            missing_items=missing_items,
            extra_items=extra_items,
            missing_item_count=len(missing_items),
            extra_item_count=len(extra_items),
        ))
    for url, routes in routes_by_url.items():
        if not routes:
            has_roster_records = any(
                str(record.get("source_url", "")) == url
                and str(record.get("name", "") or record.get("org", "")).strip()
                for record in records
            )
            extraction_failed = url in failed_urls
            routes.append({
                "scope_id": None,
                "subunit_type": "image_batch",
                "selector": {},
                "route_source": "exact_rule",
                "confidence": 0.0 if has_roster_records or extraction_failed else 1.0,
                "route_status": (
                    "ambiguous" if has_roster_records or extraction_failed else "excluded"
                ),
                "reason": (
                    "image roster extraction failed and requires an asset retry"
                    if extraction_failed
                    else
                    "roster identities were extracted but could not be routed to a scope"
                    if has_roster_records
                    else "image contains no roster identity for a submitted scope"
                ),
            })
    if routed_facts:
        result.evidence_facts = routed_facts
    result.data["image_scope_routes"] = routes_by_url


def _route_pdf_result_to_scopes(
    state: AuditCaseState,
    result: ToolResult,
    *,
    local_path: str,
) -> None:
    """Partition one unscoped PDF extraction across every routed scope."""

    if not result.ok or not result.evidence_facts:
        return
    scopes = _artifact_scope_candidates(state, local_path=local_path)
    if not scopes:
        return
    base_fact = result.evidence_facts[0]
    if base_fact.source_level == "unknown":
        artifact = next((
            item for item in state.artifacts
            if str(item.local_path) == str(local_path)
        ), None)
        parent_url = str(artifact.metadata.get("page_url", "")) if artifact else ""
        if parent_url:
            assessment = classify_source(
                parent_url,
                official_domains=_case_domain_metadata(state, "official_domains"),
                official_secondary_domains=_case_domain_metadata(
                    state, "official_secondary_domains"
                ),
            )
            if assessment.level != "unknown":
                base_fact = base_fact.model_copy(update={
                    "source_level": assessment.level,
                })
    matched_by_normalized = {
        normalize_identity(value): str(value)
        for value in result.data.get("matched_items", [])
        if str(value).strip()
    }
    pages = result.data.get("pages", [])
    extracted_parts: list[str] = []
    if isinstance(pages, list):
        for page in pages:
            if not isinstance(page, dict):
                continue
            extracted_parts.append(str(page.get("text", "")))
            for table in page.get("tables", []):
                if not isinstance(table, dict):
                    continue
                for row in table.get("rows", []):
                    if isinstance(row, list):
                        extracted_parts.extend(str(cell or "") for cell in row)
    if not extracted_parts and not matched_by_normalized:
        return
    normalized_document = normalize_identity(" ".join(extracted_parts))
    routed: list[EvidenceFact] = []
    for scope in scopes:
        submitted = scope.get("submitted_identities", {})
        if not isinstance(submitted, dict):
            continue
        scoped_matched = [
            str(display)
            for display in submitted.values()
            if normalize_identity(display)
            and (
                normalize_identity(display) in matched_by_normalized
                or normalize_identity(display) in normalized_document
            )
        ]
        submitted_count = len(submitted)
        complete = bool(base_fact.document_complete)
        coverage_complete = complete and len(scoped_matched) >= submitted_count
        missing = [
            str(display) for display in submitted.values()
            if not normalize_identity(display)
            or (
                normalize_identity(display) not in matched_by_normalized
                and normalize_identity(display) not in normalized_document
            )
        ]
        routed.append(base_fact.model_copy(update={
            "scope_id": int(scope.get("scope_id", 0) or 0),
            "role_type": str(scope.get("role_type", "")),
            "status": (
                "complete"
                if base_fact.target_match == "yes"
                and base_fact.year_match == "yes"
                and coverage_complete
                else "partial"
            ),
            "expected_count": submitted_count,
            "observed_count": len(scoped_matched),
            "submitted_count": submitted_count,
            "reference_count": submitted_count,
            "coverage_complete": coverage_complete,
            "matched_items": scoped_matched,
            "missing_items": missing,
            "missing_item_count": len(missing),
        }))
    if routed:
        result.evidence_facts = routed
        result.data["routed_scope_ids"] = [fact.scope_id for fact in routed]
        extracted_identities = {
            (fact.scope_id, normalize_identity(value))
            for fact in routed
            for value in [*fact.matched_items, *fact.extra_items]
            if normalize_identity(value)
        }
        result_scope_id = int(result.data.get("scope_id", 0) or 0)
        raw_identity_fields = (
            ("matched_items", "split_matched_items", "extra_items")
            if result_scope_id
            else ("extra_items",)
        )
        for field in raw_identity_fields:
            extracted_identities.update(
                (result_scope_id, normalize_identity(value))
                for value in result.data.get(field, [])
                if normalize_identity(value)
            )
        for index, artifact in enumerate(state.artifacts):
            if str(artifact.local_path) != str(local_path):
                continue
            state.artifacts[index] = artifact.model_copy(update={
                "metadata": {
                    **artifact.metadata,
                    "extracted_count": len(extracted_identities),
                    "routed_scope_ids": list(dict.fromkeys(
                        fact.scope_id for fact in routed if fact.scope_id
                    )),
                    "document_complete": all(
                        fact.document_complete is True for fact in routed
                    ),
                },
            })
            break


def _route_web_result_to_scopes(state: AuditCaseState, result: ToolResult) -> None:
    """Route one fetched HTML body to role scopes without fetching it again."""

    if not result.ok or not result.evidence_facts:
        return
    base_fact = result.evidence_facts[0]
    if base_fact.scope_id or base_fact.target_match != "yes" or base_fact.year_match != "yes":
        return
    page_text = _normalized_route_text(result.data.get("text", ""))
    base_matches = {
        normalize_identity(item): item for item in base_fact.matched_items if item.strip()
    }
    raw_scopes = state.submitted_summary.get("role_scopes", [])
    scopes = [scope for scope in raw_scopes if isinstance(scope, dict)]
    section_markers: list[tuple[int, int]] = []
    for index, scope in enumerate(scopes):
        profile = scope.get("profile", {})
        terms = profile.get("section_include_terms", []) if isinstance(profile, dict) else []
        positions = [
            page_text.find(_normalized_route_text(term))
            for term in terms
            if _normalized_route_text(term) and _normalized_route_text(term) in page_text
        ]
        if positions:
            section_markers.append((min(positions), index))
    section_markers.sort()
    routed: list[EvidenceFact] = []
    for index, scope in enumerate(scopes):
        scope_id = int(scope.get("scope_id", 0) or 0)
        submitted_raw = scope.get("submitted_identities", {})
        submitted = {
            normalize_identity(str(display)): str(display)
            for display in submitted_raw.values()
            if str(display).strip()
        } if isinstance(submitted_raw, dict) else {}
        if not scope_id or not submitted:
            continue
        role_type = str(scope.get("role_type", ""))
        comparison_text = page_text
        has_role_section = False
        marker_position = next(
            (position for position, marker_index in section_markers if marker_index == index),
            -1,
        )
        if marker_position >= 0:
            next_position = next(
                (
                    position for position, _marker_index in section_markers
                    if position > marker_position + 80
                ),
                len(page_text),
            )
            comparison_text = page_text[marker_position:next_position]
            has_role_section = True
        matched_keys = {
            key for key in submitted
            if key in base_matches
            or (has_role_section and key and key in comparison_text)
        }
        if not matched_keys:
            continue
        matched_items = [submitted[key] for key in submitted if key in matched_keys]
        missing_items = [submitted[key] for key in submitted if key not in matched_keys]
        document_complete = bool(
            not result.is_truncated
            and not missing_items
            and (has_role_section or role_type in {"team", "work_or_project"})
        )
        routed.append(EvidenceFact(
            status="complete" if document_complete else "partial",
            award_name=base_fact.award_name,
            year=base_fact.year,
            target_match=base_fact.target_match,
            year_match=base_fact.year_match,
            source_url=base_fact.source_url,
            source_level=base_fact.source_level,
            expected_count=len(submitted),
            observed_count=len(matched_items),
            submitted_count=len(submitted),
            coverage_complete=document_complete,
            extraction_method="direct_html_scope_route",
            comparison_scope=str(scope.get("scope_key", ""))[:100],
            scope_id=scope_id,
            role_type=role_type,
            document_complete=document_complete,
            matched_items=matched_items,
            missing_items=missing_items,
            missing_item_count=len(missing_items),
        ))
    if routed:
        result.evidence_facts = routed
        result.data["routed_scope_ids"] = [fact.scope_id for fact in routed]
        required_scope_ids = {
            int(scope.get("scope_id", 0) or 0)
            for scope in scopes
            if bool(scope.get("required", True))
        }
        complete_scope_ids = {
            fact.scope_id for fact in routed
            if fact.document_complete is True and not fact.missing_items
        }
        if required_scope_ids and required_scope_ids <= complete_scope_ids:
            pending_images = set(state.evidence_progress.pending_media_urls)
            exclusion_route = {
                "scope_id": None,
                "subunit_type": "document",
                "selector": {},
                "route_source": "exact_rule",
                "confidence": 1.0,
                "route_status": "excluded",
                "reason": "HTML body fully covers all required scopes",
            }
            if state.m4_evidence is not None:
                state.m4_evidence.assets = [
                    item.model_copy(update={
                        "status": "skipped",
                        "metadata": {
                            **item.metadata,
                            "routes": [exclusion_route],
                            "exclusion_reason": exclusion_route["reason"],
                        },
                    })
                    if item.url in pending_images else item
                    for item in state.m4_evidence.assets
                ]
            state.evidence_progress.pending_media_source_url = ""
            state.evidence_progress.pending_media_page_title = ""
            state.evidence_progress.pending_media_urls = []
            state.evidence_progress.pending_media_parent_urls = {}
            result.data["all_required_scopes_complete"] = True
            result.data["excluded_linked_image_urls"] = sorted(pending_images)


def _route_spreadsheet_result_to_scopes(
    state: AuditCaseState, result: ToolResult
) -> None:
    """Route semantic workbook rows by role and discriminating scope values."""

    raw_records = result.data.get("spreadsheet_identity_records", [])
    if not result.ok or not isinstance(raw_records, list) or not raw_records:
        return
    raw_scopes = state.submitted_summary.get("role_scopes", [])
    scopes = [scope for scope in raw_scopes if isinstance(scope, dict)]
    scopes_by_role: dict[str, list[dict[str, Any]]] = {}
    for scope in scopes:
        scopes_by_role.setdefault(str(scope.get("role_type", "")), []).append(scope)

    ignored_scope_keys = {"ZYLBM", "year", "LXNF", "HJNF"}
    discriminating_by_role: dict[str, set[str]] = {}
    for role_type, role_scopes in scopes_by_role.items():
        values_by_key: dict[str, set[str]] = {}
        for scope in role_scopes:
            business_scope = scope.get("business_scope", {})
            if not isinstance(business_scope, dict):
                continue
            for key, value in business_scope.items():
                normalized = _normalized_route_text(value)
                if key not in ignored_scope_keys and normalized:
                    values_by_key.setdefault(str(key), set()).add(normalized)
        discriminating_by_role[role_type] = {
            key for key, values in values_by_key.items() if len(values) > 1
        }

    records_by_scope: dict[int, list[dict[str, Any]]] = {}
    routes_by_url: dict[str, dict[tuple[int, str], dict[str, Any]]] = {}
    asset_identities: dict[str, set[str]] = {}
    unrouted: list[dict[str, Any]] = []
    for raw_record in raw_records:
        if not isinstance(raw_record, dict):
            continue
        record = dict(raw_record)
        role_type = str(record.get("role_type", ""))
        identity = str(record.get("identity", "")).strip()
        source_url = str(record.get("source_url", ""))
        if not role_type or not identity or not source_url:
            continue
        context = _normalized_route_text(" ".join([
            str(record.get("title", "")),
            str(record.get("attachment_label", "")),
            *(str(value) for value in record.get("category_values", [])
              if str(value).strip()),
            *(str(value) for value in record.get("level_values", [])
              if str(value).strip()),
        ]))
        candidates: list[dict[str, Any]] = []
        for scope in scopes_by_role.get(role_type, []):
            business_scope = scope.get("business_scope", {})
            if not isinstance(business_scope, dict):
                continue
            discriminators = discriminating_by_role.get(role_type, set())
            if all(
                _normalized_route_text(business_scope.get(key, "")) in context
                for key in discriminators
            ):
                candidates.append(scope)
        if len(candidates) != 1:
            unrouted.append(record)
            continue
        scope = candidates[0]
        scope_id = int(scope.get("scope_id", 0) or 0)
        if not scope_id:
            unrouted.append(record)
            continue
        records_by_scope.setdefault(scope_id, []).append(record)
        asset_identities.setdefault(source_url, set()).add(normalize_identity(identity))
        sheet = str(record.get("sheet", ""))
        route_key = (scope_id, sheet)
        route = routes_by_url.setdefault(source_url, {}).setdefault(route_key, {
            "scope_id": scope_id,
            "subunit_type": "sheet",
            "selector": {"sheet": sheet, "row_count": 0},
            "identity_fields": [str(record.get("identity_field", ""))],
            "route_source": "exact_rule",
            "confidence": 1.0,
            "route_status": "routed",
            "reason": "workbook table role and scope discriminator matched",
        })
        route["selector"]["row_count"] += 1

    base_fact = result.evidence_facts[0] if result.evidence_facts else EvidenceFact()
    all_records_complete = all(
        record.get("document_complete") is True
        for records in records_by_scope.values() for record in records
    ) and not unrouted
    facts: list[EvidenceFact] = []
    for scope in scopes:
        scope_id = int(scope.get("scope_id", 0) or 0)
        submitted = scope.get("submitted_identities", {})
        if not scope_id or not isinstance(submitted, dict):
            continue
        scope_records = records_by_scope.get(scope_id, [])
        evidence = {
            normalize_identity(str(record.get("identity", ""))): str(record.get("identity", ""))
            for record in scope_records
            if normalize_identity(str(record.get("identity", "")))
        }
        submitted_by_normalized = {
            normalize_identity(str(display)): str(display)
            for display in submitted.values()
            if normalize_identity(str(display))
        }
        matched_keys = set(evidence) & set(submitted_by_normalized)
        missing = [
            display for key, display in submitted_by_normalized.items()
            if key not in evidence
        ]
        extra = [display for key, display in evidence.items() if key not in submitted_by_normalized]
        document_complete = bool(scope_records) and all_records_complete
        facts.append(base_fact.model_copy(update={
            "scope_id": scope_id,
            "role_type": str(scope.get("role_type", "")),
            "status": "complete" if document_complete else "partial",
            "expected_count": len(submitted_by_normalized),
            "observed_count": len(evidence),
            "submitted_count": len(submitted_by_normalized),
            "reference_count": len(evidence),
            "coverage_complete": document_complete and not missing and not extra,
            "document_complete": document_complete,
            "matched_items": [submitted_by_normalized[key] for key in matched_keys],
            "missing_items": missing,
            "extra_items": extra,
            "missing_item_count": len(missing),
            "extra_item_count": len(extra),
            "extraction_method": "semantic_spreadsheet_scope_route",
        }))
    if facts:
        result.evidence_facts = facts
    result.data.update({
        "spreadsheet_scope_routes": {
            url: list(routes.values()) for url, routes in routes_by_url.items()
        },
        "spreadsheet_asset_identity_counts": {
            url: len(values) for url, values in asset_identities.items()
        },
        "spreadsheet_unrouted_record_count": len(unrouted),
        "all_required_scopes_complete": bool(facts) and all(
            fact.document_complete is True for fact in facts
        ),
    })


def _reuse_local_spreadsheet_evidence(
    state: AuditCaseState, *, allowed_roots: list[str | Path]
) -> ToolResult | None:
    records: list[dict[str, Any]] = []
    sheet_manifests: dict[str, list[dict[str, Any]]] = {}
    for artifact in state.artifacts:
        if artifact.kind not in {"xls", "xlsx"} or not artifact.local_path:
            continue
        try:
            path = validate_local_path(
                artifact.local_path, allowed_roots, must_exist=True, file_only=True
            )
            inspection = inspect_evidence_file(
                path, max_bytes=20 * 1024 * 1024, allowed_kinds={"xls", "xlsx"}
            )
        except (OSError, ValueError):
            continue
        if artifact.sha256 and inspection.sha256 != artifact.sha256:
            continue
        grid = spreadsheet_tools.parse_award_excel(path)
        semantic_records = spreadsheet_tools.extract_semantic_roster_records(grid)
        if not semantic_records:
            continue
        page_url = str(artifact.metadata.get(
            "page_url", artifact.metadata.get("parent_url", "")
        ))
        label = str(artifact.metadata.get("attachment_label", ""))
        records.extend({
            **record,
            "source_url": artifact.source_url,
            "parent_url": page_url,
            "attachment_label": label,
        } for record in semantic_records)
        raw_sheet_grids = grid.get("sheet_grids", [])
        sheet_manifests[artifact.source_url] = [
            {
                "sheet": str(sheet.get("sheet", "")),
                "row_count": int(sheet.get("n_rows", 0) or 0),
                "truncated": bool(sheet.get("truncated", False)),
            }
            for sheet in raw_sheet_grids
            if isinstance(sheet, dict)
        ] if isinstance(raw_sheet_grids, list) else []
    if not records:
        return None
    result = ToolResult(
        ok=True,
        data={
            "spreadsheet_identity_records": records,
            "document_complete": all(
                record.get("document_complete") is True for record in records
            ),
            "reused_local_artifacts": True,
        },
        evidence_facts=[EvidenceFact(
            target_match="yes",
            year_match="yes",
            source_level="institutional_secondary",
            extraction_method="reused_local_spreadsheet_assets",
        )],
    )
    _route_spreadsheet_result_to_scopes(state, result)
    routes_by_url = result.data.get("spreadsheet_scope_routes", {})
    counts_by_url = result.data.get("spreadsheet_asset_identity_counts", {})
    for index, artifact in enumerate(state.artifacts):
        if artifact.source_url not in sheet_manifests:
            continue
        routes = routes_by_url.get(artifact.source_url, []) if isinstance(
            routes_by_url, dict
        ) else []
        extracted_count = counts_by_url.get(artifact.source_url, 0) if isinstance(
            counts_by_url, dict
        ) else 0
        state.artifacts[index] = artifact.model_copy(update={
            "metadata": {
                **artifact.metadata,
                "routes": routes,
                "extracted_count": int(extracted_count or 0),
                "sheet_manifest": sheet_manifests[artifact.source_url],
                "reused_in_attempt": state.active_attempt_id,
            },
        })
    return result


def _restore_excluded_asset_routes(state: AuditCaseState, store: Store) -> None:
    excluded_urls = {
        str(route.get("url", ""))
        for route in store.list_evidence_asset_routes(state.case_id)
        if route.get("route_status") == "excluded"
    }
    if not excluded_urls:
        return
    exclusion_route = {
        "scope_id": None,
        "subunit_type": "document",
        "selector": {},
        "route_source": "exact_rule",
        "confidence": 1.0,
        "route_status": "excluded",
        "reason": "restored terminal exclusion from evidence ledger",
    }
    state.artifacts = [
        artifact.model_copy(update={
            "metadata": {**artifact.metadata, "routes": [exclusion_route]},
        }) if artifact.source_url in excluded_urls else artifact
        for artifact in state.artifacts
    ]
    if state.m4_evidence is not None:
        state.m4_evidence.assets = [
            asset.model_copy(update={
                "status": "skipped",
                "metadata": {**asset.metadata, "routes": [exclusion_route]},
            }) if asset.url in excluded_urls else asset
            for asset in state.m4_evidence.assets
        ]
def _case_domain_metadata(state: AuditCaseState, key: str) -> list[str]:
    raw = state.submitted_summary.get(key)
    if not isinstance(raw, list):
        return []
    domains: list[str] = []
    for value in raw[:8]:
        if not isinstance(value, str):
            continue
        try:
            domain = normalize_domain(value)
        except ValueError:
            continue
        if domain not in domains:
            domains.append(domain)
    return domains


def _case_source_hosts(state: AuditCaseState) -> list[str]:
    urls = [
        *state.known_urls,
        *(candidate.url for candidate in state.evidence_progress.candidates),
    ]
    hosts: list[str] = []
    for url in urls:
        if not url.startswith(("http://", "https://")):
            continue
        try:
            host = normalize_domain(urlsplit(url).hostname or "").removeprefix("www.")
        except ValueError:
            continue
        if host not in hosts:
            hosts.append(host)
    return hosts


def _case_submission_paths(state: AuditCaseState) -> list[str]:
    raw_paths = state.submitted_summary.get("submission_files")
    paths = (
        [str(item) for item in raw_paths if str(item).strip()]
        if isinstance(raw_paths, list)
        else []
    )
    legacy = state.submitted_summary.get("submission_file")
    if legacy and str(legacy) not in paths:
        paths.insert(0, str(legacy))
    return list(dict.fromkeys(paths))[:20]


def _fetch_arguments(state: AuditCaseState, arguments: dict[str, Any]) -> dict[str, Any]:
    """Fill verifier inputs from trusted case metadata without changing old Tool calls."""

    enriched = dict(arguments)
    enriched.pop("page_total_count", None)
    enriched["expected_award_name"] = state.award_name
    enriched["expected_year"] = state.year
    enriched["award_aliases"] = []
    enriched["official_domains"] = _case_domain_metadata(state, "official_domains")
    enriched["official_secondary_domains"] = _case_domain_metadata(
        state, "official_secondary_domains"
    )
    enriched["section_keywords"] = []
    enriched["section_exclude_keywords"] = []
    enriched["max_chars"] = 30_000
    submitted_paths = _case_submission_paths(state)
    match_fields = state.submitted_summary.get("match_fields")
    if submitted_paths and isinstance(match_fields, list) and match_fields:
        enriched["submitted_path"] = submitted_paths[0]
        enriched["submitted_paths"] = submitted_paths
        enriched["match_fields"] = match_fields
        enriched["match_combine"] = state.submitted_summary.get(
            "match_combine", "first"
        )
    scope_count = _case_scope_count(state)
    if scope_count is not None:
        enriched["expected_scope_count"] = scope_count
    reference_rows = state.submitted_summary.get("reference_rows")
    if isinstance(reference_rows, int) and reference_rows > 0:
        enriched["page_total_count"] = reference_rows
    relationship_terms = _public_discrepancy_terms(state)
    enriched["relationship_terms"] = relationship_terms
    return enriched


def _image_roster_arguments(
    state: AuditCaseState, arguments: dict[str, Any]
) -> dict[str, Any]:
    """Bind image comparison to the fetched page and trusted case scope."""

    enriched = _fetch_arguments(state, arguments)
    progress = state.evidence_progress
    if progress.has_pending_media():
        enriched["page_url"] = progress.pending_media_source_url
        enriched["page_title"] = progress.pending_media_page_title
        remaining_vision = max(
            0,
            state.budget.limits.max_vision_pages - state.budget.vision_pages,
        )
        image_limit = min(6, remaining_vision)
        pending_urls = progress.pending_media_urls
        route_scopes = _artifact_scope_candidates(
            state, source_url=pending_urls[0]
        ) if pending_urls else []
        if len(route_scopes) == 1:
            selected_scope = route_scopes[0]
            scope_id = int(selected_scope.get("scope_id", 0) or 0)
            scoped_urls = [
                url for url in pending_urls
                if any(
                    int(scope.get("scope_id", 0) or 0) == scope_id
                    for scope in _artifact_scope_candidates(state, source_url=url)
                )
            ]
            raw_scopes = state.submitted_summary.get("role_scopes", [])
            enriched.update(_scope_tool_arguments(
                selected_scope,
                [item for item in raw_scopes if isinstance(item, dict)],
            ))
            enriched["image_urls"] = scoped_urls[:image_limit]
        else:
            # A mixed roster page is extracted once without a scope filter. The
            # returned identity records are then partitioned deterministically
            # across every role/category scope by _route_image_result_to_scopes.
            enriched.pop("scope_id", None)
            enriched.pop("role_type", None)
            enriched.pop("submitted_scope_filter", None)
            enriched.pop("submitted_scope_exclude", None)
            enriched.pop("section_keywords", None)
            enriched.pop("section_exclude_keywords", None)
            enriched["image_urls"] = pending_urls[:image_limit]
    enriched.pop("url", None)
    enriched.pop("max_chars", None)
    enriched.pop("page_total_count", None)
    enriched.pop("relationship_terms", None)
    return {
        key: value
        for key, value in enriched.items()
        if key in VerifyPageImageRosterInput.model_fields
    }


def _calibrate_media_wall_time(state: AuditCaseState) -> None:
    image_count = len(state.evidence_progress.pending_media_urls)
    if image_count <= 20:
        return
    current = state.budget.limits.wall_time_seconds
    required = min(15 * 60, max(current, 2 * 60 + image_count * 12))
    if required > current:
        state.budget.limits = state.budget.limits.model_copy(update={
            "wall_time_seconds": required,
        })


def _collect_arguments(state: AuditCaseState, arguments: dict[str, Any]) -> dict[str, Any]:
    """Bind composite attachment collection to trusted case metadata."""

    enriched = dict(arguments)
    enriched["expected_award_name"] = state.award_name
    enriched["expected_year"] = state.year
    if state.evidence_progress.has_pending_attachments():
        progress = state.evidence_progress
        pending_urls = progress.pending_attachment_urls
        route_scopes = _artifact_scope_candidates(
            state, source_url=pending_urls[0]
        ) if pending_urls else []
        selected_urls = pending_urls
        if route_scopes:
            selected_scope = route_scopes[0]
            scope_id = int(selected_scope.get("scope_id", 0) or 0)
            selected_urls = [
                url for url in pending_urls
                if any(
                    int(scope.get("scope_id", 0) or 0) == scope_id
                    for scope in _artifact_scope_candidates(state, source_url=url)
                )
            ]
            raw_scopes = state.submitted_summary.get("role_scopes", [])
            enriched.update(_scope_tool_arguments(
                selected_scope,
                [item for item in raw_scopes if isinstance(item, dict)],
            ))
        enriched["attachment_urls"] = selected_urls
        parent_urls = {
            url: progress.pending_attachment_parent_urls[url]
            for url in selected_urls
            if url in progress.pending_attachment_parent_urls
        }
        enriched["attachment_parent_urls"] = parent_urls
        enriched["page_urls"] = list(dict.fromkeys(parent_urls.values())) or (
            progress.pending_attachment_page_urls
        )
    elif not enriched.get("page_urls") and state.known_urls:
        enriched["page_urls"] = state.known_urls[:5]
    submitted_paths = _case_submission_paths(state)
    fields = enriched.get("match_fields") or (
        state.submitted_summary.get("attachment_match_fields")
        or state.submitted_summary.get("match_fields")
    )
    if submitted_paths and isinstance(fields, list) and fields:
        enriched["submitted_path"] = submitted_paths[0]
        enriched["submitted_paths"] = submitted_paths
        enriched["match_fields"] = fields
        enriched["match_combine"] = state.submitted_summary.get(
            "match_combine", "first"
        )
    enriched["include_attachment_keywords"] = []
    enriched["exclude_attachment_keywords"] = []
    enriched["award_aliases"] = []
    enriched["official_domains"] = _case_domain_metadata(state, "official_domains")
    enriched["official_secondary_domains"] = _case_domain_metadata(
        state, "official_secondary_domains"
    )
    scope_count = enriched.get("expected_scope_count")
    if scope_count is None:
        scope_count = _case_scope_count(state)
    if scope_count is not None:
        enriched["expected_scope_count"] = scope_count
    return {
        key: value
        for key, value in enriched.items()
        if key in CollectSpreadsheetAttachmentsInput.model_fields
    }


def _extract_arguments(state: AuditCaseState, arguments: dict[str, Any]) -> dict[str, Any]:
    enriched = _fetch_arguments(state, arguments)
    raw_url = str(enriched.get("url", ""))
    host = (urlsplit(raw_url).hostname or "").lower().removeprefix("www.")
    candidate_title = next(
        (
            candidate.title
            for candidate in state.evidence_progress.candidates
            if candidate.url == raw_url and candidate.title
        ),
        "",
    )
    scope_count = _case_scope_count(state)
    query_parts = [
        f"site:{host}" if host else "",
        state.award_name,
        state.year,
        candidate_title,
        f"{scope_count}项" if scope_count is not None else "",
        "名单公示",
    ]
    enriched["search_query"] = " ".join(
        item.strip() for item in query_parts if item.strip()
    )[:100]
    return {
        key: value
        for key, value in enriched.items()
        if key in ExtractSearchDocumentInput.model_fields
    }


def _pdf_extract_arguments(
    state: AuditCaseState,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Bind PDF roster extraction to trusted case and artifact provenance."""

    enriched = dict(arguments)
    parent_keys = {
        "parent_page_url",
        "parent_attachment_linked",
        "parent_award_name",
        "parent_year",
        "parent_source_level",
    }
    for key in parent_keys:
        enriched.pop(key, None)
    enriched["expected_award_name"] = state.award_name
    enriched["expected_year"] = state.year
    enriched["award_aliases"] = []
    enriched["official_domains"] = _case_domain_metadata(state, "official_domains")
    enriched["official_secondary_domains"] = _case_domain_metadata(
        state, "official_secondary_domains"
    )
    submitted_paths = _case_submission_paths(state)
    requested_scope_id = int(enriched.get("scope_id", 0) or 0)
    scopes = _artifact_scope_candidates(state, local_path=str(enriched.get("path", "")))
    selected_scope = next((
        scope for scope in scopes
        if requested_scope_id and int(scope.get("scope_id", 0) or 0) == requested_scope_id
    ), scopes[0] if len(scopes) == 1 else None)
    if selected_scope is not None:
        raw_scopes = state.submitted_summary.get("role_scopes", [])
        enriched.update(_scope_tool_arguments(
            selected_scope,
            [item for item in raw_scopes if isinstance(item, dict)],
        ))
    match_fields = enriched.get("match_fields") or state.submitted_summary.get("match_fields")
    if submitted_paths and isinstance(match_fields, list) and match_fields:
        enriched["submitted_path"] = submitted_paths[0]
        enriched["submitted_paths"] = submitted_paths
        enriched["match_fields"] = match_fields
        enriched.setdefault("match_combine", state.submitted_summary.get("match_combine", "first"))
    if selected_scope is None:
        scope_count = _case_scope_count(state)
        if scope_count is not None:
            enriched["expected_scope_count"] = scope_count
    raw_path = str(enriched.get("path", ""))
    for artifact in state.artifacts:
        if artifact.kind.lower() == "pdf" and artifact.local_path == raw_path:
            enriched["source_url"] = artifact.source_url
            metadata = artifact.metadata
            if metadata.get("attachment_linked") is True:
                enriched["parent_attachment_linked"] = True
                enriched["parent_page_url"] = str(metadata.get("page_url", ""))
                enriched["parent_award_name"] = str(
                    metadata.get("page_observed_award_name", "")
                )
                enriched["parent_year"] = str(
                    metadata.get("page_observed_year", "")
                )
                enriched["parent_source_level"] = str(
                    metadata.get("page_source_level", "unknown")
                )
            break
    return enriched


def _search_arguments(state: AuditCaseState, arguments: dict[str, Any]) -> dict[str, Any]:
    """Exclude URLs already attempted in this case from later search candidates."""

    enriched = dict(arguments)
    for key in (
        "award_name",
        "year",
        "organizer",
        "award_type",
        "session",
        "english_name",
        "strategy",
        "official_domains",
        "official_secondary_domains",
        "site_domains",
        "recovery_terms",
        "require_award_name_match",
    ):
        enriched.pop(key, None)
    enriched["award_name"] = state.award_name
    enriched["year"] = state.year if re.fullmatch(r"[0-9]{4}(?:-[0-9]{4})?", state.year) else ""
    requested = enriched.get("exclude_urls", [])
    excluded = [str(item) for item in requested] if isinstance(requested, list) else []
    for trace in state.tool_trace:
        raw_url = trace.input_summary.get("url")
        if isinstance(raw_url, str) and raw_url.startswith(("http://", "https://")):
            excluded.append(raw_url)
    excluded.extend(
        item.url for item in state.evidence_progress.candidates if item.status != "pending"
    )
    unique = list(dict.fromkeys(item for item in excluded if item))[:20]
    if unique:
        enriched["exclude_urls"] = unique
    discrepancy_terms = _public_discrepancy_terms(state)
    enriched["official_domains"] = _case_domain_metadata(state, "official_domains")
    enriched["official_secondary_domains"] = _case_domain_metadata(
        state, "official_secondary_domains"
    )
    enriched["require_award_name_match"] = True
    if len(discrepancy_terms) >= 2 and not _discrepancy_search_completed(state):
        enriched["strategy"] = "discrepancy"
        enriched["discrepancy_terms"] = discrepancy_terms
        enriched.pop("official_domains", None)
        enriched["max_results"] = min(int(enriched.get("max_results", 5) or 5), 5)
    else:
        hosts = _case_source_hosts(state)
        failed_urls = _failed_urls(state)
        attempted_document_ids = {
            document_id
            for candidate in state.evidence_progress.candidates
            if candidate.attempts > 0
            and (document_id := _document_id(candidate.url))
        }
        recovery_terms = list(dict.fromkeys(
            document_id
            for url in failed_urls
            if (document_id := _document_id(url))
            and document_id not in attempted_document_ids
        ))[:4]
        if recovery_terms:
            enriched["recovery_terms"] = recovery_terms
        use_site_recovery = bool(
            hosts and failed_urls and state.evidence_progress.search_round == 0
        )
        if use_site_recovery:
            enriched["strategy"] = "site"
            enriched["site_domains"] = hosts[:8]
        elif failed_urls:
            enriched["strategy"] = "broad"
        elif state.evidence_progress.search_round == 1:
            enriched["strategy"] = "attachment"
            enriched["max_results"] = 8
        else:
            enriched["strategy"] = "broad"
    return enriched


def _trace_facts(trace: Any) -> dict[str, Any]:
    summary = trace.output_summary if isinstance(trace.output_summary, dict) else {}
    facts = summary.get("verification_facts", {})
    return facts if isinstance(facts, dict) else {}


def _public_discrepancy_terms(state: AuditCaseState) -> list[str]:
    """Use only discrepancy names already observed in fetched public evidence."""

    conflict: dict[str, Any] = {}
    for trace in reversed(state.tool_trace):
        facts = _trace_facts(trace)
        missing = facts.get("missing_items")
        extra = facts.get("extra_items")
        if isinstance(missing, list) and missing and isinstance(extra, list) and extra:
            conflict = facts
            break
    if not conflict:
        return []

    public_matches: list[str] = []
    for trace in state.tool_trace:
        if not trace.ok or trace.tool_name not in {
            "fetch_web_page",
            "extract_search_document",
            "verify_page_image_roster",
            "collect_spreadsheet_attachments",
        }:
            continue
        facts = _trace_facts(trace)
        for key in ("matched_items", "split_matched_items"):
            values = facts.get(key, [])
            if isinstance(values, list):
                public_matches.extend(str(item) for item in values if isinstance(item, str))
    normalized_public = _normalise_public_terms(public_matches)

    trusted_missing: list[str] = []
    trusted_extra: list[str] = []
    missing = conflict.get("missing_items", [])
    extra = conflict.get("extra_items", [])
    for value in missing if isinstance(missing, list) else []:
        term = str(value).strip()
        normalized = re.sub(r"\W+", "", term, flags=re.UNICODE).casefold()
        if normalized and normalized in normalized_public and _bounded_search_term(term):
            trusted_missing.append(term)
    for value in extra if isinstance(extra, list) else []:
        term = str(value).strip()
        if _bounded_search_term(term):
            trusted_extra.append(term)
    if not trusted_missing or not trusted_extra:
        return []
    return list(dict.fromkeys([*trusted_missing, *trusted_extra]))[:8]


def _has_observed_discrepancy(state: AuditCaseState) -> bool:
    """Return whether a fetched source recorded any item-level mismatch."""

    for trace in state.tool_trace:
        if not trace.ok:
            continue
        facts = _trace_facts(trace)
        for key in ("missing_items", "extra_items"):
            values = facts.get(key)
            if isinstance(values, list) and values:
                return True
        for key in ("missing_item_count", "extra_item_count"):
            value = facts.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                return True
    return False


def _normalise_public_terms(values: list[str]) -> str:
    return " ".join(
        re.sub(r"\W+", "", value, flags=re.UNICODE).casefold() for value in values
    )


def _bounded_search_term(value: str) -> bool:
    return bool(
        value
        and len(value) <= 80
        and not any(ord(char) < 32 for char in value)
        and not value.startswith(("http://", "https://"))
    )


def _discrepancy_search_completed(state: AuditCaseState) -> bool:
    return any(
        trace.tool_name == "search_official_award"
        and trace.input_summary.get("strategy") == "discrepancy"
        for trace in state.tool_trace
    )


def _needs_discrepancy_recovery(
    state: AuditCaseState,
    results: list[ToolResult],
    registry: ToolRegistry,
) -> bool:
    # A complete official roster may legitimately differ from the submission.
    # Missing/extra identities are a business result, not evidence incompleteness.
    if any(
        fact.document_complete is True
        and fact.target_match == "yes"
        and fact.year_match == "yes"
        and fact.source_level in {"official_primary", "official_secondary"}
        for result in results
        for fact in result.evidence_facts
    ):
        return False
    for trace in state.tool_trace:
        facts = _trace_facts(trace)
        if (
            facts.get("document_complete") is True
            and facts.get("source_level") in {"official_primary", "official_secondary"}
        ):
            return False
    return bool(
        _has_observed_discrepancy(state)
        and not _discrepancy_search_completed(state)
        and "coverage_discrepancy_recovery_started" not in state.reason_codes
        and "identity_discrepancy_recovery_started" not in state.reason_codes
        and registry.get("search_official_award") is not None
        and state.evidence_progress.search_round < 1
        and state.budget.searches < state.budget.limits.max_searches
        and state.budget.calls < state.budget.limits.max_calls
        and state.budget.candidate_urls < state.budget.limits.max_candidate_urls
    )


def _needs_known_source_recovery(
    state: AuditCaseState,
    results: list[ToolResult],
    registry: ToolRegistry,
) -> bool:
    """Guarantee one bounded search after provided sources remain inconclusive."""

    attempted_known_urls = {
        str(trace.input_summary.get("url", ""))
        for trace in state.tool_trace
        if trace.tool_name in _KNOWN_SOURCE_TOOLS
    }.intersection(state.known_urls)
    return bool(
        attempted_known_urls
        and not _has_complete_verifier_snapshot(state, results)
        and not any(result.artifacts for result in results)
        and state.evidence_progress.search_round == 0
        and "known_source_incomplete_recovery_started" not in state.reason_codes
        and registry.get("search_official_award") is not None
        and state.budget.searches < state.budget.limits.max_searches
        and state.budget.calls < state.budget.limits.max_calls
        and state.budget.candidate_urls < state.budget.limits.max_candidate_urls
    )


def _failed_urls(state: AuditCaseState) -> set[str]:
    failed: set[str] = set()
    for trace in state.tool_trace:
        raw_url = trace.input_summary.get("url")
        if (
            not trace.ok
            and isinstance(raw_url, str)
            and raw_url.startswith(("http://", "https://"))
        ):
            failed.add(raw_url)
    return failed


def _attempted_urls(state: AuditCaseState) -> set[str]:
    attempted: set[str] = set()
    for trace in state.tool_trace:
        for key in ("url", "page_url", "source_url"):
            value = trace.input_summary.get(key)
            if isinstance(value, str) and value.startswith(("http://", "https://")):
                attempted.add(value)
        for key in ("page_urls", "image_urls"):
            values = trace.input_summary.get(key)
            if isinstance(values, list):
                attempted.update(
                    value
                    for value in values
                    if isinstance(value, str)
                    and value.startswith(("http://", "https://"))
                )
    attempted.update(
        artifact.source_url
        for artifact in state.artifacts
        if artifact.source_url.startswith(("http://", "https://"))
    )
    return attempted


def _attempted_source_urls(state: AuditCaseState) -> set[str]:
    """URLs actually opened as sources; asset work must not hide their parent pages."""

    attempted: set[str] = set()
    for trace in state.tool_trace:
        if trace.tool_name not in {"fetch_web_page", "extract_search_document"}:
            continue
        value = trace.input_summary.get("url")
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            attempted.add(value)
    return attempted


def _filter_unacquired_asset_urls(
    state: AuditCaseState,
    urls: Iterable[str],
) -> list[str]:
    """Keep newly discovered asset URLs that have not already been acquired."""

    acquired = {
        artifact.source_url
        for artifact in state.artifacts
        if artifact.source_url.startswith(("http://", "https://"))
    }
    return [
        url
        for url in dict.fromkeys(urls)
        if url.startswith(("http://", "https://")) and url not in acquired
    ]


def _next_unattempted_known_url(state: AuditCaseState) -> str:
    attempted = _attempted_source_urls(state)
    return next((
        url for url in state.known_urls
        if url not in attempted
        and Path(urlsplit(url).path).suffix.casefold() not in _IMAGE_ASSET_EXTENSIONS
        and Path(urlsplit(url).path).suffix.casefold() not in {".pdf", ".xls", ".xlsx"}
    ), "")


def _result_has_identity_mismatch(result: ToolResult) -> bool:
    return any(
        fact.target_match == "no" or fact.year_match == "no"
        for fact in result.evidence_facts
    )


def _asset_followup_allowed(result: ToolResult) -> bool:
    """Inspect linked assets unless the source year is explicitly wrong.

    Notice pages often keep the award identity inside a PDF or roster image, so a
    body-level target mismatch must not suppress bounded attachment processing.
    """

    return not any(fact.year_match == "no" for fact in result.evidence_facts)


def _consume_pending_media(state: AuditCaseState, processed_urls: list[str]) -> None:
    processed = set(processed_urls)
    progress = state.evidence_progress
    progress.pending_media_urls = [
        url for url in progress.pending_media_urls if url not in processed
    ]
    if not progress.pending_media_urls:
        progress.pending_media_source_url = ""
        progress.pending_media_page_title = ""


def _update_media_queue_after_verification(
    state: AuditCaseState,
    result: ToolResult,
    attempted_urls: list[str],
) -> None:
    """Checkpoint one image batch and expose differences only after all batches."""

    has_manifest = any(
        key in result.data
        for key in (
            "processed_image_urls",
            "failed_image_urls",
            "unprocessed_image_urls",
        )
    )
    if not has_manifest:
        _consume_pending_media(state, attempted_urls)
        return

    def strings(key: str) -> list[str]:
        raw = result.data.get(key, [])
        return [str(item) for item in raw if str(item).strip()] if isinstance(raw, list) else []

    progress = state.evidence_progress
    evidence_group = progress.pending_media_source_url
    scope_id = int(result.data.get("scope_id", 0) or 0)
    scope_key = str(scope_id)
    accumulator = progress.media_scope_accumulators.setdefault(scope_key, {
        "expected_items": {},
        "matched_identity_hashes": [],
        "extra_items": [],
        "failed_urls": [],
        "asset_urls": [],
    })
    scope_asset_urls = [
        url for url in progress.pending_media_urls
        if scope_id == 0 or any(
            int(scope.get("scope_id", 0) or 0) == scope_id
            for scope in _artifact_scope_candidates(state, source_url=url)
        )
    ]
    accumulator["asset_urls"] = list(dict.fromkeys([
        *accumulator.get("asset_urls", []),
        *scope_asset_urls,
        *attempted_urls,
    ]))[:100]
    previous_matched = set(accumulator.get("matched_identity_hashes", []))
    processed = set(strings("processed_image_urls"))
    failed = set(strings("failed_image_urls"))
    unprocessed = set(strings("unprocessed_image_urls"))
    terminal = processed | failed
    progress.pending_media_urls = [
        url
        for url in progress.pending_media_urls
        if url not in terminal or url in unprocessed
    ]
    progress.media_failed_urls = list(dict.fromkeys([
        *progress.media_failed_urls,
        *[url for url in attempted_urls if url in failed],
    ]))[:100]
    accumulator["failed_urls"] = list(dict.fromkeys([
        *accumulator.get("failed_urls", []),
        *[url for url in attempted_urls if url in failed],
    ]))[:100]
    progress.pending_media_parent_urls = {
        url: parent
        for url, parent in progress.pending_media_parent_urls.items()
        if url in progress.pending_media_urls
    }

    expected_items = accumulator.setdefault("expected_items", {})
    submitted_items = result.data.get("submitted_identity_items")
    if isinstance(submitted_items, dict):
        for identity_hash, display in submitted_items.items():
            if len(expected_items) >= 10_000:
                break
            if str(identity_hash).strip() and str(display).strip():
                expected_items.setdefault(str(identity_hash), str(display))
    else:
        for display in [*strings("matched_items"), *strings("missing_items")]:
            identity_hash = hashlib.sha256(
                normalize_identity(display).encode("utf-8")
            ).hexdigest()[:32]
            expected_items.setdefault(identity_hash, display)

    matched_hashes = strings("matched_identity_hashes")
    if not matched_hashes:
        matched_hashes = [
            hashlib.sha256(normalize_identity(display).encode("utf-8")).hexdigest()[:32]
            for display in strings("matched_items")
        ]
    matched_identity_hashes = list(dict.fromkeys([
        *accumulator.get("matched_identity_hashes", []),
        *matched_hashes,
    ]))[:10_000]
    accumulator["matched_identity_hashes"] = matched_identity_hashes
    batch_new_count = len(set(matched_identity_hashes) - previous_matched)
    extra_items = list(dict.fromkeys([
        *accumulator.get("extra_items", []),
        *strings("extra_items"),
    ]))[:1_000]
    accumulator["extra_items"] = extra_items

    # Keep the legacy aggregate fields readable without using them for scope comparison.
    progress.media_expected_items.update(expected_items)
    progress.media_matched_identity_hashes = list(dict.fromkeys([
        *progress.media_matched_identity_hashes,
        *matched_identity_hashes,
    ]))[:10_000]
    progress.media_extra_items = list(dict.fromkeys([
        *progress.media_extra_items,
        *extra_items,
    ]))[:1_000]

    matched_set = set(matched_identity_hashes)
    unmatched_items = [
        display
        for identity_hash, display in expected_items.items()
        if identity_hash not in matched_set
    ]
    remaining_scope_urls = set(accumulator.get("asset_urls", [])) & set(
        progress.pending_media_urls
    )
    all_images_processed = (
        not remaining_scope_urls and not accumulator.get("failed_urls", [])
    )
    expected_count = max(
        len(expected_items),
        int(result.data.get("expected_count", 0) or 0),
    )
    coverage_complete = bool(
        all_images_processed
        and len(matched_set) == expected_count
        and not unmatched_items
        and not extra_items
    )
    missing_items = unmatched_items if all_images_processed else []
    unresolved_items = [] if all_images_processed else unmatched_items
    result.data.update({
        "evidence_group": evidence_group,
        "document_complete": all_images_processed,
        "submitted_identity_items": expected_items,
        "batch_new_identity_count": batch_new_count,
        "cumulative_identity_count": len(matched_set),
        "expected_count": expected_count,
        "observed_count": len(matched_set),
        "coverage_complete": coverage_complete,
        "matched_items": [
            display
            for identity_hash, display in expected_items.items()
            if identity_hash in matched_set
        ],
        "missing_items": missing_items,
        "extra_items": extra_items,
        "unresolved_items": unresolved_items,
        "missing_item_count": len(missing_items),
        "extra_item_count": len(extra_items),
        "unresolved_item_count": len(unresolved_items),
        "matched_identity_hashes": matched_identity_hashes,
        "unprocessed_image_urls": sorted(remaining_scope_urls),
        "failed_image_urls": accumulator.get("failed_urls", []),
        "all_images_processed": all_images_processed,
    })

    for fact in result.evidence_facts:
        if fact.extraction_method != "page_image_vision":
            continue
        fact.expected_count = expected_count
        fact.observed_count = len(matched_set)
        fact.coverage_complete = coverage_complete
        fact.document_complete = all_images_processed
        fact.matched_items = result.data["matched_items"]
        fact.missing_items = result.data["missing_items"]
        fact.extra_items = result.data["extra_items"]
        fact.unresolved_items = result.data["unresolved_items"]
        fact.missing_item_count = len(missing_items)
        fact.extra_item_count = len(progress.media_extra_items)
        fact.unresolved_item_count = len(unresolved_items)
        if coverage_complete:
            fact.status = "complete"

    if not progress.pending_media_urls:
        progress.pending_media_source_url = ""
        progress.pending_media_page_title = ""


def _tool_call_already_traced(
    state: AuditCaseState,
    tool_name: str,
    arguments: dict[str, Any],
) -> bool:
    """Detect an exact repeat without persisting or exposing raw arguments."""

    safe_arguments = _redact_untrusted(arguments)
    return isinstance(safe_arguments, dict) and any(
        trace.tool_name == tool_name and trace.input_summary == safe_arguments
        for trace in state.tool_trace
    )


def _complete_web_evidence(result: ToolResult) -> bool:
    data = result.data
    return bool(
        result.ok
        and data.get("award_name_match") is True
        and data.get("year_match") is True
        and data.get("coverage_complete") is True
        and data.get("source_level") in {
            "official_primary",
            "official_secondary",
            "institutional_secondary",
            "publisher_secondary",
        }
    )


def _document_id(url: str) -> str:
    try:
        name = unquote(urlsplit(url).path).rstrip("/").rsplit("/", 1)[-1]
    except ValueError:
        return ""
    normalized = name.casefold().strip()
    return normalized if len(normalized) >= 8 else ""


def _candidate_priority(
    state: AuditCaseState,
    candidate: EvidenceCandidate,
) -> tuple[int, int, int]:
    known_document_ids = {
        document_id
        for url in state.known_urls
        if (document_id := _document_id(url))
    }
    exact_document = _document_id(candidate.url) in known_document_ids
    source_order = {
        "official_primary": 0,
        "official_secondary": 1,
        "institutional_secondary": 2,
        "publisher_secondary": 3,
        "media_or_aggregator": 4,
        "unknown": 5,
    }
    return (
        0 if exact_document else 1,
        source_order.get(candidate.source_level, 5),
        candidate.rank or 100,
    )


def _queue_search_candidates(state: AuditCaseState, result: ToolResult) -> None:
    raw = result.data.get("candidates", [])
    if (
        not result.ok
        or not isinstance(raw, list)
        or state.evidence_progress.search_round >= 1
    ):
        return
    known = {item.url for item in state.evidence_progress.candidates}
    additions: list[EvidenceCandidate] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url", ""))
        title = str(item.get("title", ""))[:300]
        if not url.startswith(("http://", "https://")) or url in known:
            continue
        exact_document_recovery = bool(
            _document_id(url)
            and _document_id(url) in {
                _document_id(known_url) for known_url in state.known_urls
            }
        )
        relevance, score, reason = (
            ("relevant", 100, "")
            if exact_document_recovery
            else _candidate_title_gate(state, title, url)
        )
        if relevance == "excluded":
            additions.append(EvidenceCandidate(
                url=url, source_level=str(item.get("source_level", "unknown"))[:80],
                provider=str(item.get("provider", result.data.get("provider", "")))[:40],
                rank=int(item.get("rank", 0) or 0), title=title,
                query=str(item.get("query", result.data.get("query", "")))[:300],
                status="skipped", status_reason=reason,
                relevance="excluded", relevance_score=score,
            ))
            known.add(url)
            continue
        if (
            len(state.evidence_progress.pending_urls())
            + sum(candidate.status == "pending" for candidate in additions)
            >= min(3, state.budget.limits.max_candidate_urls)
        ):
            break
        additions.append(EvidenceCandidate(
            url=url,
            source_level=str(item.get("source_level", "unknown"))[:80],
            provider=str(item.get("provider", result.data.get("provider", "")))[:40],
            rank=int(item.get("rank", 0) or 0),
            title=title,
            query=str(item.get("query", result.data.get("query", "")))[:300],
        ))
        known.add(url)
    additions.sort(key=lambda item: _candidate_priority(state, item))
    if result.data.get("strategy") == "discrepancy" and additions:
        completed = [
            item for item in state.evidence_progress.candidates if item.status != "pending"
        ]
        pending = [
            item for item in state.evidence_progress.candidates if item.status == "pending"
        ]
        state.evidence_progress.candidates = [*completed, *additions, *pending]
    else:
        state.evidence_progress.candidates.extend(additions)
    state.evidence_progress.search_round += 1
    state.evidence_progress.phase = (
        "candidate_recovery" if state.evidence_progress.pending_urls() else "candidate_search"
    )


def _candidate_title_gate(
    state: AuditCaseState, title: str, url: str
) -> tuple[Literal["relevant", "excluded"], int, str]:
    """Hard-filter source drift before a candidate enters the fetch queue."""

    text = f"{title} {url}".casefold()
    negative_terms = (
        "试题", "报名", "承办", "邀请函", "会议通知", "赛程", "征集",
        "招生", "培训", "申报指南",
    )
    if any(term in text for term in negative_terms):
        return "excluded", -100, "名单类型负面关键词"
    role_types = {
        str(item.get("role_type", ""))
        for item in state.submitted_summary.get("role_scopes", [])
        if isinstance(item, dict) and item.get("required", True)
    }
    if "team" in role_types and any(
        term in text for term in ("优秀组织奖", "组织工作奖", "团体奖")
    ):
        return "excluded", -80, "组织奖页面不能进入队伍审核范围"
    positive = any(
        term in text for term in ("获奖", "名单", "公示", "结果", "拟获奖", "入选")
    )
    year_ok = not state.year or state.year in text or not re.search(r"(?:19|20)\d{2}", text)
    if not positive or not year_ok:
        return "excluded", -40, "年份或名单类型不匹配"
    return "relevant", 60, ""


def _queue_discovered_pages(state: AuditCaseState, result: ToolResult) -> None:
    raw_urls = result.data.get("candidate_page_urls", [])
    raw_titles = result.data.get("candidate_page_titles", [])
    if not result.ok or not isinstance(raw_urls, list):
        return
    titles = raw_titles if isinstance(raw_titles, list) else []
    known = set(state.known_urls)
    known.update(item.url for item in state.evidence_progress.candidates)
    known.update(
        str(trace.input_summary.get("url", ""))
        for trace in state.tool_trace
        if trace.tool_name == "fetch_web_page"
    )
    source_level = str(result.data.get("source_level", "unknown"))[:80]
    expected_year = state.year.strip()
    expected_hosts = set(_case_source_hosts(state))
    award_terms = [
        term for term in re.split(r"[\s（）()、·:：\-/]+", state.award_name.casefold())
        if len(term) >= 2 and term not in {"项目", "名单", "结果", "公示", "获奖"}
    ]
    ranked: list[
        tuple[int, int, str, str, Literal["relevant", "excluded"]]
    ] = []
    for index, raw_url in enumerate(raw_urls[:20]):
        url = str(raw_url)
        title = str(titles[index])[:300] if index < len(titles) else ""
        haystack = f"{title} {url}".casefold()
        host = urlsplit(url).netloc.casefold().split(":", 1)[0]
        score = 0
        if host and any(host == item or host.endswith(f".{item}") for item in expected_hosts):
            score += 55
        if expected_year and expected_year in haystack:
            score += 25
        years = set(re.findall(r"(?:19|20)\d{2}", haystack))
        if expected_year and years and expected_year not in years:
            score -= 45
        if any(term in haystack for term in award_terms):
            score += 30
        if state.resource_code.casefold() in haystack:
            score += 15
        relevance, gate_score, _reason = _candidate_title_gate(state, title, url)
        score += gate_score
        if score < 25:
            relevance = "excluded"
        ranked.append((score, index, url, title, relevance))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    for score, index, url, title, relevance in ranked:
        if (
            not url.startswith(("http://", "https://"))
            or url in known
            or len(state.evidence_progress.candidates)
            >= state.budget.limits.max_candidate_urls
        ):
            continue
        state.evidence_progress.candidates.append(EvidenceCandidate(
            url=url,
            source_level=source_level,
            provider="page_discovery",
            rank=index + 1,
            title=title,
            query="站内详情页发现",
            status="pending" if relevance == "relevant" else "skipped",
            status_reason=(
                "" if relevance == "relevant"
                else _candidate_title_gate(state, title, url)[2]
            ),
            relevance=relevance,
            relevance_score=max(-100, min(100, score)),
        ))
        known.add(url)
    if state.evidence_progress.pending_urls():
        state.evidence_progress.phase = "candidate_recovery"


def _record_candidate_attempt(
    state: AuditCaseState,
    url: str,
    *,
    ok: bool,
) -> None:
    for candidate in state.evidence_progress.candidates:
        if candidate.url == url:
            candidate.attempts += 1
            candidate.status = "succeeded" if ok else "failed"
            break


def _pending_extract_candidate_url(state: AuditCaseState) -> str:
    extracted = {
        str(trace.input_summary.get("url", ""))
        for trace in state.tool_trace
        if trace.tool_name == "extract_search_document"
    }
    failed = _failed_urls(state)
    for url in state.known_urls:
        if url in failed and url not in extracted:
            return url
    for candidate in state.evidence_progress.candidates:
        if candidate.status == "failed" and candidate.url not in extracted:
            return candidate.url
    return ""


def _result_has_complete_fact(result: ToolResult) -> bool:
    return any(
        fact.is_evidence
        and fact.status == "complete"
        and fact.target_match == "yes"
        and fact.year_match == "yes"
        and fact.coverage_complete is True
        for fact in result.evidence_facts
    )


def _result_has_complete_document_fact(result: ToolResult) -> bool:
    return any(
        fact.is_evidence
        and fact.document_complete is True
        and fact.target_match == "yes"
        and fact.year_match == "yes"
        for fact in result.evidence_facts
    )


def _has_authoritative_complete_document(results: list[ToolResult]) -> bool:
    return any(
        fact.is_evidence
        and fact.document_complete is True
        and fact.target_match == "yes"
        and fact.year_match == "yes"
        and fact.source_level in {"official_primary", "official_secondary"}
        for result in results
        for fact in result.evidence_facts
    )


def _result_has_authoritative_partial_fact(result: ToolResult) -> bool:
    return any(
        fact.is_evidence
        and fact.status == "partial"
        and fact.target_match == "yes"
        and fact.year_match == "yes"
        and fact.source_level in {"official_primary", "official_secondary"}
        and fact.coverage_complete is False
        and (fact.observed_count or 0) > 0
        and (fact.expected_count or 0) > (fact.observed_count or 0)
        for fact in result.evidence_facts
    )


def _has_complete_fact(results: list[ToolResult]) -> bool:
    return any(_result_has_complete_fact(result) for result in results)


def _has_complete_verifier_snapshot(
    state: AuditCaseState,
    results: list[ToolResult],
) -> bool:
    report = deterministic_verify(build_evidence_snapshot(state, results))
    return bool(
        report.target_match == "yes"
        and report.year_match == "yes"
        and report.coverage_complete == "yes"
        and not report.contradictions
    )


def _trace_path_labels(state: AuditCaseState, tool_names: set[str]) -> set[str]:
    return {
        Path(str(trace.input_summary.get("path", ""))).name
        for trace in state.tool_trace
        if trace.tool_name in tool_names and trace.input_summary.get("path")
    }


def _artifact_is_excluded(artifact: EvidenceArtifact) -> bool:
    routes = artifact.metadata.get("routes", [])
    if not isinstance(routes, list) or not routes:
        return False
    statuses = {
        str(route.get("route_status", ""))
        for route in routes if isinstance(route, dict)
    }
    return bool(statuses) and statuses <= {"excluded"}


def _pending_pdf_inspection(state: AuditCaseState) -> str:
    processed = _trace_path_labels(state, {"inspect_pdf", "extract_pdf_text"})
    for artifact in state.artifacts:
        if (
            artifact.kind.lower() == "pdf"
            and not _artifact_is_excluded(artifact)
            and Path(artifact.local_path).name not in processed
        ):
            return artifact.local_path
    return ""


def _pending_pdf_extraction(
    state: AuditCaseState,
    results: list[ToolResult],
) -> tuple[str, list[int], dict[str, Any]] | None:
    if not results:
        return None
    extracted = {
        (Path(str(trace.input_summary.get("path", ""))).name,
         int(trace.input_summary.get("scope_id", 0) or 0))
        for trace in state.tool_trace
        if trace.tool_name == "extract_pdf_text" and trace.input_summary.get("path")
    }
    run_traces = state.tool_trace[-len(results):]
    artifacts = {
        Path(artifact.local_path).name: artifact.local_path
        for artifact in state.artifacts
        if artifact.kind.lower() == "pdf" and not _artifact_is_excluded(artifact)
    }
    for trace, result in reversed(list(zip(run_traces, results, strict=False))):
        if trace.tool_name != "inspect_pdf" or not result.ok:
            continue
        label = Path(str(trace.input_summary.get("path", ""))).name
        raw_pages = result.data.get("digital_pages", [])
        pages = [
            int(page)
            for page in raw_pages
            if isinstance(page, int) and not isinstance(page, bool) and page > 0
        ] if isinstance(raw_pages, list) else []
        if label in artifacts and pages:
            route_scopes = _artifact_scope_candidates(state, local_path=artifacts[label])
            if not route_scopes:
                route_scopes = [{"scope_id": 0}]
            if len(route_scopes) > 1:
                route_key = (label, 0)
                if route_key not in extracted:
                    return artifacts[label], pages, {"scope_id": 0}
                continue
            for scope in route_scopes:
                route_key = (label, int(scope.get("scope_id", 0) or 0))
                if route_key not in extracted:
                    return artifacts[label], pages, scope
    return None


def _has_unresolved_identity_discrepancy(state: AuditCaseState) -> bool:
    return bool(
        _public_discrepancy_terms(state)
        and "identity_relationship_corroborated" not in state.reason_codes
    )


def _complete_source_levels(results: list[ToolResult]) -> set[str]:
    levels = {
        fact.source_level
        for result in results
        for fact in result.evidence_facts
        if fact.is_evidence
        and fact.status == "complete"
        and fact.target_match == "yes"
        and fact.year_match == "yes"
        and fact.coverage_complete is True
    }
    levels.update(
        str(result.data.get("source_level", ""))
        for result in results
        if _complete_web_evidence(result)
    )
    return {level for level in levels if level}


def _needs_official_corroboration(
    state: AuditCaseState,
    results: list[ToolResult],
    registry: ToolRegistry,
) -> bool:
    if (
        state.evidence_progress.search_round > 0
        or state.budget.searches >= state.budget.limits.max_searches
        or registry.get("search_official_award") is None
    ):
        return False
    levels = _complete_source_levels(results)
    return bool(levels) and levels.isdisjoint(
        {"official_primary", "official_secondary"}
    )


def _needs_attachment_recovery(
    state: AuditCaseState,
    results: list[ToolResult],
    registry: ToolRegistry,
) -> bool:
    """Run one attachment-focused round after broad corroboration is exhausted."""

    complete_levels = _complete_source_levels(results)
    return bool(
        complete_levels.isdisjoint(
            {"official_primary", "official_secondary"}
        )
        and _SEARCH_CANDIDATES_READY not in state.reason_codes
        and state.evidence_progress.search_round == 1
        and not state.evidence_progress.pending_urls()
        and _recovery_available(state, registry)
    )


def _recovery_available(state: AuditCaseState, registry: ToolRegistry) -> bool:
    if state.evidence_progress.pending_urls():
        return True
    registered = {spec.name for spec in registry.specs()}
    return (
        "search_official_award" in registered
        and state.evidence_progress.search_round < 1
        and state.budget.searches < state.budget.limits.max_searches
        and state.budget.calls < state.budget.limits.max_calls
    )


def _deterministic_action(
    state: AuditCaseState,
    results: list[ToolResult],
    registry: ToolRegistry,
) -> tuple[NextAction, str] | None:
    """Return the next mechanical evidence action without spending an LLM turn."""

    initial_known_url = (
        _next_unattempted_known_url(state)
        if not _attempted_source_urls(state)
        else ""
    )
    if initial_known_url and registry.get("fetch_web_page") is not None:
        return (
            NextAction(
                action="call_tool",
                tool_name="fetch_web_page",
                arguments={"url": initial_known_url},
                reason_summary="先读取已确认公示页正文，再决定哪些关联资产仍需处理。",
                expected_evidence="公示页正文中的名单章节、角色范围和关联资产清单。",
            ),
            "known_html_processed_before_linked_assets",
        )

    if (
        state.evidence_progress.has_pending_attachments()
        and registry.get("collect_spreadsheet_attachments") is not None
    ):
        return (
            NextAction(
                action="call_tool",
                tool_name="collect_spreadsheet_attachments",
                reason_summary="网页附件尚未核验，按实际文件类型处理。",
                expected_evidence="附件类型、目标名单覆盖范围及逐项差异。",
            ),
            "pending_page_attachments_processed_without_agent_turn",
        )
    pending_pdf_extract = _pending_pdf_extraction(state, results)
    if pending_pdf_extract and registry.get("extract_pdf_text") is not None:
        pdf_path, pages, scope = pending_pdf_extract
        return (
            NextAction(
                action="call_tool",
                tool_name="extract_pdf_text",
                arguments={
                    "path": pdf_path, "pages": pages,
                    "scope_id": int(scope.get("scope_id", 0) or 0),
                    "extract_tables": len(pages) <= 40,
                },
                reason_summary="PDF 已完成页级检查，继续提取可读页面并核对名单。",
                expected_evidence="PDF 奖项、年份、名单覆盖范围和逐项差异。",
            ),
            "pending_pdf_extracted_without_agent_turn",
        )
    pending_pdf_path = _pending_pdf_inspection(state)
    if pending_pdf_path and registry.get("inspect_pdf") is not None:
        return (
            NextAction(
                action="call_tool",
                tool_name="inspect_pdf",
                arguments={"path": pending_pdf_path},
                reason_summary="附件实际类型为 PDF，先检查页数和可提取页面。",
                expected_evidence="PDF 页数、数字文本页和扫描页范围。",
            ),
            "pending_pdf_inspected_without_agent_turn",
        )
    if (
        state.evidence_progress.has_pending_media()
        and registry.get("verify_page_image_roster") is not None
    ):
        return (
            NextAction(
                action="call_tool",
                tool_name="verify_page_image_roster",
                reason_summary="页面名单图片尚未核验，处理目标分组。",
                expected_evidence="目标分组标题及逐名差异。",
            ),
            "pending_page_images_processed_without_agent_turn",
        )
    if _has_authoritative_complete_document(results):
        return (
            NextAction(
                action="manual",
                reason_summary=(
                    "权威名单文档已经完整解析；名单差异属于业务核对结果，"
                    "停止扩展搜索并进入人工复核。"
                ),
            ),
            "authoritative_document_differences_found",
        )
    if (
        _has_complete_verifier_snapshot(state, results)
        and not _has_unresolved_identity_discrepancy(state)
    ):
        if _needs_official_corroboration(state, results, registry):
            return (
                NextAction(
                    action="call_tool",
                    tool_name="search_official_award",
                    reason_summary="名单证据已完整，执行一次有界权威来源补查。",
                    expected_evidence="同奖项同年份的官方或权威交叉来源。",
                ),
                "secondary_evidence_requires_official_corroboration",
            )
        pending_urls = state.evidence_progress.pending_urls()
        candidate_attempted = any(
            candidate.attempts > 0 for candidate in state.evidence_progress.candidates
        )
        if pending_urls and not candidate_attempted:
            return (
                NextAction(
                    action="call_tool",
                    tool_name="fetch_web_page",
                    arguments={"url": pending_urls[0]},
                    reason_summary="完整名单证据后核验首个权威候选来源。",
                    expected_evidence="候选来源的奖项、年份、权威性和名单范围。",
                ),
                "complete_evidence_first_authority_candidate_checked",
            )
        return (
            NextAction(
                action="finish",
                reason_summary="名单事实已完整且有界权威补查已结束，进入 Verifier。",
            ),
            "complete_evidence_sent_to_verifier",
        )
    pending_extract_url = _pending_extract_candidate_url(state)
    if pending_extract_url and registry.get("extract_search_document") is not None:
        return (
            NextAction(
                action="call_tool",
                tool_name="extract_search_document",
                arguments={"url": pending_extract_url},
                reason_summary="来源直连失败，执行有界搜索内容提取。",
                expected_evidence="同奖项同年份的结果章节及名单覆盖。",
            ),
            "failed_candidate_extracted_without_agent_turn",
        )
    pending_known_url = _next_unattempted_known_url(state)
    if pending_known_url and registry.get("fetch_web_page") is not None:
        return (
            NextAction(
                action="call_tool",
                tool_name="fetch_web_page",
                arguments={"url": pending_known_url},
                reason_summary="已知来源与案件身份不符，继续核验下一条已知来源。",
                expected_evidence="同奖项同年份的名单范围及附件。",
            ),
            "next_known_source_processed_without_agent_turn",
        )
    pending_urls = state.evidence_progress.pending_urls()
    if pending_urls and registry.get("fetch_web_page") is not None:
        return (
            NextAction(
                action="call_tool",
                tool_name="fetch_web_page",
                arguments={"url": pending_urls[0]},
                reason_summary="继续访问候选队列中的下一来源。",
                expected_evidence="候选来源的奖项、年份、名单范围和附件。",
            ),
            "pending_candidate_processed_without_agent_turn",
        )
    if _needs_discrepancy_recovery(state, results, registry):
        targeted = len(_public_discrepancy_terms(state)) >= 2
        return (
            NextAction(
                action="call_tool",
                tool_name="search_official_award",
                reason_summary=(
                    "已发现公开来源之间的身份差异，按公开差异词查找对应关系证据。"
                    if targeted
                    else "官网名单与提交名单存在逐项差异，按奖项和年份补查第二来源。"
                ),
                expected_evidence=(
                    "差异身份之间的权威对应关系。"
                    if targeted
                    else "同奖项同年份的另一份官方名单、勘误或补充公示。"
                ),
            ),
            (
                "identity_discrepancy_recovery_started"
                if targeted
                else "coverage_discrepancy_recovery_started"
            ),
        )
    if _needs_known_source_recovery(state, results, registry):
        return (
            NextAction(
                action="call_tool",
                tool_name="search_official_award",
                reason_summary="已知来源均已处理但证据仍不完整，执行一次有界替代来源检索。",
                expected_evidence="同奖项同年份的可访问官方页面或名单附件。",
            ),
            "known_source_incomplete_recovery_started",
        )
    if (
        state.evidence_progress.source_failures > 0
        and state.evidence_progress.search_round > 0
        and _recovery_available(state, registry)
    ):
        return (
            NextAction(
                action="call_tool",
                tool_name="search_official_award",
                reason_summary="首轮来源恢复仍未取得完整证据，扩大公开来源继续检索。",
                expected_evidence="同奖项同年份的可访问结果页或名单附件。",
            ),
            "next_source_recovery_round_started",
        )
    if _needs_attachment_recovery(state, results, registry):
        return (
            NextAction(
                action="call_tool",
                tool_name="search_official_award",
                reason_summary="首轮候选耗尽，继续查找公开名单附件。",
                expected_evidence="同奖项同年份的 xlsx、xls、pdf 或名单附件。",
            ),
            "attachment_search_started_without_agent_turn",
        )
    if (
        state.evidence_progress.search_round >= 1
        and state.evidence_progress.candidates
        and not state.evidence_progress.pending_urls()
    ):
        return (
            NextAction(
                action="manual",
                reason_summary=(
                    "一次有界补充搜索的候选已处理完毕，当前证据仍未闭环，转人工复核。"
                ),
            ),
            "bounded_search_candidates_exhausted",
        )
    return None


class EvidenceHarness:
    def __init__(
        self,
        *,
        repository: CaseRepository,
        registry: ToolRegistry,
        agent_client: AgentClient,
        allowed_roots: list[str | Path],
        limits: HarnessLimits | None = None,
        verifier: EvidenceVerifier | None = None,
        memory_service: CaseMemoryService | None = None,
        auto_approval_policy: AutoApprovalPolicy | None = None,
    ) -> None:
        self.repository = repository
        self.registry = registry
        self.agent_client = agent_client
        self.allowed_roots = allowed_roots
        self.limits = limits or HarnessLimits()
        self.verifier = verifier
        self.memory_service = memory_service
        self.auto_approval_policy = auto_approval_policy or AutoApprovalPolicy()

    @staticmethod
    def _append_reason(state: AuditCaseState, code: str) -> None:
        if code and code not in state.reason_codes:
            state.reason_codes.append(code)

    def _wait_for_human(
        self,
        state: AuditCaseState,
        *,
        reason: str,
        recommendation: str,
        last_error: str = "",
        last_error_detail: str = "",
        verifications: list[VerificationReport] | None = None,
    ) -> HarnessOutcome:
        verification_rows = list(verifications or [])
        if state.latest_verification is None:
            state.latest_verification = deterministic_verify(
                build_evidence_snapshot(state, [])
            ).model_copy(update={
                "recommended_action": "manual",
                "reason_codes": [reason, "deterministic_terminal_verification"],
            })
            verification_rows.append(state.latest_verification)
            self.repository.record_comparison(state, [], state.latest_verification)
        state.status = "waiting_human"
        state.evidence_progress.phase = (
            "fail_closed"
            if reason not in {
                "recommendation_ready",
                "source_authority_unresolved_after_bounded_search",
            }
            else "waiting_human"
        )
        state.confidence = (
            "medium"
            if reason in {
                "recommendation_ready",
                "source_authority_unresolved_after_bounded_search",
            }
            else "low"
        )
        state.recommendation = recommendation[:2000]
        state.last_error = last_error[:500]
        state.last_error_detail = last_error_detail[:200]
        self._append_reason(state, reason)
        self.repository.save(state, verifications=verification_rows or None)
        self.repository.finish_attempt(state, stopped_reason=reason)
        return HarnessOutcome(state=state, stopped_reason=reason)

    def _finish_with_verifier(
        self,
        state: AuditCaseState,
        tool_results: list[ToolResult],
        *,
        recommendation: str,
        force_manual: bool = False,
        force_reason: str = "agent_requested_manual",
        last_error: str = "",
    ) -> HarnessOutcome | None:
        if not force_manual and (
            state.evidence_progress.has_pending_attachments()
            or state.evidence_progress.has_pending_media()
            or bool(_pending_pdf_inspection(state))
            or _pending_pdf_extraction(state, tool_results) is not None
        ):
            if state.evidence_progress.has_pending_attachments():
                state.evidence_progress.phase = "spreadsheet_processing"
            elif state.evidence_progress.has_pending_media():
                state.evidence_progress.phase = "image_processing"
            else:
                state.evidence_progress.phase = "document_processing"
            self._append_reason(state, "verifier_deferred_until_assets_terminal")
            self.repository.save(state)
            return None
        configured_verifier = self.verifier is not None
        verifier = self.verifier or EvidenceVerifier()
        if verifier is not None:
            snapshot = build_evidence_snapshot(state, tool_results)
            try:
                verification = verifier.verify(snapshot)
            except VerifierError as exc:
                if exc.usage is not None:
                    state.verifier_llm_usage.append(exc.usage)
                return self._wait_for_human(
                    state,
                    reason=exc.code.lower(),
                    recommendation="证据验证器不可用或输出无效，转人工核验证据质量。",
                    last_error=exc.code,
                    last_error_detail=exc.safe_detail,
                )
            if verifier.last_usage is not None:
                state.verifier_llm_usage.append(verifier.last_usage)
            if force_manual:
                forced_missing = (
                    []
                    if force_reason in {
                        "identity_relationship_requires_business_confirmation",
                        "authoritative_partial_coverage_requires_supplement",
                        "authoritative_document_differences_found",
                    }
                    else ["Automatic evidence processing stopped before evidence acceptance."]
                )
                verification = verification.model_copy(update={
                    "recommended_action": "manual",
                    "reason_codes": list(dict.fromkeys([
                        *verification.reason_codes,
                        force_reason,
                    ])),
                    "missing_evidence": list(dict.fromkeys([
                        *verification.missing_evidence,
                        *forced_missing,
                    ])),
                })
            elif not configured_verifier:
                verification = verification.model_copy(update={
                    "recommended_action": "manual",
                    "reason_codes": list(dict.fromkeys([
                        *verification.reason_codes,
                        "deterministic_terminal_verification",
                    ])),
                })
            state.latest_verification = verification
            self.repository.record_comparison(state, tool_results, verification)
            for code in verification.reason_codes:
                self._append_reason(state, code)
            if verification.recommended_action == "supplement":
                identity_and_coverage_complete = bool(
                    verification.target_match == "yes"
                    and verification.year_match == "yes"
                    and verification.coverage_complete == "yes"
                    and not verification.contradictions
                )
                if (
                    identity_and_coverage_complete
                    and verification.source_authority == "unknown"
                    and state.evidence_progress.search_round > 0
                ):
                    return self._wait_for_human(
                        state,
                        reason="source_authority_unresolved_after_bounded_search",
                        recommendation=(
                            "名单、奖项和年份证据已完整；一次有界权威来源补查后，"
                            "来源级别仍无法确认，转人工核定来源权威性。"
                        ),
                        verifications=[verification],
                    )
                if state.reflection_count >= 1:
                    return self._wait_for_human(
                        state,
                        reason="reflection_exhausted",
                        recommendation="一次定向补证后证据仍不充分，停止自动循环并转人工。",
                        verifications=[verification],
                    )
                state.reflection_count += 1
                questions = [
                    request.question for request in verification.supplement_requests
                ] or list(verification.missing_evidence)
                for raw_question in questions:
                    question = f"Verifier 补证项：{raw_question}"[:1000]
                    if question not in state.open_questions and len(state.open_questions) < 20:
                        state.open_questions.append(question)
                self.repository.save(state, verifications=[verification])
                return None
            if verification.recommended_action == "manual":
                return self._wait_for_human(
                    state,
                    reason=(
                        force_reason if force_manual
                        else "verifier_requires_manual" if configured_verifier
                        else "recommendation_ready"
                    ),
                    recommendation=(
                        recommendation
                        if force_manual
                        else "证据存在目标、年份、覆盖或冲突风险，需人工判定。"
                    ),
                    last_error=last_error,
                    verifications=[verification],
                )
            if decide_review_route(verification, self.auto_approval_policy) == "auto_approve":
                state.status = "completed"
                state.evidence_progress.phase = "auto_approved"
                state.confidence = "high"
                state.recommendation = recommendation[:2000]
                self._append_reason(state, "strict_auto_approval_policy_satisfied")
                self.repository.save(state, verifications=[verification])
                self.repository.finish_attempt(
                    state, stopped_reason="auto_approved"
                )
                return HarnessOutcome(state=state, stopped_reason="auto_approved")
        state.status = "waiting_human"
        state.evidence_progress.phase = "waiting_human"
        state.confidence = "medium"
        state.recommendation = recommendation[:2000]
        self._append_reason(state, "agent_recommendation_ready")
        self.repository.save(
            state,
            verifications=(
                [state.latest_verification]
                if state.latest_verification is not None else None
            ),
        )
        self.repository.finish_attempt(state, stopped_reason="recommendation_ready")
        return HarnessOutcome(state=state, stopped_reason="recommendation_ready")

    def _finish_safety_stop(
        self,
        state: AuditCaseState,
        tool_results: list[ToolResult],
        *,
        reason: str,
        recommendation: str,
        last_error: str = "",
    ) -> HarnessOutcome:
        self._append_reason(state, reason)
        if self.verifier is not None or tool_results:
            finished = self._finish_with_verifier(
                state,
                tool_results,
                recommendation=recommendation,
                force_manual=True,
                force_reason=reason,
                last_error=last_error,
            )
            if finished is not None:
                return finished
        return self._wait_for_human(
            state,
            reason=reason,
            recommendation=recommendation,
            last_error=last_error,
        )

    def _stop_reason(self, state: AuditCaseState) -> str:
        if state.step_count >= self.limits.max_steps:
            return "agent_step_budget_exhausted"
        if state.token_used >= self.limits.max_tokens:
            return "agent_token_budget_exhausted"
        if state.elapsed_ms >= round(state.budget.limits.wall_time_seconds * 1000):
            return "wall_time_budget_exhausted"
        if (
            state.budget.calls - state.budget.asset_calls
            >= state.budget.limits.max_calls
        ):
            return "tool_call_budget_exhausted"
        return ""

    def run(self, case_id: int) -> HarnessOutcome:
        state = self.repository.load(case_id)
        if state.status == "completed":
            return HarnessOutcome(state=state, stopped_reason="already_completed")
        if state.status == "waiting_human" and not state.pending_supplement:
            return HarnessOutcome(state=state, stopped_reason="awaiting_human_action")
        supplement_request = state.pending_supplement
        self.repository.start_attempt(
            state,
            kind="supplement" if supplement_request else "initial",
            supplement_request=supplement_request,
        )
        if supplement_request:
            state.budget = ToolBudgetState(limits=state.budget.limits.model_copy(deep=True))
            state.step_count = 0
            state.token_used = 0
            state.elapsed_ms = 0
            state.llm_usage = []
            state.verifier_llm_usage = []
            state.reflection_count = 0
            state.latest_verification = None
            state.tool_trace = []
            state.evidence_progress = state.evidence_progress.model_copy(update={
                "phase": "initial",
                "candidates": [],
                "search_round": 0,
                "source_failures": 0,
                "successful_sources": 0,
                "pending_attachment_page_urls": [],
                "pending_attachment_urls": [],
                "pending_attachment_parent_urls": {},
                "failed_attachment_urls": [],
                "pending_media_source_url": "",
                "pending_media_page_title": "",
                "pending_media_urls": [],
                "pending_media_parent_urls": {},
                "media_expected_items": {},
                "media_matched_identity_hashes": [],
                "media_extra_items": [],
                "media_failed_urls": [],
                "media_scope_accumulators": {},
            })
        if supplement_request:
            question = f"人工补证要求：{supplement_request}"
            if question not in state.open_questions:
                state.open_questions.append(question)
            state.pending_supplement = ""
        if self.memory_service is not None:
            memories = self.memory_service.retrieve_for_case(state)
            state.retrieved_memories = [item.model_dump(mode="json") for item in memories]

        state.status = "running"
        _restore_excluded_asset_routes(state, self.repository.store)
        _hydrate_m4_evidence_progress(state, allowed_roots=self.allowed_roots)
        _calibrate_media_wall_time(state)
        context = ToolExecutionContext.create(
            self.allowed_roots, limits=state.budget.limits
        )
        context.budget = state.budget.model_copy(deep=True)
        executor = SafeToolExecutor(self.registry)
        if state.evidence_progress.phase in {"initial", "waiting_human", "fail_closed"}:
            state.evidence_progress.phase = (
                "known_source" if state.known_urls else "candidate_search"
            )
        state.last_error = ""
        state.last_error_detail = ""
        self.repository.save(state)

        started = time.monotonic()

        def account_elapsed() -> None:
            nonlocal started
            state.elapsed_ms += max(0, round((time.monotonic() - started) * 1000))
            started = time.monotonic()

        observations: list[dict[str, Any]] = []
        tool_results: list[ToolResult] = []
        reused_spreadsheets = _reuse_local_spreadsheet_evidence(
            state, allowed_roots=self.allowed_roots
        )
        if reused_spreadsheets is not None:
            tool_results.append(reused_spreadsheets)
            observations.append(_bounded_tool_observation(
                "reuse_local_spreadsheet_evidence",
                reused_spreadsheets,
                max_chars=max(1000, self.limits.max_observation_chars // 4),
            ))
            self.repository.save(state)
        consecutive_failures = 0
        while True:
            account_elapsed()
            stop = self._stop_reason(state)
            if stop:
                recommendation = "已达到案件执行预算，需人工决定是否补充预算或终止取证。"
                if stop == "tool_call_budget_exhausted":
                    return self._finish_safety_stop(
                        state,
                        tool_results,
                        reason=stop,
                        recommendation=recommendation,
                    )
                return self._wait_for_human(
                    state, reason=stop, recommendation=recommendation
                )
            forced_discrepancy_search = False
            deterministic = _deterministic_action(
                state,
                tool_results,
                self.registry,
            )
            decision_from_agent = deterministic is None
            if deterministic is not None:
                action, reason_code = deterministic
                decision = AgentDecision(action=action, route="fake")
                self._append_reason(state, reason_code)
                forced_discrepancy_search = reason_code in {
                    "coverage_discrepancy_recovery_started",
                    "identity_discrepancy_recovery_started",
                }
                if (
                    reason_code == "known_source_incomplete_recovery_started"
                    and state.evidence_progress.source_failures > 0
                ):
                    self._append_reason(
                        state, "repeated_failed_url_redirected_to_search"
                    )
            else:
                try:
                    turn_context = _turn_context(
                        state,
                        observations,
                        max_observation_chars=self.limits.max_observation_chars,
                    )
                    estimated_input_tokens = len(
                        json.dumps(turn_context.model_dump(mode="json"), ensure_ascii=False)
                    ) // 4
                    if estimated_input_tokens > self.limits.max_agent_input_tokens:
                        turn_context = turn_context.model_copy(update={
                            "observations": turn_context.observations[-2:],
                            "skill_instructions": "",
                        })
                    decision = self.agent_client.next_action(
                        turn_context,
                        _tool_schemas_for_state(self.registry, state),
                    )
                except AgentClientError as exc:
                    account_elapsed()
                    for usage in exc.usages:
                        state.token_used += usage.total_tokens
                        state.llm_usage.append(
                            usage.model_copy(update={"step": state.step_count + 1})
                        )
                    return self._wait_for_human(
                        state,
                        reason=exc.code.lower(),
                        recommendation=(
                            "Agent 客户端不可用或输出无效，转人工检查配置和当前证据。"
                        ),
                        last_error=exc.code,
                        last_error_detail=exc.safe_detail,
                    )
                account_elapsed()

            pending_extract_url = _pending_extract_candidate_url(state)
            if (
                state.evidence_progress.has_pending_attachments()
                and decision.action.action == "call_tool"
                and self.registry.get("collect_spreadsheet_attachments") is not None
                and (
                    decision.action.action != "call_tool"
                    or decision.action.tool_name not in {
                        "collect_spreadsheet_attachments", "fetch_web_page",
                    }
                )
            ):
                decision = decision.model_copy(update={
                    "action": NextAction(
                        action="call_tool",
                        tool_name="collect_spreadsheet_attachments",
                        reason_summary="网页附件尚未核验，先下载并按实际文件类型处理。",
                        expected_evidence="附件类型、目标名单覆盖范围及逐项差异。",
                    )
                })
                self._append_reason(
                    state,
                    "pending_page_attachments_processed_before_recovery",
                )
            elif (
                pending_extract_url
                and decision.action.action == "call_tool"
                and self.registry.get("extract_search_document") is not None
                and (
                    decision.action.action != "call_tool"
                    or decision.action.tool_name != "extract_search_document"
                    or decision.action.arguments.get("url") != pending_extract_url
                )
            ):
                decision = decision.model_copy(update={
                    "action": NextAction(
                        action="call_tool",
                        tool_name="extract_search_document",
                        arguments={"url": pending_extract_url},
                        reason_summary=(
                            "搜索候选直连失败，先对该候选执行一次有界内容提取。"
                        ),
                        expected_evidence="同奖项同年份的结果章节及名单覆盖。",
                    )
                })
                self._append_reason(
                    state,
                    "failed_candidate_extracted_before_recovery",
                )
            elif (
                state.evidence_progress.has_pending_media()
                and decision.action.action == "call_tool"
                and self.registry.get("verify_page_image_roster") is not None
                and (
                    decision.action.action != "call_tool"
                    or decision.action.tool_name not in {
                        "verify_page_image_roster", "fetch_web_page",
                    }
                )
            ):
                decision = decision.model_copy(update={
                    "action": NextAction(
                        action="call_tool",
                        tool_name="verify_page_image_roster",
                        reason_summary="页面名单图片尚未核验，先完成目标分组识别。",
                        expected_evidence="目标分组标题及逐名差异。",
                    )
                })
                self._append_reason(state, "pending_page_images_processed_before_recovery")
            elif (
                _needs_discrepancy_recovery(state, tool_results, self.registry)
                and decision.action.action == "call_tool"
            ):
                targeted = len(_public_discrepancy_terms(state)) >= 2
                decision = decision.model_copy(update={
                    "action": NextAction(
                        action="call_tool",
                        tool_name="search_official_award",
                        reason_summary=(
                            "已发现公开来源之间的身份差异，按公开差异词继续查找对应关系证据。"
                            if targeted
                            else "官网名单与提交名单存在逐项差异，按奖项和年份补查第二来源。"
                        ),
                        expected_evidence=(
                            "差异身份之间的权威对应关系。"
                            if targeted
                            else "同奖项同年份的另一份官方名单、勘误或补充公示。"
                        ),
                    )
                })
                forced_discrepancy_search = True
                self._append_reason(
                    state,
                    (
                        "identity_discrepancy_recovery_started"
                        if targeted
                        else "coverage_discrepancy_recovery_started"
                    ),
                )
            elif (
                state.evidence_progress.phase == "candidate_recovery"
                and state.evidence_progress.pending_urls()
                and decision.action.action == "call_tool"
                and decision.action.tool_name == "search_official_award"
                and self.registry.get("fetch_web_page") is not None
            ):
                next_url = state.evidence_progress.pending_urls()[0]
                decision = decision.model_copy(update={
                    "action": NextAction(
                        action="call_tool",
                        tool_name="fetch_web_page",
                        arguments={"url": next_url},
                        reason_summary="当前搜索候选尚未核验，继续访问队首来源。",
                        expected_evidence="候选来源的奖项、年份、名单范围和附件。",
                    )
                })
                self._append_reason(state, "pending_candidate_fetched_before_finish")
            elif (
                decision.action.action == "call_tool"
                and _needs_attachment_recovery(state, tool_results, self.registry)
            ):
                decision = decision.model_copy(update={
                    "action": NextAction(
                        action="call_tool",
                        tool_name="search_official_award",
                        reason_summary=(
                            "首轮宽泛搜索未形成权威交叉证据，继续查找公开名单附件。"
                        ),
                        expected_evidence="同奖项同年份的 xlsx、xls、pdf 或名单附件。",
                    )
                })
                self._append_reason(state, "attachment_search_started_after_broad_search")
            elif (
                decision.action.action == "call_tool"
                and _needs_official_corroboration(state, tool_results, self.registry)
            ):
                decision = decision.model_copy(update={
                    "action": NextAction(
                        action="call_tool",
                        tool_name="search_official_award",
                        reason_summary="完整证据仅来自非官方来源，继续进行一次官方交叉核验。",
                        expected_evidence="同奖项同年份的官方名单或明确口径差异。",
                    )
                })
                self._append_reason(
                    state, "secondary_evidence_requires_official_corroboration"
                )

            if decision_from_agent:
                state.step_count += 1
            if decision_from_agent:
                state.token_used += decision.token_used
                state.llm_usage.append(
                    decision.usage.model_copy(update={"step": state.step_count})
                )
            state.last_action = decision.action
            for warning in decision.warnings:
                self._append_reason(state, warning)
            if state.token_used > self.limits.max_tokens:
                return self._wait_for_human(
                    state,
                    reason="agent_token_budget_exhausted",
                    recommendation="本轮调用后 Token 预算超限，未执行其动作并转人工。",
                )

            if decision.action.action == "manual":
                recommendation = (
                    decision.action.reason_summary or "Agent 判定当前证据不足，需人工处理。"
                )
                if self.verifier is not None and tool_results:
                    finished = self._finish_with_verifier(
                        state,
                        tool_results,
                        recommendation=recommendation,
                        force_manual=True,
                        force_reason=(
                            "agent_requested_manual"
                            if decision_from_agent
                            else reason_code
                        ),
                    )
                    if finished is not None:
                        return finished
                return self._wait_for_human(
                    state,
                    reason=(
                        "agent_requested_manual" if decision_from_agent else reason_code
                    ),
                    recommendation=recommendation,
                )
            if decision.action.action == "finish":
                state.evidence_progress.phase = "verifying"
                already_deferred = (
                    "verifier_deferred_until_assets_terminal" in state.reason_codes
                )
                finished = self._finish_with_verifier(
                    state,
                    tool_results,
                    recommendation=(
                        decision.action.reason_summary or "取证步骤完成，等待人工复核。"
                    ),
                )
                if finished is not None:
                    return finished
                if already_deferred:
                    return self._wait_for_human(
                        state,
                        reason="pending_assets_not_actionable",
                        recommendation=(
                            "案件仍有未终态资产，但当前状态机没有可执行的资产动作；"
                            "停止空转并保留明确阻塞项。"
                        ),
                    )
                continue

            if (
                decision.action.tool_name == "search_official_award"
                and state.evidence_progress.search_round >= 1
            ):
                self._append_reason(state, "bounded_search_limit_reached")
                finished = self._finish_with_verifier(
                    state,
                    tool_results,
                    recommendation=(
                        "当前业务范围已完成一次有界补充搜索，停止继续扩展候选来源并转人工复核。"
                    ),
                    force_manual=True,
                    force_reason="bounded_search_limit_reached",
                )
                if finished is not None:
                    return finished
                return self._wait_for_human(
                    state,
                    reason="bounded_search_limit_reached",
                    recommendation="当前业务范围已完成一次有界补充搜索。",
                )

            if (
                decision.action.tool_name == "search_official_award"
                and state.evidence_progress.pending_urls()
                and not forced_discrepancy_search
            ):
                self._append_reason(state, "candidate_queue_must_be_processed_before_research")
                self.repository.save(state)
                continue
            if (
                decision.action.tool_name == "search_official_award"
                and not forced_discrepancy_search
                and (
                    "authority_search_completed" in state.reason_codes
                    or (
                        _SEARCH_CANDIDATES_READY in state.reason_codes
                        and state.evidence_progress.source_failures == 0
                    )
                )
            ):
                finished = self._finish_with_verifier(
                    state,
                    tool_results,
                    recommendation="已有搜索结果，停止重复搜索并核验当前有界证据。",
                )
                if finished is not None:
                    return finished
                continue

            trace_start = len(context.trace)
            execution_tool_name = decision.action.tool_name
            execution_arguments = decision.action.arguments
            if execution_tool_name == "fetch_web_page":
                execution_arguments = _fetch_arguments(state, execution_arguments)
                state.last_action = decision.action.model_copy(
                    update={"arguments": execution_arguments}
                )
            elif execution_tool_name == "search_official_award":
                execution_arguments = _search_arguments(state, execution_arguments)
                state.last_action = decision.action.model_copy(
                    update={"arguments": execution_arguments}
                )
            elif execution_tool_name == "collect_spreadsheet_attachments":
                execution_arguments = _collect_arguments(state, execution_arguments)
                state.last_action = decision.action.model_copy(
                    update={"arguments": execution_arguments}
                )
            elif execution_tool_name == "verify_page_image_roster":
                execution_arguments = _image_roster_arguments(state, execution_arguments)
                state.last_action = decision.action.model_copy(
                    update={"arguments": execution_arguments}
                )
            elif execution_tool_name == "extract_search_document":
                execution_arguments = _extract_arguments(state, execution_arguments)
                state.last_action = decision.action.model_copy(
                    update={"arguments": execution_arguments}
                )
            elif execution_tool_name == "extract_pdf_text":
                execution_arguments = _pdf_extract_arguments(state, execution_arguments)
                state.last_action = decision.action.model_copy(
                    update={"arguments": execution_arguments}
                )
            action_url = str(execution_arguments.get("url", ""))
            repeated_failed_url = bool(
                execution_tool_name in {"fetch_web_page", "download_evidence"}
                and action_url
                and action_url in _failed_urls(state)
            )
            repeated_successful_fetch = bool(
                execution_tool_name == "fetch_web_page"
                and action_url
                and action_url in _attempted_urls(state)
                and action_url not in _failed_urls(state)
            )
            repeated_tool_call = _tool_call_already_traced(
                state,
                execution_tool_name,
                execution_arguments,
            ) or repeated_successful_fetch
            if (
                (repeated_failed_url or repeated_tool_call)
                and self.registry.get("fetch_web_page") is not None
            ):
                for candidate_url in state.evidence_progress.pending_urls():
                    redirected = _fetch_arguments(state, {"url": candidate_url})
                    if not _tool_call_already_traced(
                        state,
                        "fetch_web_page",
                        redirected,
                    ):
                        execution_tool_name = "fetch_web_page"
                        execution_arguments = redirected
                        action_url = candidate_url
                        state.last_action = decision.action.model_copy(
                            update={
                                "tool_name": execution_tool_name,
                                "arguments": execution_arguments,
                            }
                        )
                        repeated_failed_url = False
                        repeated_tool_call = False
                        self._append_reason(
                            state,
                            "repeated_tool_call_redirected_to_candidate",
                        )
                        break
            if (
                repeated_failed_url
                and not state.evidence_progress.pending_urls()
                and _recovery_available(state, self.registry)
            ):
                execution_tool_name = "search_official_award"
                execution_arguments = _search_arguments(state, {})
                action_url = ""
                state.last_action = decision.action.model_copy(
                    update={
                        "tool_name": execution_tool_name,
                        "arguments": execution_arguments,
                    }
                )
                repeated_failed_url = False
                repeated_tool_call = False
                self._append_reason(state, "repeated_failed_url_redirected_to_search")
            if repeated_failed_url:
                self._append_reason(state, "repeated_failed_url_blocked")
                if self.verifier is not None and tool_results:
                    finished = self._finish_with_verifier(
                        state,
                        tool_results,
                        recommendation=(
                            "候选 URL 已在本案失败，停止重复请求并转人工补充有效来源。"
                        ),
                        force_manual=True,
                    )
                    if finished is not None:
                        return finished
                return self._wait_for_human(
                    state,
                    reason="repeated_failed_url_blocked",
                    recommendation="候选 URL 已在本案失败，停止重复请求并转人工补充有效来源。",
                )
            if repeated_tool_call and _needs_attachment_recovery(
                state,
                tool_results,
                self.registry,
            ):
                execution_tool_name = "search_official_award"
                execution_arguments = _search_arguments(state, {})
                action_url = ""
                state.last_action = decision.action.model_copy(update={
                    "tool_name": execution_tool_name,
                    "arguments": execution_arguments,
                })
                repeated_tool_call = False
                self._append_reason(
                    state,
                    "repeated_tool_call_redirected_to_attachment_search",
                )
            if repeated_tool_call:
                self._append_reason(state, "repeated_tool_call_blocked")
                complete_evidence = _has_complete_verifier_snapshot(
                    state, tool_results
                )
                bounded_search_has_complete_fact = bool(
                    state.evidence_progress.search_round > 0
                    and _has_complete_fact(tool_results)
                )
                if self.verifier is not None and (
                    complete_evidence or bounded_search_has_complete_fact
                ):
                    finished = self._finish_with_verifier(
                        state,
                        tool_results,
                        recommendation=(
                            "完全相同的 Tool 调用已执行；停止重复取证，"
                            "按当前完整证据进入人工复核。"
                        ),
                    )
                    if finished is not None:
                        return finished
                return self._finish_safety_stop(
                    state,
                    tool_results,
                    reason="repeated_tool_call_blocked",
                    recommendation="完全相同的 Tool 调用已执行，停止重复取证并核验当前证据。",
                )
            result = executor.execute(
                execution_tool_name,
                execution_arguments,
                context,
            )
            tool_results.append(result)
            account_elapsed()
            new_traces = context.trace[trace_start:]
            state.budget = context.budget.model_copy(deep=True)
            state.tool_trace.extend(new_traces)
            if execution_tool_name == "search_official_award":
                _queue_search_candidates(state, result)
            elif execution_tool_name == "fetch_web_page" and result.ok:
                _route_web_result_to_scopes(state, result)
                if action_url:
                    _record_candidate_attempt(state, action_url, ok=True)
                identity_mismatch = _result_has_identity_mismatch(result)
                if not identity_mismatch:
                    _queue_discovered_pages(state, result)
                attachment_urls = result.data.get("candidate_attachment_urls", [])
                if (
                    _asset_followup_allowed(result)
                    and not _complete_web_evidence(result)
                    and not result.data.get("all_required_scopes_complete")
                    and
                    result.data.get("next_evidence_stage") == "spreadsheet_processing"
                    and isinstance(attachment_urls, list)
                ):
                    bounded_attachments = _filter_unacquired_asset_urls(
                        state,
                        (str(item) for item in attachment_urls if isinstance(item, str)),
                    )[:image_tools.MAX_PAGE_IMAGES]
                    source_page = result.source_url or action_url
                    if source_page and bounded_attachments:
                        state.evidence_progress.pending_attachment_page_urls = [
                            source_page
                        ]
                        state.evidence_progress.pending_attachment_urls = (
                            bounded_attachments
                        )
                        state.evidence_progress.phase = "spreadsheet_processing"
                image_urls = result.data.get("candidate_image_urls", [])
                if (
                    _asset_followup_allowed(result)
                    and not _complete_web_evidence(result)
                    and
                    result.data.get("next_evidence_stage") == "image_processing"
                    and isinstance(image_urls, list)
                    and not _has_complete_fact(tool_results[:-1])
                ):
                    bounded_urls = _filter_unacquired_asset_urls(
                        state,
                        (str(item) for item in image_urls if isinstance(item, str)),
                    )[:image_tools.MAX_PAGE_IMAGES]
                    if bounded_urls:
                        state.evidence_progress.pending_media_source_url = (
                            result.source_url or action_url
                        )
                        state.evidence_progress.pending_media_page_title = str(
                            result.data.get("title", "")
                        )[:500]
                        state.evidence_progress.pending_media_urls = bounded_urls
                        state.evidence_progress.phase = "image_processing"
            elif execution_tool_name == "collect_spreadsheet_attachments":
                attempted_attachments = execution_arguments.get("attachment_urls", [])
                _update_attachment_queue_after_collection(
                    state,
                    result,
                    attempted_attachments
                    if isinstance(attempted_attachments, list) else [],
                )
                _route_spreadsheet_result_to_scopes(state, result)
            elif execution_tool_name == "verify_page_image_roster":
                processed_media = execution_arguments.get("image_urls", [])
                _update_media_queue_after_verification(
                    state,
                    result,
                    processed_media if isinstance(processed_media, list) else [],
                )
                _route_image_result_to_scopes(
                    state,
                    result,
                    processed_media if isinstance(processed_media, list) else [],
                )
            elif execution_tool_name == "extract_pdf_text":
                _route_pdf_result_to_scopes(
                    state,
                    result,
                    local_path=str(execution_arguments.get("path", "")),
                )
            elif action_url:
                _record_candidate_attempt(state, action_url, ok=result.ok)
            source_operation = execution_tool_name in {
                "fetch_web_page",
                "download_evidence",
                "search_official_award",
                "extract_search_document",
            }
            if source_operation and result.ok:
                state.evidence_progress.successful_sources += 1
            elif source_operation:
                state.evidence_progress.source_failures += 1
            result_complete = (
                _result_has_complete_fact(result)
                or _result_has_complete_document_fact(result)
                or _complete_web_evidence(result)
            )
            snapshot_complete = _has_complete_verifier_snapshot(state, tool_results)
            if result_complete or snapshot_complete:
                state.evidence_progress.phase = "evidence_ready"
            elif state.evidence_progress.has_pending_attachments():
                state.evidence_progress.phase = "spreadsheet_processing"
            elif state.evidence_progress.has_pending_media():
                state.evidence_progress.phase = "image_processing"
            elif result.artifacts:
                state.evidence_progress.phase = "document_processing"
            elif state.evidence_progress.pending_urls():
                state.evidence_progress.phase = "candidate_recovery"
            existing_artifacts = {
                (item.sha256, item.local_path): index
                for index, item in enumerate(state.artifacts)
            }
            new_artifacts: list[EvidenceArtifact] = []
            for raw_artifact in result.artifacts:
                identity_records = result.data.get("identity_records", [])
                extracted_count = sum(
                    isinstance(record, dict)
                    and str(record.get("source_url", "")) == raw_artifact.source_url
                    for record in identity_records
                ) if isinstance(identity_records, list) else 0
                page_summaries = result.data.get("image_page_summaries", [])
                page_summary = next((
                    summary for summary in page_summaries
                    if isinstance(summary, dict)
                    and str(summary.get("source_url", "")) == raw_artifact.source_url
                ), None) if isinstance(page_summaries, list) else None
                if extracted_count or page_summary is not None:
                    raw_artifact = raw_artifact.model_copy(update={
                        "metadata": {
                            **raw_artifact.metadata,
                            "extracted_count": extracted_count,
                            "page_summary": page_summary or {},
                        },
                    })
                attachment_manifest = result.data.get("attachment_manifest", [])
                asset_manifest = next((
                    item for item in attachment_manifest
                    if isinstance(item, dict)
                    and str(item.get("url", "")) == raw_artifact.source_url
                ), None) if isinstance(attachment_manifest, list) else None
                if asset_manifest is not None:
                    spreadsheet_routes: list[dict[str, Any]] = []
                    semantic_routes = result.data.get("spreadsheet_scope_routes", {})
                    if isinstance(semantic_routes, dict):
                        raw_semantic_routes = semantic_routes.get(raw_artifact.source_url, [])
                        if isinstance(raw_semantic_routes, list):
                            spreadsheet_routes.extend(
                                dict(route) for route in raw_semantic_routes
                                if isinstance(route, dict)
                            )
                    scope_id = int(result.data.get("scope_id", 0) or 0)
                    sheets = asset_manifest.get("sheets", [])
                    if (
                        not spreadsheet_routes
                        and asset_manifest.get("selected") is True
                        and scope_id
                    ):
                        for sheet in sheets if isinstance(sheets, list) else []:
                            if not isinstance(sheet, dict):
                                continue
                            spreadsheet_routes.append({
                                "scope_id": scope_id,
                                "subunit_type": "sheet",
                                "selector": {
                                    "sheet": str(sheet.get("sheet", "")),
                                    "row_count": int(sheet.get("row_count", 0) or 0),
                                },
                                "route_source": "exact_rule",
                                "confidence": 1.0,
                                "route_status": "routed",
                                "reason": "spreadsheet selected for current role scope",
                            })
                    raw_artifact = raw_artifact.model_copy(update={
                        "metadata": {
                            **raw_artifact.metadata,
                            "extracted_count": int((
                                result.data.get("spreadsheet_asset_identity_counts", {})
                                if isinstance(
                                    result.data.get("spreadsheet_asset_identity_counts", {}),
                                    dict,
                                )
                                else {}
                            ).get(
                                raw_artifact.source_url,
                                asset_manifest.get("matched_identity_count", 0),
                            ) or 0),
                            "sheet_manifest": sheets if isinstance(sheets, list) else [],
                            "spreadsheet_truncated": bool(
                                asset_manifest.get("truncated", False)
                            ),
                            **({"routes": spreadsheet_routes} if spreadsheet_routes else {}),
                        },
                    })
                image_routes = result.data.get("image_scope_routes", {})
                if isinstance(image_routes, dict):
                    raw_routes = image_routes.get(raw_artifact.source_url)
                    if isinstance(raw_routes, list) and raw_routes:
                        raw_artifact = raw_artifact.model_copy(update={
                            "metadata": {
                                **raw_artifact.metadata,
                                "routes": raw_routes,
                            }
                        })
                artifact = _annotate_m5_artifact(state, raw_artifact)
                key = (artifact.sha256, artifact.local_path)
                existing_index = existing_artifacts.get(key)
                if existing_index is None:
                    existing_artifacts[key] = len(state.artifacts)
                    state.artifacts.append(artifact)
                    new_artifacts.append(artifact)
                else:
                    existing = state.artifacts[existing_index]
                    merged_metadata = {**existing.metadata, **artifact.metadata}
                    existing_routes = existing.metadata.get("routes", [])
                    new_routes = artifact.metadata.get("routes", [])
                    if isinstance(existing_routes, list) and isinstance(new_routes, list):
                        merged_routes: list[dict[str, Any]] = []
                        route_keys: set[str] = set()
                        for route in [*existing_routes, *new_routes]:
                            if not isinstance(route, dict):
                                continue
                            route_key = json.dumps(route, ensure_ascii=False, sort_keys=True)
                            if route_key in route_keys:
                                continue
                            route_keys.add(route_key)
                            merged_routes.append(route)
                        if merged_routes:
                            merged_metadata["routes"] = merged_routes
                    state.artifacts[existing_index] = existing.model_copy(update={
                        "metadata": merged_metadata,
                    })
            observations.append(
                _bounded_tool_observation(
                    execution_tool_name,
                    result,
                    max_chars=max(1000, self.limits.max_observation_chars // 4),
                )
            )
            if (
                execution_tool_name == "fetch_web_page"
                and action_url in state.known_urls
                and _complete_web_evidence(result)
            ):
                self._append_reason(state, _PROVIDED_EVIDENCE_COMPLETE)
            if (
                execution_tool_name == "search_official_award"
                and result.ok
                and int(result.data.get("official_candidate_count", 0) or 0) > 0
            ):
                self._append_reason(state, _SEARCH_CANDIDATES_READY)
            consecutive_failures = 0 if result.ok else consecutive_failures + 1
            state.last_error = result.error_code if not result.ok else ""
            self.repository.save(state, traces=new_traces, artifacts=new_artifacts)

            if (
                execution_tool_name == "extract_search_document"
                and _result_has_authoritative_partial_fact(result)
            ):
                state.evidence_progress.phase = "evidence_ready"
                self._append_reason(
                    state,
                    "authoritative_partial_coverage_requires_supplement",
                )
                self.repository.save(state)
                finished = self._finish_with_verifier(
                    state,
                    tool_results,
                    recommendation=(
                        "已确认同奖项同年份的权威来源，但当前可提取正文未覆盖全部提交记录；"
                        "停止继续访问低相关候选，转人工补充完整名单或附件。"
                    ),
                    force_manual=True,
                    force_reason="authoritative_partial_coverage_requires_supplement",
                )
                if finished is not None:
                    return finished

            if result.data.get("relationship_confirmed") is True:
                for candidate in state.evidence_progress.candidates:
                    if candidate.status == "pending":
                        candidate.status = "skipped"
                        candidate.status_reason = "对应关系补证已取得，无需继续访问"
                state.evidence_progress.phase = "evidence_ready"
                self._append_reason(state, "identity_relationship_corroborated")
                self.repository.save(state)
                finished = self._finish_with_verifier(
                    state,
                    tool_results,
                    recommendation=(
                        "已找到同时包含差异姓名与群体名称的公开补证来源，"
                        "对应关系已有证据支持，仍保留人工确认业务口径。"
                    ),
                    force_manual=True,
                    force_reason="identity_relationship_requires_business_confirmation",
                )
                if finished is not None:
                    return finished

            if (
                execution_tool_name == "fetch_web_page"
                and action_url not in state.known_urls
                and _has_complete_fact(tool_results[:-1])
                and result_complete
                and not any(fact.contradictions for fact in result.evidence_facts)
            ):
                for candidate in state.evidence_progress.candidates:
                    if candidate.status == "pending":
                        candidate.status = "skipped"
                        candidate.status_reason = "已取得完整且无冲突的交叉核验证据"
                state.evidence_progress.pending_media_source_url = ""
                state.evidence_progress.pending_media_page_title = ""
                state.evidence_progress.pending_media_urls = []
                state.evidence_progress.phase = "evidence_ready"
                self.repository.save(state)
                finished = self._finish_with_verifier(
                    state,
                    tool_results,
                    recommendation=(
                        "已用一个官方候选来源交叉核验完整的给定来源，"
                        "保留具体一致项或差异并转人工复核。"
                    ),
                    force_manual=any(
                        fact.contradictions for fact in result.evidence_facts
                    ),
                )
                if finished is not None:
                    return finished

            if (
                execution_tool_name == "search_official_award"
                and not state.evidence_progress.pending_urls()
                and int(result.data.get("official_candidate_count", 0) or 0) == 0
                and not _needs_attachment_recovery(
                    state,
                    tool_results,
                    self.registry,
                )
            ):
                finished = self._finish_with_verifier(
                    state,
                    tool_results,
                    recommendation=(
                        (
                            "未找到合格官方替代来源，保留已完整核实的次级发布者证据，"
                            "等待人工复核。"
                        )
                        if _complete_source_levels(tool_results)
                        else (
                            "宽泛搜索和附件搜索均未形成完整证据，"
                            "保留具体缺失项并等待人工补证。"
                        )
                    ),
                )
                if finished is not None:
                    return finished
            elif (
                result_complete
                and state.evidence_progress.search_round > 0
                and action_url not in state.known_urls
            ):
                finished = self._finish_with_verifier(
                    state,
                    tool_results,
                    recommendation="候选来源已形成完整有界证据，进入 Verifier。",
                )
                if finished is not None:
                    return finished
            if result.error_code == "TOOL_BUDGET_EXCEEDED":
                return self._finish_safety_stop(
                    state,
                    reason="tool_budget_exhausted",
                    recommendation="Tool 预算已耗尽，需人工决定是否继续补证。",
                    tool_results=tool_results,
                )
            if consecutive_failures >= self.limits.max_consecutive_tool_failures:
                if _recovery_available(state, self.registry):
                    self._append_reason(state, "source_recovery_continues_after_failures")
                    state.evidence_progress.phase = "candidate_recovery"
                    consecutive_failures = 0
                    self.repository.save(state)
                    continue
                return self._finish_safety_stop(
                    state,
                    reason="consecutive_tool_failures",
                    recommendation="连续 Tool 调用失败，停止自动循环并转人工。",
                    last_error=result.error_code,
                    tool_results=tool_results,
                )


def build_default_harness(
    store: Store,
    *,
    allowed_roots: list[str | Path],
    registry: ToolRegistry | None = None,
    agent_client: AgentClient | None = None,
    limits: HarnessLimits | None = None,
    verifier: EvidenceVerifier | None = None,
    memory_service: CaseMemoryService | None = None,
    auto_approval_policy: AutoApprovalPolicy | None = None,
) -> EvidenceHarness:
    """Construct the production Harness without instantiating any LLM client."""

    client = agent_client or FallbackAgentClient(
        OpenAINativeAgentClient(), StructuredActionClient()
    )
    return EvidenceHarness(
        repository=CaseRepository(store),
        registry=registry or build_default_registry(),
        agent_client=client,
        allowed_roots=allowed_roots,
        limits=limits,
        verifier=verifier or EvidenceVerifier(StructuredVerifierClient()),
        memory_service=memory_service or CaseMemoryService(store),
        auto_approval_policy=auto_approval_policy,
    )
