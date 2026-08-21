"""压测（提交-12/13/14）暴露问题的降噪回归测试。"""

from __future__ import annotations

from award_audit.core.pipeline.checks import l1_field
from award_audit.core.reference.template_registry import load_template_registry


# 功能：验证未知资源项码按码聚合为文件级一条，而非逐行刷屏（压测：1195 行 1195 条）
# 设计：3 行同一个未知码，断言 L1-03 恰 1 条、row 为 None（文件级）、消息含行数
def test_l1_03_aggregated_per_code(kit, xwlwhj_spec, resource_map) -> None:
    row = {"ZYLBM": "99999999", "ZYLB": kit.AWARD, "LWTM": "t", "ZZXM": "张三", "PDNY": "2024"}
    imp = kit.build([dict(row), dict(row), dict(row)])
    hits = [i for i in l1_field.run(imp, xwlwhj_spec, resource_map) if i.rule_id == "L1-03"]
    assert len(hits) == 1 and hits[0].row is None and "3 行" in hits[0].message


# 功能：验证奖项名内嵌引号（官方命名如 创“芯”大赛）不再误报 L1-06（压测：3100 条误报）
# 设计：ZYLB 内嵌引号断言不命中；整值被“”包裹断言命中——区分"官方命名"与"采集多余包裹"
def test_l1_06_zylb_embedded_quotes_ok(kit, xwlwhj_spec, resource_map) -> None:
    imp = kit.build([{"ZYLBM": "04050014", "ZYLB": '中国研究生创“芯”大赛', "LWTM": "t", "ZZXM": "张三", "PDNY": "2024"}],
                    award='中国研究生创“芯”大赛')
    zylb_hits = [i for i in l1_field.run(imp, xwlwhj_spec, resource_map)
                 if i.rule_id == "L1-06" and i.field_code == "ZYLB"]
    assert zylb_hits == []

    imp2 = kit.build([{"ZYLBM": "04050014", "ZYLB": "“某某奖”", "LWTM": "t", "ZZXM": "张三", "PDNY": "2024"}])
    zylb_hits2 = [i for i in l1_field.run(imp2, xwlwhj_spec, resource_map)
                  if i.rule_id == "L1-06" and i.field_code == "ZYLB"]
    assert len(zylb_hits2) == 1


# 功能：验证人名列含引号仍逐行报 L1-06（人名有引号一定是错）
# 设计：作者姓名含书名号，断言命中且定位人名列
def test_l1_06_name_quotes_still_flagged(kit, xwlwhj_spec, resource_map) -> None:
    imp = kit.build([{"ZYLBM": "04050014", "ZYLB": kit.AWARD, "LWTM": "t", "ZZXM": "《张三》", "PDNY": "2024"}])
    hits = [i for i in l1_field.run(imp, xwlwhj_spec, resource_map)
            if i.rule_id == "L1-06" and i.field_code == "ZZXM"]
    assert len(hits) == 1


# 功能：验证候选必填大面积部分缺失聚合为一条，不逐行刷屏（压测：XKPM 2 万行）
# 设计：100 行中 60 行作者为空（>50 且 >20%），断言 L1-02 恰 1 条聚合、消息含比例
def test_l1_02_partial_missing_aggregated(kit, xwlwhj_spec, resource_map) -> None:
    rows = []
    for i in range(100):
        zz = "" if i < 60 else "张三"
        rows.append({"ZYLBM": "04050014", "ZYLB": kit.AWARD, "LWTM": f"论文{i}", "ZZXM": zz, "PDNY": "2024"})
    hits = [i for i in l1_field.run(kit.build(rows), xwlwhj_spec, resource_map)
            if i.rule_id == "L1-02" and i.field_code == "ZZXM"]
    assert len(hits) == 1 and "60/100" in hits[0].message


# 功能：验证等级未知值按 列×值 聚合报一次并带次数（压测：600 条逐行）
# 设计：用 JXKYJL 规格（有 HJDJ 等级列）造 3 行同一个未知等级，断言 1 条、消息含"3 行"
def test_l1_09_grade_aggregated(kit, resource_map) -> None:
    reg = load_template_registry()
    jx = reg["CON_GG_XK_KXYJ_JXKYJL"]
    rows = [{"ZYLBM": "04050014", "ZYLB": "某奖", "XMCG": "成果", "XRYXM": "张三", "HJND": "2024", "HJDJ": "卓越贡献奖"}] * 3
    imp = kit.build(rows, codes=jx.field_codes, names=[jx.name_of(c) for c in jx.field_codes],
                    table_code=jx.table_code, award="某奖")
    hits = [i for i in l1_field.run(imp, jx, resource_map) if i.rule_id == "L1-09"]
    assert len(hits) == 1 and "3 行" in hits[0].message


# 功能：验证排名类去重键使用显式覆盖并包含学校标识（压测：软科排名 2 万条 L4-01 键碰撞）
# 设计：真实加载注册表，断言 XKPM 键含 XXDM/XXMC_YW、DXPM 键含 XXDM——同学科不同学校不再同键
def test_ranking_dedup_keys_include_school() -> None:
    reg = load_template_registry()
    assert "XXDM" in reg["CON_GG_XK_SHSY_PMQK_XKPM"].dedup_key_cols
    assert "XXMC_YW" in reg["CON_GG_XK_SHSY_PMQK_XKPM"].dedup_key_cols
    assert "XXDM" in reg["CON_GG_XK_SHSY_PMQK_DXPM"].dedup_key_cols


# 功能：验证人名列"角色：姓名"复合内容不再误判为 L1-04 姓名空格（TUI 实测：教材奖 697 条误报）
# 设计：'主编：危道军 副主编：程红艳' 断言不命中 L1-04/05/07，改命中 review 级 L1-10 且按列聚合一条
def test_l1_10_role_mixed_not_name_space(kit, xwlwhj_spec, resource_map) -> None:
    rows = [
        {"ZYLBM": "04050014", "ZYLB": kit.AWARD, "LWTM": "t1", "ZZXM": "主编：危道军 副主编：程红艳", "PDNY": "2024"},
        {"ZYLBM": "04050014", "ZYLB": kit.AWARD, "LWTM": "t2", "ZZXM": "陈梅梅 副主编：仝小芳", "PDNY": "2024"},
    ]
    issues = l1_field.run(kit.build(rows), xwlwhj_spec, resource_map)
    ids = {i.rule_id for i in issues}
    assert "L1-04" not in ids and "L1-05" not in ids and "L1-07" not in ids
    hits = [i for i in issues if i.rule_id == "L1-10"]
    assert len(hits) == 1 and hits[0].severity.value == "review" and "2 行" in hits[0].message


# 功能：验证普通姓名空格（无角色词）仍走 L1-04，守卫不误吞真实错误
# 设计：'张 楠' 无冒号无角色词，断言 L1-04 照常命中、L1-10 不出现
def test_l1_04_still_works_without_role_words(kit, xwlwhj_spec, resource_map) -> None:
    imp = kit.build([{"ZYLBM": "04050014", "ZYLB": kit.AWARD, "LWTM": "t", "ZZXM": "张 楠", "PDNY": "2024"}])
    ids = {i.rule_id for i in l1_field.run(imp, xwlwhj_spec, resource_map)}
    assert "L1-04" in ids and "L1-10" not in ids


# 功能：验证 7 位码补零后命中映射表时，L1-03 点破"前导零丢失"根因并给出建议值
# 设计：填 "4050014"（映射表有 04050014），断言消息含"前导零"、suggestion 为补零后的码
def test_l1_03_leading_zero_hint(kit, xwlwhj_spec, resource_map) -> None:
    imp = kit.build([{"ZYLBM": "4050014", "ZYLB": kit.AWARD, "LWTM": "t", "ZZXM": "张三", "PDNY": "2024"}])
    hits = [i for i in l1_field.run(imp, xwlwhj_spec, resource_map) if i.rule_id == "L1-03"]
    assert len(hits) == 1 and "前导零" in hits[0].message and hits[0].suggestion == "04050014"
