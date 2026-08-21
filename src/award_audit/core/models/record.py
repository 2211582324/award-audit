"""导入文件（ImportedFile）模型：一个提交 xlsx 解析后的结构。

设计：数据区用"定长行 + 按代码首匹配取值"存储，不用 dict[代码->值]——因为个别模板有重复字段代码
（YXJC 的 XSMQK/ZZSMQK 都叫"作者署名情况"），dict 会互相覆盖丢数据。定长行保全所有列。
文件名解析出的 ``claimed_table_code`` 是乙方"声称"用的模板，核查时要与资源项码映射出的"真相"比对（L0-02）。
"""

from __future__ import annotations

from pydantic import BaseModel


class ImportedFile(BaseModel):
    """一个提交文件的原始快照：绝不修改，出问题能对照乙方到底给了什么。"""

    batch: str                    # 批次名，如 提交-27
    path: str                     # 原始文件绝对路径
    file_name: str                # 文件名（不含路径）
    claimed_table_code: str       # 文件名解析出的公共表表名（乙方声称）
    award_name: str               # 文件名解析出的奖项名
    year: str                     # 文件名解析出的年份
    sheet_name: str               # 首个 sheet 名
    header_codes: list[str]       # 第1行字段代码（已去尾部空列）
    header_names: list[str]       # 第2行中文名（与 header_codes 对齐）
    rows: list[list[str]]         # 数据区（第3行起），行×列，与 header_codes 对齐，值已转 str、空为 ""

    # 数据行数
    @property
    def n_rows(self) -> int:
        return len(self.rows)

    # 某字段代码在表头的首个列序号，不存在返回 None
    def idx_of(self, code: str) -> int | None:
        try:
            return self.header_codes.index(code)
        except ValueError:
            return None

    # 取第 row_idx 行、字段 code 的值（首匹配列），越界或无此列返回 ""
    def value(self, row_idx: int, code: str) -> str:
        j = self.idx_of(code)
        if j is None:
            return ""
        row = self.rows[row_idx]
        return row[j] if j < len(row) else ""

    # 首行资源项码（ZYLBM），用于查映射表定位应用模板；无数据返回 ""
    @property
    def first_zylbm(self) -> str:
        return self.value(0, "ZYLBM") if self.rows else ""
