"""模板注册表加载器：读 附件5 的 18 个模板 xlsx → dict[公共表表名 -> TemplateSpec]。

加载算法（实施方案 §2.1）：
1. 从文件名解析公共表表名（<表码>-<中文名>.xlsx，表码不含 '-'）；非 CON_ 开头的（院士表）走特例，M1 跳过。
2. 读第1行字段代码、第2行中文名，**去尾部空列**（多个模板 max_column 被撑大）。
3. 检测重复字段代码（如 YXJC 的 XSMQK/ZZSMQK）。
4. 合并 TYPE_REGISTRY 的列角色，推导候选必填。
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import openpyxl

from award_audit.core import config
from award_audit.core.models.template import (
    IDENTITY_PROFILES,
    TYPE_REGISTRY,
    TemplateSpec,
    type_suffix,
)


# 从模板文件名解析公共表表名：CON_GG_XK_RCPY_XWLWHJ-学位论文获奖信息.xlsx -> CON_GG_XK_RCPY_XWLWHJ
def _table_code_from_filename(stem: str) -> str:
    return stem.split("-", 1)[0]


# 读一行并去掉尾部空单元格，返回字符串列表（None/空 -> ""）
def _row_values(row: tuple[object, ...], ncol: int) -> list[str]:
    return ["" if row[i] is None else str(row[i]).strip() for i in range(ncol)]


# 计算表头真实列数：第1行最后一个非空单元格的位置
def _real_ncol(header: tuple[object, ...]) -> int:
    ncol = 0
    for i, v in enumerate(header):
        if v is not None and str(v).strip() != "":
            ncol = i + 1
    return ncol


# 由字段代码/中文名构建 TemplateSpec（含重复列检测 + 列角色合并），加载器与测试共用
def build_template_spec(
    table_code: str, sheet_name: str, codes: list[str], names: list[str]
) -> TemplateSpec:
    # 重复字段代码（容错记录，不崩溃）
    dup = [c for c, n in Counter(codes).items() if c and n > 1]
    # 字段代码 -> 中文名（首次出现为准）
    field_names: dict[str, str] = {}
    for c, n in zip(codes, names, strict=True):
        if c and c not in field_names:
            field_names[c] = n
    spec = TemplateSpec(
        table_code=table_code,
        sheet_name=sheet_name,
        field_codes=codes,
        field_names=field_names,
        dup_columns=dup,
    )
    _apply_type_roles(spec)
    return spec


# 加载单个模板 xlsx -> TemplateSpec；非标准（无 CON 码/无表头）返回 None
def load_one_template(path: Path) -> TemplateSpec | None:
    table_code = _table_code_from_filename(path.stem)
    if not table_code.startswith("CON_"):
        return None  # 世界主要国家院士等特例，M1 不纳入通用核查
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        ws = wb.worksheets[0]
        rows = list(ws.iter_rows(min_row=1, max_row=2, values_only=True))
        if len(rows) < 2:
            return None
        ncol = _real_ncol(rows[0])
        codes = _row_values(rows[0], ncol)
        names = _row_values(rows[1], ncol)
        sheet_name = ws.title
    finally:
        wb.close()
    return build_template_spec(table_code, sheet_name, codes, names)


# 合并 TYPE_REGISTRY 的列角色到 spec，并推导候选必填与多值列（就地修改）
def _apply_type_roles(spec: TemplateSpec) -> None:
    suffix = type_suffix(spec.table_code)
    roles = TYPE_REGISTRY.get(suffix)
    if roles is None:
        return
    present = set(spec.field_codes)
    spec.title_col = roles.title_col if (roles.title_col in present) else None
    spec.name_cols = [c for c in roles.name_cols if c in present]
    spec.org_cols = [c for c in roles.org_cols if c in present]
    spec.year_cols = [c for c in roles.year_cols if c in present]
    spec.grade_cols = [c for c in roles.grade_cols if c in present]
    # 多值列：人名列 + 机构列（这些列可能用 ; 承载多人/多单位）
    spec.multi_value_cols = spec.name_cols + spec.org_cols
    profile = IDENTITY_PROFILES.get(suffix)
    spec.identity_profile = profile
    # 候选必填由示例驱动身份决定。存在多个回退方案时不能把其中任一列硬报为空；
    # 单一方案才逐字段提示，主年份仍保留格式检查。
    parts: list[str] = []
    if profile is not None and len(profile.primary_alternatives) == 1:
        parts.extend(profile.primary_alternatives[0])
    year = spec.primary_year_col
    if year:
        parts.append(year)
    spec.required_candidate = [
        c for c in dict.fromkeys(parts) if c in present and c not in spec.hard_required
    ]
    if profile is not None:
        primary_columns = [
            column
            for alternative in profile.primary_alternatives
            for column in alternative
        ]
        identity_columns = [
            *profile.scope_fields,
            *primary_columns,
            *profile.discriminator_fields,
            *profile.conflict_fields,
            *profile.occurrence_fields,
        ]
        spec.dedup_key_cols = [
            c for c in dict.fromkeys(identity_columns) if c in present
        ]
    elif roles.dedup_cols:
        spec.dedup_key_cols = [c for c in roles.dedup_cols if c in present]
    else:
        first_name = spec.name_cols[0] if spec.name_cols else None
        dk_raw = ["ZYLB", spec.primary_year_col, spec.title_col, first_name]
        spec.dedup_key_cols = list(dict.fromkeys([c for c in dk_raw if c and c in present]))


# 加载模板目录下全部标准模板 -> dict[公共表表名 -> TemplateSpec]
def load_template_registry(templates_dir: Path | None = None) -> dict[str, TemplateSpec]:
    d = templates_dir or config.templates_dir()
    registry: dict[str, TemplateSpec] = {}
    for path in sorted(d.glob("*.xlsx")):
        spec = load_one_template(path)
        if spec is not None:
            registry[spec.table_code] = spec
    return registry
