"""NDJSON JSON-RPC 服务器：读行 → 解析请求 → 分发 handler → 写响应；订阅连接收事件广播。

复刻 KamaClaude socket_server 的模式：
- handler 通过 register("cmd.xxx", fn) 注册，fn: (params: dict) -> dict（同步——本地单用户，sqlite 快）；
- 特殊方法 "subscribe" 把当前连接转为订阅者，此后 publish() 的事件以通知形式推给它；
- 三类协议错误各有错误码：解析失败 / 方法不存在 / handler 异常。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, ValidationError

from award_audit.core.bus.envelope import (
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    SERVER_ERROR,
    JsonRpcNotification,
    JsonRpcRequest,
    JsonRpcSuccess,
    make_error,
)

CommandHandler = Callable[[dict[str, Any]], dict[str, Any]]

# 单行读缓冲上限：NDJSON 一行一消息，大批次的记录列表响应可达数 MB（asyncio 默认 64KB 会炸）
READ_LIMIT = 16 * 1024 * 1024


class SocketServer:
    """TCP NDJSON JSON-RPC 服务器 + 事件广播。"""

    # 记录监听地址与注册表
    def __init__(self, host: str = "127.0.0.1", port: int = 7438) -> None:
        self.host = host
        self.port = port
        self._handlers: dict[str, CommandHandler] = {}
        self._subscribers: set[asyncio.StreamWriter] = set()
        self._server: asyncio.Server | None = None

    # 注册命令处理器（method 形如 "cmd.list_batches"）
    def register(self, method: str, handler: CommandHandler) -> None:
        self._handlers[method] = handler

    # 启动监听
    async def start(self) -> None:
        self._server = await asyncio.start_server(self._on_client, self.host, self.port, limit=READ_LIMIT)

    # 停止监听并断开订阅者
    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        for w in list(self._subscribers):
            w.close()
        self._subscribers.clear()

    # 向所有订阅连接广播一个事件（pydantic 模型 → 通知）
    async def publish(self, event: BaseModel) -> None:
        note = JsonRpcNotification(params=event.model_dump())
        line = note.model_dump_json() + "\n"
        dead: list[asyncio.StreamWriter] = []
        for w in self._subscribers:
            try:
                w.write(line.encode("utf-8"))
                await w.drain()
            except (ConnectionError, RuntimeError):
                dead.append(w)
        for w in dead:
            self._subscribers.discard(w)

    # 单连接生命周期：逐行读请求、分发、写响应；断开时移除订阅
    async def _on_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                raw = await reader.readline()
                if not raw:
                    break
                await self._handle_line(raw, writer)
        except ConnectionError:
            pass
        finally:
            self._subscribers.discard(writer)
            writer.close()

    # 处理一行：解析 → subscribe 特判 → handler 分发 → 响应
    async def _handle_line(self, raw: bytes, writer: asyncio.StreamWriter) -> None:
        try:
            req = JsonRpcRequest.model_validate(json.loads(raw.decode("utf-8")))
        except (json.JSONDecodeError, ValidationError, UnicodeDecodeError):
            await self._send(writer, make_error(None, PARSE_ERROR, "请求不是合法的 JSON-RPC"))
            return

        if req.method == "subscribe":
            self._subscribers.add(writer)
            await self._send(writer, JsonRpcSuccess(id=req.id, result={"subscribed": True}))
            return

        handler = self._handlers.get(req.method)
        if handler is None:
            await self._send(writer, make_error(req.id, METHOD_NOT_FOUND, f"未知方法：{req.method}"))
            return
        try:
            result = handler(req.params)
        except Exception as exc:  # handler 内业务异常统一转 SERVER_ERROR，不让 daemon 崩
            await self._send(writer, make_error(req.id, SERVER_ERROR, f"{type(exc).__name__}: {exc}"))
            return
        await self._send(writer, JsonRpcSuccess(id=req.id, result=result))

    # 发送一个响应并刷新写缓冲区
    async def _send(self, writer: asyncio.StreamWriter, msg: BaseModel) -> None:
        writer.write((msg.model_dump_json() + "\n").encode("utf-8"))
        await writer.drain()
