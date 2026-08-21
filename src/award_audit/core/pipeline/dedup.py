"""去重键计算：把一行数据压成"业务主键"字符串，用于文件内/跨批次去重与版本化定位。

去重键 = 模板登记的 dedup_key_cols（奖项+年份+名称+首个人名）各列值用不可见分隔符拼接。
无登记则退化为整行拼接。分隔符用 \\x1f（单元分隔符），避免与数据内容冲突。
"""

from __future__ import annotations

from collections.abc import Mapping

from award_audit.core.identity import build_business_identity_key
from award_audit.core.models.record import ImportedFile
from award_audit.core.models.template import TemplateSpec, resolve_identity_profile

_SEP = "\x1f"


# 计算某行的去重键
def dedup_key(imported: ImportedFile, row_idx: int, spec: TemplateSpec | None) -> str:
    if spec is not None and spec.identity_profile is not None:
        return build_business_identity_key(
            row_data(imported, row_idx), resolve_identity_profile(spec)
        )
    cols = spec.dedup_key_cols if (spec and spec.dedup_key_cols) else imported.header_codes
    return _SEP.join(imported.value(row_idx, c).strip() for c in cols)


def dedup_key_from_mapping(data: Mapping[str, object], spec: TemplateSpec | None) -> str:
    """Recompute the current identity key from persisted row JSON."""

    if spec is not None and spec.identity_profile is not None:
        return build_business_identity_key(data, resolve_identity_profile(spec))
    if spec is None:
        return ""
    return _SEP.join(str(data.get(column, "") or "").strip() for column in spec.dedup_key_cols)


# 取某行的完整数据字典（字段代码 -> 值），用于入库/审计
def row_data(imported: ImportedFile, row_idx: int) -> dict[str, str]:
    return {c: imported.value(row_idx, c) for c in imported.header_codes}


# 去重键是否"空"（所有组成列都为空）——空键不参与去重，避免误判
def is_empty_key(key: str) -> bool:
    return key.replace(_SEP, "").strip() == ""
