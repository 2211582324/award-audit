"""L5 软规则：LLM 批量判定语义异常（错误清单 #2/#4/#11/#13 —— 规则写不死的那类）。

两类核查，各自"疑点启发式筛 → 一次 LLM 批判 → review 级 Issue 挂人工"：
- ``run_file``（L5S-01，#2/#4）：人名列语义异常（角色混排/乱码误拆/中英混杂/姓名单位混排）；
- ``run_columns``（L5S-02，#11/#13）：机构列内取值类型异常（推荐单位/专家列混入人名等）。

成本控制三板斧：
1. 只送**疑点值**（启发式筛：角色词/生僻符号/超长/中英混杂；机构列里的少数派人名），干净值不花钱；
2. 值去重 + 一个文件**一次请求**批判（上限 80 个值）；
3. 用快速档模型（haiku），system 提示打 prompt cache。
产出全部 review 级挂人工终审——LLM 只提建议，不改数据。
"""

from __future__ import annotations

import json
import re

from award_audit.agent.llm import LlmClient, LlmError
from award_audit.core.models.issue import Issue, make_issue
from award_audit.core.models.record import ImportedFile
from award_audit.core.models.template import TemplateSpec

# 一次请求最多送多少个疑点值（再多截断，避免超上下文与失控成本）
SUSPECT_MAX = 80
# 疑点启发式：角色词/冒号、非常见字符（乱码痕迹）、中英混杂、超长
_ROLE = re.compile(r"[:：]|主编|副主编|总主编|参编|编委|主译|主审|通讯作者|第一作者")
_ODD_CHAR = re.compile(r"[�□▯]|_x[0-9A-Fa-f]{4}_")
_HAS_CJK = re.compile(r"[一-鿿]")
_HAS_LATIN = re.compile(r"[A-Za-z]")
# 机构关键词：命中即视为"机构类"取值（推荐单位/完成单位常填这些）
_ORG_KW = re.compile(
    r"大学|学院|学校|公司|研究院|研究所|科学院|工程院|协会|学会|中心|委员会|部|局|厅|署|所|"
    r"集团|实验室|医院|银行|基金会|联合会|有限|股份|事务所|单位")
# 强机构词（绝不出现在人名里）：人名列命中即疑似姓名/单位混排
_ORG_STRONG = re.compile(
    r"大学|学院|学校|公司|研究院|研究所|科学院|工程院|协会|学会|委员会|中心|医院|集团|实验室|基金会|联合会")
# 纯汉字（含间隔号）——判"像人名"用
_ALL_CJK_NAME = re.compile(r"[一-鿿·]+")

VERDICT_CN = {
    "role_mixed": "角色与姓名混排",
    "garbled": "乱码或名字被错误拆分/合并",
    "mixed_lang": "中英文名混杂",
    "name_org_mixed": "姓名与单位混排",
    "other": "其他语义异常",
}

SYSTEM_PROMPT = """你是教育部学位中心的数据质检员，负责审查获奖名单里"人名列"的取值是否规范。
规范要求：人名列只能是人名本身，多人用分号分隔，如 "张三;李四"；不得混入角色（主编/副主编）、单位、括号注释；
中文名内部不得有空格；英文名用 - 连接（John-Smith）；不得出现乱码或把一个四五字名字错拆成两个人。

对输入数组中的每个值给出判定，输出 JSON 数组，每项：
{"id": 原样返回, "verdict": "ok|role_mixed|garbled|mixed_lang|name_org_mixed|other", "reason": "一句话", "fixed": "建议修正值(尽力而为,不确定给原值)"}

判定示例：
- "张三;李四" → ok
- "主编：危道军 副主编：程红艳" → role_mixed，fixed "危道军;程红艳"
- "王大 力" → garbled（若像一个三字名被断开），fixed "王大力"
- "张三李四王五" → garbled（多个名字被合并、缺分号），fixed "张三;李四;王五"
- "李明John-Lee" → mixed_lang（同一人中英名连写）
- "张三 清华大学" → name_org_mixed，fixed "张三"
- "李四（同济大学）" → name_org_mixed（姓名后缀单位），fixed "李四"
- "Ma Yun" → ok 之外应报（英文名空格），但该问题由确定性规则负责，这里报 ok
只对语义级问题给非 ok 判定；纯格式问题（分隔符、英文空格）报 ok。"""


# 启发式：这个值值得花钱送 LLM 吗（recall 向的廉价筛，宁多送不漏——真判定交 LLM）
def is_suspect(value: str) -> bool:
    if len(value) > 40:
        return True
    if _ROLE.search(value) or _ODD_CHAR.search(value):
        return True
    if _ORG_STRONG.search(value):  # 人名列不该含"大学/学院/公司…" → 疑似姓名/单位混排
        return True
    if re.search(r"[一-鿿]\s+[一-鿿]", value):  # 汉字间夹空格：名字被拆/多名未用;分隔
        return True
    for part in re.split(r"[;；]", value):
        p = part.strip()
        if not p:
            continue
        if _HAS_CJK.search(p) and _HAS_LATIN.search(p):  # 单段中英混杂
            return True
        if len(p) >= 5 and _ALL_CJK_NAME.fullmatch(p):  # 纯汉字≥5：疑似多名被合并
            return True
    return False


# 收集一个文件的疑点值：人名列 → 去重 → 截断，返回 [{id,col,col_name,value,row}]
def collect_suspects(imported: ImportedFile, spec: TemplateSpec) -> list[dict[str, object]]:
    seen: dict[tuple[str, str], int] = {}  # (col, value) -> 首现行
    for ri in range(imported.n_rows):
        for col in spec.name_cols:
            val = imported.value(ri, col).strip()
            if val and (col, val) not in seen and is_suspect(val):
                seen[(col, val)] = ri + 1
    out = []
    for idx, ((col, val), row) in enumerate(seen.items()):
        out.append({"id": idx, "col": col, "col_name": spec.name_of(col), "value": val, "row": row})
    return out[:SUSPECT_MAX]


# 对一个文件跑软规则：疑点收集 → 一次 LLM 批判 → verdict!=ok 产出 review 级 Issue
def run_file(imported: ImportedFile, spec: TemplateSpec | None, llm: LlmClient) -> list[Issue]:
    if spec is None or not spec.name_cols:
        return []
    suspects = collect_suspects(imported, spec)
    if not suspects:
        return []

    user = json.dumps(
        [{"id": s["id"], "列": s["col_name"], "value": s["value"]} for s in suspects],
        ensure_ascii=False,
    )
    try:
        verdicts = llm.json_call(SYSTEM_PROMPT, user, max_tokens=4000)
    except LlmError as exc:
        # LLM 不可用不阻塞管道：返回一条 review 提示（管道的确定性部分照常工作）
        return [make_issue("L5S-01", batch=imported.batch, file=imported.file_name,
                           sheet=imported.sheet_name,
                           message=f"软规则核查未执行（{exc}），{len(suspects)} 个疑点值待人工")]

    by_id = {s["id"]: s for s in suspects}
    issues: list[Issue] = []
    if not isinstance(verdicts, list):
        verdicts = []
    for v in verdicts:
        if not isinstance(v, dict) or v.get("verdict", "ok") == "ok":
            continue
        s = by_id.get(v.get("id"))
        if s is None:
            continue
        label = VERDICT_CN.get(str(v.get("verdict")), "语义异常")
        issues.append(make_issue(
            "L5S-01", batch=imported.batch, file=imported.file_name, sheet=imported.sheet_name,
            row=int(s["row"]) if isinstance(s["row"], int) else None,
            field_code=str(s["col"]), field_name=str(s["col_name"]),
            message=f"{s['col_name']} 疑似{label}：{v.get('reason', '')}（LLM 判定，待人工确认）",
            current_value=str(s["value"]), suggestion=str(v.get("fixed") or "") or None))
    return issues


# 像机构名吗：含机构关键词，或明显较长（≥7 字，人名极少这么长）
def _looks_like_org(value: str) -> bool:
    s = value.strip()
    return bool(_ORG_KW.search(s)) or len(s) >= 7


# 像人名吗：2–4 个连续汉字（含间隔号）、不含机构关键词、无数字/拉丁/分隔符
def _looks_like_person(value: str) -> bool:
    s = value.strip()
    if not (2 <= len(s) <= 4):
        return False
    if _ORG_KW.search(s) or re.search(r"[0-9A-Za-z;；、,，]", s):
        return False
    return bool(_ALL_CJK_NAME.fullmatch(s))


# 收集机构列的"少数派人名"疑点（#11/#13）：某 org 列多数值 org-like、少数 person-like →
# 少数派为列内取值类型异常疑点。返回 [{id,col,col_name,value,row,majority}]。控成本：只送少数派。
def collect_column_anomalies(imported: ImportedFile, spec: TemplateSpec) -> list[dict[str, object]]:
    out: list[dict[str, object]] = []
    idx = 0
    for col in spec.org_cols:
        vals: list[tuple[str, int]] = []  # (取值, 首现行)
        seen_val: set[str] = set()
        for ri in range(imported.n_rows):
            v = imported.value(ri, col).strip()
            if v and v not in seen_val:
                seen_val.add(v)
                vals.append((v, ri + 1))
        if len(vals) < 3:  # 太少无"多数/少数"可言
            continue
        org_like = [v for v, _ in vals if _looks_like_org(v)]
        person_like = [(v, r) for v, r in vals if _looks_like_person(v)]
        # 该列确以机构为主（多数 org-like），且人名是小众（≤1/5）→ 少数派人名可疑
        if len(org_like) >= max(2, int(len(vals) * 0.6)) and 0 < len(person_like) <= max(1, len(vals) // 5):
            examples = org_like[:3]
            for v, r in person_like:
                out.append({"id": idx, "col": col, "col_name": spec.name_of(col),
                            "value": v, "row": r, "majority": examples})
                idx += 1
                if len(out) >= SUSPECT_MAX:
                    return out
    return out


COL_SYSTEM_PROMPT = """你是教育部学位中心的数据质检员，负责审查获奖名单里"机构类列"（推荐单位、推荐单位/专家、完成单位、参赛单位等）的取值是否与该列语义一致。
该列应当填机构/单位名称。若某取值其实是人名（而非机构），即为"列内取值类型异常"（机构列混入了人名）。

给你一组该列的少数派疑点值（附该列多数取值样例，供你判断该列语义），对每个疑点值判定，输出 JSON 数组，每项：
{"id": 原样返回, "verdict": "ok|type_mismatch|other", "reason": "一句话", "fixed": "若能补出应有机构则给，不确定给空串"}

判定示例（该列多数为机构：上海交通大学 / 复旦大学 / 浙江大学）：
- "同济大学" → ok（是机构）
- "王占山" → type_mismatch（该列应为推荐单位，却填了人名），reason "推荐单位列混入人名"
- "教育部" → ok（机构）
只在取值类型与列语义明显不符时给非 ok；拿不准给 ok（宁漏勿枉，全部 review 级挂人工终审）。"""


# 对一个文件跑列内取值类型异常软规则（#11/#13）：机构列少数派人名疑点 → 一次 LLM 批判 → L5S-02 Issue
def run_columns(imported: ImportedFile, spec: TemplateSpec | None, llm: LlmClient) -> list[Issue]:
    if spec is None or not spec.org_cols:
        return []
    suspects = collect_column_anomalies(imported, spec)
    if not suspects:
        return []

    user = json.dumps(
        [{"id": s["id"], "列": s["col_name"], "该列多数取值": s["majority"], "value": s["value"]}
         for s in suspects],
        ensure_ascii=False,
    )
    try:
        verdicts = llm.json_call(COL_SYSTEM_PROMPT, user, max_tokens=4000)
    except LlmError as exc:
        # LLM 不可用不阻塞管道：返回一条 review 提示（确定性部分照常工作）
        return [make_issue("L5S-02", batch=imported.batch, file=imported.file_name,
                           sheet=imported.sheet_name,
                           message=f"列内取值类型软规则未执行（{exc}），{len(suspects)} 个疑点值待人工")]

    by_id = {s["id"]: s for s in suspects}
    issues: list[Issue] = []
    if not isinstance(verdicts, list):
        verdicts = []
    for v in verdicts:
        if not isinstance(v, dict) or v.get("verdict", "ok") == "ok":
            continue
        s = by_id.get(v.get("id"))
        if s is None:
            continue
        issues.append(make_issue(
            "L5S-02", batch=imported.batch, file=imported.file_name, sheet=imported.sheet_name,
            row=int(s["row"]) if isinstance(s["row"], int) else None,
            field_code=str(s["col"]), field_name=str(s["col_name"]),
            message=f"{s['col_name']} 疑似列内取值类型异常：{v.get('reason', '')}（LLM 判定，待人工确认）",
            current_value=str(s["value"]), suggestion=str(v.get("fixed") or "") or None))
    return issues


# 评测评分器（纯函数，离线评测脚本与单测复用）：非 ok 视为"报警"。
# 返回总正确率（verdict 精确匹配）+ 报警二分类的 precision/recall。
def score_predictions(gold: list[str], pred: list[str]) -> dict[str, float]:
    n = len(gold)
    pairs = list(zip(gold, pred, strict=True))
    correct = sum(1 for g, p in pairs if g == p)
    tp = sum(1 for g, p in pairs if g != "ok" and p != "ok")
    fp = sum(1 for g, p in pairs if g == "ok" and p != "ok")
    fn = sum(1 for g, p in pairs if g != "ok" and p == "ok")
    precision = tp / (tp + fp) if (tp + fp) else 1.0
    recall = tp / (tp + fn) if (tp + fn) else 1.0
    return {"n": float(n), "correct": float(correct),
            "accuracy": correct / n if n else 1.0, "precision": precision, "recall": recall}
