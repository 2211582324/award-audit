"""采集清单加载器：读 网络数据采集-2025 → dict[资源项码 -> LedgerEntry]。

作用：
- L3 查全：提交文件实际行数 vs 应采数量（采集数量条）——错误清单 #9。
- L5 来源核查（M4）：采集网址 就是官网 URL，喂给联网核查 Agent。
应采数量列可能是数字/"所有"/空，解析不出整数时记为 None（该资源项跳过 L3 数量核查）。
"""

from __future__ import annotations

import re
from pathlib import Path

import openpyxl
from pydantic import BaseModel

from award_audit.core import config

_INT = re.compile(r"\d+")


class LedgerEntry(BaseModel):
    """采集清单一行：某资源项的应采/交付数量与官网网址。"""

    resource_code: str
    resource_name: str = ""
    expected_count: int | None = None   # 采集数量（条），解析不出为 None
    delivered_count: int | None = None  # 交付数量（条）
    collect_url: str = ""               # 采集网址（官网，供 L5）
    source: str = ""                    # 数据来源
    category: str = ""                  # 资源类别


# 从单元格解析非负整数：纯数字或含数字文本取首个数字；"所有"/空 -> None
def _to_int(v: object) -> int | None:
    if v is None:
        return None
    if isinstance(v, int | float):
        return int(v)
    m = _INT.search(str(v))
    return int(m.group(0)) if m else None


# 按表头中文名定位各列序号（抗列序变动）
def _locate(header: tuple[object, ...]) -> dict[str, int]:
    want = ["资源项码", "资源项", "资源类别", "数据来源", "采集网址", "采集数量（条）", "交付数量（条）"]
    idx: dict[str, int] = {}
    for i, v in enumerate(header):
        name = "" if v is None else str(v).strip()
        if name in want:
            idx[name] = i
    return idx


# 安全取单元格字符串
def _cell(row: tuple[object, ...], idx: int | None) -> str:
    if idx is None or idx >= len(row) or row[idx] is None:
        return ""
    return str(row[idx]).strip()


# 加载采集清单 -> dict[资源项码 -> LedgerEntry]（Sheet1）
def load_ledger(path: Path | None = None) -> dict[str, LedgerEntry]:
    p = path or config.ledger_path()
    wb = openpyxl.load_workbook(p, read_only=True, data_only=True)
    result: dict[str, LedgerEntry] = {}
    try:
        ws = wb.worksheets[0]
        rows = ws.iter_rows(min_row=1, values_only=True)
        header = next(rows)
        col = _locate(header)
        if "资源项码" not in col:
            raise ValueError(f"采集清单缺少'资源项码'列，实际表头: {header}")
        for r in rows:
            code = _cell(r, col.get("资源项码"))
            if not code or set(code) == {"*"}:  # 跳过空行与被打码的示例行
                continue
            result[code] = LedgerEntry(
                resource_code=code,
                resource_name=_cell(r, col.get("资源项")),
                expected_count=_to_int(r[col["采集数量（条）"]]) if "采集数量（条）" in col else None,
                delivered_count=_to_int(r[col["交付数量（条）"]]) if "交付数量（条）" in col else None,
                collect_url=_cell(r, col.get("采集网址")),
                source=_cell(r, col.get("数据来源")),
                category=_cell(r, col.get("资源类别")),
            )
    finally:
        wb.close()
    return result
