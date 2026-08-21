"""模板规格（TemplateSpec）+ 各类型字段登记表（TYPE_REGISTRY）。

设计：标准答案②"附件5 的 18 个模板"决定字段结构，但"哪列是人名/机构/年份/名称"是模型判不出来的领域知识，
需要人工登记（实施方案 §2.4，实测得出）。于是拆两层：
- ``TYPE_REGISTRY``：按公共表表名尾码（如 XWLWHJ）登记各列角色 —— 领域知识，静态数据；
- ``TemplateSpec``：加载某个模板 xlsx 后的产物（字段代码/中文名 + 合并进来的列角色）。
新增奖项类型 = 丢一个模板文件 + 在 TYPE_REGISTRY 加一行，不改核查逻辑。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator


RoleType = Literal[
    "work_or_project",
    "team",
    "instructor_or_person",
    "organization",
    "ranking_or_special",
]


class RoleProfile(BaseModel):
    """One independently comparable business role within a template."""

    model_config = ConfigDict(frozen=True)

    role_type: RoleType
    role_label: str
    required: bool = True
    primary_alternatives: list[list[str]]
    scope_fields: list[str] = []
    fallback_scope_fields: list[str] = []
    discriminator_fields: list[str] = []
    conflict_fields: list[str] = []
    attribute_fields: list[str] = []
    selector_any_fields: list[str] = []
    selector_terms_by_field: dict[str, list[str]] = {}
    section_include_terms: list[str] = []
    section_exclude_terms: list[str] = []


class TypeRoles(BaseModel):
    """某类型各列的"角色"登记：领域知识，供 L1/L2 定位该查哪几列。"""

    model_config = ConfigDict(frozen=True)

    title_col: str | None            # 名称列（论文题目/项目名/课程名…）
    name_cols: list[str] = []        # 人名列（查空格/多值分隔）
    org_cols: list[str] = []         # 机构列
    year_cols: list[str] = []        # 年份列，第 0 个为"主年份列"（与文件名年份比对）
    grade_cols: list[str] = []       # 等级列（查枚举），无则空
    dedup_cols: list[str] = []       # 显式去重键覆盖：排名/认证等"逐校记录"类必须含学校标识，
                                     # 通用公式（奖项+年份+名称+首人名）对它们会海量键碰撞（压测实证）


class IdentityProfile(BaseModel):
    """附件 5 示例驱动的一类记录身份规则，供 L4、M4 和 M5 共用。"""

    model_config = ConfigDict(frozen=True)

    kind: Literal["roster", "ranking", "cert"] = "roster"
    scope_fields: list[str] = []
    primary_alternatives: list[list[str]] = []
    discriminator_fields: list[str] = []
    conflict_fields: list[str] = []
    occurrence_fields: list[str] = []
    attribute_fields: list[str] = []
    submit_cols: list[str]
    attachment_submit_cols: list[str] = []
    web_fields: list[str]
    combine: Literal["first", "all"] = "first"
    label: str = ""
    role_profiles: list[RoleProfile] = []

    @model_validator(mode="after")
    def _legacy_primary_alternatives(self) -> IdentityProfile:
        if not self.primary_alternatives and self.submit_cols:
            object.__setattr__(
                self, "primary_alternatives", [[field] for field in self.submit_cols]
            )
        if not self.role_profiles:
            role_type: RoleType = (
                "ranking_or_special"
                if self.kind in {"ranking", "cert"}
                else "work_or_project"
            )
            object.__setattr__(self, "role_profiles", [RoleProfile(
                role_type=role_type,
                role_label="主审核范围",
                primary_alternatives=self.primary_alternatives,
                scope_fields=self.scope_fields,
                discriminator_fields=self.discriminator_fields,
                conflict_fields=self.conflict_fields,
                attribute_fields=self.attribute_fields,
                selector_any_fields=list(dict.fromkeys(
                    field for fields in self.primary_alternatives for field in fields
                )),
            )])
        return self


# 各类型字段登记表：键 = 公共表表名尾码；值 = 列角色。实测自 附件5 的 18 个模板（院士表特例不在内）
TYPE_REGISTRY: dict[str, TypeRoles] = {
    "MKPTKC": TypeRoles(title_col="KCMC", name_cols=["XFZR"], org_cols=["XDWMC"], year_cols=["PZNY", "CJSJ"]),
    "JXKYJL": TypeRoles(title_col="XMCG", name_cols=["XRYXM"], org_cols=["XRYDW", "TJDW", "XDWMC"], year_cols=["HJND"], grade_cols=["HJDJ"]),
    "KYXM": TypeRoles(
        title_col="XMMC",
        name_cols=["XFZRXM", "XCYRXM"],
        org_cols=["XDWMC"],
        year_cols=["LXNF", "LXSJ"],
        # 科研项目表同时承载项目奖、个人奖和组织奖。项目编号优先，其余字段作为
        # 行级替代身份，避免空项目名的团体奖和同名项目成组误判为重复。
        dedup_cols=[
            "ZYLB", "LXNF", "XMBH", "XMMC", "XMLB", "XFZRXM", "XCYRXM", "XDWMC",
        ],
    ),
    "GXDJSCGR": TypeRoles(title_col="XMMC", name_cols=["XRYXM"], org_cols=["XDWMC", "ZZPXJG"], year_cols=["ND"]),
    "GXDJSCJT": TypeRoles(title_col="XMMC", name_cols=[], org_cols=["XDWMC", "ZBMC", "ZZPXJG"], year_cols=["RXSJ"]),
    "KCTX": TypeRoles(title_col="KCMC", name_cols=["XFZRXM", "XWCRXM"], org_cols=["XDWMC", "XCYDW"], year_cols=["PZNY"]),
    "XSJSHJ": TypeRoles(
        title_col="ZPMC",
        name_cols=["FZRXM", "CSZZMD", "XRYXM"],
        org_cols=["XCSDW", "XDWMC"],
        year_cols=["HJNF"],
        grade_cols=["HJDJ"],
        # 同一学校可有多支队伍，一个文件也可混合队伍、指导教师和组织单位等奖项。
        dedup_cols=[
            "ZYLB", "HJNF", "ZPMC", "CSDWMC", "FZRXM", "XRYXM", "XCSDW", "HJDJ",
        ],
    ),
    "XWLWHJ": TypeRoles(title_col="LWTM", name_cols=["ZZXM", "DSXM"], org_cols=["XDWMC", "PDJG"], year_cols=["PDNY"]),
    "YXJC": TypeRoles(title_col="JCMC", name_cols=["XRYXM"], org_cols=["XDWMC", "CBDW"], year_cols=["CBNY", "HPNY"],
                       dedup_cols=["ZYLB", "ISBN", "JCMC", "XRYXM", "BZ"]),
    "DXPM": TypeRoles(title_col="ZYLB", name_cols=[], org_cols=["XDWMC"], year_cols=["FBND"],
                       dedup_cols=["ZYLB", "FBND", "XXDM", "XXMC_YW"]),
    "XKPM": TypeRoles(title_col="XKMC", name_cols=[], org_cols=["XDWMC"], year_cols=["FBND"],
                       dedup_cols=["ZYLB", "FBND", "XKDM", "XKMC", "XXDM", "XXMC_YW"]),
    "RZXX": TypeRoles(title_col="ZYLB", name_cols=[], org_cols=["XDWMC", "TGRZDW"], year_cols=["QSSJ", "ZZSJ"],
                       dedup_cols=["ZYLB", "TGRZDW", "TGRZZY", "QSSJ", "XDWMC"]),
    "RCCH": TypeRoles(title_col="XMMC", name_cols=["XRYXM"], org_cols=["XDWMC", "XDQGZDW", "TJDW"], year_cols=["PZND", "ZZND"]),
    "XSTD": TypeRoles(title_col="TDMC", name_cols=["XDTRXM", "XRYXM"], org_cols=["XDWMC"], year_cols=["PXND"]),
    "ZCPT": TypeRoles(title_col="PTMC", name_cols=["PTVR", "XRYXM"], org_cols=["XDWMC"], year_cols=["PZND", "CXND"]),
    "JJCGWK": TypeRoles(title_col="XMMC", name_cols=["XRYXM", "CYRYMD"], org_cols=["XDWMC", "CYDWMD"], year_cols=["LXNF"]),
    "QK": TypeRoles(title_col="QKMC", name_cols=[], org_cols=["CBDW"], year_cols=["SLNF"]),
}


# 只把填写示例能够支持的字段放入现行身份。示例未填写字段仍保留在模板结构中，
# 但不会抢占主身份；待真实样本和业务口径确认后再升级 identity_version。
IDENTITY_PROFILES: dict[str, IdentityProfile] = {
    "MKPTKC": IdentityProfile(
        scope_fields=["ZYLBM", "PZNY"],
        primary_alternatives=[["KCMC"]],
        discriminator_fields=["XDWMC", "KKPT"],
        conflict_fields=["XFZR"],
        attribute_fields=["BZ", "CJSJ"],
        submit_cols=["KCMC"],
        attachment_submit_cols=["KCMC", "XDWMC", "KKPT", "XFZR"],
        web_fields=["title"],
        label="按课程名称核对，学校和平台区分同名课程，负责人冲突转人工",
    ),
    "JXKYJL": IdentityProfile(
        scope_fields=["ZYLBM", "HJND"],
        primary_alternatives=[["XMBH"], ["XMCG"]],
        discriminator_fields=["JXLX", "XDWMC"],
        conflict_fields=["XRYXM"],
        attribute_fields=["HJDJ"],
        submit_cols=["XMBH", "XMCG"],
        attachment_submit_cols=["XMBH", "XMCG", "JXLX", "XDWMC", "XRYXM"],
        web_fields=["identifier", "title"],
        label="项目成果编号优先，缺失时按成果名称核对",
    ),
    "KYXM": IdentityProfile(
        scope_fields=["ZYLBM", "LXNF"],
        primary_alternatives=[["XMBH"], ["XMMC"]],
        discriminator_fields=["XFZRXM", "XDWMC", "XMLB"],
        conflict_fields=["XCYRXM"],
        attribute_fields=["ZZJF", "XKFL", "XKFLDM"],
        submit_cols=["XMBH", "XMMC", "XFZRXM", "XCYRXM", "XDWMC"],
        attachment_submit_cols=["XMBH", "XMMC", "XFZRXM", "XCYRXM", "XDWMC", "XMLB"],
        web_fields=["identifier", "title", "names", "org"],
        role_profiles=[
            RoleProfile(
                role_type="work_or_project",
                role_label="项目/成果",
                primary_alternatives=[["XMBH"], ["XMMC"]],
                scope_fields=["ZYLBM", "LXNF", "XMLB"],
                fallback_scope_fields=["BZ"],
                discriminator_fields=["XFZRXM", "XDWMC"],
                conflict_fields=["XCYRXM"],
                selector_any_fields=["XMBH", "XMMC"],
                section_include_terms=["项目", "成果", "立项", "获奖"],
                section_exclude_terms=["个人奖", "组织奖", "团体奖"],
            ),
            RoleProfile(
                role_type="instructor_or_person",
                role_label="负责人/个人奖",
                required=False,
                primary_alternatives=[["XFZRXM"], ["XCYRXM"]],
                scope_fields=["ZYLBM", "LXNF", "XMLB"],
                discriminator_fields=["XDWMC"],
                selector_any_fields=["XFZRXM", "XCYRXM"],
                selector_terms_by_field={
                    "XMLB": ["个人奖", "负责人", "指导教师", "推荐奖"]
                },
                section_include_terms=["个人奖", "负责人", "获奖人", "推荐奖"],
            ),
            RoleProfile(
                role_type="organization",
                role_label="组织/团体奖",
                required=False,
                primary_alternatives=[["XDWMC"]],
                scope_fields=["ZYLBM", "LXNF", "XMLB"],
                selector_any_fields=["XDWMC"],
                selector_terms_by_field={
                    "XMLB": ["组织奖", "团体奖", "单位奖", "最佳组织", "优秀组织"]
                },
                section_include_terms=["组织奖", "团体奖", "单位奖"],
            ),
        ],
        label="项目编号优先、项目名称回退；二者都空时转人工确认业务对象",
    ),
    "GXDJSCGR": IdentityProfile(
        scope_fields=["ZYLBM", "ND"],
        primary_alternatives=[["XRYXM"]],
        discriminator_fields=["XDWMC"],
        attribute_fields=["XMMC", "ZZPXJG"],
        submit_cols=["XRYXM"],
        attachment_submit_cols=["XRYXM", "XDWMC"],
        web_fields=["names"],
        label="按获奖人核对个人名单，学校用于同名消歧",
    ),
    "GXDJSCJT": IdentityProfile(
        scope_fields=["ZYLBM", "RXSJ"],
        primary_alternatives=[["ZBMC"]],
        discriminator_fields=["XDWMC"],
        attribute_fields=["XMMC", "ZZPXJG"],
        submit_cols=["ZBMC"],
        attachment_submit_cols=["ZBMC", "XDWMC"],
        web_fields=["title"],
        label="按具体支部或部门名称核对集体名单",
    ),
    "KCTX": IdentityProfile(
        scope_fields=["ZYLBM", "PZNY"],
        primary_alternatives=[["KCMC"]],
        discriminator_fields=["XDWMC"],
        conflict_fields=["XFZRXM"],
        attribute_fields=["BZ", "XCYDW", "KKPT"],
        submit_cols=["KCMC"],
        attachment_submit_cols=["KCMC", "XDWMC", "XFZRXM", "XCYDW"],
        web_fields=["title"],
        label="按课程名称核对，建设单位区分同名课程",
    ),
    "XSJSHJ": IdentityProfile(
        scope_fields=["ZYLBM", "HJNF", "BSJS"],
        primary_alternatives=[["ZPBH"], ["ZPMC"], ["CSDWMC"]],
        discriminator_fields=["FZRXM", "XCSDW", "SDLB"],
        attribute_fields=["CSZZMD", "XRYXM", "HJDJ", "XDWMC"],
        submit_cols=["ZPBH", "ZPMC", "CSDWMC"],
        attachment_submit_cols=[
            "ZPBH", "ZPMC", "CSDWMC", "FZRXM", "XCSDW", "SDLB",
            "CSZZMD", "HJDJ",
        ],
        web_fields=["title"],
        role_profiles=[
            RoleProfile(
                role_type="team",
                role_label="参赛队伍/作品",
                primary_alternatives=[["ZPBH"], ["ZPMC"], ["CSDWMC"]],
                scope_fields=["ZYLBM", "HJNF", "BSJS", "SDLB"],
                discriminator_fields=["XCSDW"],
                selector_any_fields=["ZPBH", "ZPMC", "CSDWMC"],
                section_include_terms=["参赛队伍", "获奖队伍", "作品", "项目"],
                section_exclude_terms=["指导教师", "优秀组织", "团体奖"],
            ),
            RoleProfile(
                role_type="instructor_or_person",
                role_label="指导教师/个人奖",
                required=False,
                primary_alternatives=[["XRYXM"], ["FZRXM"]],
                scope_fields=["ZYLBM", "HJNF", "BSJS", "SDLB"],
                discriminator_fields=["XCSDW", "XDWMC", "HJDJ"],
                selector_any_fields=["XRYXM", "FZRXM"],
                selector_terms_by_field={"HJDJ": ["指导", "教师", "个人"]},
                section_include_terms=["指导教师", "优秀指导", "个人奖"],
            ),
            RoleProfile(
                role_type="organization",
                role_label="组织/团体奖",
                required=False,
                primary_alternatives=[["XCSDW"], ["XDWMC"]],
                scope_fields=["ZYLBM", "HJNF", "BSJS"],
                discriminator_fields=["HJDJ"],
                selector_any_fields=["XCSDW", "XDWMC"],
                selector_terms_by_field={"HJDJ": ["组织", "团体", "单位"]},
                section_include_terms=["优秀组织", "组织奖", "组织单位", "团体奖"],
            ),
            RoleProfile(
                role_type="ranking_or_special",
                role_label="排名/专项奖",
                required=False,
                primary_alternatives=[["ZPMC"], ["CSDWMC"]],
                scope_fields=["ZYLBM", "HJNF", "BSJS", "SDLB"],
                discriminator_fields=["HJDJ", "XCSDW"],
                selector_any_fields=["ZPMC", "CSDWMC"],
                selector_terms_by_field={"HJDJ": ["专项", "排名", "特别"]},
                section_include_terms=["专项奖", "特别奖", "排名"],
            ),
        ],
        label="按作品名称核对；参赛者、负责人和指导教师保持角色分离",
    ),
    "XWLWHJ": IdentityProfile(
        scope_fields=["ZYLBM", "PDNY"],
        primary_alternatives=[["LWTM", "ZZXM"]],
        discriminator_fields=["XXDM", "XDWMC"],
        conflict_fields=["DSXM"],
        attribute_fields=["PDJG", "XWJB", "LWGJC"],
        submit_cols=["LWTM", "ZZXM"],
        attachment_submit_cols=["LWTM", "ZZXM", "XXDM", "XDWMC", "DSXM"],
        web_fields=["title", "names"],
        combine="all",
        label="按论文题目和作者联合核对",
    ),
    "YXJC": IdentityProfile(
        scope_fields=["ZYLBM", "HPNY"],
        primary_alternatives=[["JCMC"]],
        conflict_fields=["ISBN", "CBDW", "ZZSMQK"],
        attribute_fields=["BZ", "DYKC", "YSSL", "DJ"],
        submit_cols=["JCMC"],
        attachment_submit_cols=["JCMC", "ISBN", "CBDW", "ZZSMQK"],
        web_fields=["title"],
        label="按教材名称核对；ISBN 只作联合校验，不能单独判重",
    ),
    "DXPM": IdentityProfile(
        kind="ranking",
        scope_fields=["ZYLBM", "FBND"],
        primary_alternatives=[["XXDM"], ["XDWMC"], ["XXMC_YW", "GBM"]],
        attribute_fields=["ZHPM", "ZHDF", "GNPM", "FXZB"],
        submit_cols=["XXDM", "XDWMC", "XXMC_YW", "GBM"],
        attachment_submit_cols=["XXDM", "XDWMC", "XXMC_YW", "GBM"],
        web_fields=["org", "title", "names"],
        label="学校代码优先，缺失时按中英文校名核对排名",
    ),
    "XKPM": IdentityProfile(
        kind="ranking",
        scope_fields=["ZYLBM", "FBND"],
        primary_alternatives=[
            ["XXDM", "XKMC"], ["XDWMC", "XKMC"], ["XXMC_YW", "XKMC_YW", "GBM"],
        ],
        attribute_fields=["ZHPM", "ZHDF", "ZHPJ", "GNPM", "FXZB", "GXRQ"],
        submit_cols=["XDWMC", "XKMC"],
        attachment_submit_cols=["XXDM", "XKMC", "XDWMC", "XXMC_YW", "XKMC_YW", "GBM"],
        web_fields=["org", "title"],
        combine="all",
        label="按学校和学科联合核对排名",
    ),
    "RZXX": IdentityProfile(
        kind="cert",
        scope_fields=["ZYLBM", "RZLX"],
        primary_alternatives=[["XDWMC", "TGRZZY"]],
        occurrence_fields=["QSSJ", "ZZSJ"],
        attribute_fields=["TGTJ", "BZ"],
        submit_cols=["XDWMC", "TGRZZY"],
        attachment_submit_cols=["XDWMC", "TGRZZY", "QSSJ", "ZZSJ"],
        web_fields=["org", "title"],
        combine="all",
        label="按学校和认证专业核对，不同有效期作为续期发生项",
    ),
    "RCCH": IdentityProfile(
        scope_fields=["ZYLBM", "PZND", "DXLX"],
        primary_alternatives=[["XRYXM"]],
        discriminator_fields=["XDWMC"],
        attribute_fields=["XMMC", "XZZW", "ZZPXJG", "XDQGZDW"],
        submit_cols=["XRYXM"],
        attachment_submit_cols=["XRYXM", "XDWMC"],
        web_fields=["names"],
        label="按人员姓名核对人才称号，单位用于同名消歧",
    ),
    "XSTD": IdentityProfile(
        scope_fields=["ZYLBM", "PXND"],
        primary_alternatives=[["YJFX", "XDTRXM", "XDWMC"]],
        attribute_fields=["ZZJE", "ZZQX", "XRYXM"],
        submit_cols=["YJFX", "XDTRXM", "XDWMC"],
        attachment_submit_cols=["YJFX", "XDTRXM", "XDWMC"],
        web_fields=["title", "names", "org"],
        combine="all",
        label="按研究方向、学术带头人和单位联合核对团队",
    ),
    "ZCPT": IdentityProfile(
        scope_fields=["ZYLBM", "PZND"],
        primary_alternatives=[["PTMC"]],
        discriminator_fields=["XDWMC"],
        attribute_fields=["CXND", "PTVR", "XRYXM"],
        submit_cols=["PTMC"],
        attachment_submit_cols=["PTMC", "XDWMC"],
        web_fields=["title"],
        label="按平台名称核对，共建单位集合用于同名消歧",
    ),
    "JJCGWK": IdentityProfile(
        scope_fields=["ZYLBM", "LXNF"],
        primary_alternatives=[["XMMC"]],
        discriminator_fields=["XRYXM", "XDWMC"],
        attribute_fields=["CYRYMD", "CYDWMD", "JXSJ", "SHXK"],
        submit_cols=["XMMC"],
        attachment_submit_cols=["XMMC", "XRYXM", "XDWMC"],
        web_fields=["title"],
        label="按成果项目名称核对，负责人和单位用于同名消歧",
    ),
    "QK": IdentityProfile(
        scope_fields=["ZYLBM", "SLNF"],
        primary_alternatives=[["QKMC", "CBDW"]],
        attribute_fields=["BZ", "CN", "ISSN", "FLH", "ZYXK"],
        submit_cols=["QKMC", "CBDW"],
        attachment_submit_cols=["QKMC", "CBDW"],
        web_fields=["title", "org"],
        combine="all",
        label="按期刊名称和出版/主办单位联合核对",
    ),
}


# 取公共表表名的尾码（类型判别符），如 CON_GG_XK_RCPY_XWLWHJ -> XWLWHJ
def type_suffix(table_code: str) -> str:
    return table_code.rsplit("_", 1)[-1]


class TemplateSpec(BaseModel):
    """一个模板加载后的完整规格：字段结构（来自 xlsx）+ 列角色（来自 TYPE_REGISTRY）。"""

    table_code: str                     # 公共表表名，如 CON_GG_XK_RCPY_XWLWHJ
    sheet_name: str
    field_codes: list[str]              # 第1行字段代码（已去尾部空列）
    field_names: dict[str, str]         # 字段代码 -> 中文名（第2行）
    dup_columns: list[str] = []         # 重复出现的字段代码（如 YXJC 的 XSMQK/ZZSMQK 都叫"作者署名情况"）

    hard_required: list[str] = ["ZYLBM", "ZYLB"]  # 结构性硬必填，全类型统一
    required_candidate: list[str] = []  # 候选必填（待质检组确认，M1 按 format 级）
    name_cols: list[str] = []
    org_cols: list[str] = []
    year_cols: list[str] = []
    grade_cols: list[str] = []
    title_col: str | None = None
    multi_value_cols: list[str] = []    # 可能多值的列（人名/机构），查 ; 分隔用
    dedup_key_cols: list[str] = []      # 去重键字段（业务主键组合），M2 去重/版本化用
    identity_profile: IdentityProfile | None = None

    # 主年份列：与文件名年份比对用（L2-02），取 year_cols 第一个
    @property
    def primary_year_col(self) -> str | None:
        return self.year_cols[0] if self.year_cols else None

    # 字段代码 -> 中文名，查不到回退代码本身
    def name_of(self, code: str) -> str:
        return self.field_names.get(code, code)


MatchProfile = IdentityProfile
MATCH_PROFILES = IDENTITY_PROFILES


def resolve_identity_profile(spec: TemplateSpec | None) -> IdentityProfile:
    """取得模板的版本化身份规则；未知模板保留最小兼容回退。"""

    if spec is not None:
        if spec.identity_profile is not None:
            return spec.identity_profile
        profile = IDENTITY_PROFILES.get(type_suffix(spec.table_code))
        if profile is not None:
            return profile
    cols: list[str] = []
    if spec is not None and spec.title_col:
        cols.append(spec.title_col)
    if spec is not None and spec.name_cols:
        cols.append(spec.name_cols[0])
    primary = [[column] for column in cols] or [["ZYLBM"]]
    return IdentityProfile(
        primary_alternatives=primary,
        submit_cols=cols,
        web_fields=["title", "names"],
        label="未知模板兼容身份，需人工确认",
    )


def resolve_match_profile(spec: TemplateSpec | None) -> MatchProfile:
    """兼容旧调用名；M4/M5 与 L4 实际读取同一个 IdentityProfile。"""

    return resolve_identity_profile(spec)
