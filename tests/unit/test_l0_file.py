"""L0 文件规范核查的单元测试。"""

from __future__ import annotations

from award_audit.core.pipeline.checks import l0_file


# 功能：验证资源项码映射出的应用模板与文件名前缀不符时，L0-02 命中
# 设计：ZYLBM=04050014 映射到 XWLWHJ，但文件名前缀故意写成 KYXM，claimed_spec 仍传结构相符的 xwlwhj_spec 以隔离 L0-04 噪声
def test_l0_02_template_mismatch(kit, xwlwhj_spec, resource_map) -> None:
    imp = kit.build(
        [{"ZYLBM": "04050014", "ZYLB": kit.AWARD, "LWTM": "某论文", "ZZXM": "张三", "PDNY": "2024"}],
        table_code="CON_GG_XK_KXYJ_KYXM",
        sheet="CON_GG_XK_KXYJ_KYXM-x",
    )
    ids = {i.rule_id for i in l0_file.run(imp, xwlwhj_spec, resource_map)}
    assert "L0-02" in ids


# 功能：验证模板用对时不误报 L0-02
# 设计：文件名前缀=映射结果=XWLWHJ，期望规则不命中，防止假阳性
def test_l0_02_ok(kit, xwlwhj_spec, resource_map) -> None:
    imp = kit.build([{"ZYLBM": "04050014", "ZYLB": kit.AWARD, "LWTM": "某论文", "ZZXM": "张三", "PDNY": "2024"}])
    ids = {i.rule_id for i in l0_file.run(imp, xwlwhj_spec, resource_map)}
    assert "L0-02" not in ids


# 功能：验证数据区残留“填写范例，请删除”时 L0-06 命中
# 设计：把范例标记放进 BZ 列，断言命中且定位到该数据行
def test_l0_06_example_row(kit, xwlwhj_spec, resource_map) -> None:
    imp = kit.build([{"ZYLBM": "04050014", "ZYLB": kit.AWARD, "BZ": "填写范例，请删除", "LWTM": "t", "ZZXM": "张三", "PDNY": "2024"}])
    issues = l0_file.run(imp, xwlwhj_spec, resource_map)
    assert any(i.rule_id == "L0-06" and i.row == 1 for i in issues)


# 功能：验证表头字段与模板不一致时 L0-04 命中
# 设计：删掉最后一个字段 XDWMC 制造表头缺列，claimed_spec 为完整模板，断言结构差异被抓出
def test_l0_04_header_mismatch(kit, xwlwhj_spec, resource_map) -> None:
    imp = kit.build(
        [{"ZYLBM": "04050014", "ZYLB": kit.AWARD}],
        codes=kit.CODES[:-1],
        names=kit.NAMES[:-1],
    )
    ids = {i.rule_id for i in l0_file.run(imp, xwlwhj_spec, resource_map)}
    assert "L0-04" in ids


# 功能：验证结构完整的正常文件不误报 L0-04
# 设计：表头与模板严格一致，断言无结构类问题
def test_l0_04_ok(kit, xwlwhj_spec, resource_map) -> None:
    imp = kit.build([{"ZYLBM": "04050014", "ZYLB": kit.AWARD, "LWTM": "t", "ZZXM": "张三", "PDNY": "2024"}])
    ids = {i.rule_id for i in l0_file.run(imp, xwlwhj_spec, resource_map)}
    assert "L0-04" not in ids
