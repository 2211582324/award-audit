"""测试夹具与内存构建辅助。

设计：核查器只吃 ImportedFile + TemplateSpec + 资源项码映射，三者都能纯内存构造，
故绝大多数规则测试无需落盘 xlsx——快、可控、边界清晰。仅加载器/导入器测试才用 tmp_path 造真实 xlsx。
"""

from __future__ import annotations

import pytest

from award_audit.core import config
from award_audit.core.models.record import ImportedFile
from award_audit.core.models.template import TemplateSpec
from award_audit.core.reference.resource_map import ResourceMapEntry
from award_audit.core.reference.template_registry import build_template_spec


@pytest.fixture(autouse=True)
def _isolate_project_dotenv(monkeypatch: pytest.MonkeyPatch) -> None:
    """Offline tests never inspect a developer's real project configuration."""

    monkeypatch.setattr(config, "load_env", lambda path=None: None)

# 学位论文获奖模板的字段代码/中文名（实测自 附件5）
XWLWHJ_CODES = [
    "ZYLBM", "BZ", "ZYLB", "SSDM", "SSMC", "XXDM", "MLM", "YJXKM", "YJXKMC",
    "XKDM", "XKMC", "LWTM", "LWGJC", "ZZXM", "DSXM", "XWJB", "PDNY", "PDJG", "XDWMC",
]
XWLWHJ_NAMES = [
    "资源项码", "备注", "资源项", "省市代码", "省市名称", "学校代码", "门类码", "一级学科码", "一级学科名称",
    "学科码", "学科名称", "论文题目", "论文关键词", "作者姓名", "导师姓名", "学位级别", "评定年月", "评定机构", "学校名称",
]
XWLWHJ_CODE = "CON_GG_XK_RCPY_XWLWHJ"
AWARD = "中国人工智能学会优秀博士学位论文"


# 学位论文获奖 TemplateSpec 夹具
@pytest.fixture
def xwlwhj_spec() -> TemplateSpec:
    return build_template_spec(XWLWHJ_CODE, "学位论文获奖", XWLWHJ_CODES, XWLWHJ_NAMES)


# 资源项码映射夹具：04050014 → 学位论文获奖模板
@pytest.fixture
def resource_map() -> dict[str, ResourceMapEntry]:
    return {
        "04050014": ResourceMapEntry(
            resource_code="04050014", resource_name=AWARD, table_code=XWLWHJ_CODE
        )
    }


# ---- 阶段3 多样性：排名/认证类模板夹具（身份=学校/学科/认证专业，非题目+人名）----
# 表码尾码决定 type_suffix → MATCH_PROFILES 命中对应核对形态；字段取自 附件5 真实模板列。
DXPM_CODE = "CON_GG_XK_SHSY_DXPM"
DXPM_CODES = ["ZYLBM", "BZ", "ZYLB", "FBND", "ZHPM", "XDWMC"]
DXPM_NAMES = ["资源项码", "备注", "资源类", "发布年度", "综合排名", "学校名称"]

XKPM_CODE = "CON_GG_XK_SHSY_XKPM"
XKPM_CODES = ["ZYLBM", "BZ", "ZYLB", "XKMC", "FBND", "ZHPM", "XDWMC"]
XKPM_NAMES = ["资源项码", "备注", "资源类", "学科名称", "发布年度", "综合排名", "学校名称"]

RZXX_CODE = "CON_GG_XK_SHSY_RZXX"
RZXX_CODES = ["ZYLBM", "BZ", "ZYLB", "RZLX", "TGRZDW", "TGRZZY", "QSSJ", "ZZSJ", "XDWMC"]
RZXX_NAMES = ["资源项码", "备注", "资源类", "认证类型", "通过认证单位", "通过认证专业",
              "起始时间", "终止时间", "学校名称"]


# 大学排名 TemplateSpec 夹具（身份=学校 XDWMC；title_col=ZYLB 是常量，不能当键）
@pytest.fixture
def dxpm_spec() -> TemplateSpec:
    return build_template_spec(DXPM_CODE, "大学排名", DXPM_CODES, DXPM_NAMES)


# 学科排名 TemplateSpec 夹具（身份=学科 XKMC + 学校 XDWMC 复合键）
@pytest.fixture
def xkpm_spec() -> TemplateSpec:
    return build_template_spec(XKPM_CODE, "学科排名", XKPM_CODES, XKPM_NAMES)


# 认证信息 TemplateSpec 夹具（身份=学校 XDWMC + 认证专业 TGRZZY；TGRZDW 实测常空，故不用它）
@pytest.fixture
def rzxx_spec() -> TemplateSpec:
    return build_template_spec(RZXX_CODE, "认证信息", RZXX_CODES, RZXX_NAMES)


# 由 {字段代码:值} 的行构造 ImportedFile（缺字段补 ""）
def build_imported(
    dict_rows: list[dict[str, str]],
    *,
    codes: list[str] | None = None,
    names: list[str] | None = None,
    table_code: str = XWLWHJ_CODE,
    award: str = AWARD,
    year: str = "2024",
    sheet: str | None = None,
    batch: str = "批次-T",
) -> ImportedFile:
    codes = codes or XWLWHJ_CODES
    names = names or XWLWHJ_NAMES
    rows = [[dr.get(c, "") for c in codes] for dr in dict_rows]
    return ImportedFile(
        batch=batch,
        path=f"/tmp/{table_code}-{award}-{year}.xlsx",
        file_name=f"{table_code}-{award}-{year}.xlsx",
        claimed_table_code=table_code,
        award_name=award,
        year=year,
        sheet_name=sheet if sheet is not None else f"{table_code}-学位论文获奖信息_",
        header_codes=codes,
        header_names=names,
        rows=rows,
    )


class Kit:
    """把常量与构造器打包成一个夹具对象，供各 unit 测试直接取用（免跨目录 import）。"""

    AWARD = AWARD
    XWLWHJ_CODE = XWLWHJ_CODE
    CODES = XWLWHJ_CODES
    NAMES = XWLWHJ_NAMES
    build = staticmethod(build_imported)


# 测试工具包夹具
@pytest.fixture
def kit() -> Kit:
    return Kit()


# 从 Issue 列表取命中的规则ID集合
def rule_ids(issues: list) -> set[str]:
    return {i.rule_id for i in issues}
