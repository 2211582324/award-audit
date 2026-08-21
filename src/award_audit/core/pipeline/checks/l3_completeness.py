"""L3 查全核查（L3-01/02，需采集清单）：数量对不对——批次级聚合。

关键设计：同一资源项可能分多个文件提交（如"高等教育学优秀博士学位论文"分 2018/2019 两个文件），
采集清单的"应采数量"是该资源项的总量，所以必须**按资源项码把整个批次的行数汇总**再与应采数量比，
逐文件比对会对多文件资源项系统性误报。命中时给该资源项的**每个成员文件**各挂一条（入库门禁一致）。
资源项不在清单、或应采数量未填（如"所有"）时跳过——无基准不硬判。
"""

from __future__ import annotations

from award_audit.core.models.issue import Issue, make_issue
from award_audit.core.models.record import ImportedFile
from award_audit.core.reference.ledger import LedgerEntry


# 跑 L3 查全核查（批次级：按资源项码聚合全部文件）
def run_batch(files: list[ImportedFile], ledger: dict[str, LedgerEntry]) -> list[Issue]:
    issues: list[Issue] = []

    # 按资源项码分组
    groups: dict[str, list[ImportedFile]] = {}
    for f in files:
        code = f.first_zylbm
        if code:
            groups.setdefault(code, []).append(f)

    for code, members in groups.items():
        entry = ledger.get(code)
        if entry is None or entry.expected_count is None:
            continue  # 清单无此项或未填应采数量，无基准可比
        exp = entry.expected_count
        total = sum(m.n_rows for m in members)
        if total != exp:
            diff = exp - total
            detail = f"缺 {diff} 条" if diff > 0 else f"超 {-diff} 条"
            scope = (
                f"本批次该资源项共 {len(members)} 个文件合计 {total} 行"
                if len(members) > 1 else f"实际 {total} 行"
            )
            for m in members:
                issues.append(make_issue(
                    "L3-01", batch=m.batch, file=m.file_name, sheet=m.sheet_name,
                    field_code="ZYLBM",
                    message=f"采集数量不符：{scope}，应采 {exp} 条（{detail}）",
                    current_value=str(total), suggestion=str(exp)))

        # L3-02 清单自查：交付数量与应采数量不符（每资源项报一次，挂首个成员文件）
        if entry.delivered_count is not None and entry.delivered_count != exp:
            m0 = members[0]
            issues.append(make_issue(
                "L3-02", batch=m0.batch, file=m0.file_name, sheet=m0.sheet_name,
                message=f"采集清单内交付数量 {entry.delivered_count} ≠ 应采数量 {exp}",
                current_value=str(entry.delivered_count), suggestion=str(exp)))
    return issues
