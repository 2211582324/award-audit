"""L2 一致性核查的单元测试。"""

from __future__ import annotations

from award_audit.core.pipeline.checks import l2_consistency


# 功能：验证文件名年份与表内年份不一致时 L2-02 命中
# 设计：文件名年份 2024，评定年月填 2025，断言命中，覆盖错误清单 #8 的自洽部分
def test_l2_02_year_mismatch(kit, xwlwhj_spec) -> None:
    imp = kit.build([{"ZYLBM": "04050014", "ZYLB": kit.AWARD, "PDNY": "2025"}], year="2024")
    ids = {i.rule_id for i in l2_consistency.run(imp, xwlwhj_spec)}
    assert "L2-02" in ids


# 功能：验证年月格式（2024-10）与文件名年份自洽时不误报 L2-02
# 设计：文件名 2024，评定年月 2024-10，提取年份 2024 相等，断言不命中
def test_l2_02_ok_yyyymm(kit, xwlwhj_spec) -> None:
    imp = kit.build([{"ZYLBM": "04050014", "ZYLB": kit.AWARD, "PDNY": "2024-10"}], year="2024")
    ids = {i.rule_id for i in l2_consistency.run(imp, xwlwhj_spec)}
    assert "L2-02" not in ids


# 功能：验证文件名奖项名与表内资源项(ZYLB)不一致时 L2-01 命中
# 设计：ZYLB 填“别的奖”，与文件名奖项名不符，断言命中
def test_l2_01_award_mismatch(kit, xwlwhj_spec) -> None:
    imp = kit.build([{"ZYLBM": "04050014", "ZYLB": "别的奖", "PDNY": "2024"}], year="2024")
    ids = {i.rule_id for i in l2_consistency.run(imp, xwlwhj_spec)}
    assert "L2-01" in ids


# 功能：验证同一文件出现多个资源项码时 L2-03 命中
# 设计：两行填不同 ZYLBM，断言命中文件内不一致
def test_l2_03_multi_zylbm(kit, xwlwhj_spec) -> None:
    imp = kit.build(
        [
            {"ZYLBM": "04050014", "ZYLB": kit.AWARD, "PDNY": "2024"},
            {"ZYLBM": "04050012", "ZYLB": kit.AWARD, "PDNY": "2024"},
        ],
        year="2024",
    )
    ids = {i.rule_id for i in l2_consistency.run(imp, xwlwhj_spec)}
    assert "L2-03" in ids
