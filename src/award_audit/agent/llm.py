"""LLM 封装：JSON 抽取 + 视觉抽取，provider 可切换（Claude / GPT 等）。

我们让 LLM 做的只有"按 schema 抽 JSON"和"看图抽名单"——通用任务，不锁厂商。
通过 env 切换后端：
- AWARD_AUDIT_PROVIDER=anthropic（默认）：anthropic SDK，Anthropic 格式（/v1/messages）
- AWARD_AUDIT_PROVIDER=openai：openai SDK，OpenAI 格式（/v1/chat/completions），可接 GPT 或任何
  OpenAI 兼容中转站
接入地址 AWARD_AUDIT_BASE_URL（或 ANTHROPIC_BASE_URL）、密钥 ANTHROPIC_API_KEY、模型 AWARD_AUDIT_MODEL。

安全约定：密钥只进本进程 os.environ；不打印、不记录、不进报告/台账。
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any

from award_audit.core import config

# 各 provider 的默认模型（env 未指定时兜底）
DEFAULT_MODEL = "claude-opus-4-8"
FAST_MODEL = "claude-haiku-4-5"
OPENAI_MODEL = "gpt-4o"       # 视觉 + JSON 都行
OPENAI_FAST = "gpt-4o-mini"

_JSON_BLOCK = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


class LlmError(RuntimeError):
    """LLM 调用失败（配置缺失 / API 错误 / 输出不可解析）。"""


# 把常见的中转站/网络错误翻译成人话提示，附在异常消息后
def _hint(exc: Exception) -> str:
    s = str(exc)
    if "client_restricted" in s or "does not allow the current client" in s:
        return ("\n  ↳ 该中转站按客户端指纹放行（只认 Claude Code 等特定客户端），"
                "拒绝裸 SDK/自建程序——换一个不做客户端限制的通用中转站或用官方 key。")
    if "503" in s or "Service Unavailable" in s:
        return "\n  ↳ 503 多为中转站上游过载或对本请求的拦截，换线路/换中转站或稍后重试。"
    if ("RemoteProtocolError" in s or "incomplete chunked read" in s
            or "peer closed connection" in s):
        return ("\n  ↳ 中转站在流式响应中途断连（多因大请求/长响应不稳）。已自动重试若干次仍失败；"
                "换中转站线路，或切 AWARD_AUDIT_PROVIDER=openai 接更稳的兼容站。")
    if "invalid x-api-key" in s or "401" in s:
        return "\n  ↳ 401：key 与接入地址不匹配（中转站 key 不能发官方地址，反之亦然），检查 base_url。"
    if "404" in s and "/v1/v1" in s:
        return "\n  ↳ 路径出现 /v1/v1：base_url 结尾多了 /v1，去掉即可（代码通常会自动处理）。"
    return ""


# provider：anthropic（默认）/ openai
def _provider() -> str:
    config.load_env()
    return (os.environ.get("AWARD_AUDIT_PROVIDER", "anthropic").strip().lower() or "anthropic")


# 取 API key：缺失时给出明确配置指引
def _api_key() -> str:
    config.load_env()
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        raise LlmError(
            "缺少 ANTHROPIC_API_KEY：请复制 .env.example 为 .env 并填入密钥"
            "（或在终端 set ANTHROPIC_API_KEY=...）"
        )
    return key


# 原始接入地址：AWARD_AUDIT_BASE_URL 优先，回退 ANTHROPIC_BASE_URL；空 -> None（走官方）
def _raw_base() -> str | None:
    config.load_env()
    return (os.environ.get("AWARD_AUDIT_BASE_URL") or os.environ.get("ANTHROPIC_BASE_URL") or "").strip() or None


# anthropic 接入地址：去掉结尾 /v1（SDK 会自补 /v1/messages，否则 /v1/v1）
def _base_url() -> str | None:
    raw = _raw_base()
    return re.sub(r"/v1/?$", "", raw.rstrip("/")) if raw else None


# openai 接入地址：确保以 /v1 结尾（openai SDK 会自补 /chat/completions）
def _base_url_openai() -> str | None:
    raw = _raw_base()
    if not raw:
        return None
    raw = raw.rstrip("/")
    return raw if raw.endswith("/v1") else raw + "/v1"


# 清洗模型名：去掉客户端式 [1M]/[fast] 方括号后缀（API 模型 ID 必须干净）
def _clean_model(name: str) -> str:
    return re.sub(r"\s*\[[^\]]*\]\s*$", "", name.strip())


_DEFAULT_1M_BETA = "context-1m-2025-08-07"


# 是否启用 1M 上下文（仅 anthropic；AWARD_AUDIT_1M=1 时开）
def _use_1m() -> bool:
    config.load_env()
    return os.environ.get("AWARD_AUDIT_1M", "").strip() in ("1", "true", "yes", "on")


# 1M 上下文 beta 标识（可 env 覆盖）
def _beta_1m() -> str:
    config.load_env()
    return os.environ.get("AWARD_AUDIT_1M_BETA", "").strip() or _DEFAULT_1M_BETA


# LLM 调用重试次数（中转站流式偶发断连/过载，AWARD_AUDIT_LLM_RETRIES 覆盖，默认 3）
def _max_retries() -> int:
    config.load_env()
    raw = os.environ.get("AWARD_AUDIT_LLM_RETRIES", "").strip()
    try:
        return max(1, int(raw)) if raw else 3
    except ValueError:
        return 3


# 瞬时错误特征（连接断/流中途截断/超时/过载）——这类可重试，鉴权/参数类错误则不重试
_TRANSIENT_HINTS = (
    "RemoteProtocolError", "incomplete chunked read", "peer closed connection",
    "APIConnectionError", "APITimeoutError", "ConnectionError", "ReadTimeout", "ConnectTimeout",
    "ReadError", "503", "Service Unavailable", "Overloaded", "overloaded_error", "502", "504",
)


# 判断异常是否为可重试的瞬时错误
def _is_transient(exc: Exception) -> bool:
    s = f"{type(exc).__name__}: {exc}"
    return any(t in s for t in _TRANSIENT_HINTS)


# 单次 LLM 请求超时秒数（中转站会挂起不返回，必须设上限；AWARD_AUDIT_LLM_TIMEOUT 覆盖，默认 90）
def _timeout() -> float:
    config.load_env()
    raw = os.environ.get("AWARD_AUDIT_LLM_TIMEOUT", "").strip()
    try:
        return float(raw) if raw else 90.0
    except ValueError:
        return 90.0


# 从模型回复文本中提取 JSON（容忍 ```json 包裹与前后闲话）
def extract_json(text: str) -> Any:
    m = _JSON_BLOCK.search(text)
    candidate = m.group(1) if m else text
    positions = [i for i in (candidate.find("{"), candidate.find("[")) if i >= 0]
    if positions:
        candidate = candidate[min(positions):]
    try:
        return json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise LlmError(f"模型输出不是合法 JSON：{exc}") from exc


class LlmClient:
    """LLM 客户端：json_call / vision_json_call 为入口，按 provider 走对应后端。"""

    # 初始化：定 provider 与模型（env 优先，按 provider 兜底），lazy 建 SDK 客户端
    def __init__(self, fast: bool = False, model: str | None = None) -> None:
        config.load_env()
        self.provider = _provider()
        if model:
            self.model = model
        else:
            env_key = "AWARD_AUDIT_MODEL_FAST" if fast else "AWARD_AUDIT_MODEL"
            if self.provider == "openai":
                default = OPENAI_FAST if fast else OPENAI_MODEL
            else:
                default = FAST_MODEL if fast else DEFAULT_MODEL
            self.model = os.environ.get(env_key, default)
        self.model = _clean_model(self.model)
        self._client: Any = None
        # The Harness reads this after json_call so failed/compatibility routes
        # do not silently lose provider-reported token usage.
        self.last_usage: Any = None

    # 建立/复用 SDK 客户端（按 provider）
    def _sdk(self) -> Any:
        if self._client is not None:
            return self._client
        # Validate configuration before importing an optional SDK so a missing
        # key is reported accurately even when that SDK is not installed.
        api_key = _api_key()
        if self.provider == "openai":
            try:
                import openai
            except ImportError as exc:
                raise LlmError("需要 openai SDK：pip install openai") from exc
            # timeout 限单次请求上限、max_retries=0 关掉 SDK 自带重试（改由本类可控重试），避免叠加空转
            kwargs: dict[str, Any] = {"api_key": api_key, "timeout": _timeout(), "max_retries": 0}
            base = _base_url_openai()
            if base:
                kwargs["base_url"] = base
            self._client = openai.OpenAI(**kwargs)
        else:
            try:
                import anthropic
            except ImportError as exc:
                raise LlmError("需要 anthropic SDK：pip install anthropic") from exc
            kwargs = {"api_key": api_key, "timeout": _timeout(), "max_retries": 0}
            base = _base_url()
            if base:
                kwargs["base_url"] = base
            self._client = anthropic.Anthropic(**kwargs)
        return self._client

    # anthropic 可选参数：启用 1M 时带 beta header
    def _extra(self) -> dict[str, Any]:
        if _use_1m():
            return {"extra_headers": {"anthropic-beta": _beta_1m()}}
        return {}

    # 发一次 JSON 任务，返回解析后的对象
    def json_call(self, system: str, user: str, max_tokens: int = 2000) -> Any:
        self.last_usage = None
        sys_prompt = system + "\n\n只输出 JSON，不要任何解释文字。"
        if self.provider == "openai":
            return self._openai_json(sys_prompt, [{"type": "text", "text": user}], max_tokens)
        return self._anthropic_json(sys_prompt, user, max_tokens)

    # 发一次带图片的 JSON 任务（页面图片名单的视觉抽取）
    def vision_json_call(self, system: str, user: str, image_bytes: bytes,
                         media_type: str, max_tokens: int = 4000) -> Any:
        import base64

        self.last_usage = None
        sys_prompt = system + "\n\n只输出 JSON，不要任何解释文字。"
        b64 = base64.b64encode(image_bytes).decode("ascii")
        if self.provider == "openai":
            content = [
                {"type": "text", "text": user},
                {"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{b64}"}},
            ]
            return self._openai_json(sys_prompt, content, max_tokens)
        content_a = [
            {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}},
            {"type": "text", "text": user},
        ]
        return self._anthropic_json_msg(sys_prompt, content_a, max_tokens, "视觉调用")

    # —— anthropic 后端（流式，兼容中转站）——

    def _anthropic_json(self, sys_prompt: str, user: str, max_tokens: int) -> Any:
        return self._anthropic_json_msg(sys_prompt, user, max_tokens, "调用")

    def _anthropic_json_msg(self, sys_prompt: str, content: Any, max_tokens: int, label: str) -> Any:
        client = self._sdk()
        attempts = _max_retries()
        for i in range(attempts):
            try:
                with client.messages.stream(
                    model=self.model, max_tokens=max_tokens, system=sys_prompt,
                    messages=[{"role": "user", "content": content}], **self._extra(),
                ) as stream:
                    resp = stream.get_final_message()
                self.last_usage = getattr(resp, "usage", None)
                break
            except Exception as exc:  # noqa: BLE001  瞬时错误重试，其余立即抛
                if i < attempts - 1 and _is_transient(exc):
                    time.sleep(1.5 * (i + 1))  # 退避：1.5s、3s…
                    continue
                raise LlmError(f"Claude API {label}失败：{type(exc).__name__}: {exc}{_hint(exc)}") from exc
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        return extract_json(text)

    # —— openai 后端（chat.completions，可接 GPT 或 OpenAI 兼容中转站）——

    def _openai_json(self, sys_prompt: str, user_content: Any, max_tokens: int) -> Any:
        client = self._sdk()
        attempts = _max_retries()
        for i in range(attempts):
            try:
                resp = client.chat.completions.create(
                    model=self.model, max_tokens=max_tokens,
                    messages=[
                        {"role": "system", "content": sys_prompt},
                        {"role": "user", "content": user_content},
                    ],
                )
                self.last_usage = getattr(resp, "usage", None)
                break
            except Exception as exc:  # noqa: BLE001  瞬时错误重试，其余立即抛
                if i < attempts - 1 and _is_transient(exc):
                    time.sleep(1.5 * (i + 1))
                    continue
                raise LlmError(f"OpenAI 兼容 API 调用失败：{type(exc).__name__}: {exc}{_hint(exc)}") from exc
        text = resp.choices[0].message.content or ""
        return extract_json(text)
