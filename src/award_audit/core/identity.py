"""Versioned, template-driven identity construction shared by M4 and M5."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from award_audit.core.models.template import IdentityProfile, RoleProfile

IDENTITY_VERSION = "identity-v2"
IDENTITY_SEPARATOR = "\x1f"
_NORMALIZE_CHARS = re.compile(r"[^0-9a-z\u4e00-\u9fff]+", re.IGNORECASE)


@dataclass(frozen=True)
class IdentityItem:
    key: str
    display: str
    field_code: str
    fields: tuple[str, ...]
    scope_key: str = ""
    discriminator_key: str = ""
    conflict_key: str = ""
    version: str = IDENTITY_VERSION


def role_applies(row: Mapping[str, object], profile: RoleProfile) -> bool:
    """Classify a row without sending the roster to an LLM."""

    if profile.selector_any_fields and not any(
        normalize_identity(row.get(field, "")) for field in profile.selector_any_fields
    ):
        return False
    if profile.selector_terms_by_field:
        return any(
            any(term.casefold() in str(row.get(field, "")).casefold() for term in terms)
            for field, terms in profile.selector_terms_by_field.items()
        )
    return True


def build_role_identity(
    row: Mapping[str, object], profile: RoleProfile
) -> IdentityItem | None:
    if not role_applies(row, profile):
        return None
    selected = _select_primary(row, profile.primary_alternatives)
    if selected is None:
        return None
    fields, displays, normalized = selected
    return IdentityItem(
        key=IDENTITY_SEPARATOR.join(normalized),
        display=";".join(displays),
        field_code="+".join(fields),
        fields=fields,
        scope_key=_field_key(row, active_role_scope_fields(row, profile), prefix="scope"),
        discriminator_key=_field_key(
            row, profile.discriminator_fields, prefix="discriminator"
        ),
        conflict_key=_field_key(row, profile.conflict_fields, prefix="conflict"),
    )


def active_role_scope_fields(
    row: Mapping[str, object], profile: RoleProfile
) -> list[str]:
    """Use fallback scope fields only when configured business dimensions are blank."""

    base_fields = {"ZYLBM", "year", "LXNF", "HJNF"}
    business_fields = [field for field in profile.scope_fields if field not in base_fields]
    if any(normalize_identity(row.get(field, "")) for field in business_fields):
        return list(profile.scope_fields)
    return list(dict.fromkeys([
        *profile.scope_fields,
        *profile.fallback_scope_fields,
    ]))


def normalize_identity(value: object) -> str:
    """Normalize display punctuation without embedding template-specific aliases."""

    return _NORMALIZE_CHARS.sub("", str(value or "").strip().lower())


def normalize_comparison_identity(value: object, *, role_type: str = "") -> str:
    """Normalize a source identity for equality against a persisted role scope."""

    del role_type
    return normalize_identity(value)


def route_text_variants(value: object) -> set[str]:
    """Return conservative label variants for deterministic asset routing."""

    normalized = normalize_identity(value)
    variants = {normalized} if normalized else set()
    current = normalized
    for suffix in ("名单", "项目", "奖项", "类别"):
        if current.endswith(suffix) and len(current) > len(suffix) + 1:
            current = current.removesuffix(suffix)
            variants.add(current)
    return variants


def _field_key(
    row: Mapping[str, object], fields: Sequence[str], *, prefix: str
) -> str:
    parts: list[str] = []
    for field in fields:
        value = normalize_identity(row.get(field, ""))
        if value:
            parts.append(f"{prefix}:{field}={value}")
    return IDENTITY_SEPARATOR.join(parts)


def _select_primary(
    row: Mapping[str, object], alternatives: Sequence[Sequence[str]]
) -> tuple[tuple[str, ...], list[str], list[str]] | None:
    for alternative in alternatives:
        raw = [str(row.get(field, "") or "").strip() for field in alternative]
        normalized = [normalize_identity(value) for value in raw]
        if raw and all(normalized):
            return tuple(alternative), raw, normalized
    return None


def build_profile_identity(
    row: Mapping[str, object], profile: IdentityProfile
) -> IdentityItem | None:
    """Build the row identity selected by a template's ordered alternatives."""

    selected = _select_primary(row, profile.primary_alternatives)
    if selected is None:
        return None
    fields, displays, normalized = selected
    return IdentityItem(
        key=IDENTITY_SEPARATOR.join(normalized),
        display=";".join(displays),
        field_code="+".join(fields),
        fields=fields,
        scope_key=_field_key(row, profile.scope_fields, prefix="scope"),
        discriminator_key=_field_key(
            row, profile.discriminator_fields, prefix="discriminator"
        ),
        conflict_key=_field_key(row, profile.conflict_fields, prefix="conflict"),
    )


def build_business_identity_key(
    row: Mapping[str, object], profile: IdentityProfile
) -> str:
    """Build a deterministic storage key while keeping scope out of row matching."""

    selected = _select_primary(row, profile.primary_alternatives)
    if selected is None:
        return ""
    fields, _displays, normalized = selected
    parts = [
        f"primary:{field}={value}"
        for field, value in zip(fields, normalized, strict=True)
    ]
    for prefix, component_fields in (
        ("scope", profile.scope_fields),
        ("discriminator", profile.discriminator_fields),
        ("conflict", profile.conflict_fields),
        ("occurrence", profile.occurrence_fields),
    ):
        component = _field_key(row, component_fields, prefix=prefix)
        if component:
            parts.append(component)
    if not parts:
        return ""
    return IDENTITY_SEPARATOR.join(parts)


def build_identities(
    rows: Sequence[Mapping[str, object]],
    fields: Sequence[str],
    *,
    combine: Literal["first", "all"] = "first",
) -> list[IdentityItem]:
    """Build identities and disambiguate duplicate first-choice values across all rows."""

    candidates_by_row: list[list[tuple[str, str, str]]] = []
    for row in rows:
        candidates: list[tuple[str, str, str]] = []
        for field in fields:
            display = str(row.get(field, "") or "").strip()
            normalized = normalize_identity(display)
            if normalized:
                candidates.append((field, normalized, display))
        if candidates:
            candidates_by_row.append(candidates)

    base_counts: dict[tuple[str, str], int] = {}
    if combine == "first":
        for candidates in candidates_by_row:
            base = (candidates[0][0], candidates[0][1])
            base_counts[base] = base_counts.get(base, 0) + 1

    identities: list[IdentityItem] = []
    seen: set[tuple[str, str]] = set()
    for candidates in candidates_by_row:
        selected = candidates
        if combine == "first":
            base = (candidates[0][0], candidates[0][1])
            selected = candidates if base_counts[base] > 1 else candidates[:1]
        field_code = "+".join(item[0] for item in selected)
        key = IDENTITY_SEPARATOR.join(item[1] for item in selected)
        display = ";".join(item[2] for item in selected)
        unique = (field_code, key)
        if unique in seen:
            continue
        seen.add(unique)
        identities.append(IdentityItem(
            key=key,
            display=display,
            field_code=field_code,
            fields=tuple(item[0] for item in selected),
        ))
    return identities
