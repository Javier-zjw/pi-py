"""MCP 客户端：JSON-RPC 2.0 之上的握手、列工具、调工具。只做 pi 用得到的部分：

- 握手 initialize → notifications/initialized；协议版本报最新的，服务器回哪个用哪个；
- tools/list（翻页）、tools/call；服务器发 notifications/tools/list_changed 时标记，下一轮重新拉；
- 服务器反过来问的：ping 回空、roots/list 回工作目录（文件类服务器靠它知道能访问哪）；
  别的（sampling、elicitation）没声明支持，回"方法不存在"。
- 请求超时 / 用户中断时发 notifications/cancelled，让服务器别再做了。

不用官方 SDK：它拖进 pydantic、anyio、starlette 一串依赖，打包变大；协议本身就是几种 JSON 消息。
"""

from __future__ import annotations

import asyncio
import itertools
from pathlib import Path
from typing import Any

from .transport import Transport, TransportError

PROTOCOL_VERSION = "2025-06-18"
CLIENT_INFO = {"name": "pi", "version": "0.1.0"}


class McpError(Exception):
    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.code = code


class McpClient:
    def __init__(self, transport: Transport, *, roots: list[str] | None = None,
                 request_timeout: float = 60.0) -> None:
        self.transport = transport
        self.roots = roots or []
        self.request_timeout = request_timeout
        self.server_info: dict = {}
        self.capabilities: dict = {}
        self.instructions = ""
        self.tools_changed = False
        self.closed_reason = ""
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future] = {}

    # -- 生命周期 ------------------------------------------------------- #

    async def start(self, timeout: float = 30.0) -> None:
        await self.transport.start(self._on_message, self._on_close)
        result = await self.request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"roots": {"listChanged": False}},
            "clientInfo": CLIENT_INFO,
        }, timeout=timeout)
        self.server_info = result.get("serverInfo") or {}
        self.capabilities = result.get("capabilities") or {}
        self.instructions = str(result.get("instructions") or "")
        version = str(result.get("protocolVersion") or PROTOCOL_VERSION)
        if hasattr(self.transport, "protocol_version"):
            self.transport.protocol_version = version   # Streamable HTTP 之后的请求要带这个头
        await self.notify("notifications/initialized")

    async def close(self) -> None:
        self._fail_all("连接已关闭")
        await self.transport.close()

    # -- 收发 ----------------------------------------------------------- #

    async def request(self, method: str, params: dict | None = None, *,
                      timeout: float | None = None,
                      cancel: asyncio.Event | None = None) -> dict:
        if self.closed_reason:
            raise McpError(self.closed_reason)
        request_id = next(self._ids)
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        message = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        try:
            try:
                await self.transport.send(message)
            except TransportError as exc:
                raise McpError(str(exc)) from exc
            waiters = [future]
            stopper = asyncio.ensure_future(cancel.wait()) if cancel is not None else None
            if stopper:
                waiters.append(stopper)
            try:
                done, _ = await asyncio.wait(waiters, timeout=timeout or self.request_timeout,
                                             return_when=asyncio.FIRST_COMPLETED)
            finally:
                if stopper:
                    stopper.cancel()
            if future in done:
                return future.result()
            reason = "用户中断" if stopper in done else f"{timeout or self.request_timeout:g} 秒没有响应"
            await self._cancel(request_id, reason)
            raise McpError(f"{method} 已取消：{reason}")
        finally:
            self._pending.pop(request_id, None)

    async def notify(self, method: str, params: dict | None = None) -> None:
        message = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        try:
            await self.transport.send(message)
        except TransportError as exc:
            raise McpError(str(exc)) from exc

    async def _cancel(self, request_id: int, reason: str) -> None:
        try:
            await self.notify("notifications/cancelled", {"requestId": request_id, "reason": reason})
        except McpError:
            pass

    async def _on_message(self, message: dict) -> None:
        if "id" in message and ("result" in message or "error" in message) and "method" not in message:
            future = self._pending.get(message["id"])
            if future is None or future.done():
                return
            if "error" in message:
                error = message["error"] or {}
                future.set_exception(McpError(str(error.get("message") or error), error.get("code")))
            else:
                future.set_result(message.get("result") or {})
            return
        method = message.get("method")
        if not method:
            return
        if "id" in message:     # 服务器反过来发的请求，要回
            await self._answer(message["id"], method)
        elif method == "notifications/tools/list_changed":
            self.tools_changed = True

    async def _answer(self, request_id: Any, method: str) -> None:
        if method == "ping":
            reply = {"jsonrpc": "2.0", "id": request_id, "result": {}}
        elif method == "roots/list":
            roots = [{"uri": Path(root).resolve().as_uri(), "name": Path(root).name or root}
                     for root in self.roots]
            reply = {"jsonrpc": "2.0", "id": request_id, "result": {"roots": roots}}
        else:
            reply = {"jsonrpc": "2.0", "id": request_id,
                     "error": {"code": -32601, "message": f"pi 不支持 {method}"}}
        try:
            await self.transport.send(reply)
        except TransportError:
            pass

    def _on_close(self, reason: str) -> None:
        self.closed_reason = reason
        self._fail_all(reason)

    def _fail_all(self, reason: str) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(McpError(reason))

    # -- 工具 ----------------------------------------------------------- #

    async def list_tools(self) -> list[dict]:
        tools: list[dict] = []
        cursor = None
        for _ in range(50):     # 翻页上限：防服务器一直给同一个 cursor
            result = await self.request("tools/list", {"cursor": cursor} if cursor else {})
            tools += [t for t in result.get("tools") or [] if isinstance(t, dict) and t.get("name")]
            cursor = result.get("nextCursor")
            if not cursor:
                break
        self.tools_changed = False
        return tools

    async def call_tool(self, name: str, arguments: dict, *, timeout: float | None = None,
                        cancel: asyncio.Event | None = None) -> dict:
        return await self.request("tools/call", {"name": name, "arguments": arguments},
                                  timeout=timeout, cancel=cancel)
