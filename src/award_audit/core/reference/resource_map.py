"""资源项码映射表加载器：读 数据资源对应模板-1016 → dict[资源项码 -> ResourceMapEntry]。

作用：这是判断"模板用没用对"（错误清单 #7 / 规则 L0-02）的**权威**——
提交文件的资源项码(ZYLBM) → 查此表得"应该用的公共表表名" → 与文件名前缀、表头结构比对。
资源项码按文本读、保前导零（如 04050014），否则与提交文件的文本码对不上。
"""

from __future__ import annotations

from pathlib import Path

import openpyxl
from pydantic import BaseModel

from award_audit.core import config


class ResourceMapEntry(BaseModel):
    """一条资源项 → 应用模板的映射。"""

    resource_code: str    # 资源项码，如 04050014
    resource_name: str    # 资源项，如 中国人工智能学会优秀博士学位论文
    table_code: str       # 公共表表名，如 CON_GG_XK_RCPY_XWLWHJ
    category_code: str = ""  # 资源类别码
    category: str = ""       # 资源类别


# 按表头中文名定位各列序号（抗列序变动），缺列返回 None
def _locate_columns(header: tuple[object, ...]) -> dict[str, int]:
    want = ["资源项码", "资源项", "公共表表名", "资源类别码", "资源类别"]
    idx: dict[str, int] = {}
    for i, v in enumerate(header):
        name = "" if v is None else str(v).strip()
        if name in want:
            idx[name] = i
    return idx


# 加载 1016 映射表 -> dict[资源项码 -> ResourceMapEntry]
def load_resource_map(path: Path | None = None) -> dict[str, ResourceMapEntry]:
    p = path or config.resource_map_path()
    wb = openpyxl.load_workbook(p, read_only=True, data_only=True)
    result: dict[str, ResourceMapEntry] = {}
    try:
        ws = wb.worksheets[0]
        rows = ws.iter_rows(min_row=1, values_only=True)
        header = next(rows)
        col = _locate_columns(header)
        if "资源项码" not in col or "公共表表名" not in col:
            raise ValueError(f"1016 映射表缺少必要列，实际表头: {header}")
        for r in rows:
            code = r[col["资源项码"]]
            if code is None or str(code).strip() == "":
                continue
            code_s = str(code).strip()  # 保前导零：源表若存为文本则原样；存为数字会丢零（数据源问题）
            result[code_s] = ResourceMapEntry(
                resource_code=code_s,
                resource_name=_cell(r, col.get("资源项")),
                table_code=_cell(r, col.get("公共表表名")),
                category_code=_cell(r, col.get("资源类别码")),
                category=_cell(r, col.get("资源类别")),
            )
    finally:
        wb.close()
    return result


# 安全取单元格字符串：列不存在或空 -> ""
def _cell(row: tuple[object, ...], idx: int | None) -> str:
    if idx is None or idx >= len(row) or row[idx] is None:
        return ""
    return str(row[idx]).strip()
