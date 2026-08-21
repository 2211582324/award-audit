"""Deterministic M5 bridges for the existing M4 review pipeline."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from pydantic import BaseModel, Field

from award_audit.agent.harness.models import CaseSeed, TriggerCode
from award_audit.agent.harness.persistence import CaseRepository
from award_audit.agent.harness.seeds import seed_from_search_handoff
from award_audit.agent.toolkit import ToolBudgetLimits
from award_audit.agent.toolkit.provenance import normalize_domain
from award_audit.core.identity import (
    IDENTITY_SEPARATOR,
    IDENTITY_VERSION,
    active_role_scope_fields,
    build_role_identity,
    normalize_identity,
)
from award_audit.core.models.record import ImportedFile
from award_audit.core.models.template import RoleProfile, TemplateSpec, resolve_match_profile
from award_audit.core.models.triage import decide_triage
from award_audit.core.pipeline.checks.l5_precheck import SearchHandoff, split_urls
from award_audit.core.pipeline.store import Store
from award_audit.core.reference.ledger import LedgerEntry


class CaseBridgeResult(BaseModel):
    case_ids: list[int]
    created: int = Field(ge=0)
    existing: int = Field(ge=0)
    skipped: int = Field(ge=0)


class LocalIssueSummary(BaseModel):
    rule_id: str = Field(default="", max_length=40)
    severity: str = Field(default="", max_length=20)
    file: str = Field(default="", max_length=300)
    field_code: str = Field(default="", max_length=40)
    message: str = Field(default="", max_length=1000)


class AuditCaseInput(BaseModel):
    """Trusted context derived from imported files and project reference data."""

    resource_code: str
    award_name: str = ""
    year: str = ""
    submission_files: list[str] = Field(default_factory=list, max_length=20)
    table_codes: list[str] = Field(default_factory=list, max_length=10)
    identity_version: str = IDENTITY_VERSION
    identity_primary_alternatives: list[list[str]] = Field(default_factory=list)
    identity_scope_fields: list[str] = Field(default_factory=list, max_length=20)
    identity_discriminator_fields: list[str] = Field(default_factory=list, max_length=20)
    identity_conflict_fields: list[str] = Field(default_factory=list, max_length=20)
    match_profile: str = ""
    match_fields: list[str] = Field(default_factory=list, max_length=20)
    attachment_match_fields: list[str] = Field(default_factory=list, max_length=20)
    match_combine: str = "first"
    submitted_rows: int = Field(default=0, ge=0)
    expected_scope_count: int | None = Field(default=None, ge=0)
    ledger_expected_count: int | None = Field(default=None, ge=0)
    known_urls: list[str] = Field(default_factory=list, max_length=20)
    official_domains: list[str] = Field(default_factory=list, max_length=8)
    official_secondary_domains: list[str] = Field(default_factory=list, max_length=8)
    ledger_resource_name: str = ""
    ledger_source: str = ""
    source_only_items: list[str] = Field(default_factory=list, max_length=100)
    submitted_only_items: list[str] = Field(default_factory=list, max_length=100)
    unresolved_items: list[str] = Field(default_factory=list, max_length=100)
    local_issues: list[LocalIssueSummary] = Field(default_factory=list, max_length=200)
    role_scopes: list[dict[str, Any]] = Field(default_factory=list, max_length=100)
    row_conservation: dict[str, int] = Field(default_factory=dict)
    row_assignments: list[dict[str, Any]] = Field(default_factory=list, max_length=100_000)


def _strings(value: object, *, limit: int = 50) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [str(item)[:500] for item in value[:limit] if str(item).strip()]


def _field_has_value(imported_files: list[ImportedFile], field: str) -> bool:
    return any(
        imported.value(row_index, field).strip()
        for imported in imported_files
        for row_index in range(imported.n_rows)
    )


def _alternative_has_value(
    imported_files: list[ImportedFile], fields: list[str]
) -> bool:
    return any(
        all(imported.value(row_index, field).strip() for field in fields)
        for imported in imported_files
        for row_index in range(imported.n_rows)
    )


def _active_identity_alternatives(
    imported_files: list[ImportedFile],
    alternatives: list[list[str]],
    present_fields: set[str],
) -> list[list[str]]:
    return [
        list(fields)
        for fields in alternatives
        if all(field in present_fields for field in fields)
        and _alternative_has_value(imported_files, list(fields))
    ]


def _effective_role_profiles(
    imported_files: Sequence[ImportedFile],
    role_profiles: Sequence[object],
) -> list[object]:
    """Promote a complete categorical fallback field to a scope dimension.

    Some templates use a nominal subtype (for example ``XMLB``) together with
    a parent category stored in a fallback field (for example ``BZ``).  The
    latter must partition scopes when it is populated for every comparable
    row and has more than one value.  Sparse remarks remain fallback-only.
    """

    effective: list[object] = []
    for role in role_profiles:
        fallback_fields = [
            field for field in getattr(role, "fallback_scope_fields", [])
            if field not in getattr(role, "scope_fields", [])
        ]
        promoted: list[str] = []
        for field in fallback_fields:
            values: set[str] = set()
            comparable_rows = 0
            complete = True
            for imported in imported_files:
                for row_index in range(imported.n_rows):
                    row = {
                        header: imported.value(row_index, header)
                        for header in imported.header_codes
                    }
                    if build_role_identity(row, role) is None:
                        continue
                    comparable_rows += 1
                    value = normalize_identity(row.get(field, ""))
                    if not value:
                        complete = False
                        continue
                    values.add(value)
            if comparable_rows and complete and len(values) > 1:
                promoted.append(field)
        if promoted:
            effective.append(role.model_copy(update={
                "scope_fields": list(dict.fromkeys([
                    *role.scope_fields,
                    *promoted,
                ])),
            }))
        else:
            effective.append(role)
    return effective


def _role_scope_summaries(
    imported_files: Sequence[ImportedFile],
    profile: object,
) -> list[dict[str, Any]]:
    role_profiles = _effective_role_profiles(
        imported_files,
        list(getattr(profile, "role_profiles", []) or []),
    )
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for imported in imported_files:
        for row_index in range(imported.n_rows):
            row = {
                field: imported.value(row_index, field)
                for field in imported.header_codes
            }
            for role in role_profiles:
                identity = build_role_identity(row, role)
                if identity is None:
                    continue
                scope_value = identity.scope_key or "all"
                group_key = (role.role_type, scope_value)
                group = groups.setdefault(group_key, {
                    "scope_key": f"{role.role_type}:{scope_value}",
                    "role_type": role.role_type,
                    "role_label": role.role_label,
                    "required": bool(role.required),
                    "profile": role.model_dump(mode="json"),
                    "business_scope": {
                        field: row.get(field, "")
                        for field in active_role_scope_fields(row, role)
                        if str(row.get(field, "")).strip()
                    },
                    "submitted_row_count": 0,
                    "submitted_identity_rows": [],
                    "unidentified_row_count": 0,
                })
                group["submitted_row_count"] += 1
                identity_key = identity.key
                if identity.discriminator_key:
                    identity_key = IDENTITY_SEPARATOR.join(
                        [identity_key, identity.discriminator_key]
                    )
                discriminator_values = [
                    str(row.get(field, "") or "").strip()
                    for field in role.discriminator_fields
                    if str(row.get(field, "") or "").strip()
                ]
                alternative_values: list[str] = []
                selected_fields = tuple(identity.fields)
                selected_seen = False
                for alternative in role.primary_alternatives:
                    alternative_fields = tuple(alternative)
                    if alternative_fields == selected_fields:
                        selected_seen = True
                        continue
                    if not selected_seen:
                        continue
                    values = [
                        str(row.get(field, "") or "").strip()
                        for field in alternative_fields
                    ]
                    if values and all(normalize_identity(value) for value in values):
                        alternative_values = values
                        break
                group["submitted_identity_rows"].append({
                    "key": identity_key,
                    "primary_display": identity.display,
                    "alternative_values": alternative_values,
                    "discriminator_values": discriminator_values,
                })
    result: list[dict[str, Any]] = []
    for group in groups.values():
        unique_rows = {
            str(item["key"]): item
            for item in group.pop("submitted_identity_rows", [])
        }
        primary_counts: dict[str, int] = {}
        for item in unique_rows.values():
            primary = normalize_identity(item["primary_display"])
            primary_counts[primary] = primary_counts.get(primary, 0) + 1
        group["submitted_identities"] = {
            key: (
                ";".join([
                    str(item["primary_display"]),
                    *[
                        str(value) for value in (
                            item["alternative_values"]
                            or item["discriminator_values"]
                        )
                    ],
                ])
                if primary_counts.get(normalize_identity(item["primary_display"]), 0) > 1
                and (item["alternative_values"] or item["discriminator_values"])
                else str(item["primary_display"])
            )
            for key, item in unique_rows.items()
        }
        group["submitted_identity_count"] = len(group["submitted_identities"])
        result.append(group)
    return sorted(result, key=lambda item: (item["role_type"], item["scope_key"]))


def _submitted_row_assignments(
    imported_files: Sequence[ImportedFile],
    profile: object,
) -> tuple[dict[str, int], list[dict[str, Any]]]:
    role_profiles = _effective_role_profiles(
        imported_files,
        list(getattr(profile, "role_profiles", []) or []),
    )
    assignments: list[dict[str, Any]] = []
    for imported in imported_files:
        for row_index in range(imported.n_rows):
            row = {
                field: imported.value(row_index, field)
                for field in imported.header_codes
            }
            scope_keys: list[str] = []
            reasons: list[str] = []
            for role in role_profiles:
                identity = build_role_identity(row, role)
                if identity is None:
                    continue
                scope_value = identity.scope_key or "all"
                scope_keys.append(f"{role.role_type}:{scope_value}")
            if scope_keys:
                status = "assigned"
            else:
                status = "unassigned"
                reasons.append("no_role_identity")
            assignments.append({
                "source_path": imported.path,
                "sheet_name": imported.sheet_name,
                "row_number": row_index + 3,
                "category": str(row.get("XMLB", "") or row.get("HJDJ", "")),
                "status": status,
                "scope_keys": list(dict.fromkeys(scope_keys)),
                "reasons": reasons,
            })
    counts = {
        "total_rows": len(assignments),
        "assigned_rows": sum(item["status"] == "assigned" for item in assignments),
        "ambiguous_rows": sum(item["status"] == "ambiguous" for item in assignments),
        "unassigned_rows": sum(item["status"] == "unassigned" for item in assignments),
    }
    return counts, assignments


def _flatten_fields(alternatives: list[list[str]]) -> list[str]:
    return list(dict.fromkeys(
        field for alternative in alternatives for field in alternative
    ))


def _public_urls(report: Mapping[str, Any]) -> list[str]:
    candidates = [str(report.get("source_url", ""))]
    candidates.extend(_strings(report.get("source_urls"), limit=20))
    candidates.extend(_strings(report.get("found_assets"), limit=20))
    result: list[str] = []
    for raw in candidates:
        url = raw.strip()[:2048]
        if url.lower().startswith(("http://", "https://")) and url not in result:
            result.append(url)
    return result[:20]


def _count(value: object) -> int:
    if not isinstance(value, (str, int, float, bool)):
        return 0
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _url_domains(urls: Sequence[str]) -> list[str]:
    result: list[str] = []
    for url in urls:
        try:
            domain = normalize_domain(url)
        except ValueError:
            continue
        if domain not in result:
            result.append(domain)
    return result[:8]


def _local_issues(value: object) -> list[LocalIssueSummary]:
    if not isinstance(value, list):
        return []
    issues: list[LocalIssueSummary] = []
    for item in value[:200]:
        if isinstance(item, Mapping):
            issues.append(LocalIssueSummary(
                rule_id=str(item.get("rule_id", "")),
                severity=str(item.get("severity", "")),
                file=str(item.get("file", "")),
                field_code=str(item.get("field_code", "") or ""),
                message=str(item.get("message", "")),
            ))
    return issues


def _normalized_code(value: str) -> str:
    bounded = value.strip()
    return bounded.zfill(8) if bounded.isdigit() else bounded.casefold()


def _matching_imports(
    report: Mapping[str, Any],
    imported_files: Sequence[ImportedFile],
) -> list[ImportedFile]:
    resource_code = _normalized_code(str(report.get("resource_code", "")))
    year = str(report.get("year", "")).strip()
    return [
        item
        for item in imported_files
        if _normalized_code(item.first_zylbm) == resource_code
        and (not year or not item.year or item.year == year)
    ][:20]


def _ledger_entry(
    resource_code: str,
    ledger: Mapping[str, LedgerEntry],
) -> LedgerEntry | None:
    direct = ledger.get(resource_code)
    if direct is not None:
        return direct
    normalized = _normalized_code(resource_code)
    return next(
        (
            entry
            for code, entry in ledger.items()
            if _normalized_code(code) == normalized
        ),
        None,
    )


def derive_audit_case_input(
    report: Mapping[str, Any],
    *,
    imported_files: Sequence[ImportedFile] = (),
    registry: Mapping[str, TemplateSpec] | None = None,
    ledger: Mapping[str, LedgerEntry] | None = None,
) -> AuditCaseInput:
    """Derive production M5 input without case-specific manifest metadata."""

    raw_resource_code = str(report.get("resource_code", "")).strip()[:40]
    resource_code = (
        raw_resource_code.zfill(8) if raw_resource_code.isdigit() else raw_resource_code
    )
    matching = _matching_imports(report, imported_files)
    table_codes = list(dict.fromkeys(
        item.claimed_table_code for item in matching if item.claimed_table_code
    ))[:10]
    registry_map = registry or {}
    ledger_map = ledger or {}
    spec = registry_map.get(table_codes[0]) if len(table_codes) == 1 else None
    profile = resolve_match_profile(spec) if spec is not None else None
    present_fields = set(spec.field_codes) if spec is not None else set()
    active_primary_alternatives = (
        _active_identity_alternatives(
            matching,
            profile.primary_alternatives,
            present_fields,
        )
        if profile is not None
        else []
    )
    match_fields = (
        _flatten_fields(active_primary_alternatives)
        if profile is not None and active_primary_alternatives
        else []
    )
    used_title_fallback = False
    if (
        not match_fields
        and spec is not None
        and spec.title_col
        and spec.title_col in present_fields
        and _field_has_value(matching, spec.title_col)
    ):
        # Some production workbooks leave every configured composite identity
        # column blank while their registered title column is populated. Keep
        # the profile unchanged and use that existing template role as a
        # bounded fallback for source-roster comparison.
        match_fields = [
            spec.title_col,
            *[
                field for field in spec.org_cols
                if field in present_fields and _field_has_value(matching, field)
            ][:1],
        ]
        used_title_fallback = True
    attachment_match_fields = (
        [
            field
            for field in (profile.attachment_submit_cols or profile.submit_cols)
            if field in present_fields and _field_has_value(matching, field)
        ]
        if profile is not None and active_primary_alternatives
        else match_fields
    )
    if used_title_fallback and profile is not None:
        original_role = profile.role_profiles[0] if profile.role_profiles else None
        fallback_role = RoleProfile(
            role_type=(original_role.role_type if original_role else "work_or_project"),
            role_label=(original_role.role_label if original_role else "主审核范围"),
            required=(original_role.required if original_role else True),
            primary_alternatives=[match_fields],
            scope_fields=list(profile.scope_fields),
            fallback_scope_fields=(
                list(original_role.fallback_scope_fields) if original_role else []
            ),
            discriminator_fields=list(profile.discriminator_fields),
            conflict_fields=list(profile.conflict_fields),
            attribute_fields=list(profile.attribute_fields),
            selector_any_fields=match_fields,
            section_include_terms=(
                list(original_role.section_include_terms) if original_role else []
            ),
            section_exclude_terms=(
                list(original_role.section_exclude_terms) if original_role else []
            ),
        )
        profile = profile.model_copy(update={
            "primary_alternatives": [match_fields],
            "submit_cols": match_fields,
            "attachment_submit_cols": match_fields,
            "combine": "all" if len(match_fields) > 1 else "first",
            "role_profiles": [fallback_role],
        })
        active_primary_alternatives = [match_fields]
    entry = _ledger_entry(resource_code, ledger_map)
    known_urls = _public_urls(report)
    direct_source = str(report.get("source_url", "")).strip()
    official_domains = _url_domains([direct_source] if direct_source else [])
    if not official_domains and known_urls:
        # M4 may bind a verified public URL without duplicating it in source_url.
        official_domains = _url_domains(known_urls[:1])
    registered_sources = _strings(report.get("source_urls"), limit=20)
    official_secondary_domains = [
        domain for domain in _url_domains(registered_sources)
        if domain not in official_domains
    ][:8]
    if entry is not None:
        for url in split_urls(entry.collect_url):
            if url.startswith(("http://", "https://")) and url not in known_urls:
                known_urls.append(url)
    submitted_rows = sum(item.n_rows for item in matching) or _count(
        report.get("submitted_count", 0)
    )
    role_scopes = _role_scope_summaries(matching, profile) if profile is not None else []
    row_conservation, row_assignments = (
        _submitted_row_assignments(matching, profile)
        if profile is not None
        else ({
            "total_rows": submitted_rows,
            "assigned_rows": 0,
            "ambiguous_rows": 0,
            "unassigned_rows": submitted_rows,
        }, [])
    )
    unique_identity_count = sum(
        int(scope.get("submitted_identity_count", 0)) for scope in role_scopes
        if bool(scope.get("required", True))
    )
    return AuditCaseInput(
        resource_code=resource_code,
        award_name=(
            str(report.get("award_name", "")).strip()
            or (matching[0].award_name if matching else "")
        )[:200],
        year=(
            str(report.get("year", "")).strip()
            or (matching[0].year if matching else "")
        )[:20],
        submission_files=[item.path for item in matching],
        table_codes=table_codes,
        identity_version=IDENTITY_VERSION,
        identity_primary_alternatives=(
            active_primary_alternatives
        ),
        identity_scope_fields=(
            [field for field in profile.scope_fields if field in present_fields]
            if profile is not None
            else []
        ),
        identity_discriminator_fields=(
            [field for field in profile.discriminator_fields if field in present_fields]
            if profile is not None
            else []
        ),
        identity_conflict_fields=(
            [field for field in profile.conflict_fields if field in present_fields]
            if profile is not None
            else []
        ),
        match_profile=profile.kind if profile is not None else "",
        match_fields=match_fields,
        attachment_match_fields=attachment_match_fields,
        match_combine=(
            profile.combine
            if profile is not None and active_primary_alternatives
            else "all" if used_title_fallback and len(match_fields) > 1 else "first"
        ),
        submitted_rows=submitted_rows,
        expected_scope_count=unique_identity_count or submitted_rows or None,
        ledger_expected_count=(entry.expected_count if entry is not None else None),
        known_urls=known_urls[:20],
        official_domains=official_domains,
        official_secondary_domains=official_secondary_domains,
        ledger_resource_name=(entry.resource_name if entry is not None else "")[:200],
        ledger_source=(entry.source if entry is not None else "")[:200],
        source_only_items=_strings(report.get("missing"), limit=100),
        submitted_only_items=_strings(report.get("extra"), limit=100),
        unresolved_items=_strings(report.get("unresolved"), limit=100),
        local_issues=_local_issues(report.get("local_issues")),
        role_scopes=role_scopes,
        row_conservation=row_conservation,
        row_assignments=row_assignments,
    )


def _context_summary(context: AuditCaseInput) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "submission_files": context.submission_files,
        "table_codes": context.table_codes,
        "table_code": context.table_codes[0] if len(context.table_codes) == 1 else "",
        "identity_version": context.identity_version,
        "identity_primary_alternatives": context.identity_primary_alternatives,
        "identity_scope_fields": context.identity_scope_fields,
        "identity_discriminator_fields": context.identity_discriminator_fields,
        "identity_conflict_fields": context.identity_conflict_fields,
        "match_profile": context.match_profile,
        "match_fields": context.match_fields,
        "match_combine": context.match_combine,
        "attachment_match_fields": context.attachment_match_fields,
        "submitted_rows": context.submitted_rows,
        "expected_scope_count": context.expected_scope_count,
        "ledger_expected_count": context.ledger_expected_count,
        "ledger_resource_name": context.ledger_resource_name,
        "ledger_source": context.ledger_source,
        "official_domains": context.official_domains,
        "official_secondary_domains": context.official_secondary_domains,
        "source_only_items": context.source_only_items,
        "submitted_only_items": context.submitted_only_items,
        "unresolved_items": context.unresolved_items,
        "local_issues": [item.model_dump(mode="json") for item in context.local_issues],
        "role_scopes": context.role_scopes,
        "row_conservation": context.row_conservation,
        "row_assignments": context.row_assignments,
    }
    if context.submission_files:
        summary["submission_file"] = context.submission_files[0]
    return summary


def _trigger(report: Mapping[str, Any]) -> TriggerCode:
    reasons = {item.casefold() for item in _strings(report.get("reason_codes"))}
    source_kind = str(report.get("source_kind", "")).casefold()
    assets = [item.casefold().split("?", 1)[0] for item in _strings(
        report.get("found_assets"), limit=20
    )]
    if reasons.intersection({"evidence_conflict", "source_conflict"}):
        return "EVIDENCE_CONFLICT"
    if reasons.intersection({
        "year_mismatch", "year_unverified", "year_no_match", "cross_year_dropped",
        "page_target_uncertain", "non_final_source",
    }):
        return "PAGE_TARGET_UNCERTAIN"
    if "zero_overlap" in reasons:
        return "ZERO_OVERLAP"
    if "pdf_only" in reasons:
        return "PDF_ONLY"
    if "image_only" in reasons:
        return "IMAGE_ONLY"
    if any(item.endswith(".pdf") for item in assets):
        return "PDF_ONLY"
    if any(
        item.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp"))
        for item in assets
    ):
        return "IMAGE_ONLY"
    if source_kind == "pdf":
        return "PDF_ONLY"
    if source_kind == "image":
        return "IMAGE_ONLY"
    return "COVERAGE_UNKNOWN"


_OBJECTIVES: dict[TriggerCode, str] = {
    "EVIDENCE_CONFLICT": "核验多个来源的冲突并识别可采用的最终官方版本",
    "PAGE_TARGET_UNCERTAIN": "核验奖项目标、年份或届次与来源页面是否一致",
    "ZERO_OVERLAP": "核验提交与来源零重叠是目标错误、认列错误还是实际不一致",
    "IMAGE_ONLY": "对图片名单执行有界 OCR/视觉抽取并验证页数与序号覆盖",
    "PDF_ONLY": "对 PDF 名单执行分层文本/OCR 抽取并验证完整性",
    "COVERAGE_UNKNOWN": "核验名单数量、页数、序号和提交覆盖是否完整",
}

_QUESTIONS: dict[TriggerCode, str] = {
    "EVIDENCE_CONFLICT": "哪个来源是对应年份/届次的最终正式版本？",
    "PAGE_TARGET_UNCERTAIN": "当前来源能否确认奖项、年份和届次身份？",
    "ZERO_OVERLAP": "零重叠是否由跨年、目标页面或字段映射错误造成？",
    "IMAGE_ONLY": "图片页数、序号和关键字段是否全部可辨认？",
    "PDF_ONLY": "PDF 是否包含完整名单，扫描页是否需要 OCR/vision？",
    "COVERAGE_UNKNOWN": "是否存在遗漏页、遗漏赛道、截断或数量不一致？",
}


def seed_from_evidence_report(
    batch_id: int,
    report: Mapping[str, Any],
    *,
    imported_files: Sequence[ImportedFile] = (),
    registry: Mapping[str, TemplateSpec] | None = None,
    ledger: Mapping[str, LedgerEntry] | None = None,
) -> CaseSeed | None:
    """Create a bounded difficult-case seed; never widen an M4 auto-pass."""

    resource_code = str(report.get("resource_code", "")).strip()[:40]
    verdict = str(report.get("verdict", "")).strip()
    confidence = str(report.get("confidence", "low")).strip().lower()
    if not resource_code:
        return None
    if decide_triage(verdict, confidence) == "auto_pass" and confidence == "high":
        return None
    context = derive_audit_case_input(
        report,
        imported_files=imported_files,
        registry=registry,
        ledger=ledger,
    )
    trigger = _trigger(report)
    reasons = _strings(report.get("reason_codes"), limit=30)
    return CaseSeed(
        batch_id=batch_id,
        resource_code=context.resource_code,
        award_name=context.award_name,
        year=context.year,
        trigger_codes=[trigger],
        objective=_OBJECTIVES[trigger],
        submitted_summary={
            "verdict": verdict[:100],
            "confidence": confidence[:20],
            "source_kind": str(report.get("source_kind", ""))[:40],
            "submitted_count": _count(report.get("submitted_count", 0)),
            "extracted_count": _count(report.get("extracted_count", 0)),
            "missing_count": len(_strings(report.get("missing"), limit=1000)),
            "extra_count": len(_strings(report.get("extra"), limit=1000)),
            "reason_codes": reasons,
            **_context_summary(context),
        },
        known_urls=context.known_urls,
        open_questions=[_QUESTIONS[trigger]],
    )


def ensure_review_cases(
    store: Store,
    batch_id: int,
    *,
    search_handoffs: Iterable[SearchHandoff] = (),
    audit_reports: Iterable[Mapping[str, Any]] = (),
    imported_files: Sequence[ImportedFile] = (),
    registry: Mapping[str, TemplateSpec] | None = None,
    ledger: Mapping[str, LedgerEntry] | None = None,
    tool_limits: ToolBudgetLimits | None = None,
    require_m4_binding: bool = False,
) -> CaseBridgeResult:
    repository = CaseRepository(store)
    seeds: list[CaseSeed] = []
    for handoff in search_handoffs:
        seed = seed_from_search_handoff(batch_id, handoff)
        context = derive_audit_case_input(
            handoff.model_dump(mode="json"),
            imported_files=imported_files,
            registry=registry,
            ledger=ledger,
        )
        seed.award_name = context.award_name or seed.award_name
        seed.year = context.year or seed.year
        seed.resource_code = context.resource_code or seed.resource_code
        seed.known_urls = list(dict.fromkeys([
            *seed.known_urls,
            *context.known_urls,
        ]))[:20]
        seed.submitted_summary.update(_context_summary(context))
        seeds.append(seed)
    skipped = 0
    for report in audit_reports:
        evidence_seed = seed_from_evidence_report(
            batch_id,
            report,
            imported_files=imported_files,
            registry=registry,
            ledger=ledger,
        )
        if evidence_seed is None:
            skipped += 1
        else:
            seeds.append(evidence_seed)
    case_ids: list[int] = []
    created = 0
    for seed in seeds:
        stage_item = store.get_stage_item(
            batch_id, seed.resource_code, seed.year, stage="m4"
        )
        current_result_id = int(
            stage_item["current_result_id"] or 0 if stage_item is not None else 0
        )
        if require_m4_binding and current_result_id <= 0:
            raise RuntimeError(
                f"M5 case requires current M4 result: {seed.resource_code}/{seed.year}"
            )
        if current_result_id > 0:
            seed.origin_m4_result_id = current_result_id
        state, was_created = repository.create_or_get(seed, tool_limits=tool_limits)
        case_ids.append(state.case_id)
        created += int(was_created)
    return CaseBridgeResult(
        case_ids=list(dict.fromkeys(case_ids)),
        created=created,
        existing=len(seeds) - created,
        skipped=skipped,
    )


def ensure_imported_review_cases(
    store: Store,
    batch_id: int,
    *,
    imported_files: Sequence[ImportedFile],
    eligible_files: Sequence[str],
    registry: Mapping[str, TemplateSpec],
    ledger: Mapping[str, LedgerEntry],
    issue_codes_by_file: Mapping[str, Sequence[str]] | None = None,
    issues_by_file: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
    tool_limits: ToolBudgetLimits | None = None,
    require_m4_binding: bool = False,
) -> CaseBridgeResult:
    """Queue generic M5 cases for locally valid imported resource/year groups."""

    eligible_names = set(eligible_files)
    eligible_imports = [
        item for item in imported_files if item.file_name in eligible_names
    ]
    groups: dict[tuple[str, str], list[ImportedFile]] = {}
    skipped = 0
    for item in eligible_imports:
        resource_code = item.first_zylbm.strip()
        if not resource_code:
            skipped += 1
            continue
        key = (_normalized_code(resource_code), item.year.strip())
        groups.setdefault(key, []).append(item)

    issue_map = issue_codes_by_file or {}
    issue_details = issues_by_file or {}
    reports: list[dict[str, Any]] = []
    for (resource_code, year), files in groups.items():
        reason_codes = list(dict.fromkeys([
            *[
                code
                for item in files
                for code in issue_map.get(item.file_name, ())
                if code
            ],
            "evidence_review_not_started",
        ]))[:30]
        reports.append({
            "resource_code": resource_code,
            "award_name": files[0].award_name,
            "year": year,
            "verdict": "无法核对",
            "confidence": "low",
            "source_kind": "none",
            "submitted_count": sum(item.n_rows for item in files),
            "extracted_count": 0,
            "missing": [],
            "extra": [],
            "unresolved": [],
            "reason_codes": reason_codes,
            "local_issues": [
                issue
                for item in files
                for issue in issue_details.get(item.file_name, ())
            ][:200],
        })

    result = ensure_review_cases(
        store,
        batch_id,
        audit_reports=reports,
        imported_files=eligible_imports,
        registry=registry,
        ledger=ledger,
        tool_limits=tool_limits,
        require_m4_binding=require_m4_binding,
    )
    result.skipped += skipped
    return result


def case_report_rows(store: Store, batch_id: int) -> list[dict[str, Any]]:
    """Return bounded report data with provenance but without local paths or raw Trace data."""

    repository = CaseRepository(store)
    rows: list[dict[str, Any]] = []
    for row in store.list_audit_cases(batch_id=batch_id):
        state = repository.load(int(row["id"]))
        sources = list(dict.fromkeys([
            *[url for url in state.known_urls if url.startswith(("http://", "https://"))],
            *[item.source_url for item in state.artifacts],
        ]))[:20]
        rows.append({
            "case_id": state.case_id,
            "resource_code": state.resource_code,
            "award_name": state.award_name,
            "year": state.year,
            "status": state.status,
            "trigger_codes": list(state.trigger_codes),
            "recommendation": state.recommendation,
            "confidence": state.confidence,
            "step_count": state.step_count,
            "token_used": state.token_used,
            "elapsed_ms": state.elapsed_ms,
            "reflection_count": state.reflection_count,
            "evidence_sources": sources,
            "evidence_hashes": [item.sha256 for item in state.artifacts],
            "evidence_times": [item.fetched_at for item in state.artifacts],
            "verification_action": (
                state.latest_verification.recommended_action
                if state.latest_verification else ""
            ),
            "human_decision": state.human_decision,
            "human_decision_summary": state.human_decision_summary,
            "reviewed_by": state.reviewed_by,
            "state_version": state.state_version,
        })
    return rows
