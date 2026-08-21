"""L4 去重核查 + 去重键计算的单元测试。"""

from __future__ import annotations

from award_audit.core.pipeline.checks import l4_dedup
from award_audit.core.pipeline.dedup import dedup_key, is_empty_key
from award_audit.core.reference.template_registry import build_template_spec


# 功能：验证文件内两行去重键相同（同奖项+同年+同题目+同作者）时 L4-01 命中并指回首现行
# 设计：第 1/3 行完全相同、第 2 行不同，断言命中 1 条、定位第 3 行、消息指向第 1 行
def test_l4_01_in_file_duplicate(kit, xwlwhj_spec) -> None:
    same = {"ZYLBM": "04050014", "ZYLB": kit.AWARD, "LWTM": "论文A", "ZZXM": "张三", "PDNY": "2024"}
    other = {"ZYLBM": "04050014", "ZYLB": kit.AWARD, "LWTM": "论文B", "ZZXM": "李四", "PDNY": "2024"}
    imp = kit.build([same, other, dict(same)])
    issues = l4_dedup.run(imp, xwlwhj_spec)
    hit = [i for i in issues if i.rule_id == "L4-01"]
    assert len(hit) == 1 and hit[0].row == 3 and "第 1 行" in hit[0].message


# 功能：验证去重键已在正式库（current_keys）时 L4-02 命中——跨批次重复
# 设计：先算第 0 行的键放进集合，再跑核查，断言 L4-02 定位到该行
def test_l4_02_cross_batch_duplicate(kit, xwlwhj_spec) -> None:
    row = {"ZYLBM": "04050014", "ZYLB": kit.AWARD, "LWTM": "论文A", "ZZXM": "张三", "PDNY": "2024"}
    imp = kit.build([row])
    key = dedup_key(imp, 0, xwlwhj_spec)
    issues = l4_dedup.run(imp, xwlwhj_spec, current_keys={key})
    assert any(i.rule_id == "L4-02" and i.row == 1 for i in issues)


# 功能：验证互不重复且库为空时 L4 无命中
# 设计：两行题目/作者不同，断言零命中，防假阳性
def test_l4_clean(kit, xwlwhj_spec) -> None:
    imp = kit.build([
        {"ZYLBM": "04050014", "ZYLB": kit.AWARD, "LWTM": "论文A", "ZZXM": "张三", "PDNY": "2024"},
        {"ZYLBM": "04050014", "ZYLB": kit.AWARD, "LWTM": "论文B", "ZZXM": "李四", "PDNY": "2024"},
    ])
    assert l4_dedup.run(imp, xwlwhj_spec, current_keys=set()) == []


# 功能：验证去重键组成列全空的行不参与去重（空键不判重）
# 设计：两行 dedup 列全空（仅 BZ 有值），断言无命中；并直接断言 is_empty_key 判定
def test_l4_empty_key_skipped(kit, xwlwhj_spec) -> None:
    imp = kit.build([{"BZ": "备注1"}, {"BZ": "备注2"}])
    issues = l4_dedup.run(imp, xwlwhj_spec)
    assert not any(item.rule_id in {"L4-01", "L4-02"} for item in issues)
    assert sum(item.rule_id == "L4-04" for item in issues) == 2
    assert is_empty_key(dedup_key(imp, 0, xwlwhj_spec))


# 功能：验证学位论文模板的去重键组成为 奖项+年份+题目+首个人名
# 设计：断言注册表推导出的 dedup_key_cols 恰为业务主键四列，锁定通用公式
def test_dedup_key_cols(xwlwhj_spec) -> None:
    assert xwlwhj_spec.dedup_key_cols == [
        "ZYLBM", "PDNY", "LWTM", "ZZXM", "XXDM", "XDWMC", "DSXM",
    ]


def test_group_awards_without_project_name_do_not_collapse_by_organisation(kit) -> None:
    codes = [
        "ZYLBM", "ZYLB", "XMBH", "XMMC", "XMLB", "XFZRXM", "XCYRXM",
        "XDWMC", "LXNF",
    ]
    spec = build_template_spec(
        "CON_GG_XK_KXYJ_KYXM", "数据", codes, codes,
    )
    rows = [
        {
            "ZYLBM": "06020007", "ZYLB": "示例专利奖", "XMLB": "最佳组织奖",
            "XDWMC": "甲单位",
        },
        {
            "ZYLBM": "06020007", "ZYLB": "示例专利奖", "XMLB": "最佳组织奖",
            "XDWMC": "乙单位",
        },
        {
            "ZYLBM": "06020007", "ZYLB": "示例专利奖", "XMLB": "最佳推荐奖",
            "XCYRXM": "张三", "XDWMC": "甲单位",
        },
    ]
    imported = kit.build(
        rows,
        codes=codes,
        names=codes,
        table_code="CON_GG_XK_KXYJ_KYXM",
        award="示例专利奖",
        year="2025",
    )

    assert [item for item in l4_dedup.run(imported, spec) if item.rule_id == "L4-01"] == []


def test_competition_teams_at_same_school_do_not_collapse(kit) -> None:
    codes = [
        "ZYLBM", "ZYLB", "ZPMC", "CSDWMC", "FZRXM", "XRYXM", "XCSDW",
        "HJDJ", "HJNF",
    ]
    spec = build_template_spec(
        "CON_GG_XK_RCPY_XSJSHJ", "数据", codes, codes,
    )
    rows = [
        {
            "ZYLBM": "04030061", "ZYLB": "示例竞赛", "CSDWMC": "甲队",
            "XCSDW": "同一大学", "HJDJ": "一等奖", "HJNF": "2025",
        },
        {
            "ZYLBM": "04030061", "ZYLB": "示例竞赛", "CSDWMC": "乙队",
            "XCSDW": "同一大学", "HJDJ": "二等奖", "HJNF": "2025",
        },
    ]
    imported = kit.build(
        rows,
        codes=codes,
        names=codes,
        table_code="CON_GG_XK_RCPY_XSJSHJ",
        award="示例竞赛",
        year="2025",
    )

    assert [item for item in l4_dedup.run(imported, spec) if item.rule_id == "L4-01"] == []
