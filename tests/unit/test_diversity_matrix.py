"""阶段3 多样性矩阵：排名/认证类按「记录身份」(学校/学科/认证专业)组键核对，而非题目+人名。

锁两件事：① 身份型不再因 title 常量/空而全折叠或被跳行护栏丢弃（DXPM 回归 bug）；
② 提交侧与官网侧用同一核对形态档案组键、落同一中文名身份空间，能正确判一致/缺漏且不串键。
名单型(roster)的零回归由 test_agent_loop.py 现有用例保证，这里只测新增的身份型。
"""

from __future__ import annotations

from award_audit.agent import loop as loop_mod
from award_audit.agent.loop import verify_resource
from award_audit.agent.tools import Attachment, PageContent
from award_audit.core.models.template import TemplateSpec


class FakeLlm:
    """返回预设抽取结果的假 LLM（身份型走确定性认列，通常不会被调用）。"""

    def __init__(self, extraction):  # noqa: ANN001
        self.extraction = extraction

    def json_call(self, system, user, max_tokens=2000):  # noqa: ANN001, ANN201
        return self.extraction


# 用某类型 spec 的字段代码/中文名构造该类型的 ImportedFile（借 kit.build 的 codes/names 通路）
def _build(kit, spec: TemplateSpec, dict_rows):  # noqa: ANN001, ANN202
    return kit.build(dict_rows, codes=spec.field_codes,
                     names=[spec.field_names.get(c, c) for c in spec.field_codes],
                     table_code=spec.table_code, award="排名类奖项", year="2024")


# 把 fetch_page/download/parse 换成离线假件：页面带一个 Excel 附件、附件解析出给定网格
def _patch_excel(monkeypatch, grid_rows):  # noqa: ANN001
    page = PageContent(url="https://x.gov.cn/a", status=200, text="",
                       attachments=[Attachment(text="附件:名单(Excel)",
                                               url="https://x.gov.cn/m.xlsx", is_excel=True)])
    monkeypatch.setattr(loop_mod.tools, "fetch_page", lambda url, timeout=15.0: page)
    monkeypatch.setattr(loop_mod.tools, "download_file", lambda url, d, timeout=30.0, **kw: d / "m.xlsx")
    monkeypatch.setattr(loop_mod.tools, "parse_award_excel",
                        lambda p, max_rows=2000: {"sheet": "s", "n_rows": len(grid_rows), "rows": grid_rows})


# 功能：大学排名——官网排名表(排名|学校名称|国家,无题无人)3 校 = 提交 3 校 → "一致"、不再全折叠
# 设计：DXPM title_col=ZYLB 常量、name_cols=[]；旧逻辑会把每行判"无题无人"整表丢弃/键全碰撞。
#       断言 extracted_count==3(护栏放行 org-only 行)、verdict=="一致"且非"基本一致"(不折叠)、无 collapsed_rows
def test_dxpm_ranking_consistent_no_collapse(kit, dxpm_spec, tmp_path, monkeypatch) -> None:
    rows = [{"ZYLBM": "05010001", "ZYLB": "软科中国大学排名", "FBND": "2024",
             "ZHPM": str(i + 1), "XDWMC": sch}
            for i, sch in enumerate(["北京大学", "清华大学", "复旦大学"])]
    files = [_build(kit, dxpm_spec, rows)]
    _patch_excel(monkeypatch, [["排名", "学校名称", "国家"],
                               ["1", "北京大学", "中国"], ["2", "清华大学", "中国"],
                               ["3", "复旦大学", "中国"]])
    rep = verify_resource("05010001", files, ["https://x.gov.cn/a"], dxpm_spec,
                          FakeLlm({}), tmp_path)
    assert rep.verdict == "一致" and "基本一致" not in rep.verdict
    assert rep.extracted_count == 3 and rep.submitted_count == 3
    assert "collapsed_rows" not in rep.reason_codes
    assert rep.source_kind == "excel" and rep.confidence == "high"


# 功能：大学排名——官网 3 校、提交少 1 校 → "疑似缺漏"，缺的以学校名(展示文本)列出
# 设计：身份型 missing/extra 的展示文本须走身份列(学校名)而非空 title；断言 missing==["复旦大学"]
def test_dxpm_ranking_missing(kit, dxpm_spec, tmp_path, monkeypatch) -> None:
    rows = [{"ZYLBM": "05010001", "ZYLB": "软科中国大学排名", "FBND": "2024", "XDWMC": sch}
            for sch in ["北京大学", "清华大学"]]
    files = [_build(kit, dxpm_spec, rows)]
    _patch_excel(monkeypatch, [["排名", "学校名称", "国家"],
                               ["1", "北京大学", "中国"], ["2", "清华大学", "中国"],
                               ["3", "复旦大学", "中国"]])
    rep = verify_resource("05010001", files, ["https://x.gov.cn/a"], dxpm_spec,
                          FakeLlm({}), tmp_path)
    assert rep.verdict == "疑似缺漏" and rep.missing == ["复旦大学"]


# 功能：学科排名——学科名+学校名复合键(combine="all")一一对齐 → "一致"
# 设计：官网表头"学科名称|排名|学校名称"经 ranking 词表映射 title/grade/org；断言两条复合键都匹配
def test_xkpm_composite_key_consistent(kit, xkpm_spec, tmp_path, monkeypatch) -> None:
    rows = [{"ZYLBM": "05020001", "ZYLB": "软科学科排名", "XKMC": xk, "FBND": "2024", "XDWMC": sch}
            for xk, sch in [("计算机科学与技术", "清华大学"), ("数学", "北京大学")]]
    files = [_build(kit, xkpm_spec, rows)]
    _patch_excel(monkeypatch, [["学科名称", "排名", "学校名称"],
                               ["计算机科学与技术", "1", "清华大学"], ["数学", "1", "北京大学"]])
    rep = verify_resource("05020001", files, ["https://x.gov.cn/a"], xkpm_spec,
                          FakeLlm({}), tmp_path)
    assert rep.verdict == "一致" and rep.extracted_count == 2 and rep.missing == []


# 功能：认证信息——两校认证同一专业 → 学校+专业复合键不串键、各算一条 → "一致"
# 设计：RZXX 身份=XDWMC+TGRZZY(非常空的 TGRZDW)；若只用专业会串成一个键。断言 extracted_count==2 不碰撞
def test_rzxx_cert_two_schools_same_major_no_collision(kit, rzxx_spec, tmp_path, monkeypatch) -> None:
    rows = [{"ZYLBM": "08030001", "ZYLB": "工程教育专业认证",
             "TGRZDW": "", "TGRZZY": "机械工程", "XDWMC": sch}
            for sch in ["清华大学", "北京大学"]]
    files = [_build(kit, rzxx_spec, rows)]
    _patch_excel(monkeypatch, [["学校名称", "专业名称", "认证结论"],
                               ["清华大学", "机械工程", "通过"], ["北京大学", "机械工程", "通过"]])
    rep = verify_resource("08030001", files, ["https://x.gov.cn/a"], rzxx_spec,
                          FakeLlm({}), tmp_path)
    assert rep.verdict == "一致" and rep.extracted_count == 2 and rep.submitted_count == 2


# 功能：身份型仅取到非结构化来源(页面正文)→ 不硬比缺漏/多采，"无法核对"转人工 + identity_needs_excel
# 设计：排名/认证大表页面抽取易截断不可审计(§1-F)。页面有正文、LLM 返回条目、无 Excel 附件；
#       断言 verdict=="无法核对"、reason_codes 含 identity_needs_excel（守铁律：非结构化不出结论）
def test_identity_type_non_excel_source_to_human(kit, dxpm_spec, tmp_path, monkeypatch) -> None:
    page = PageContent(url="https://x.gov.cn/a", status=200, text="某排名榜网页正文……")
    monkeypatch.setattr(loop_mod.tools, "fetch_page", lambda url, timeout=15.0: page)
    rows = [{"ZYLBM": "05010001", "ZYLB": "软科中国大学排名", "XDWMC": "北京大学"}]
    files = [_build(kit, dxpm_spec, rows)]
    rep = verify_resource("05010001", files, ["https://x.gov.cn/a"], dxpm_spec,
                          FakeLlm({"page_is_target": True, "page_year": "",
                                   "entries": [{"org": "北京大学"}]}), tmp_path)
    assert rep.verdict == "无法核对" and "identity_needs_excel" in rep.reason_codes
