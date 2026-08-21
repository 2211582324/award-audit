"""JSON-RPC 2.0 信封：请求 / 成功响应 / 错误响应 / 事件通知。

线格式：NDJSON（每行一个 JSON 对象）。请求带 id；事件是无 id 的通知（method="event"）。
错误码沿用 JSON-RPC 约定：-32700 解析错误 / -32600 无效请求 / -32601 方法不存在 / -32000 服务端错误。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
SERVER_ERROR = -32000


class JsonRpcRequest(BaseModel):
    """客户端 → daemon 的命令请求。"""

    jsonrpc: str = "2.0"
    id: int
    method: str                      # 如 "cmd.list_batches"
    params: dict[str, Any] = {}


class JsonRpcSuccess(BaseModel):
    """daemon → 客户端的成功响应。"""

    jsonrpc: str = "2.0"
    id: int
    result: dict[str, Any] = {}


class JsonRpcErrorBody(BaseModel):
    """错误体：码 + 人读消息。"""

    code: int
    message: str


class JsonRpcError(BaseModel):
    """daemon → 客户端的错误响应。"""

    jsonrpc: str = "2.0"
    id: int | None = None
    error: JsonRpcErrorBody


class JsonRpcNotification(BaseModel):
    """daemon → 订阅客户端的事件通知（无 id）。"""

    jsonrpc: str = "2.0"
    method: str = "event"
    params: dict[str, Any] = {}


# 构造错误响应
def make_error(req_id: int | None, code: int, message: str) -> JsonRpcError:
    return JsonRpcError(id=req_id, error=JsonRpcErrorBody(code=code, message=message))
