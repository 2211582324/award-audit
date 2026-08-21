"""Deterministic URL canonicalization and source-authority assessment."""

from __future__ import annotations

import re
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import BaseModel, Field

from award_audit.agent.toolkit.safety import validate_public_url

SourceLevel = Literal[
    "official_primary",
    "official_secondary",
    "institutional_secondary",
    "publisher_secondary",
    "media_or_aggregator",
    "unknown",
]

_TRACKING_PARAMS = {
    "fbclid",
    "gclid",
    "spm",
    "from",
    "ref",
    "source",
    "yclid",
}
_PUBLISHER_DOMAINS = {
    "163.com",
    "chinazy.org",
    "chinanews.com.cn",
    "cnr.cn",
    "eol.cn",
    "mp.weixin.qq.com",
    "news.cn",
    "people.com.cn",
    "qq.com",
    "sohu.com",
    "toutiao.com",
}
_AGGREGATOR_DOMAINS = {"baidu.com"}
_GOVERNMENT_SUFFIXES = (
    ".gov.cn",
    ".gov.uk",
    ".go.jp",
    ".gouv.fr",
    ".gc.ca",
    ".gov",
    ".europa.eu",
)
_INSTITUTION_SUFFIXES = (".edu.cn", ".edu")
_TEXT_NORMALIZER = re.compile(r"[^0-9a-z\u4e00-\u9fff]+", re.IGNORECASE)


class SourceAssessment(BaseModel):
    level: SourceLevel
    domain: str
    reason: str


class OfficialSearchCandidate(BaseModel):
    """One search lead. A lead is never evidence until fetched and verified."""

    title: str = Field(max_length=300)
    url: str = Field(max_length=2048)
    domain: str = Field(min_length=1, max_length=253)
    snippet: str = Field(max_length=1000)
    provider: str = Field(min_length=1, max_length=40)
    rank: int = Field(ge=1, le=100)
    source_level: SourceLevel
    source_reason: str = Field(min_length=1, max_length=300)
    match_reasons: list[str] = Field(min_length=1, max_length=8)
    query: str = Field(min_length=1, max_length=100)
    is_evidence: bool = False
    requires_fetch_verification: bool = True


def normalize_domain(value: str) -> str:
    """Normalize a caller-supplied domain or URL to a bare IDNA hostname."""

    raw = value.strip().lower().rstrip(".")
    if not raw:
        raise ValueError("domain cannot be empty")
    parsed = urlsplit(raw if "://" in raw else "//" + raw)
    host = parsed.hostname
    if not host or parsed.username or parsed.password:
        raise ValueError(f"invalid domain: {value[:100]}")
    try:
        normalized = host.encode("idna").decode("ascii").lower().rstrip(".")
    except UnicodeError as exc:
        raise ValueError(f"invalid internationalized domain: {value[:100]}") from exc
    if not normalized or len(normalized) > 253 or ".." in normalized:
        raise ValueError(f"invalid domain: {value[:100]}")
    return normalized


def _domain_matches(host: str, domains: list[str]) -> bool:
    return any(host == domain or host.endswith("." + domain) for domain in domains)


def canonicalize_candidate_url(url: str) -> str:
    """Reject unsafe schemes/hosts and remove tracking-only URL variation."""

    validated = validate_public_url(url, resolve_dns=False)
    parsed = urlsplit(validated)
    host = normalize_domain(parsed.hostname or "")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("invalid URL port") from exc
    default_port = (parsed.scheme == "http" and port == 80) or (
        parsed.scheme == "https" and port == 443
    )
    netloc = host if port is None or default_port else f"{host}:{port}"
    query = urlencode([
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in _TRACKING_PARAMS
    ])
    return urlunsplit((parsed.scheme.lower(), netloc, parsed.path or "/", query, ""))


def classify_source(
    domain: str,
    *,
    official_domains: list[str],
    official_secondary_domains: list[str],
) -> SourceAssessment:
    """Classify authority without trusting page text or search-provider ranking."""

    host = normalize_domain(domain)
    primary = [normalize_domain(item) for item in official_domains]
    secondary = [normalize_domain(item) for item in official_secondary_domains]
    if _domain_matches(host, primary):
        return SourceAssessment(
            level="official_primary",
            domain=host,
            reason="matches an explicitly supplied organizer/主管方 domain",
        )
    if _domain_matches(host, secondary):
        return SourceAssessment(
            level="official_secondary",
            domain=host,
            reason="matches an explicitly supplied organizer-authorized platform domain",
        )
    if host == "gov.cn" or host.endswith(_GOVERNMENT_SUFFIXES):
        return SourceAssessment(
            level="official_secondary",
            domain=host,
            reason="government domain outside the explicitly supplied primary domain",
        )
    if host.endswith(_INSTITUTION_SUFFIXES):
        return SourceAssessment(
            level="institutional_secondary",
            domain=host,
            reason="educational institution domain; may be an official institutional repost",
        )
    if _domain_matches(host, sorted(_PUBLISHER_DOMAINS)):
        return SourceAssessment(
            level="publisher_secondary",
            domain=host,
            reason="known media or public-account publishing domain",
        )
    if _domain_matches(host, sorted(_AGGREGATOR_DOMAINS)):
        return SourceAssessment(
            level="media_or_aggregator",
            domain=host,
            reason="known search or aggregation domain",
        )
    return SourceAssessment(
        level="unknown",
        domain=host,
        reason="domain authority is not established by explicit project metadata",
    )


def _normalized_text(value: str) -> str:
    return _TEXT_NORMALIZER.sub("", value).lower()


def build_candidate(
    *,
    title: str,
    url: str,
    snippet: str,
    provider: str,
    rank: int,
    query: str,
    award_name: str,
    year: str,
    organizer: str,
    session: str,
    official_domains: list[str],
    official_secondary_domains: list[str],
) -> OfficialSearchCandidate:
    canonical = canonicalize_candidate_url(url)
    domain = normalize_domain(urlsplit(canonical).hostname or "")
    assessment = classify_source(
        domain,
        official_domains=official_domains,
        official_secondary_domains=official_secondary_domains,
    )
    haystack = _normalized_text(f"{title} {snippet}")
    reasons: list[str] = []
    for label, value in (
        ("award_name_match", award_name),
        ("year_match", year),
        ("organizer_match", organizer),
        ("session_match", session),
    ):
        needle = _normalized_text(value)
        if needle and needle in haystack:
            reasons.append(label)
    if assessment.level == "official_primary":
        reasons.append("explicit_official_domain_match")
    elif assessment.level == "official_secondary":
        reasons.append("official_secondary_domain_match")
    if not reasons:
        reasons.append("provider_ranked_lead_only")
    return OfficialSearchCandidate(
        title=title[:300],
        url=canonical,
        domain=domain,
        snippet=snippet[:1000],
        provider=provider,
        rank=rank,
        source_level=assessment.level,
        source_reason=assessment.reason,
        match_reasons=reasons,
        query=query,
    )
