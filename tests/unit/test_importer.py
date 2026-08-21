"""批次导入器测试：文件名解析 + 前导零保留 + 空行跳过（用 tmp_path 造真实 xlsx）。"""

from __future__ import annotations

from pathlib import Path

import openpyxl

from award_audit.core.pipeline.importer import import_file, parse_filename


# 在 tmp 目录造一个最小 xlsx（两行表头 + 数据行）
def _make_xlsx(path: Path, codes: list[str], names: list[str], data: list[list[str]]) -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "CON_GG_XK_RCPY_XWLWHJ-学位论文获奖信息_"
    ws.append(codes)
    ws.append(names)
    for r in data:
        ws.append(r)
    wb.save(path)


# 功能：验证文件名按 <表码>-<奖项名>-<年份> 正确三分（奖项名含连字符也不误切）
# 设计：末段作年份、首段作表码、中间 join 作奖项名，覆盖多段情形
def test_parse_filename() -> None:
    assert parse_filename("CON_GG_XK_RCPY_XWLWHJ-中国人工智能学会优秀博士学位论文-2024") == (
        "CON_GG_XK_RCPY_XWLWHJ", "中国人工智能学会优秀博士学位论文", "2024",
    )


# 功能：验证资源项码前导零在导入后不丢失
# 设计：写入字符串 "04050014"，读回断言仍为 "04050014"，守住“按文本读”这条底线
def test_import_file_preserves_leading_zero(tmp_path) -> None:
    p = tmp_path / "CON_GG_XK_RCPY_XWLWHJ-某奖-2024.xlsx"
    _make_xlsx(p, ["ZYLBM", "ZYLB"], ["资源项码", "资源项"],
               [["04050014", "某奖"], ["04050014", "某奖"]])
    imp = import_file(p, "提交-T")
    assert imp.first_zylbm == "04050014"
    assert imp.n_rows == 2
    assert imp.claimed_table_code == "CON_GG_XK_RCPY_XWLWHJ"


# 功能：验证整行全空的数据行被跳过
# 设计：夹一行全空，断言只保留有内容的行，避免尾部空行污染行数
def test_import_file_skips_blank_rows(tmp_path) -> None:
    p = tmp_path / "CON_GG_XK_RCPY_XWLWHJ-某奖-2024.xlsx"
    _make_xlsx(p, ["ZYLBM", "ZYLB"], ["资源项码", "资源项"],
               [["04050014", "某奖"], ["", ""], ["04050014", "某奖"]])
    imp = import_file(p, "提交-T")
    assert imp.n_rows == 2
