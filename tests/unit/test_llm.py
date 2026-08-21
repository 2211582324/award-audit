"""LLM 封装层的单元测试：JSON 提取、缺 key 指引、mock SDK 调用（不真调 API）。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from award_audit.agent import llm as llm_mod
from award_audit.agent.llm import LlmClient, LlmError, extract_json


# 功能：验证 JSON 提取容忍三种形态：裸 JSON / ```json 包裹 / 前后闲话
# 设计：三种输入都应解析出同一对象，覆盖模型输出的常见不规范形态
def test_extract_json_forms() -> None:
    assert extract_json('{"a": 1}') == {"a": 1}
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('好的，结果如下：\n{"a": 1}') == {"a": 1}
    assert extract_json('结果：[1, 2]') == [1, 2]


# 功能：验证完全不是 JSON 的输出抛 LlmError 而非静默
# 设计：喂纯文本断言异常，守住"输出必须可解析"的契约
def test_extract_json_invalid() -> None:
    with pytest.raises(LlmError):
        extract_json("这不是 JSON")


# 功能：验证缺 ANTHROPIC_API_KEY 时报错并给配置指引（.env.example）
# 设计：清掉环境变量 + 把 load_env 换成 no-op（隔离本机可能存在的 .env），断言指引文案
def test_missing_key_guidance(monkeypatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(llm_mod.config, "load_env", lambda: None)
    client = LlmClient()
    with pytest.raises(LlmError) as ei:
        client.json_call("s", "u")
    assert ".env" in str(ei.value)


# 功能：验证 json_call 正常路径（anthropic 流式）：取回 text、解析 JSON
# 设计：默认 provider=anthropic；模拟 .stream(...) 上下文管理器 + get_final_message()
def test_json_call_with_mock(monkeypatch) -> None:
    monkeypatch.setattr(llm_mod.config, "load_env", lambda: None)
    monkeypatch.delenv("AWARD_AUDIT_PROVIDER", raising=False)
    fake_resp = SimpleNamespace(content=[SimpleNamespace(type="text", text='{"ok": true}')])

    class FakeStream:
        # 模拟 with client.messages.stream(...) as s: s.get_final_message()
        def __init__(self, **kwargs):  # noqa: ANN003
            assert kwargs["model"] == "test-model"
            assert "只输出 JSON" in kwargs["system"]

        def __enter__(self):  # noqa: ANN204
            return self

        def __exit__(self, *a):  # noqa: ANN002, ANN204
            return False

        def get_final_message(self):  # noqa: ANN201
            return fake_resp

    class FakeMessages:
        # 返回 fake 流上下文管理器
        def stream(self, **kwargs):  # noqa: ANN003, ANN201
            return FakeStream(**kwargs)

    client = LlmClient(model="test-model")
    client._client = SimpleNamespace(messages=FakeMessages())
    assert client.json_call("system", "user") == {"ok": True}


# 功能：验证 openai provider 路径：走 chat.completions、解析 JSON
# 设计：设 AWARD_AUDIT_PROVIDER=openai，注入 fake openai 客户端（choices[0].message.content）
def test_openai_json_call(monkeypatch) -> None:
    monkeypatch.setattr(llm_mod.config, "load_env", lambda: None)
    monkeypatch.setenv("AWARD_AUDIT_PROVIDER", "openai")
    fake_resp = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content='{"ok": true}'))]
    )

    class FakeCompletions:
        # 断言 OpenAI 式 messages 结构并返回固定响应
        def create(self, **kwargs):  # noqa: ANN003, ANN201
            assert kwargs["model"] == "gpt-x"
            assert kwargs["messages"][0]["role"] == "system"
            return fake_resp

    client = LlmClient(model="gpt-x")
    assert client.provider == "openai"
    client._client = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions()))
    assert client.json_call("system", "user") == {"ok": True}


# 功能：验证 provider 默认 anthropic、可 env 切 openai，以及默认模型随 provider 变
# 设计：不设 provider→anthropic 默认档；设 openai→gpt-4o 默认档
def test_provider_and_default_model(monkeypatch) -> None:
    monkeypatch.setattr(llm_mod.config, "load_env", lambda: None)
    monkeypatch.delenv("AWARD_AUDIT_MODEL", raising=False)
    monkeypatch.delenv("AWARD_AUDIT_PROVIDER", raising=False)
    assert LlmClient().provider == "anthropic"
    assert LlmClient().model == llm_mod.DEFAULT_MODEL
    monkeypatch.setenv("AWARD_AUDIT_PROVIDER", "openai")
    assert LlmClient().model == llm_mod.OPENAI_MODEL


# 功能：验证模型档位选择：默认档 / fast 档 / 环境变量覆盖
# 设计：三种构造方式断言 model 字段，锁定档位约定
def test_model_tiers(monkeypatch) -> None:
    monkeypatch.setattr(llm_mod.config, "load_env", lambda: None)
    monkeypatch.delenv("AWARD_AUDIT_MODEL", raising=False)
    monkeypatch.delenv("AWARD_AUDIT_MODEL_FAST", raising=False)
    assert LlmClient().model == llm_mod.DEFAULT_MODEL
    assert LlmClient(fast=True).model == llm_mod.FAST_MODEL
    monkeypatch.setenv("AWARD_AUDIT_MODEL", "my-model")
    assert LlmClient().model == "my-model"


# 功能：验证模型名清洗——去掉 Claude Code 式的 [1M]/[fast] 后缀（API 模型 ID 必须干净）
# 设计：env 里带 [1M] 后缀，断言构造后 model 不含方括号段
def test_model_name_cleaned(monkeypatch) -> None:
    monkeypatch.setattr(llm_mod.config, "load_env", lambda: None)
    monkeypatch.setenv("AWARD_AUDIT_MODEL", "claude-opus-4-8[1M]")
    assert LlmClient().model == "claude-opus-4-8"
    assert llm_mod._clean_model("claude-opus-4-8 [1m]") == "claude-opus-4-8"
    assert llm_mod._clean_model("claude-opus-4-8") == "claude-opus-4-8"


# 功能：验证 base_url 自动去掉结尾 /v1，避免中转站出现 /v1/v1/messages（404）
# 设计：覆盖带 /v1、带 /v1/、不带、空 四种输入；空 -> None（走官方）
def test_base_url_normalization(monkeypatch) -> None:
    monkeypatch.setattr(llm_mod.config, "load_env", lambda: None)
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://relay.com/v1")
    assert llm_mod._base_url() == "https://relay.com"
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://relay.com/v1/")
    assert llm_mod._base_url() == "https://relay.com"
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://relay.com")
    assert llm_mod._base_url() == "https://relay.com"
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    assert llm_mod._base_url() is None


def test_openai_sdk_timeout_is_retryable() -> None:
    assert llm_mod._is_transient(RuntimeError("APITimeoutError: request timed out"))
