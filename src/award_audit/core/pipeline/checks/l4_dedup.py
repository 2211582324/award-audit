"""L4 去重核查（L4-01/02）：文件内重复 + 跨批次重复。

L4-01 文件内重复：同一文件出现相同去重键（业务主键）的多行。
L4-02 跨批次重复：去重键已在正式库（is_current）——需台账，无台账时 current_keys 传空集即不触发。
空去重键（所有组成列都空）不参与判定，避免误判。
"""

from __future__ import annotations

from award_audit.core.identity import IDENTITY_SEPARATOR, build_profile_identity
from award_audit.core.models.issue import Issue, make_issue
from award_audit.core.models.record import ImportedFile
from award_audit.core.models.template import TemplateSpec, resolve_identity_profile
from award_audit.core.pipeline.dedup import dedup_key, is_empty_key, row_data


# 跑 L4 去重核查；current_keys 为正式库当前有效去重键集合（跨批次判定用）
def run(
    imported: ImportedFile,
    spec: TemplateSpec | None,
    current_keys: set[str] | None = None,
) -> list[Issue]:
    issues: list[Issue] = []
    b, f, s = imported.batch, imported.file_name, imported.sheet_name
    existing = current_keys or set()

    occurrences: dict[str, list[int]] = {}
    conflict_groups: dict[str, list[tuple[int, str]]] = {}
    profile = (
        resolve_identity_profile(spec)
        if spec is not None and spec.identity_profile is not None
        else None
    )
    for ri in range(imported.n_rows):
        key = dedup_key(imported, ri, spec)
        row_no = ri + 1
        if profile is not None:
            identity = build_profile_identity(row_data(imported, ri), profile)
            if identity is None:
                expected = " 或 ".join(
                    "+".join(fields) for fields in profile.primary_alternatives
                )
                issues.append(make_issue(
                    "L4-04", batch=b, file=f, sheet=s, row=row_no,
                    message=f"该行未满足身份方案（{expected}），无法可靠判重",
                ))
            elif profile.conflict_fields:
                resolved = IDENTITY_SEPARATOR.join(
                    part for part in (identity.key, identity.discriminator_key) if part
                )
                conflict_groups.setdefault(resolved, []).append(
                    (row_no, identity.conflict_key)
                )
        if is_empty_key(key):
            continue
        occurrences.setdefault(key, []).append(row_no)

        # L4-02 跨批次重复（已在正式库）——逐行报，promote 闸门按行拦
        if key in existing:
            issues.append(make_issue("L4-02", batch=b, file=f, sheet=s, row=row_no,
                                     message="该记录去重键已在正式库，疑似跨批次重复入库"))

    # L4-01 文件内重复：按重复组聚合报一条（row=第二次出现行），避免一组 N 行刷 N-1 条
    for occurrence_rows in occurrences.values():
        if len(occurrence_rows) > 1:
            more = (
                f"，另见行 {occurrence_rows[2:]}"
                if len(occurrence_rows) > 2
                else ""
            )
            issues.append(make_issue(
                "L4-01", batch=b, file=f, sheet=s, row=occurrence_rows[1],
                message=(
                    f"与第 {occurrence_rows[0]} 行重复"
                    f"（该去重键共 {len(occurrence_rows)} 行{more}）"
                ),
            ))
    for conflict_rows in conflict_groups.values():
        if len(conflict_rows) < 2:
            continue
        first_row, first_conflict = conflict_rows[0]
        for row_no, conflict_key in conflict_rows[1:]:
            if conflict_key != first_conflict:
                issues.append(make_issue(
                    "L4-03", batch=b, file=f, sheet=s, row=row_no,
                    message=(
                        f"与第 {first_row} 行主身份相同，但"
                        f"{'+'.join(profile.conflict_fields) if profile else '校验字段'}不一致，需核对"
                    ),
                ))
    return issues
