"""L5 软规则（LLM 语义核查）的单元测试——全部 mock LLM，不真调 API。"""

from __future__ import annotations

from award_audit.agent import soft_rules
from award_audit.agent.llm import LlmError


class FakeLlm:
    """可编程的假 LLM：记录收到的 user、返回预设 verdicts。"""

    def __init__(self, verdicts=None, error: str | None = None):  # noqa: ANN001
        self.verdicts = verdicts or []
        self.error = error
        self.last_user: str | None = None

    # 模拟 json_call：可抛错或返回预设
    def json_call(self, system: str, user: str, max_tokens: int = 2000):  # noqa: ANN201
        self.last_user = user
        if self.error:
            raise LlmError(self.error)
        return self.verdicts


# 功能：验证疑点启发式：角色词/乱码痕/中英混杂/超长判疑，干净名字不判疑（不花钱）
# 设计：正反例逐一断言，锁定"只送疑点"的成本闸门
def test_is_suspect() -> None:
    assert soft_rules.is_suspect("主编：危道军 副主编：程红艳")
    assert soft_rules.is_suspect("张_x000D_楠")
    assert soft_rules.is_suspect("李明John-Lee")
    assert soft_rules.is_suspect("很长" * 25)
    assert not soft_rules.is_suspect("张三;李四")
    assert not soft_rules.is_suspect("John-Smith")


# 功能：验证 LLM 判定非 ok 的值转成 review 级 L5S-01 Issue 并带建议值
# 设计：一行角色混排 + 一行干净名，fake LLM 判前者 role_mixed；断言恰 1 条、定位行/列、建议值透传
def test_run_file_verdict_to_issue(kit, xwlwhj_spec) -> None:
    imp = kit.build([
        {"ZYLBM": "04050014", "ZYLB": kit.AWARD, "LWTM": "t", "ZZXM": "主编：危道军 副主编：程红艳", "PDNY": "2024"},
        {"ZYLBM": "04050014", "ZYLB": kit.AWARD, "LWTM": "t2", "ZZXM": "张三", "PDNY": "2024"},
    ])
    fake = FakeLlm(verdicts=[{"id": 0, "verdict": "role_mixed", "reason": "含主编/副主编角色词", "fixed": "危道军;程红艳"}])
    issues = soft_rules.run_file(imp, xwlwhj_spec, fake)  # type: ignore[arg-type]
    assert len(issues) == 1
    i = issues[0]
    assert i.rule_id == "L5S-01" and i.severity.value == "review"
    assert i.row == 1 and i.field_code == "ZZXM" and i.suggestion == "危道军;程红艳"
    # 干净名字没被送进请求（成本闸门）
    assert fake.last_user is not None and "张三" not in fake.last_user


# 功能：验证无疑点时不发起 LLM 请求（零成本路径）
# 设计：全干净名字，断言返回空且 fake 未被调用
def test_run_file_no_suspects_no_call(kit, xwlwhj_spec) -> None:
    imp = kit.build([{"ZYLBM": "04050014", "ZYLB": kit.AWARD, "LWTM": "t", "ZZXM": "张三;李四", "PDNY": "2024"}])
    fake = FakeLlm()
    assert soft_rules.run_file(imp, xwlwhj_spec, fake) == []  # type: ignore[arg-type]
    assert fake.last_user is None


# 功能：验证 LLM 不可用时不阻塞管道，产出一条"未执行待人工"的 review 提示
# 设计：fake 抛 LlmError，断言仍返回 L5S-01 一条且消息含疑点数——确定性管道不受智能层故障牵连
def test_run_file_llm_down_degrades(kit, xwlwhj_spec) -> None:
    imp = kit.build([{"ZYLBM": "04050014", "ZYLB": kit.AWARD, "LWTM": "t", "ZZXM": "主编：某人", "PDNY": "2024"}])
    issues = soft_rules.run_file(imp, xwlwhj_spec, FakeLlm(error="没有 key"))  # type: ignore[arg-type]
    assert len(issues) == 1 and "未执行" in issues[0].message and "1 个疑点" in issues[0].message
