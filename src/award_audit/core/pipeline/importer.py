"""批次导入器：吃任意 提交-XX 文件夹 → list[ImportedFile]。

实测格式：文件名 <公共表表名>-<奖项名>-<年份>.xlsx；sheet 第1行字段代码、第2行中文名、第3行起数据。
关键：所有单元格按文本读，资源项码保前导零（04050014）；范例行不在此剔除（交给 L0-06 检查）。
"""

from __future__ import annotations

from pathlib import Path

import openpyxl

from award_audit.core.models.record import ImportedFile


# 单元格转字符串：None->""；整数值浮点去掉 .0（如 2024.0->"2024"）；其余原样 str
def _s(v: object) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


# 解析文件名 -> (表码, 奖项名, 年份)：按 '-' 切，首段=表码，末段=年份，中间=奖项名
def parse_filename(stem: str) -> tuple[str, str, str]:
    parts = stem.split("-")
    if len(parts) >= 3:
        return parts[0], "-".join(parts[1:-1]), parts[-1]
    if len(parts) == 2:
        return parts[0], parts[1], ""
    return stem, "", ""


# 计算表头真实列数：第1行最后一个非空单元格位置
def _real_ncol(header: tuple[object, ...]) -> int:
    ncol = 0
    for i, v in enumerate(header):
        if v is not None and str(v).strip() != "":
            ncol = i + 1
    return ncol


# 导入单个提交 xlsx -> ImportedFile
def import_file(path: Path, batch: str) -> ImportedFile:
    table_code, award_name, year = parse_filename(path.stem)
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        ws = wb.worksheets[0]
        all_rows = list(ws.iter_rows(values_only=True))
        sheet_name = ws.title
    finally:
        wb.close()

    if len(all_rows) < 2:
        # 没有表头，返回空壳，交给 L0 检查报"结构不对"
        return ImportedFile(
            batch=batch, path=str(path), file_name=path.name,
            claimed_table_code=table_code, award_name=award_name, year=year,
            sheet_name=sheet_name, header_codes=[], header_names=[], rows=[],
        )

    ncol = _real_ncol(all_rows[0])
    header_codes = [_s(all_rows[0][i]) for i in range(ncol)]
    header_names = [_s(all_rows[1][i]) for i in range(ncol)]

    rows: list[list[str]] = []
    for raw in all_rows[2:]:
        row = [_s(raw[i]) if i < len(raw) else "" for i in range(ncol)]
        if any(cell != "" for cell in row):  # 跳过整行全空
            rows.append(row)

    return ImportedFile(
        batch=batch, path=str(path), file_name=path.name,
        claimed_table_code=table_code, award_name=award_name, year=year,
        sheet_name=sheet_name, header_codes=header_codes, header_names=header_names, rows=rows,
    )


# 导入整个批次文件夹 -> list[ImportedFile]（忽略临时文件与非 xlsx）
def import_batch(folder: Path) -> list[ImportedFile]:
    batch = folder.name
    files: list[ImportedFile] = []
    for path in sorted(folder.glob("*.xlsx")):
        if path.name.startswith("~$"):  # Excel 临时锁文件
            continue
        files.append(import_file(path, batch))
    return files
