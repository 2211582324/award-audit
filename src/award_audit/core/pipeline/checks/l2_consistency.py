"""L2 一致性核查（L2-01 ~ L2-03）：文件名与表内数据是否自洽。

注意：L2-02 只能查"文件名年份 vs 表内年份列是否自洽"，查不了"客观上到底哪年才对"——后者需联网（L5-04）。
为避免同一系统性错误刷屏，按"去重后每个不同值报一次"，并带出现次数。
"""

from __future__ import annotations

from collections import Counter

from award_audit.core.models.issue import Issue, make_issue
from award_audit.core.models.record import ImportedFile
from award_audit.core.models.template import TemplateSpec
from award_audit.core.pipeline.checks import _util


# 跑完整 L2 一致性核查
def run(imported: ImportedFile, claimed_spec: TemplateSpec | None) -> list[Issue]:
    issues: list[Issue] = []
    b, f, s = imported.batch, imported.file_name, imported.sheet_name
    if imported.n_rows == 0:
        return issues

    # L2-03 资源项码文件内一致
    zylbm_vals = [imported.value(ri, "ZYLBM").strip() for ri in range(imported.n_rows)]
    distinct_zylbm = sorted({v for v in zylbm_vals if v})
    if len(distinct_zylbm) > 1:
        issues.append(make_issue("L2-03", batch=b, file=f, sheet=s, field_code="ZYLBM",
                                 message=f"同一文件出现多个资源项码：{distinct_zylbm}",
                                 current_value="；".join(distinct_zylbm)))

    # L2-01 文件名奖项名 == 表内资源项(ZYLB)
    zylb_counter = Counter(imported.value(ri, "ZYLB").strip() for ri in range(imported.n_rows))
    for zylb_val, cnt in zylb_counter.items():
        if zylb_val and zylb_val != imported.award_name.strip():
            issues.append(make_issue("L2-01", batch=b, file=f, sheet=s, field_code="ZYLB",
                                     message=f"文件名奖项名与表内资源项(ZYLB)不一致（{cnt} 行）",
                                     current_value=zylb_val, suggestion=imported.award_name))

    # L2-02 文件名年份 == 表内主年份列（自洽）
    yc = claimed_spec.primary_year_col if claimed_spec else None
    if yc and imported.year:
        bad: Counter[str] = Counter()
        example_row: dict[str, int] = {}
        for ri in range(imported.n_rows):
            yval = imported.value(ri, yc)
            yr = _util.extract_year(yval)
            if yr and yr != imported.year:
                bad[yr] += 1
                example_row.setdefault(yr, ri + 1)
        for yr, cnt in bad.items():
            issues.append(make_issue("L2-02", batch=b, file=f, sheet=s, row=example_row[yr],
                                     field_code=yc, field_name=claimed_spec.name_of(yc) if claimed_spec else yc,
                                     message=f"文件名年份 {imported.year} 与表内年份 {yr} 不一致（{cnt} 行）",
                                     current_value=yr, suggestion=imported.year))

    return issues
