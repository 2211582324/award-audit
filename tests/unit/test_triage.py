"""分诊纯函数 + 词表的单元测试：把握档×结论 → 分诊桶的确定性映射，守"置信低绝不放行"铁律。"""

from __future__ import annotations

from award_audit.core.models.triage import (
    CONF_ORDER,
    TRIAGE_ORDER,
    decide_triage,
    reason_label,
    triage_label,
    triage_sort_key,
)


# 功能：验证 decide_triage 的全档位映射——只有"一致×high"放行，无法核对/年份不符转人工，其余复核
# 设计：逐 (verdict, confidence) 断言桶归属，重点锁"一致×medium/low 绝不 auto_pass"（§7.5 铁律）
def test_decide_triage_buckets() -> None:
    assert decide_triage("一致", "high") == "auto_pass"
    assert decide_triage("一致", "medium") == "review"   # 铁律：置信非 high 不放行
    assert decide_triage("一致", "low") == "review"
    assert decide_triage("基本一致（需人工抽核）", "medium") == "review"
    assert decide_triage("疑似缺漏", "high") == "review"
    assert decide_triage("疑似多采", "high") == "review"
    assert decide_triage("无法核对", "low") == "manual"
    assert decide_triage("来源年份不符", "high") == "manual"  # 来源存疑，把握再高也转人工


# 功能：验证排序键让"转人工/低把握"浮顶、"快速放行/高把握"沉底
# 设计：断言 manual<review<auto_pass、同桶内 low<high；对一组结论排序断言首尾
def test_triage_sort_key_orders_urgent_first() -> None:
    assert TRIAGE_ORDER["manual"] < TRIAGE_ORDER["review"] < TRIAGE_ORDER["auto_pass"]
    assert CONF_ORDER["low"] < CONF_ORDER["high"]
    rows = [("一致", "high"), ("无法核对", "low"), ("疑似缺漏", "medium")]
    ordered = sorted(rows, key=lambda t: triage_sort_key(decide_triage(*t), t[1]))
    assert ordered[0] == ("无法核对", "low")   # 转人工浮顶
    assert ordered[-1] == ("一致", "high")     # 放行沉底


# 功能：验证标签工具对已知码给中文、未知码回退原样（容错不炸）
# 设计：断言已知降级码/分诊桶有中文标签，未知码原样返回
def test_labels_fallback() -> None:
    assert triage_label("manual") == "转人工"
    assert reason_label("image_source") == "来源为图片视觉抽取（置信低）"
    assert reason_label("unknown_code") == "unknown_code"  # 未知码回退
    assert triage_label("weird") == "weird"
