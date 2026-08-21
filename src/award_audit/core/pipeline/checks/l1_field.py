"""L1 字段格式核查（L1-01 ~ L1-09）：逐行逐字段的确定性格式规则。

只查"确定性可判"的（空格、书名号、分隔符、年份格式）；语义类问题（四五字名被拆、中英混杂）留给 L5 LLM。

降噪设计（提交-12/13/14 压测实证后确立）：
- L1-03 未知资源项码：按码聚合成**文件级一条**（新资源项未登记会整文件同码，逐行报刷屏上千条）；
- L1-02 候选必填：整列全空 → 一条"疑似不适用"；部分空超阈值（>50 且 >20% 行）→ 一条聚合；少量才逐行；
- L1-06 引号：人名列任何引号都报；奖项名(ZYLB) 只报"整值被引号包裹"——官方奖项名内嵌引号
  （创“芯”大赛、“美丽中国”）是合法命名，不是采集错误；
- L1-09 等级：按"列×取值"聚合报一次（带出现次数），不逐行。
"""

from __future__ import annotations

import re
from collections import defaultdict

from award_audit.core.models.issue import Issue, make_issue
from award_audit.core.models.record import ImportedFile
from award_audit.core.models.template import TemplateSpec
from award_audit.core.pipeline.checks import _util
from award_audit.core.reference.resource_map import ResourceMapEntry

# 常见获奖等级枚举（L1-09，可调；format 级，不在内仅提示人工确认）
KNOWN_GRADES = {
    "特等奖", "一等奖", "二等奖", "三等奖", "优秀奖", "提名奖",
    "金奖", "银奖", "铜奖", "一类", "二类", "三类",
}
# 引号/书名号字符集
_QUOTE_CHARS = ("《", "》", "“", "”", "〈", "〉")
# 整值包裹判定的括对
_WRAP_PAIRS = (("《", "》"), ("“", "”"), ("〈", "〉"))
# L1-02 部分空聚合阈值：空行数超过 max(50, 20%行数) 或超过绝对上限 500 时不逐行刷屏
_L102_ABS = 50
_L102_RATIO = 0.2
_L102_CAP = 500
# L1-10 复合内容启发式：人名列出现角色词或冒号 → 是"角色：姓名"混排而非单纯姓名，
# 空格问题不能按"姓名含空格"硬判（去空格建议会把两个人黏成一个），挂人工确认口径
_ROLE_PATTERN = re.compile(r"[:：]|主编|副主编|总主编|参编|编委|主译|主审|通讯作者|第一作者")


# 值是否被引号/书名号整体包裹（首尾成对）——奖项名的"采集多余包裹"才算错，内嵌引号是官方命名
def _fully_wrapped(s: str) -> bool:
    t = s.strip()
    return len(t) >= 2 and any(t.startswith(a) and t.endswith(b) for a, b in _WRAP_PAIRS)


# 跑完整 L1 字段格式核查
def run(
    imported: ImportedFile,
    claimed_spec: TemplateSpec | None,
    resource_map: dict[str, ResourceMapEntry],
) -> list[Issue]:
    issues: list[Issue] = []
    b, f, s = imported.batch, imported.file_name, imported.sheet_name
    spec = claimed_spec

    unknown_codes: dict[str, list[int]] = defaultdict(list)   # 未知资源项码 → 行号们（L1-03 聚合）
    bad_grades: dict[tuple[str, str], list[int]] = defaultdict(list)  # (等级列,值) → 行号们（L1-09 聚合）
    role_mixed: dict[str, list[int]] = defaultdict(list)      # 人名列 → 行号们（L1-10 复合内容聚合）
    role_example: dict[str, str] = {}                          # 人名列 → 首个复合内容示例

    for ri in range(imported.n_rows):
        row_no = ri + 1

        # L1-01 硬必填 ZYLBM/ZYLB 非空
        for code in ("ZYLBM", "ZYLB"):
            if imported.value(ri, code).strip() == "":
                issues.append(make_issue("L1-01", batch=b, file=f, sheet=s, row=row_no,
                                         field_code=code, message=f"硬必填字段 {code} 为空"))

        # L1-03 资源项码存在于映射表（先收集，循环外按码聚合）
        zylbm = imported.value(ri, "ZYLBM").strip()
        if zylbm and zylbm not in resource_map:
            unknown_codes[zylbm].append(row_no)

        if spec is None:
            continue  # 无模板规格，类型相关规则无从判定

        # L1-04/05/06/07/10 人名列：复合内容守卫 → 空格、连字符、引号、多值分隔符
        for code in spec.name_cols:
            val = imported.value(ri, code)
            if val == "":
                continue
            fn = spec.name_of(code)
            # L1-10 守卫：含角色词/冒号 → 是"角色：姓名"混排，空格判定不适用（先收集，循环外聚合）
            if _ROLE_PATTERN.search(val):
                role_mixed[code].append(row_no)
                role_example.setdefault(code, val)
                continue
            # L1-07 非法多值分隔符（应为中文 ;）
            if _util.has_bad_separator(val):
                issues.append(make_issue("L1-07", batch=b, file=f, sheet=s, row=row_no,
                                         field_code=code, field_name=fn,
                                         message=f"{fn} 多值分隔符不规范，应用中文分号 ; 分隔",
                                         current_value=val))
            # L1-06 人名不应含任何书名号/引号
            if any(m in val for m in _QUOTE_CHARS):
                issues.append(make_issue("L1-06", batch=b, file=f, sheet=s, row=row_no,
                                         field_code=code, field_name=fn,
                                         message=f"{fn} 含书名号/引号，人名不应出现",
                                         current_value=val))
            for part in (_util.split_multi(val) or [val]):
                # L1-04 中文名内部空格
                if _util.cjk_has_inner_space(part):
                    issues.append(make_issue("L1-04", batch=b, file=f, sheet=s, row=row_no,
                                             field_code=code, field_name=fn,
                                             message=f"{fn} 中文姓名含空格：{part!r}（去空格；多人请用 ; 分隔）",
                                             current_value=val, suggestion=part.replace(" ", "").replace("　", "")))
                # L1-05 英文名内部空格
                elif _util.latin_has_inner_space(part):
                    issues.append(make_issue("L1-05", batch=b, file=f, sheet=s, row=row_no,
                                             field_code=code, field_name=fn,
                                             message=f"{fn} 英文姓名含空格：{part!r}，应用 - 连接",
                                             current_value=val, suggestion=part.strip().replace(" ", "-")))

        # L1-06 奖项名(ZYLB)：仅整值被引号包裹才算采集错误（内嵌引号是官方命名，压测实证不报）
        zylb_val = imported.value(ri, "ZYLB")
        if zylb_val and _fully_wrapped(zylb_val):
            issues.append(make_issue("L1-06", batch=b, file=f, sheet=s, row=row_no,
                                     field_code="ZYLB", field_name="资源项",
                                     message="资源项整值被书名号/引号包裹，应去除外层包裹",
                                     current_value=zylb_val, suggestion=zylb_val.strip().strip("《》“”〈〉")))

        # L1-08 主年份列含合法 4 位年份
        yc = spec.primary_year_col
        if yc:
            yval = imported.value(ri, yc)
            if yval and _util.extract_year(yval) is None:
                issues.append(make_issue("L1-08", batch=b, file=f, sheet=s, row=row_no,
                                         field_code=yc, field_name=spec.name_of(yc),
                                         message=f"{spec.name_of(yc)} 未含合法年份（应形如 2024 或 2024-10）",
                                         current_value=yval))

        # L1-09 等级列取值（先收集，循环外按 列×值 聚合）
        for code in spec.grade_cols:
            val = imported.value(ri, code).strip()
            if val and val not in KNOWN_GRADES:
                bad_grades[(code, val)].append(row_no)

    # —— 文件级聚合区 ——

    # L1-03 未知资源项码：每个码一条文件级 blocker（新资源项未登记/填错码，整文件同码时不刷屏）；
    # 码位数不足且补零后能在映射表命中 → 点破根因"前导零丢失"（Excel 转数字的经典错误）
    for code_val, rows in unknown_codes.items():
        padded = code_val.zfill(8)
        if padded != code_val and padded in resource_map:
            hint = f"；疑似前导零丢失，应为 {padded}（{resource_map[padded].resource_name}）"
            suggestion: str | None = padded
        else:
            hint = "；新资源项未登记或填错码，需与映射表维护方核对"
            suggestion = None
        issues.append(make_issue("L1-03", batch=b, file=f, sheet=s, field_code="ZYLBM",
                                 message=f"资源项码 {code_val} 不在资源项码映射表中（共 {len(rows)} 行）{hint}",
                                 current_value=code_val, suggestion=suggestion))

    # L1-09 等级：每个 列×未知值 报一条（row=首现行，带次数）
    if spec is not None:
        for (gcode, gval), rows in bad_grades.items():
            issues.append(make_issue("L1-09", batch=b, file=f, sheet=s, row=rows[0],
                                     field_code=gcode, field_name=spec.name_of(gcode),
                                     message=f"{spec.name_of(gcode)} 取值 {gval!r} 不在常见等级枚举内"
                                             f"（{len(rows)} 行），请人工确认",
                                     current_value=gval))

    # L1-10 人名列复合内容：按列聚合报一条（review 级——是否合规是采集口径问题，交人工/质检组）
    if spec is not None:
        for code, rows in role_mixed.items():
            fn = spec.name_of(code)
            issues.append(make_issue("L1-10", batch=b, file=f, sheet=s, row=rows[0],
                                     field_code=code, field_name=fn,
                                     message=f"{fn} 疑似混入角色/多信息（{len(rows)} 行，如 {role_example[code]!r}）；"
                                             f"若规范要求仅填姓名，请将角色移入署名情况列并以 ; 分隔多人",
                                     current_value=role_example[code]))

    # L1-02 候选必填：全空→一条"疑似不适用"；部分空超阈值→一条聚合；少量→逐行
    if spec is not None and imported.n_rows > 0:
        threshold = min(_L102_CAP, max(_L102_ABS, int(imported.n_rows * _L102_RATIO)))
        for code in spec.required_candidate:
            empties: list[int] = []
            for ri in range(imported.n_rows):
                if imported.value(ri, code).strip():
                    continue
                # 名称列是候选必填而非结构性硬必填。团体奖、组织奖、个人奖可能
                # 合法地没有项目/作品名；该行已有人员或机构身份时不报“缺项目名”。
                alternatives = [*spec.name_cols, *spec.org_cols]
                if code == spec.title_col and any(
                    imported.value(ri, alt).strip()
                    for alt in alternatives
                    if alt != code
                ):
                    continue
                empties.append(ri + 1)
            if not empties:
                continue
            fn = spec.name_of(code)
            if len(empties) == imported.n_rows:
                issues.append(make_issue("L1-02", batch=b, file=f, sheet=s, field_code=code, field_name=fn,
                                         message=f"候选必填 {fn}({code}) 全表 {imported.n_rows} 行均为空，疑似该类型不适用此字段，待质检组确认是否必填"))
            elif len(empties) > threshold:
                issues.append(make_issue("L1-02", batch=b, file=f, sheet=s, field_code=code, field_name=fn,
                                         message=f"候选必填 {fn}({code}) 大面积缺失：{len(empties)}/{imported.n_rows} 行为空"
                                                 f"（示例行 {empties[0]}），请与采集口径核对后批量处理"))
            else:
                for r in empties:
                    issues.append(make_issue("L1-02", batch=b, file=f, sheet=s, row=r, field_code=code, field_name=fn,
                                             message=f"候选必填 {fn}({code}) 为空（{len(empties)}/{imported.n_rows} 行缺）"))

    return issues
