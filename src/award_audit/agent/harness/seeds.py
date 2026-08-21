"""Deterministic bridges from existing M4/M5.3 findings into M5.4 case seeds."""

from __future__ import annotations

from award_audit.agent.harness.models import CaseSeed
from award_audit.core.models.issue import Issue, Severity
from award_audit.core.models.record import ImportedFile
from award_audit.core.pipeline.checks.l5_precheck import SearchHandoff


def seed_from_search_handoff(batch_id: int, handoff: SearchHandoff) -> CaseSeed:
    return CaseSeed(
        batch_id=batch_id,
        resource_code=handoff.resource_code,
        award_name=handoff.award_name,
        year=handoff.year,
        trigger_codes=[handoff.trigger_code],
        objective=handoff.objective,
        known_urls=handoff.known_urls,
    )


def seed_from_soft_rule(
    batch_id: int,
    issue: Issue,
    imported: ImportedFile,
) -> CaseSeed | None:
    """Only L5S review issues become soft-rule cases; ordinary L0-L4 issues never do."""

    if issue.severity != Severity.REVIEW or issue.rule_id not in {"L5S-01", "L5S-02"}:
        return None
    code = imported.first_zylbm
    if not code:
        return None
    summary = {
        "rule_id": issue.rule_id,
        "file": issue.file,
        "sheet": issue.sheet,
        "row": issue.row,
        "field_code": issue.field_code,
        "issue_summary": issue.message[:500],
    }
    return CaseSeed(
        batch_id=batch_id,
        resource_code=code,
        award_name=imported.award_name,
        year=imported.year,
        trigger_codes=["SOFT_RULE_SUSPECT"],
        objective="核验软规则疑点的字段语义，形成可追溯建议或转人工认定",
        submitted_summary=summary,
        open_questions=["该疑点是否为真实数据错误，还是该奖项允许的特殊填报口径？"],
    )


def seeds_from_file_issues(
    batch_id: int,
    imported: ImportedFile,
    issues: list[Issue],
) -> list[CaseSeed]:
    """Create soft-rule cases only when deterministic blockers do not settle the file."""

    if any(issue.severity == Severity.BLOCKER for issue in issues):
        return []
    seeds: list[CaseSeed] = []
    for issue in issues:
        seed = seed_from_soft_rule(batch_id, issue, imported)
        if seed is not None:
            seeds.append(seed)
    return seeds
