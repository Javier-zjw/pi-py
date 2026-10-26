"""管理一组 MCP 服务器：并行连接（每个有超时）、记状态、把它们的工具变成 pi 的 AgentTool。

工具名 ``mcp__<服务器>__<工具>``（同 Claude Code）：一眼看出来自哪个服务器，权限规则也好写
（``mcp__github__*``、``mcp__github``）。名字只留字母数字下划线横线、最长 64（各家 API 的限制）。

某个服务器连不上不影响别的，也不影响 pi 本身：状态里记下原因，``/mcp`` 里看得到。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

from pi_agent import AgentTool, AgentToolResult, ToolContext
from pi_ai import ImageContent, TextContent

from .client import McpClient, McpError
from .config import ServerConfig
from .transport import (
    HttpTransport,
    Opener,
    SseTransport,
    StdioTransport,
    TransportError,
    httpx_opener,
)

MAX_OUTPUT_CHARS = 50_000
DESCRIPTION_CHARS = 2000


@dataclass
class ServerState:
    config: ServerConfig
    status: str = "pending"         # pending / connecting / ready / failed / disabled
    error: str = ""
    client: McpClient | None = None
    tools: list[dict] = field(default_factory=list)
    transport_used: str = ""

    @property
    def name(self) -> str:
        return self.config.name


def tool_name(server: str, tool: str) -> str:
    raw = f"mcp__{server}__{tool}"
    name = re.sub(r"[^A-Za-z0-9_-]", "_", raw)
    if len(name) > 64:
        digest = hashlib.sha1(raw.encode()).hexdigest()[:6]
        name = f"{name[:57]}_{digest}"
    return name


def clean_schema(schema: Any) -> dict:
    """服务器给的 inputSchema 基本原样用；去掉个别服务商不认的键，保证顶层是 object"""
    schema = dict(schema) if isinstance(schema, dict) else {}
    for key in ("$schema", "$id"):
        schema.pop(key, None)
    schema["type"] = "object"
    if not isinstance(schema.get("properties"), dict):
        schema["properties"] = {}
    return schema


def convert_result(result: dict, limit: int = MAX_OUTPUT_CHARS) -> AgentToolResult:
    """MCP 的 content（text / image / audio / resource / resource_link）→ pi 的工具结果"""
    texts: list[str] = []
    images: list[ImageContent] = []
    for item in result.get("content") or []:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "text":
            texts.append(str(item.get("text") or ""))
        elif kind == "image" and item.get("data"):
            images.append(ImageContent(data=str(item["data"]), mime_type=str(item.get("mimeType") or "image/png")))
        elif kind == "audio":
            size = len(str(item.get("data") or "")) * 3 // 4      # base64 → 大约的字节数
            texts.append(f"[音频 {item.get('mimeType', '')}，{size} 字节，这里不能播放]")
        elif kind == "resource":
            resource = item.get("resource") or {}
            if "text" in resource:
                texts.append(f"[资源 {resource.get('uri', '')}]\n{resource['text']}")
            else:
                texts.append(f"[二进制资源 {resource.get('uri', '')} {resource.get('mimeType', '')}]")
        elif kind == "resource_link":
            texts.append(f"[链接 {item.get('name') or ''} {item.get('uri', '')}]".replace("  ", " "))
    if not texts and result.get("structuredContent") is not None:
        texts.append(json.dumps(result["structuredContent"], ensure_ascii=False, indent=2))
    text = "\n\n".join(t for t in texts if t)
    if len(text) > limit:
        text = text[:limit] + f"\n\n…（输出太长，省略了后面 {len(text) - limit} 字）"
    if not text and not images:
        text = "（工具没有返回内容）"
    content: list[TextContent | ImageContent] = [TextContent(text=text)] if text else []
    content += images
    return AgentToolResult(content=content, is_error=bool(result.get("isError")),
                           details={"chars": len(text), "images": len(images)})


class McpManager:
    def __init__(self, servers: list[ServerConfig], *, cwd: str, opener: Opener = httpx_opener,
                 startup_timeout: float = 60.0, tool_timeout: float = 120.0,
                 max_output: int = MAX_OUTPUT_CHARS) -> None:
        self.cwd = cwd
        self.opener = opener
        self.startup_timeout = startup_timeout
        self.tool_timeout = tool_timeout
        self.max_output = max_output
        self.servers = {s.name: ServerState(s, status="disabled" if s.disabled else "pending")
                        for s in servers}
        self._started = False
        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()      # 后台连接中的服务器

    # -- 连接 ----------------------------------------------------------- #

    def start(self) -> None:
        """第一次用到时在后台并行连接所有服务器，不等（重连走 reconnect）"""
        if self._started:
            return
        self._started = True
        for state in self.servers.values():
            if state.status == "pending":
                task = asyncio.ensure_future(self._connect(state))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)

    async def wait(self, timeout: float | None = None) -> list[str]:
        """等后台连接做完，最多 timeout 秒（None 一直等）。返回还没连完的服务器名。

        提问前只等一小会儿：npx / uvx 每次启动都要去源上查版本，网络慢时一个服务器能卡几十秒，
        等它连完再把问题交给模型，屏幕上就什么动静都没有、像模型不回复了。没连完的这一轮先不用，
        连上之后下一轮自动加进来"""
        pending = [task for task in self._tasks if not task.done()]
        if pending:
            await asyncio.wait(pending, timeout=timeout)
        return [s.name for s in self.servers.values() if s.status in ("pending", "connecting")]

    async def ensure_started(self) -> None:
        """开始连接并等全部连完（/mcp、/mcp tools、预连接用：要看到完整结果）"""
        self.start()
        await self.wait()

    async def reconnect(self, name: str | None = None) -> None:
        async with self._lock:
            self._started = True
            targets = [s for s in self.servers.values()
                       if s.status != "disabled" and (name is None or s.name == name)]
            for state in targets:
                await self._disconnect(state)
            await asyncio.gather(*(self._connect(s) for s in targets))

    async def add(self, config: ServerConfig) -> ServerState:
        """加一个（或替换同名的）服务器并马上连，别的服务器不动"""
        async with self._lock:
            old = self.servers.get(config.name)
            if old is not None:
                await self._disconnect(old)
            state = ServerState(config, status="disabled" if config.disabled else "pending")
            self.servers[config.name] = state
            if not config.disabled:
                await self._connect(state)
            return state

    async def remove(self, name: str) -> None:
        async with self._lock:
            state = self.servers.pop(name, None)
            if state is not None:
                await self._disconnect(state)

    async def _connect(self, state: ServerState) -> None:
        config = state.config
        state.status, state.error, state.tools = "connecting", "", []
        if config.missing_vars:
            state.status = "failed"
            state.error = "缺少环境变量 " + "、".join(config.missing_vars)
            return
        kinds = ["http", "sse"] if config.transport == "auto" else [config.transport]
        errors = []
        for kind in kinds:
            client = McpClient(self._transport(config, kind), roots=[self.cwd],
                               request_timeout=config.timeout or self.tool_timeout)
            try:
                await asyncio.wait_for(client.start(timeout=self.startup_timeout), self.startup_timeout)
                tools = await asyncio.wait_for(client.list_tools(), self.startup_timeout)
            except (McpError, TransportError, asyncio.TimeoutError, OSError, ValueError) as exc:
                message = getattr(exc, "message", None) or str(exc) or f"{self.startup_timeout:g} 秒内没连上"
                stderr = getattr(client.transport, "stderr", None)
                if stderr:
                    message += "｜服务器输出：" + " / ".join(list(stderr)[-3:])
                errors.append(f"{kind}：{message}" if len(kinds) > 1 else message)
                await self._close_quietly(client)
                continue
            state.client, state.tools, state.transport_used = client, tools, kind
            state.status = "ready"
            return
        state.status, state.error = "failed", "；".join(errors)

    def _transport(self, config: ServerConfig, kind: str):
        if kind == "stdio":
            return StdioTransport(config.command, config.args, config.env, config.cwd or self.cwd)
        if kind == "sse":
            return SseTransport(config.url, config.headers, self.opener)
        return HttpTransport(config.url, config.headers, self.opener)

    async def _close_quietly(self, client: McpClient) -> None:
        try:
            await asyncio.wait_for(client.close(), 5)
        except Exception:  # noqa: BLE001, S110 —— 关不干净也不能影响别的
            pass

    async def _disconnect(self, state: ServerState) -> None:
        if state.client is not None:
            await self._close_quietly(state.client)
        state.client, state.tools = None, []
        if state.status != "disabled":
            state.status = "pending"

    async def close(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        for state in self.servers.values():
            await self._disconnect(state)
        self._started = False

    async def refresh_changed(self) -> None:
        """服务器说工具列表变了：重新拉"""
        for state in self.servers.values():
            client = state.client
            if state.status == "ready" and client is not None and client.tools_changed:
                try:
                    state.tools = await client.list_tools()
                except McpError as exc:
                    state.error = f"刷新工具列表失败：{exc.message}"
            if state.status == "ready" and client is not None and client.closed_reason:
                state.status, state.error = "failed", client.closed_reason

    # -- 工具 ----------------------------------------------------------- #

    def agent_tools(self, read_only: bool = False) -> list[AgentTool]:
        """所有已连上服务器的工具。read_only：只要声明了 readOnlyHint 的（计划模式用）"""
        tools: list[AgentTool] = []
        seen: set[str] = set()
        for state in self.servers.values():
            if state.status != "ready":
                continue
            for spec in state.tools:
                annotations = spec.get("annotations") or {}
                if read_only and not annotations.get("readOnlyHint"):
                    continue
                name = tool_name(state.name, str(spec["name"]))
                if name in seen:
                    continue
                seen.add(name)
                tools.append(self._agent_tool(state, spec, name))
        return tools

    def _agent_tool(self, state: ServerState, spec: dict, name: str) -> AgentTool:
        original = str(spec["name"])
        title = spec.get("title") or (spec.get("annotations") or {}).get("title") or ""
        description = str(spec.get("description") or title or original).strip()
        if len(description) > DESCRIPTION_CHARS:
            description = description[:DESCRIPTION_CHARS] + "…"
        timeout = state.config.timeout or self.tool_timeout

        async def execute(args: dict, ctx: ToolContext) -> AgentToolResult:
            client = state.client
            if state.status != "ready" or client is None:
                return AgentToolResult.text(
                    f"MCP 服务器 {state.name} 现在不可用（{state.error or state.status}）。"
                    "可以让用户用 /mcp reconnect 重连", is_error=True)
            try:
                result = await client.call_tool(original, args, timeout=timeout, cancel=ctx.cancel_event)
            except McpError as exc:
                if client.closed_reason:
                    state.status, state.error = "failed", client.closed_reason
                return AgentToolResult.text(f"MCP 工具 {state.name}/{original} 出错：{exc.message}",
                                            is_error=True, details={"server": state.name, "tool": original})
            converted = convert_result(result, self.max_output)
            converted.details = {**(converted.details or {}), "server": state.name, "tool": original}
            return converted

        return AgentTool(name=name, description=f"[MCP {state.name}] {description}",
                         parameters=clean_schema(spec.get("inputSchema")), execute=execute,
                         label=f"{state.name} · {title or original}")
