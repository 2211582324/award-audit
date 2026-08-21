"""L0 文件规范核查（L0-01 ~ L0-07）。

最关键两条：
- L0-02 模板用对：资源项码 → 1016 映射表 → "应该用的公共表表名"，与文件名前缀比对（错误 #7）。
- L0-06 范例残留：提交文件不应保留模板自带的"填写范例，请删除"行。
"""

from __future__ import annotations

import re

from award_audit.core.models.issue import Issue, make_issue
from award_audit.core.models.record import ImportedFile
from award_audit.core.models.template import TemplateSpec
from award_audit.core.reference.resource_map import ResourceMapEntry

_YEAR4 = re.compile(r"^\d{4}$")


# 跑完整 L0 文件规范核查
def run(
    imported: ImportedFile,
    claimed_spec: TemplateSpec | None,
    resource_map: dict[str, ResourceMapEntry],
) -> list[Issue]:
    issues: list[Issue] = []
    b, f, s = imported.batch, imported.file_name, imported.sheet_name

    # L0-07 结构：至少要有表头
    if not imported.header_codes:
        issues.append(make_issue("L0-07", batch=b, file=f, sheet=s,
                                  message="未解析到表头（前两行应为字段代码/中文名）"))
        return issues  # 无表头，后续规则无从谈起

    # L0-01 文件名格式
    if not imported.claimed_table_code.startswith("CON_") or not imported.award_name or not _YEAR4.match(imported.year):
        issues.append(make_issue("L0-01", batch=b, file=f, sheet=s,
                                  message="文件名不符合 <公共表表名>-<奖项名>-<年份>.xlsx",
                                  current_value=imported.file_name))

    # L0-02 模板用对：资源项码 → 映射表 → 应用模板，与文件名前缀比对
    zylbm = imported.first_zylbm
    if zylbm:
        entry = resource_map.get(zylbm)
        if entry and entry.table_code and entry.table_code != imported.claimed_table_code:
            issues.append(make_issue(
                "L0-02", batch=b, file=f, sheet=s, field_code="ZYLBM",
                message=f"模板用错：资源项码 {zylbm}（{entry.resource_name}）应用模板 {entry.table_code}，实际用了 {imported.claimed_table_code}",
                current_value=imported.claimed_table_code, suggestion=entry.table_code))

    # L0-03 sheet 名含公共表表名
    if imported.claimed_table_code not in imported.sheet_name:
        issues.append(make_issue("L0-03", batch=b, file=f, sheet=s,
                                  message=f"sheet 名未包含公共表表名 {imported.claimed_table_code}",
                                  current_value=imported.sheet_name))

    # L0-04 表头字段代码严格等于模板
    if claimed_spec is None:
        issues.append(make_issue("L0-04", batch=b, file=f, sheet=s,
                                  message=f"文件名模板代码 {imported.claimed_table_code} 在模板库中不存在，无法校验表头",
                                  current_value=imported.claimed_table_code))
    else:
        if imported.header_codes != claimed_spec.field_codes:
            miss = [c for c in claimed_spec.field_codes if c not in imported.header_codes]
            extra = [c for c in imported.header_codes if c not in claimed_spec.field_codes]
            detail = []
            if miss:
                detail.append(f"缺字段 {miss}")
            if extra:
                detail.append(f"多字段 {extra}")
            if not detail:
                detail.append("字段顺序与模板不一致")
            issues.append(make_issue("L0-04", batch=b, file=f, sheet=s,
                                      message="表头字段代码与模板不一致：" + "；".join(detail)))
        # L0-05 表头中文名与模板一致（格式级）
        elif imported.header_names != [claimed_spec.name_of(c) for c in imported.header_codes]:
            issues.append(make_issue("L0-05", batch=b, file=f, sheet=s,
                                      message="表头中文名与模板不一致"))

    # L0-06 范例残留：数据区任一单元格含"填写范例"
    for ri, row in enumerate(imported.rows):
        if any("填写范例" in cell for cell in row):
            issues.append(make_issue("L0-06", batch=b, file=f, sheet=s, row=ri + 1,
                                      message="残留模板范例行（含“填写范例，请删除”），提交前应删除"))

    return issues
