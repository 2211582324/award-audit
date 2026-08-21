"""生成"脏样本"批次，用于演示核查规则确实能抓到问题（真实 提交-27 数据本身干净）。

运行：PYTHONPATH=src python scripts/fixtures/make_dirty_sample.py
产出：samples/提交-脏样本/ 下两个故意做错的文件，随后可 award-audit check samples/提交-脏样本 看抓取效果。
"""

from __future__ import annotations

from pathlib import Path

import openpyxl

XWLWHJ_CODES = [
    "ZYLBM", "BZ", "ZYLB", "SSDM", "SSMC", "XXDM", "MLM", "YJXKM", "YJXKMC",
    "XKDM", "XKMC", "LWTM", "LWGJC", "ZZXM", "DSXM", "XWJB", "PDNY", "PDJG", "XDWMC",
]
XWLWHJ_NAMES = [
    "资源项码", "备注", "资源项", "省市代码", "省市名称", "学校代码", "门类码", "一级学科码", "一级学科名称",
    "学科码", "学科名称", "论文题目", "论文关键词", "作者姓名", "导师姓名", "学位级别", "评定年月", "评定机构", "学校名称",
]


# 由 {字段代码:值} 构造一整行（缺字段补空）
def _row(d: dict[str, str]) -> list[str]:
    return [d.get(c, "") for c in XWLWHJ_CODES]


# 写一个 XWLWHJ 结构的 xlsx（两行表头 + 数据行）
def _write(path: Path, sheet: str, data: list[dict[str, str]]) -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = sheet
    ws.append(XWLWHJ_CODES)
    ws.append(XWLWHJ_NAMES)
    for d in data:
        ws.append(_row(d))
    wb.save(path)


def main() -> None:
    out = Path(__file__).resolve().parents[2] / "samples" / "提交-脏样本"
    out.mkdir(parents=True, exist_ok=True)

    award = "中国人工智能学会优秀博士学位论文"
    # 文件A：模板用对、但注入 3 类内容错误（L0-06 范例残留 / L1-04 名字空格 / L2-02 年份不符）
    file_a = out / "CON_GG_XK_RCPY_XWLWHJ-中国人工智能学会优秀博士学位论文-2024.xlsx"
    _write(file_a, "CON_GG_XK_RCPY_XWLWHJ-学位论文获奖信息_", [
        {"ZYLBM": "04050014", "ZYLB": award, "LWTM": "正常论文一", "ZZXM": "周雄", "DSXM": "刘贤明", "XWJB": "博士", "PDNY": "2024", "PDJG": "中国人工智能学会", "XDWMC": "哈尔滨工业大学"},
        {"ZYLBM": "04050014", "ZYLB": award, "LWTM": "名字含空格", "ZZXM": "张 楠", "XWJB": "博士", "PDNY": "2024", "PDJG": "中国人工智能学会", "XDWMC": "某大学"},  # L1-04
        {"ZYLBM": "04050014", "ZYLB": award, "LWTM": "年份对不上", "ZZXM": "李四", "XWJB": "博士", "PDNY": "2025", "PDJG": "中国人工智能学会", "XDWMC": "某大学"},  # L2-02
        {"ZYLBM": "04050014", "ZYLB": award, "BZ": "填写范例，请删除", "LWTM": "范例残留", "ZZXM": "示例", "PDNY": "2024"},  # L0-06
    ])

    # 文件B：模板用错——资源项码 05020005（国家杰出青年→应用 KYXM 模板），却套了 XWLWHJ 模板与文件名
    file_b = out / "CON_GG_XK_RCPY_XWLWHJ-国家杰出青年科学基金-2025.xlsx"
    _write(file_b, "CON_GG_XK_RCPY_XWLWHJ-学位论文获奖信息_", [
        {"ZYLBM": "05020005", "ZYLB": "国家杰出青年科学基金", "LWTM": "x", "ZZXM": "王五", "PDNY": "2025"},  # L0-02
    ])

    print(f"已生成脏样本：{out}")
    print("  A:", file_a.name, "（应抓 L0-06 / L1-04 / L2-02）")
    print("  B:", file_b.name, "（应抓 L0-02 模板用错）")


if __name__ == "__main__":
    main()
