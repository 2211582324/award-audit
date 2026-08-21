"""核查问题（Issue）模型 + 严重度 + 规则目录。

设计：把"规则目录"做成一等公民——每条规则用 ``RuleDef`` 登记（ID / 严重度 / 查什么 / 对应人工错误清单#），
核查器只管产出 ``Issue`` 并引用 ``rule_id``，严重度从注册表反查。好处：
1. 实施方案里的 L0–L2 规则表 == 代码里的 ``RULES`` 字典，一一对应、可核对；
2. 新增/调整规则只动注册表，不散落在各处 if。
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict


class Severity(str, Enum):
    """严重度三档：决定批次是打回、反馈修正、还是挂人工复核。"""

    BLOCKER = "blocker"  # 严重：会导致入库错误，必须打回
    FORMAT = "format"    # 格式：可批量修正，反馈乙方改
    REVIEW = "review"    # 待人工/待取证：L5 抽查或联网核查，挂起


class RuleDef(BaseModel):
    """一条核查规则的元信息（不含判定逻辑，逻辑在各 checks 模块）。"""

    model_config = ConfigDict(frozen=True)

    id: str          # 规则ID，如 "L0-02"
    severity: Severity
    title: str       # 查什么（人读）
    error_ref: str   # 对应《评奖文件错误情况》里的条目号，"—" 表示无直接对应


# 规则目录：与实施方案第 4 节的 L0–L2 表一一对应
RULES: dict[str, RuleDef] = {
    # —— L0 文件规范 ——
    "L0-01": RuleDef(id="L0-01", severity=Severity.BLOCKER, title="文件名格式合法（<表码>-<奖项名>-<年份>.xlsx）", error_ref="—"),
    "L0-02": RuleDef(id="L0-02", severity=Severity.BLOCKER, title="模板用对：文件名前缀表码==映射表[资源项码].公共表表名", error_ref="7"),
    "L0-03": RuleDef(id="L0-03", severity=Severity.FORMAT, title="sheet 名含公共表表名", error_ref="—"),
    "L0-04": RuleDef(id="L0-04", severity=Severity.BLOCKER, title="表头字段代码严格等于模板", error_ref="7"),
    "L0-05": RuleDef(id="L0-05", severity=Severity.FORMAT, title="表头中文名与模板一致", error_ref="—"),
    "L0-06": RuleDef(id="L0-06", severity=Severity.BLOCKER, title="无“填写范例，请删除”残留行", error_ref="—"),
    "L0-07": RuleDef(id="L0-07", severity=Severity.BLOCKER, title="前两行为表头，数据从第3行起", error_ref="—"),
    # —— L1 字段格式 ——
    "L1-01": RuleDef(id="L1-01", severity=Severity.BLOCKER, title="硬必填 ZYLBM/ZYLB 非空", error_ref="6"),
    "L1-02": RuleDef(id="L1-02", severity=Severity.FORMAT, title="候选必填非空（待质检组确认）", error_ref="6"),
    "L1-03": RuleDef(id="L1-03", severity=Severity.BLOCKER, title="资源项码为文本/保前导零/存在于映射表", error_ref="7"),
    "L1-04": RuleDef(id="L1-04", severity=Severity.FORMAT, title="中文人名无内部空格", error_ref="1"),
    "L1-05": RuleDef(id="L1-05", severity=Severity.FORMAT, title="英文人名用-连接、无空格", error_ref="3"),
    "L1-06": RuleDef(id="L1-06", severity=Severity.FORMAT, title="人名/奖项名无书名号《》和双引号", error_ref="—"),
    "L1-07": RuleDef(id="L1-07", severity=Severity.FORMAT, title="多值字段用中文;分隔、无多余空格", error_ref="1"),
    "L1-08": RuleDef(id="L1-08", severity=Severity.FORMAT, title="年份列格式为 YYYY 或 YYYY-MM", error_ref="14"),
    "L1-09": RuleDef(id="L1-09", severity=Severity.FORMAT, title="等级列取值在允许枚举内", error_ref="10"),
    "L1-10": RuleDef(id="L1-10", severity=Severity.REVIEW, title="人名列疑似混入角色/多信息（主编：xx 副主编：yy），待人工确认口径", error_ref="11"),
    # —— L2 一致性 ——
    "L2-01": RuleDef(id="L2-01", severity=Severity.BLOCKER, title="文件名奖项名==每行资源项(ZYLB)", error_ref="—"),
    "L2-02": RuleDef(id="L2-02", severity=Severity.BLOCKER, title="文件名年份==行内主年份列（自洽）", error_ref="8"),
    "L2-03": RuleDef(id="L2-03", severity=Severity.BLOCKER, title="资源项码文件内全行一致", error_ref="—"),
    "L2-04": RuleDef(id="L2-04", severity=Severity.BLOCKER, title="资源项码映射结果==文件名前缀（与L0-02互证）", error_ref="7"),
    # —— L3 查全（需采集清单，M2）——
    "L3-01": RuleDef(id="L3-01", severity=Severity.BLOCKER, title="实际数据行数 vs 采集清单应采数量", error_ref="9"),
    "L3-02": RuleDef(id="L3-02", severity=Severity.FORMAT, title="采集清单内 交付数量 vs 应采数量", error_ref="9"),
    # —— L4 去重（需台账，M2）——
    "L4-01": RuleDef(id="L4-01", severity=Severity.FORMAT, title="文件内重复行（按去重键）", error_ref="—"),
    "L4-02": RuleDef(id="L4-02", severity=Severity.BLOCKER, title="跨批次重复（去重键已在正式库）", error_ref="—"),
    "L4-03": RuleDef(id="L4-03", severity=Severity.REVIEW, title="主身份相同但身份校验字段冲突", error_ref="—"),
    "L4-04": RuleDef(id="L4-04", severity=Severity.REVIEW, title="未满足模板身份方案，无法可靠判重", error_ref="6"),
    # —— L5P 联网核对前的链接预检（M4，纯确定性；全部 review 级转人工/转 Agent）——
    "L5P-01": RuleDef(id="L5P-01", severity=Severity.REVIEW, title="采集清单未收录该资源项码，无来源可核", error_ref="5"),
    "L5P-02": RuleDef(id="L5P-02", severity=Severity.REVIEW, title="清单已收录但未登记采集网址", error_ref="5"),
    "L5P-03": RuleDef(id="L5P-03", severity=Severity.REVIEW, title="采集网址格式异常，需人工修正清单", error_ref="—"),
    "L5P-04": RuleDef(id="L5P-04", severity=Severity.REVIEW, title="采集网址需登录/权限(401/403)，转人工核对", error_ref="—"),
    "L5P-05": RuleDef(id="L5P-05", severity=Severity.REVIEW, title="采集网址不可达(404/5xx/超时)，待人工更新清单", error_ref="—"),
    # —— L5S 软规则：LLM 语义核查（M4；review 级，人工终审）——
    "L5S-01": RuleDef(id="L5S-01", severity=Severity.REVIEW, title="LLM 判定人名列语义异常（乱码/误拆/中英混杂/姓名单位混排）", error_ref="2"),
    "L5S-02": RuleDef(id="L5S-02", severity=Severity.REVIEW, title="LLM 判定非人名列混入异类取值（推荐单位/专家列现人名等，列内取值类型异常）", error_ref="11"),
}


class Issue(BaseModel):
    """一条核查问题：定位（批次/文件/行/字段）+ 命中规则 + 描述 + 建议。反馈意见的最小单元。"""

    batch: str
    file: str
    sheet: str = ""
    row: int | None = None          # 数据行号（1 起，指数据区第几条；None=文件级问题）
    field_code: str | None = None
    field_name: str | None = None
    rule_id: str
    severity: Severity
    message: str
    current_value: str | None = None
    suggestion: str | None = None


# 按规则ID构造 Issue：严重度从 RULES 反查，避免各处手写导致不一致
def make_issue(
    rule_id: str,
    *,
    batch: str,
    file: str,
    sheet: str = "",
    row: int | None = None,
    field_code: str | None = None,
    field_name: str | None = None,
    message: str,
    current_value: str | None = None,
    suggestion: str | None = None,
) -> Issue:
    rule = RULES[rule_id]
    return Issue(
        batch=batch,
        file=file,
        sheet=sheet,
        row=row,
        field_code=field_code,
        field_name=field_name,
        rule_id=rule_id,
        severity=rule.severity,
        message=message,
        current_value=current_value,
        suggestion=suggestion,
    )
