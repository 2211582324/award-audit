"""核查工具函数 _util 的单元测试。"""

from __future__ import annotations

from award_audit.core.pipeline.checks import _util


# 功能：验证中文姓名内部空格判定（半角/全角）
# 设计：覆盖“张 楠”（半角）判真、“张楠”判假，全角空格另测，界定 L1-04 边界
def test_cjk_inner_space() -> None:
    assert _util.cjk_has_inner_space("张 楠")
    assert _util.cjk_has_inner_space("张　楠")
    assert not _util.cjk_has_inner_space("张楠")


# 功能：验证 4 位年份提取
# 设计：覆盖“2018-10”取 2018、“去年”取 None，支撑 L1-08/L2-02
def test_extract_year() -> None:
    assert _util.extract_year("2018-10") == "2018"
    assert _util.extract_year("2024") == "2024"
    assert _util.extract_year("去年") is None


# 功能：验证多值切分（半角/全角分号都切）
# 设计：断言两种分号都被正确切成两项，与规范“分号分隔”一致
def test_split_multi() -> None:
    assert _util.split_multi("陈恩红;刘淇") == ["陈恩红", "刘淇"]
    assert _util.split_multi("陈恩红；刘淇") == ["陈恩红", "刘淇"]


# 功能：验证非法分隔符判定（分号不算错）
# 设计：顿号判真、正确分号判假，界定 L1-07 边界，防止把规范分号误判为错误
def test_bad_separator() -> None:
    assert _util.has_bad_separator("陈恩红、刘淇")
    assert not _util.has_bad_separator("陈恩红;刘淇")


# 功能：验证英文姓名内部空格判定
# 设计：“John Smith”判真、“John-Smith”判假，支撑 L1-05
def test_latin_inner_space() -> None:
    assert _util.latin_has_inner_space("John Smith")
    assert not _util.latin_has_inner_space("John-Smith")
