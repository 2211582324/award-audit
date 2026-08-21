"""软规则评测：用金标准集（tests/data/soft_rules_golden.json）锁两件事——
① 廉价启发式的召回/精度（该送 LLM 的疑点被 is_suspect / collect_column_anomalies 命中、干净值不命中）；
② 评分器 score_predictions 计算正确 + run_columns（L5S-02）接线正确。
全程不真调 LLM（真调精度评测走 scripts/eval_soft_rules.py）。
"""

from __future__ import annotations

import json
from pathlib import Path

from award_audit.agent import soft_rules
from award_audit.core.models.record import ImportedFile
from award_audit.core.models.template import TemplateSpec

GOLDEN = json.loads(
    (Path(__file__).resolve().parent.parent / "data" / "soft_rules_golden.json").read_text(encoding="utf-8"))


class FakeLlm:
    """返回预设 verdicts 的假 LLM。"""

    def __init__(self, verdicts):  # noqa: ANN001
        self.verdicts = verdicts

    def json_call(self, system, user, max_tokens=2000):  # noqa: ANN001, ANN201
        return self.verdicts


# 用单个机构列构造 ImportedFile：列值 = column_values + [outlier]，末行为疑点
def _one_col_file(col: str, col_name: str, values: list[str]) -> tuple[ImportedFile, TemplateSpec]:
    spec = TemplateSpec(table_code="CON_TEST", sheet_name="s", field_codes=[col],
                        field_names={col: col_name}, org_cols=[col])
    imp = ImportedFile(batch="批次-T", path="/tmp/t.xlsx", file_name="t.xlsx",
                       claimed_table_code="CON_TEST", award_name="测试奖", year="2024",
                       sheet_name="s", header_codes=[col], header_names=[col_name],
                       rows=[[v] for v in values])
    return imp, spec


# 功能：人名列金标准里"该报"的值全被 is_suspect 命中、干净值不命中（廉价筛的召回/精度）
# 设计：遍历 name_cases，gold!=ok → is_suspect True（进 LLM）；gold==ok → False（不花钱）
def test_golden_name_heuristic_recall() -> None:
    for case in GOLDEN["name_cases"]:
        suspect = soft_rules.is_suspect(case["value"])
        if case["gold"] == "ok":
            assert not suspect, f"干净值被误判疑点：{case['value']}"
        else:
            assert suspect, f"该报的值未被 is_suspect 命中：{case['value']}"


# 功能：机构列金标准里的"混入人名"被 collect_column_anomalies 收为疑点、正常机构不被收
# 设计：每 column_case 造单列文件（多数机构 + 该值），gold==type_mismatch → 疑点含该值；ok → 不含
def test_golden_column_heuristic_recall() -> None:
    for case in GOLDEN["column_cases"]:
        imp, spec = _one_col_file("TJDW", case["col_name"], list(case["column_values"]) + [case["value"]])
        suspects = soft_rules.collect_column_anomalies(imp, spec)
        flagged = {s["value"] for s in suspects}
        if case["gold"] == "type_mismatch":
            assert case["value"] in flagged, f"混入人名未被收为疑点：{case['value']}"
        else:
            assert case["value"] not in flagged, f"正常机构被误收疑点：{case['value']}"


# 功能：评分器 score_predictions 正确算总正确率 + 报警二分类 precision/recall
# 设计：构造含 TP/FP/FN/TN 各一的 gold/pred，逐指标断言
def test_score_predictions() -> None:
    gold = ["role_mixed", "ok", "garbled", "ok"]
    pred = ["role_mixed", "role_mixed", "ok", "ok"]  # TP1 / FP1 / FN1 / TN1
    r = soft_rules.score_predictions(gold, pred)
    assert r["n"] == 4.0 and r["correct"] == 2.0 and r["accuracy"] == 0.5
    assert r["precision"] == 0.5 and r["recall"] == 0.5


# 功能：run_columns 接线——mock LLM 判金标准 type_mismatch 值 → 恰产一条 L5S-02 Issue、定位到该行/列
# 设计：造"3 机构 + 王占山"单列文件，fake 判 id0 为 type_mismatch；断言 rule_id/severity/current_value
def test_run_columns_wiring_l5s02() -> None:
    imp, spec = _one_col_file("TJDW", "推荐单位/专家", ["上海交通大学", "复旦大学", "浙江大学", "王占山"])
    fake = FakeLlm([{"id": 0, "verdict": "type_mismatch", "reason": "推荐单位列混入人名", "fixed": ""}])
    issues = soft_rules.run_columns(imp, spec, fake)  # type: ignore[arg-type]
    assert len(issues) == 1
    i = issues[0]
    assert i.rule_id == "L5S-02" and i.severity.value == "review"
    assert i.field_code == "TJDW" and i.current_value == "王占山" and i.row == 4


# 功能：全干净机构列 → 无疑点、不发起 LLM 请求（零成本路径）
# 设计：一列全是大学，collect 返回空、run_columns 直接返回空
def test_run_columns_no_suspect_no_call() -> None:
    imp, spec = _one_col_file("TJDW", "推荐单位", ["清华大学", "北京大学", "浙江大学", "武汉大学"])

    class Boom:
        def json_call(self, *a, **k):  # noqa: ANN002, ANN003, ANN201
            raise AssertionError("无疑点却调了 LLM")

    assert soft_rules.run_columns(imp, spec, Boom()) == []  # type: ignore[arg-type]
