"""L1 字段格式核查的单元测试。"""

from __future__ import annotations

from award_audit.core.pipeline.checks import l1_field
from award_audit.core.reference.template_registry import build_template_spec


# 功能：验证中文姓名内部空格（如“张 楠”）命中 L1-04
# 设计：作者姓名列填“张 楠”，断言命中且定位到 ZZXM，覆盖错误清单 #1
def test_l1_04_chinese_name_space(kit, xwlwhj_spec, resource_map) -> None:
    imp = kit.build([{"ZYLBM": "04050014", "ZYLB": kit.AWARD, "LWTM": "t", "ZZXM": "张 楠", "PDNY": "2024"}])
    issues = l1_field.run(imp, xwlwhj_spec, resource_map)
    assert any(i.rule_id == "L1-04" and i.field_code == "ZZXM" for i in issues)


# 功能：验证硬必填 ZYLBM/ZYLB 为空时 L1-01 命中
# 设计：两个硬必填留空，断言命中，覆盖错误清单 #6
def test_l1_01_missing_hard_required(kit, xwlwhj_spec, resource_map) -> None:
    imp = kit.build([{"ZYLBM": "", "ZYLB": "", "LWTM": "t"}])
    ids = {i.rule_id for i in l1_field.run(imp, xwlwhj_spec, resource_map)}
    assert "L1-01" in ids


# 功能：验证多值用错分隔符（顿号）命中 L1-07
# 设计：导师姓名用“陈恩红、刘淇”，断言命中错误分隔符
def test_l1_07_bad_separator(kit, xwlwhj_spec, resource_map) -> None:
    imp = kit.build([{"ZYLBM": "04050014", "ZYLB": kit.AWARD, "LWTM": "t", "ZZXM": "张三", "DSXM": "陈恩红、刘淇", "PDNY": "2024"}])
    ids = {i.rule_id for i in l1_field.run(imp, xwlwhj_spec, resource_map)}
    assert "L1-07" in ids


# 功能：验证正确的分号多值不误报 L1-07/L1-04
# 设计：导师“陈恩红;刘淇”为规范写法，断言不命中，防止把正确数据判错
def test_l1_07_ok_semicolon(kit, xwlwhj_spec, resource_map) -> None:
    imp = kit.build([{"ZYLBM": "04050014", "ZYLB": kit.AWARD, "LWTM": "t", "ZZXM": "张三", "DSXM": "陈恩红;刘淇", "PDNY": "2024"}])
    ids = {i.rule_id for i in l1_field.run(imp, xwlwhj_spec, resource_map)}
    assert "L1-07" not in ids and "L1-04" not in ids


# 功能：验证主年份列无合法年份时 L1-08 命中
# 设计：评定年月填“去年”，断言命中年份格式问题
def test_l1_08_bad_year(kit, xwlwhj_spec, resource_map) -> None:
    imp = kit.build([{"ZYLBM": "04050014", "ZYLB": kit.AWARD, "LWTM": "t", "ZZXM": "张三", "PDNY": "去年"}])
    ids = {i.rule_id for i in l1_field.run(imp, xwlwhj_spec, resource_map)}
    assert "L1-08" in ids


# 功能：验证资源项码不在映射表时 L1-03 命中
# 设计：填未知码 99999999，断言命中，覆盖“未知码/前导零丢失”
def test_l1_03_unknown_code(kit, xwlwhj_spec, resource_map) -> None:
    imp = kit.build([{"ZYLBM": "99999999", "ZYLB": kit.AWARD, "LWTM": "t", "ZZXM": "张三", "PDNY": "2024"}])
    ids = {i.rule_id for i in l1_field.run(imp, xwlwhj_spec, resource_map)}
    assert "L1-03" in ids


# 功能：验证一条干净的真实风格数据不产生任何 L1 假阳性
# 设计：用提交-27 里真实数据构造一行，断言 L1-01/03/04/05/07/08 全不命中
def test_l1_clean_row_no_false_positive(kit, xwlwhj_spec, resource_map) -> None:
    imp = kit.build([{
        "ZYLBM": "04050014", "ZYLB": kit.AWARD, "BZ": "激励计划论文",
        "LWTM": "不完备标注学习的理论与方法研究", "ZZXM": "周雄", "DSXM": "刘贤明",
        "XWJB": "博士", "PDNY": "2024", "PDJG": "中国人工智能学会", "XDWMC": "哈尔滨工业大学",
    }])
    issues = l1_field.run(imp, xwlwhj_spec, resource_map)
    bad = {i.rule_id for i in issues} & {"L1-01", "L1-03", "L1-04", "L1-05", "L1-07", "L1-08"}
    assert not bad, [i.message for i in issues]


def test_l1_02_accepts_organisation_identity_when_group_award_has_no_title(
    kit, resource_map,
) -> None:
    codes = [
        "ZYLBM", "ZYLB", "XMBH", "XMMC", "XMLB", "XFZRXM", "XCYRXM",
        "XDWMC", "LXNF",
    ]
    spec = build_template_spec(
        "CON_GG_XK_KXYJ_KYXM", "数据", codes, codes,
    )
    imported = kit.build(
        [{
            "ZYLBM": "06020007", "ZYLB": "示例专利奖", "XMLB": "最佳组织奖",
            "XDWMC": "甲单位",
        }],
        codes=codes,
        names=codes,
        table_code="CON_GG_XK_KXYJ_KYXM",
        award="示例专利奖",
        year="2025",
    )

    issues = l1_field.run(imported, spec, {
        "06020007": resource_map["04050014"].model_copy(update={
            "resource_code": "06020007", "resource_name": "示例专利奖",
            "table_code": "CON_GG_XK_KXYJ_KYXM",
        })
    })

    assert not any(
        item.rule_id == "L1-02" and item.field_code == "XMMC" for item in issues
    )
