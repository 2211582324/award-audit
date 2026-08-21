"""daemon 客户端：命令连接（请求-响应）+ 订阅连接（事件流）。

两条 TCP 连接分工：call() 走命令连接串行收发；subscribe() 单开一条连接，
后台循环读事件行并回调——TUI 用它做实时刷新。CLI/测试也可复用本客户端。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any


class CoreClientError(RuntimeError):
    """daemon 返回错误响应。"""


# 单行读缓冲上限：与服务端一致，大批次记录列表的一行响应可达数 MB
READ_LIMIT = 16 * 1024 * 1024


class CoreClient:
    """award-core 的异步客户端。"""

    # 记录目标地址
    def __init__(self, host: str = "127.0.0.1", port: int = 7438) -> None:
        self.host = host
        self.port = port
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._next_id = 0
        self._lock = asyncio.Lock()

    # 建立命令连接
    async def connect(self) -> None:
        self._reader, self._writer = await asyncio.open_connection(self.host, self.port, limit=READ_LIMIT)

    # 关闭命令连接
    async def close(self) -> None:
        if self._writer is not None:
            self._writer.close()
            self._reader = self._writer = None

    # 发一个命令并等响应；错误响应抛 CoreClientError
    async def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if self._reader is None or self._writer is None:
            raise CoreClientError("未连接 daemon（先 connect）")
        async with self._lock:  # 串行化请求-响应，避免行序错乱
            self._next_id += 1
            req = {"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params or {}}
            self._writer.write((json.dumps(req, ensure_ascii=False) + "\n").encode("utf-8"))
            await self._writer.drain()
            raw = await self._reader.readline()
        if not raw:
            raise CoreClientError("daemon 断开连接")
        resp: dict[str, Any] = json.loads(raw.decode("utf-8"))
        if "error" in resp:
            err = resp["error"]
            raise CoreClientError(f"[{err.get('code')}] {err.get('message')}")
        result: dict[str, Any] = resp.get("result", {})
        return result

    # 单开订阅连接，循环把事件 params 交给回调；随连接关闭而结束
    async def subscribe(self, on_event: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
        reader, writer = await asyncio.open_connection(self.host, self.port, limit=READ_LIMIT)
        try:
            writer.write((json.dumps({"jsonrpc": "2.0", "id": 0, "method": "subscribe", "params": {}}) + "\n").encode("utf-8"))
            await writer.drain()
            await reader.readline()  # 消费订阅确认
            while True:
                raw = await reader.readline()
                if not raw:
                    break
                msg = json.loads(raw.decode("utf-8"))
                if msg.get("method") == "event":
                    await on_event(msg.get("params", {}))
        finally:
            writer.close()
