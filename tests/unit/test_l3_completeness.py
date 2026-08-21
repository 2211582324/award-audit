"""L3 查全核查（批次级聚合）的单元测试。"""

from __future__ import annotations

from award_audit.core.pipeline.checks import l3_completeness
from award_audit.core.reference.ledger import LedgerEntry


# 造一个采集清单：04050014 应采 expected 条（可带交付数量）
def _ledger(expected: int | None, delivered: int | None = None) -> dict[str, LedgerEntry]:
    return {"04050014": LedgerEntry(resource_code="04050014", resource_name="某奖",
                                    expected_count=expected, delivered_count=delivered)}


# 功能：验证单文件行数少于应采数量时 L3-01 命中（错误清单 #9 采集不全）
# 设计：文件 2 行 vs 应采 3 条，断言命中且建议值为应采数
def test_l3_01_shortfall(kit) -> None:
    imp = kit.build([
        {"ZYLBM": "04050014", "ZYLB": kit.AWARD, "PDNY": "2024"},
        {"ZYLBM": "04050014", "ZYLB": kit.AWARD, "PDNY": "2024"},
    ])
    issues = l3_completeness.run_batch([imp], _ledger(3))
    hit = [i for i in issues if i.rule_id == "L3-01"]
    assert len(hit) == 1 and hit[0].suggestion == "3"


# 功能：验证数量吻合时不误报 L3-01
# 设计：2 行 vs 应采 2 条，断言无命中，防假阳性
def test_l3_01_ok(kit) -> None:
    imp = kit.build([
        {"ZYLBM": "04050014", "ZYLB": kit.AWARD, "PDNY": "2024"},
        {"ZYLBM": "04050014", "ZYLB": kit.AWARD, "PDNY": "2024"},
    ])
    assert l3_completeness.run_batch([imp], _ledger(2)) == []


# 功能：验证同一资源项分多个文件时按批次聚合比对，不逐文件误报
# 设计：两个文件各 2/1 行、应采 3 条——聚合正好 3，断言无命中；这是本规则最关键的设计点
def test_l3_01_multifile_aggregate_ok(kit) -> None:
    f1 = kit.build([{"ZYLBM": "04050014", "ZYLB": kit.AWARD, "PDNY": "2018"}] * 2, year="2018")
    f2 = kit.build([{"ZYLBM": "04050014", "ZYLB": kit.AWARD, "PDNY": "2019"}], year="2019")
    assert l3_completeness.run_batch([f1, f2], _ledger(3)) == []


# 功能：验证多文件聚合仍不足时，每个成员文件各挂一条 L3-01（入库门禁一致）
# 设计：2+1=3 行 vs 应采 5 条，断言两个文件各 1 条、消息含聚合口径
def test_l3_01_multifile_shortfall_each_flagged(kit) -> None:
    f1 = kit.build([{"ZYLBM": "04050014", "ZYLB": kit.AWARD, "PDNY": "2018"}] * 2, year="2018")
    f2 = kit.build([{"ZYLBM": "04050014", "ZYLB": kit.AWARD, "PDNY": "2019"}], year="2019")
    issues = l3_completeness.run_batch([f1, f2], _ledger(5))
    hit = [i for i in issues if i.rule_id == "L3-01"]
    assert len(hit) == 2
    assert {i.file for i in hit} == {f1.file_name, f2.file_name}
    assert "2 个文件" in hit[0].message


# 功能：验证资源项不在清单或应采数量未填时跳过（无基准不硬判）
# 设计：清单为空 dict 与 expected=None 两种情形都断言无命中
def test_l3_skip_without_baseline(kit) -> None:
    imp = kit.build([{"ZYLBM": "04050014", "ZYLB": kit.AWARD, "PDNY": "2024"}])
    assert l3_completeness.run_batch([imp], {}) == []
    assert l3_completeness.run_batch([imp], _ledger(None)) == []


# 功能：验证清单自查：交付数量≠应采数量时 L3-02 命中一次
# 设计：应采 1、交付 9，断言 L3-02 恰一条（挂首个成员文件）
def test_l3_02_delivered_mismatch(kit) -> None:
    imp = kit.build([{"ZYLBM": "04050014", "ZYLB": kit.AWARD, "PDNY": "2024"}])
    issues = l3_completeness.run_batch([imp], _ledger(1, delivered=9))
    assert len([i for i in issues if i.rule_id == "L3-02"]) == 1
