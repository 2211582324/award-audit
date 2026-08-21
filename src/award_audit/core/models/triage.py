"""分诊（triage）与降级原因（reason code）词表 + 分诊纯函数。

设计：L5 联网核对的"把握程度 + 为什么没把握"要变成**程序可读的规范信息**，才能自动排队
（把握高的快速放行、把握低的挑出来让人重点盯）。本模块是这套规范的**唯一权威定义**：
1. `decide_triage`：由 (verdict, confidence) 确定性算出分诊桶——单一真源，不散落各处；
2. `REASON_LABELS`：降级/成因码 → 中文标签，`verify_resource` 在分支点打码、展示层反查标签；
3. 排序权重（`TRIAGE_ORDER`/`CONF_ORDER`）：复核队列据此把"重点盯"浮顶、"放行"沉底。

放在 `core.models` 而非 `agent`：`agent.loop` 本就依赖 `core.models.*`，`core.pipeline`/`core.app`
也能安全 import——避免 core 反向依赖 agent（§7.7 已定的依赖方向）。
"""

from __future__ import annotations

# 分诊桶：agent 做"分诊 + 取证"、不做判决，这三个桶决定复核员怎么排队处理
TRIAGE_AUTO_PASS = "auto_pass"   # 快速放行：结论明确一致且高把握，复核员可快速确认
TRIAGE_REVIEW = "review"         # 重点复核：机器有存疑结论或把握不足，需人工重点盯
TRIAGE_MANUAL = "manual"         # 转人工取证：机器核不了/来源存疑，带着证据入口人工核

# 分诊桶 → 中文标签（展示用）
TRIAGE_LABELS: dict[str, str] = {
    TRIAGE_AUTO_PASS: "快速放行",
    TRIAGE_REVIEW: "重点复核",
    TRIAGE_MANUAL: "转人工",
}

# 分诊桶排序权重：数值小=更紧急、浮顶（转人工 > 重点复核 > 快速放行）
TRIAGE_ORDER: dict[str, int] = {
    TRIAGE_MANUAL: 0,
    TRIAGE_REVIEW: 1,
    TRIAGE_AUTO_PASS: 2,
}

# 把握档排序权重：同一分诊桶内，把握低者浮上（更该先看）
CONF_ORDER: dict[str, int] = {"low": 0, "medium": 1, "high": 2}

# 走"转人工取证"的结论：机器核不了或来源存疑，人工带证据入口核，不进自动放行/复核
_MANUAL_VERDICTS = frozenset({"无法核对", "来源年份不符"})

# 降级/成因码 → 中文标签（"为什么没把握"的规范化，verify_resource 分支点打、展示层反查）
REASON_LABELS: dict[str, str] = {
    "corpus_hit": "命中参考库缓存名单",
    "multi_source": "名单合并自多份赛道/附件",
    "image_source": "来源为图片视觉抽取（置信低）",
    "no_list": "未获取到任何官网名单",
    "year_mismatch": "官网年份与文件年份不一致",
    "year_no_match": "官网各来源年度标签均不含提交年度",
    "cross_year_dropped": "已剔除与提交零重叠的疑似跨届来源",
    "zero_overlap": "官网名单与提交零重叠（来源/年度存疑）",
    "collapsed_rows": "存在被折叠、未逐行独立核对的行",
    "year_unverified": "官网年份未能识别，未做年份交叉验证",
    "identity_needs_excel": "排名/认证类仅取到非结构化来源，需结构化名单",
}


# 由结论 + 把握档确定性算出分诊桶（单一真源，永不与 verdict/confidence 脱节）
def decide_triage(verdict: str, confidence: str) -> str:
    if verdict in _MANUAL_VERDICTS:
        return TRIAGE_MANUAL
    if verdict == "一致" and confidence == "high":  # 铁律：一致但把握非 high 绝不放行
        return TRIAGE_AUTO_PASS
    return TRIAGE_REVIEW


# 降级码 → 中文标签（未知码回退原样，容错不炸）
def reason_label(code: str) -> str:
    return REASON_LABELS.get(code, code)


# 分诊桶 → 中文标签（未知桶回退原样）
def triage_label(code: str) -> str:
    return TRIAGE_LABELS.get(code, code)


# 分诊排序键：(分诊桶权重, 把握档权重)，供复核队列 sorted() 用——重点盯浮顶
def triage_sort_key(triage: str, confidence: str) -> tuple[int, int]:
    return (TRIAGE_ORDER.get(triage, 1), CONF_ORDER.get(confidence, 1))
