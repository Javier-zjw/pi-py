"""MCP 客户端测试：不联网、不装东西。

    python tests/test_mcp.py
    pytest tests/test_mcp.py

- 配置：各客户端的写法、${VAR} 替换、项目覆盖全局、从别的客户端导入；
- stdio：真起一个子进程（tests/fake_mcp_server.py），握手、翻页、调用、图片、出错、
  反向请求 roots/list、中断、超时、进程崩溃、找不到命令；
- HTTP：Streamable HTTP（JSON / SSE 回复、会话头）、401 提示、自动退回老 SSE、老 SSE 传输——
  经假的 Opener，不起服务器；
- 扩展：真会话里模型调用 MCP 工具、服务器说明进系统提示、计划模式只给只读工具、/mcp 命令、权限规则。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[1]))                     # packages/pi-coding-agent
sys.path.insert(0, str(HERE.parents[2] / "pi-agent"))
sys.path.insert(0, str(HERE.parents[2] / "pi-ai"))
sys.path.insert(0, str(HERE.parent))

from fakes import FAKE_MODEL, Sandbox, fake_models  # noqa: E402
from pi_agent import ToolContext  # noqa: E402
from pi_ai import ImageContent, TextContent, ToolCall  # noqa: E402
from pi_coding_agent import (  # noqa: E402
    AgentSession,
    ModelRuntime,
    SessionManager,
    create_agent_session_services,
)
from pi_coding_agent.builtin_extensions import permissions  # noqa: E402
from pi_coding_agent.mcp import config as mcp_config  # noqa: E402
from pi_coding_agent.mcp.client import McpClient, McpError  # noqa: E402
from pi_coding_agent.mcp.manager import (  # noqa: E402
    McpManager,
    clean_schema,
    convert_result,
    tool_name,
)
from pi_coding_agent.mcp.transport import (  # noqa: E402
    HttpTransport,
    OpenedResponse,
    SseTransport,
    TransportError,
)

FAILURES: list[str] = []
SERVER = str(HERE.parent / "fake_mcp_server.py")


def under_pytest() -> bool:
    return "PYTEST_CURRENT_TEST" in os.environ


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
        return
    FAILURES.append(f"{name}: {detail}")
    print(f"  FAIL {name} {detail}")
    if under_pytest():
        raise AssertionError(f"{name}: {detail}")


def fake_server_config(name: str = "fake", **extra) -> mcp_config.ServerConfig:
    return mcp_config.parse_server(name, {"command": sys.executable, "args": [SERVER], **extra})


# --------------------------------------------------------------------------- #
# 1. 配置
# --------------------------------------------------------------------------- #

def test_config() -> None:
    env = {"TOKEN": "t0k", "HOME": "/home/me"}
    s = mcp_config.parse_server("gh", {"command": "npx", "args": ["-y", "server-github"],
                                       "env": {"GITHUB_TOKEN": "${TOKEN}"}}, env=env)
    check("配置: Claude Desktop 写法 = stdio，${VAR} 替换", s.transport == "stdio" and s.command == "npx"
          and s.env == {"GITHUB_TOKEN": "t0k"} and s.args == ["-y", "server-github"], str(s))
    s = mcp_config.parse_server("r", {"type": "http", "url": "https://x/mcp",
                                      "headers": {"Authorization": "Bearer ${TOKEN}"}}, env=env)
    check("配置: http + headers", s.transport == "http" and s.headers["Authorization"] == "Bearer t0k")
    check("配置: 只有 url 时自动判断（先 http 再 sse）",
          mcp_config.parse_server("u", {"url": "https://x/sse"}, env=env).transport == "auto")
    check("配置: streamable-http 的别名",
          mcp_config.parse_server("u", {"type": "streamable-http", "url": "u"}, env=env).transport == "http")
    s = mcp_config.parse_server("c", {"command": ["uvx", "srv", "--x"], "args": ["--y"]}, env=env)
    check("配置: 命令写成数组", s.command == "uvx" and s.args == ["srv", "--x", "--y"], str(s))
    s = mcp_config.parse_server("d", {"command": "x", "args": ["${MISSING:-dflt}", "${NOPE}"]}, env=env)
    check("配置: ${VAR:-默认值}；没设的变量记下来", s.args == ["dflt", ""] and s.missing_vars == ["NOPE"], str(s))
    check("配置: disabled / enabled=false 都算停用",
          mcp_config.parse_server("a", {"command": "x", "disabled": True}, env=env).disabled
          and mcp_config.parse_server("b", {"command": "x", "enabled": False}, env=env).disabled)
    check("配置: VS Code 的 servers 键", list(mcp_config.servers_in({"servers": {"a": {"command": "x"}}})) == ["a"])

    with Sandbox() as root:
        agent, cwd = root / "agent", root / "proj"
        (cwd / ".pi").mkdir(parents=True)
        agent.mkdir()
        (agent / "mcp.json").write_text(json.dumps({"mcpServers": {
            "a": {"command": "global-a"}, "b": {"command": "global-b"}}}))
        (cwd / ".pi" / "mcp.json").write_text(json.dumps({"mcpServers": {"b": {"command": "project-b"}}}))
        servers, problems = mcp_config.load_servers(cwd, agent, env={})
        check("配置: 项目级覆盖全局同名项", {s.name: s.command for s in servers}
              == {"a": "global-a", "b": "project-b"} and not problems, str(servers))
        (cwd / ".pi" / "mcp.json").write_text("{坏的")
        servers, problems = mcp_config.load_servers(cwd, agent, env={})
        check("配置: 坏文件跳过并说明", [s.name for s in servers] == ["a", "b"] and "不是合法的 JSON" in problems[0])


def test_import() -> None:
    with Sandbox() as root:
        home, cwd, agent = root / "home", root / "proj", root / "home" / ".pi" / "agent"
        cwd.mkdir()
        (home / ".cursor").mkdir(parents=True)
        (home / ".cursor" / "mcp.json").write_text(json.dumps({"mcpServers": {
            "cursor-srv": {"command": "npx", "args": ["a"], "autoApprove": ["x"]}}}))
        (home / ".claude.json").write_text(json.dumps({
            "mcpServers": {"cc-user": {"type": "http", "url": "https://u"}},
            "projects": {str(cwd.resolve()): {"mcpServers": {"cc-local": {"command": "uvx", "args": ["b"]}}},
                         "/other": {"mcpServers": {"not-mine": {"command": "x"}}}}}))
        (cwd / ".mcp.json").write_text(json.dumps({"mcpServers": {"proj-srv": {"command": "node",
                                                                              "args": ["s.js"]}}}))
        (cwd / ".vscode").mkdir()
        (cwd / ".vscode" / "mcp.json").write_text(json.dumps({"servers": {
            "needs-input": {"command": "x", "env": {"K": "${input:key}"}},
            "proj-srv": {"command": "dup"}}}))
        saved = {k: os.environ.get(k) for k in ("HOME", "USERPROFILE", "XDG_CONFIG_HOME", "APPDATA")}
        os.environ.update(HOME=str(home), USERPROFILE=str(home), XDG_CONFIG_HOME=str(home / ".config"),
                          APPDATA=str(home / "AppData"))
        try:
            claude = mcp_config._app_support() / "Claude"
            claude.mkdir(parents=True)
            (claude / "claude_desktop_config.json").write_text(json.dumps({"mcpServers": {
                "desktop-srv": {"command": "python", "args": ["-m", "srv"]}}}))
            found = mcp_config.importable(cwd, agent)
            check("导入: 列出各客户端里能导入的", set(found) == {"cursor-srv", "cc-user", "cc-local", "proj-srv",
                                                         "desktop-srv"}, str(found))
            done, skipped = mcp_config.import_servers(cwd, agent)
            check("导入: 全部导进来", len(done) == 5, str(done))
            project_servers = mcp_config.servers_in(json.loads((cwd / ".pi" / "mcp.json").read_text()))
            check("导入: 项目里来的写进项目配置", set(project_servers) == {"proj-srv"}
                  and project_servers["proj-srv"]["command"] == "node", str(project_servers))
            global_servers = mcp_config.servers_in(json.loads((agent / "mcp.json").read_text()))
            check("导入: 全局来的写进 ~/.pi/agent/mcp.json，含 ~/.claude.json 里这个项目的",
                  set(global_servers) == {"cursor-srv", "cc-user", "cc-local", "desktop-srv"}, str(global_servers))
            check("导入: 去掉别的客户端的自动批准设置（权限按 pi 的来）", "autoApprove" not in global_servers["cursor-srv"])
            check("导入: VS Code 的 ${input:} 项跳过", "needs-input" not in json.dumps(done))
            check("导入: 同名的先到先得（项目的 .mcp.json 优先），后面的跳过并说明",
                  any(item.startswith("proj-srv（VS Code") for item in skipped), str(skipped))
            done2, skipped2 = mcp_config.import_servers(cwd, agent)
            check("导入: 再导一次什么都不重复", done2 == [] and len(skipped2) >= 5, str(done2))
            check("导入: 导入后就没有可导入的了", mcp_config.importable(cwd, agent) == [])
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


# --------------------------------------------------------------------------- #
# 2. stdio：真子进程
# --------------------------------------------------------------------------- #

async def case_stdio() -> None:
    with Sandbox() as cwd:
        mgr = McpManager([fake_server_config()], cwd=str(cwd), startup_timeout=20, tool_timeout=20)
        try:
            await mgr.ensure_started()
            state = mgr.servers["fake"]
            check("stdio: 连上（跳过 stdout 里的非 JSON 日志）", state.status == "ready", state.error)
            check("stdio: 工具列表翻页取全", [t["name"] for t in state.tools] == [
                "echo", "add", "image", "fail", "slow", "roots", "crash"], str(state.tools))
            check("stdio: 记下服务器说明", state.client.instructions == "Use echo to repeat things.")
            tools = {t.name: t for t in mgr.agent_tools()}
            check("stdio: 工具名 mcp__服务器__工具", "mcp__fake__echo" in tools and len(tools) == 7, str(list(tools)))
            check("stdio: schema 去掉 $schema、保留参数", "$schema" not in tools["mcp__fake__echo"].parameters
                  and tools["mcp__fake__echo"].parameters["required"] == ["text"])
            check("stdio: 只读模式只给 readOnlyHint 的", [t.name for t in mgr.agent_tools(read_only=True)]
                  == ["mcp__fake__echo"])

            ctx = lambda: ToolContext(tool_call_id="x")
            r = await tools["mcp__fake__echo"].execute({"text": "你好"}, ctx())
            check("调用: 文字结果", r.content[0].text == "echo: 你好" and not r.is_error
                  and r.details["server"] == "fake", str(r))
            r = await tools["mcp__fake__add"].execute({"a": 2, "b": 3}, ctx())
            check("调用: 只有 structuredContent 时转成 JSON", '"sum": 5' in r.content[0].text, r.content[0].text)
            r = await tools["mcp__fake__image"].execute({}, ctx())
            check("调用: 图片变成 ImageContent", any(isinstance(c, ImageContent) for c in r.content)
                  and r.content[0].text == "an image", str(r.content))
            r = await tools["mcp__fake__fail"].execute({}, ctx())
            check("调用: isError 标成失败", r.is_error and "went wrong" in r.content[0].text)
            r = await tools["mcp__fake__roots"].execute({}, ctx())
            check("调用: 回答服务器的 roots/list（工作目录）", cwd.resolve().as_uri() in r.content[0].text,
                  r.content[0].text)

            cancel_ctx = ctx()
            started = time.monotonic()
            task = asyncio.ensure_future(tools["mcp__fake__slow"].execute({}, cancel_ctx))
            await asyncio.sleep(0.3)
            cancel_ctx.cancel_event.set()
            r = await task
            check("中断: Esc 立刻返回、告诉模型被中断", r.is_error and "用户中断" in r.content[0].text
                  and time.monotonic() - started < 3, r.content[0].text)
            r = await tools["mcp__fake__echo"].execute({"text": "还活着"}, ctx())
            check("中断: 之后服务器照常可用", r.content[0].text == "echo: 还活着")

            try:
                await state.client.call_tool("slow", {}, timeout=0.5)
                msg = ""
            except McpError as exc:
                msg = exc.message
            check("超时: 按设置的秒数放弃", "0.5 秒没有响应" in msg, msg)

            r = await tools["mcp__fake__crash"].execute({}, ctx())
            check("崩溃: 工具报错并带上服务器的 stderr", r.is_error and "crashing on purpose" in r.content[0].text,
                  r.content[0].text)
            check("崩溃: 状态变成失败", state.status == "failed", state.status)
            r = await tools["mcp__fake__echo"].execute({"text": "x"}, ctx())
            check("崩溃: 之后的调用直接说不可用、提示重连", r.is_error and "/mcp reconnect" in r.content[0].text)
            await mgr.reconnect("fake")
            r = await mgr.agent_tools()[0].execute({"text": "重连了"}, ctx())
            check("重连: reconnect 之后恢复", state.status == "ready" and r.content[0].text == "echo: 重连了")
        finally:
            await mgr.close()

        bad = McpManager([mcp_config.parse_server("nope", {"command": "definitely-not-a-command-xyz"}),
                          mcp_config.parse_server("node", {"command": "npx-not-here", "args": []}),
                          mcp_config.parse_server("vars", {"command": "x", "env": {"K": "${PI_TEST_UNSET_VAR}"}},
                                                  env={}),
                          mcp_config.parse_server("off", {"command": "x", "disabled": True}),
                          fake_server_config("ok")], cwd=str(cwd), startup_timeout=20)
        try:
            await bad.ensure_started()
            status = {n: (s.status, s.error) for n, s in bad.servers.items()}
            check("失败: 找不到命令说清楚", status["nope"][0] == "failed" and "找不到命令" in status["nope"][1],
                  str(status["nope"]))
            check("失败: 缺环境变量不启动", status["vars"] == ("failed", "缺少环境变量 PI_TEST_UNSET_VAR"),
                  str(status["vars"]))
            check("失败: 停用的不连", status["off"][0] == "disabled")
            check("失败: 别的服务器不受影响", status["ok"][0] == "ready" and len(bad.agent_tools()) == 7)
        finally:
            await bad.close()


# --------------------------------------------------------------------------- #
# 3. HTTP：假 Opener
# --------------------------------------------------------------------------- #

def opened(status: int, headers: dict, body: str = "", lines: list[str] | None = None,
           queue: asyncio.Queue | None = None) -> OpenedResponse:
    async def gen():
        if queue is not None:
            while True:
                item = await queue.get()
                if item is None:
                    return
                yield item
        for line in lines or []:
            yield line

    async def close():
        if queue is not None:
            queue.put_nowait(None)

    async def body_text():
        return body

    return OpenedResponse(status, {k.lower(): v for k, v in headers.items()}, gen(), close, body_text)


def sse_lines(*messages: dict) -> list[str]:
    out = []
    for m in messages:
        out += ["event: message", f"data: {json.dumps(m)}", ""]
    return out


INIT_RESULT = {"protocolVersion": "2025-06-18", "capabilities": {}, "serverInfo": {"name": "remote"}}
TOOLS_RESULT = {"tools": [{"name": "search", "description": "Search docs",
                           "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}}}}]}


async def case_http() -> None:
    seen: list[tuple[str, dict, dict | None]] = []

    async def streamable(method, url, headers, body):
        message = json.loads(body) if body else None
        seen.append((method, dict(headers), message))
        if method == "DELETE":
            return opened(200, {})
        if message.get("method") == "initialize":
            return opened(200, {"Content-Type": "application/json", "Mcp-Session-Id": "sess-1"},
                          json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": INIT_RESULT}))
        if "id" not in message or "method" not in message:     # 通知、对服务器请求的回答
            return opened(202, {})
        if message["method"] == "tools/list":
            return opened(200, {"Content-Type": "application/json"},
                          json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": TOOLS_RESULT}))
        # tools/call 用 SSE 回：先一条进度通知，再一个反向 ping，最后是结果
        return opened(200, {"Content-Type": "text/event-stream"}, lines=sse_lines(
            {"jsonrpc": "2.0", "method": "notifications/progress", "params": {"progress": 1}},
            {"jsonrpc": "2.0", "id": "srv-1", "method": "ping"},
            {"jsonrpc": "2.0", "id": message["id"], "result": {"content": [{"type": "text", "text": "found 3"}]}}))

    mgr = McpManager([mcp_config.parse_server("docs", {"type": "http", "url": "https://docs.example.com/mcp",
                                                        "headers": {"Authorization": "Bearer abc"}})],
                     cwd=".", opener=streamable, startup_timeout=5, tool_timeout=5)
    await mgr.ensure_started()
    state = mgr.servers["docs"]
    check("HTTP: 连上并列出工具", state.status == "ready" and state.tools[0]["name"] == "search", state.error)
    tools_req = next(h for m, h, msg in seen if msg and msg.get("method") == "tools/list")
    check("HTTP: 之后的请求带会话号、协议版本、配置的 headers", tools_req.get("Mcp-Session-Id") == "sess-1"
          and tools_req.get("MCP-Protocol-Version") == "2025-06-18" and tools_req.get("Authorization") == "Bearer abc",
          str(tools_req))
    check("HTTP: Accept 同时接受 JSON 和 SSE", "text/event-stream" in tools_req["Accept"])
    r = await mgr.agent_tools()[0].execute({"q": "x"}, ToolContext(tool_call_id="1"))
    check("HTTP: SSE 回复里取到结果（夹着通知也不乱）", r.content[0].text == "found 3", str(r))
    check("HTTP: 回答服务器在流里发的 ping", any(msg and msg.get("id") == "srv-1" and "result" in msg
                                            for _, _, msg in seen))
    await mgr.close()
    check("HTTP: 关闭时 DELETE 会话", seen[-1][0] == "DELETE" and seen[-1][1].get("Mcp-Session-Id") == "sess-1")

    # 回答服务器请求时出了意外错误：同一条流里后面的结果不能丢
    async def flaky(method, url, headers, body):
        message = json.loads(body)
        if message.get("method") == "initialize":
            return opened(200, {"Content-Type": "application/json"},
                          json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": INIT_RESULT}))
        if "method" not in message:
            raise RuntimeError("boom while answering")
        if "id" not in message:
            return opened(202, {})
        return opened(200, {"Content-Type": "text/event-stream"}, lines=sse_lines(
            {"jsonrpc": "2.0", "id": "srv-9", "method": "ping"},
            {"jsonrpc": "2.0", "id": message["id"], "result": {"content": [{"type": "text", "text": "still ok"}]}}))

    client = McpClient(HttpTransport("https://f/mcp", opener=flaky), request_timeout=2)
    await client.start()
    try:
        result = await client.call_tool("x", {})
    except McpError as exc:
        result = {"error": exc.message}
    check("HTTP: 处理一条消息出错不影响同一条流里后面的结果",
          (result.get("content") or [{}])[0].get("text") == "still ok", str(result))

    async def unauthorized(method, url, headers, body):
        return opened(401, {}, "unauthorized")

    mgr = McpManager([mcp_config.parse_server("secret", {"type": "http", "url": "https://s/mcp"})],
                     cwd=".", opener=unauthorized, startup_timeout=5)
    await mgr.ensure_started()
    check("HTTP: 401 提示在 headers 里配 token", "Authorization" in mgr.servers["secret"].error,
          mgr.servers["secret"].error)

    # 自动判断：POST 405 → 退回老 SSE
    queue: asyncio.Queue = asyncio.Queue()
    calls: list[str] = []

    async def legacy(method, url, headers, body):
        calls.append(f"{method} {url}")
        if method == "GET":
            queue.put_nowait("event: endpoint")
            queue.put_nowait("data: /messages?session=42")
            queue.put_nowait("")
            return opened(200, {"Content-Type": "text/event-stream"}, queue=queue)
        if url.endswith("/sse"):
            return opened(405, {}, "method not allowed")
        message = json.loads(body)
        if "id" in message:
            result = INIT_RESULT if message["method"] == "initialize" else TOOLS_RESULT
            for line in sse_lines({"jsonrpc": "2.0", "id": message["id"], "result": result}):
                queue.put_nowait(line)
        return opened(202, {}, "Accepted")

    mgr = McpManager([mcp_config.parse_server("old", {"url": "https://old.example.com/sse"})],
                     cwd=".", opener=legacy, startup_timeout=5)
    await mgr.ensure_started()
    state = mgr.servers["old"]
    check("自动: Streamable HTTP 不行就退回老 SSE", state.status == "ready" and state.transport_used == "sse",
          f"{state.status} {state.error}")
    check("老 SSE: 消息 POST 到 endpoint（按相对地址补全）",
          "POST https://old.example.com/messages?session=42" in calls, str(calls))
    await mgr.close()

    async def no_endpoint(method, url, headers, body):
        return opened(200, {"Content-Type": "text/event-stream"}, lines=["event: message", "data: {}", ""])

    try:
        await SseTransport("https://x/sse", opener=no_endpoint, endpoint_timeout=1).start(
            lambda m: asyncio.sleep(0), lambda r: None)
        msg = ""
    except TransportError as exc:
        msg = str(exc)
    check("老 SSE: 没给 endpoint 时说清楚", "endpoint" in msg, msg)

    check("HTTP: 传输层类型", isinstance(HttpTransport("u"), HttpTransport))


# --------------------------------------------------------------------------- #
# 4. 小函数
# --------------------------------------------------------------------------- #

def test_helpers() -> None:
    check("工具名: 非法字符换成下划线", tool_name("my server", "do.thing") == "mcp__my_server__do_thing")
    long = tool_name("s" * 40, "t" * 40)
    check("工具名: 超过 64 截短并加哈希", len(long) == 64 and long != tool_name("s" * 40, "t" * 39 + "u"), long)
    check("schema: 没有 properties 补上、顶层是 object", clean_schema({}) == {"type": "object", "properties": {}})
    r = convert_result({"content": [{"type": "resource", "resource": {"uri": "file:///a", "text": "内容"}},
                                    {"type": "resource_link", "name": "doc", "uri": "https://d"},
                                    {"type": "audio", "data": "AAAA", "mimeType": "audio/wav"}]})
    text = r.content[0].text
    check("结果: 资源、链接、音频都变成文字说明", "[资源 file:///a]\n内容" in text and "[链接 doc https://d]" in text
          and "音频 audio/wav" in text, text)
    r = convert_result({"content": [{"type": "text", "text": "x" * 100}]}, limit=10)
    check("结果: 太长截断并说明", r.content[0].text.startswith("x" * 10) and "省略了后面 90 字" in r.content[0].text)
    check("结果: 什么都没有时也给一句", convert_result({}).content[0].text == "（工具没有返回内容）")


def test_permissions() -> None:
    match = permissions.rule_matches
    check("权限: mcp__github__* 通配", match("mcp__github__*", "mcp__github__search", {}, "."))
    check("权限: mcp__github 指整个服务器", match("mcp__github", "mcp__github__create_issue", {}, ".")
          and not match("mcp__github", "mcp__githubx__a", {}, "."))
    check("权限: 精确名照常", match("mcp__github__search", "mcp__github__search", {}, ".")
          and not match("mcp__github__search", "mcp__github__create", {}, "."))
    request = permissions.build_request(type("S", (), {"cwd": "."})(), "mcp__github__create_issue",
                                        {"title": "Bug", "body": "详情"})
    check("权限: 确认面板写清服务器和工具、参数逐行", request["title"] == "调用 MCP 工具 · github"
          and "create_issue" in request["question"] and '  "title": "Bug",' in request["detail"], str(request))
    check("权限: 不再询问按单个工具记", request["remember"]["keys"] == ["tool:mcp__github__create_issue"])


# --------------------------------------------------------------------------- #
# 5. 扩展：真会话
# --------------------------------------------------------------------------- #

async def case_extension() -> None:
    with Sandbox() as cwd:
        (cwd / ".pi").mkdir()
        (cwd / ".pi" / "mcp.json").write_text(json.dumps({"mcpServers": {
            "fake": {"command": sys.executable, "args": [SERVER]},
            "broken": {"command": "no-such-cmd-xyz"}}}))
        script = [[ToolCall(id="c1", name="mcp__fake__echo", arguments={"text": "从会话里调"})],
                  [TextContent(text="好了")]]
        models, provider = fake_models(script)
        runtime = ModelRuntime(models, cwd / "agent")
        services = create_agent_session_services(cwd=cwd, agent_dir=cwd / "agent", model_runtime=runtime)
        session = AgentSession(services, SessionManager.in_memory(cwd), model=FAKE_MODEL, tools=["read"])
        try:
            warm = await session.run_command("mcp", "warmup")
            check("扩展: 后台预连接报告失败的服务器", "broken" in warm and "fake" not in warm, warm)
            await session.prompt("调一下")
            sent = provider.calls[0]["context"]
            names = [t.name for t in sent.tools]
            check("扩展: 本轮工具集里有 MCP 工具", "mcp__fake__echo" in names and "read" in names, str(names))
            check("扩展: 服务器说明附在系统提示后", "Use echo to repeat things." in sent.system_prompt
                  and "mcp__<server>__<tool>" in sent.system_prompt)
            result = next(m for m in session.messages if getattr(m, "role", "") == "toolResult")
            check("扩展: 模型调用 MCP 工具拿到结果", result.content[0].text == "echo: 从会话里调", str(result.content))

            status = await session.run_command("mcp", "")
            check("/mcp: 列出状态和失败原因", "✓ fake" in status and "7 个工具" in status
                  and "✗ broken" in status and "找不到命令" in status, status)
            tools = await session.run_command("mcp", "tools fake")
            check("/mcp tools: 列出工具和只读标记", "mcp__fake__echo [只读]" in tools and "mcp__fake__crash" in tools,
                  tools)

            session.switch_mode("plan")
            provider.script = iter([[TextContent(text="计划")]]) if hasattr(provider, "script") else None
            await session._prepare_run()
            plan_tools = {t.name for t in session.agent.state.tools}
            check("计划模式: 只挂只读的 MCP 工具", "mcp__fake__echo" in plan_tools
                  and "mcp__fake__crash" not in plan_tools, str(plan_tools))
            session.switch_mode("chat")
            await session._prepare_run()
            check("切回执行: 全部 MCP 工具", "mcp__fake__crash" in {t.name for t in session.agent.state.tools})

            reply = await session.run_command("mcp", "reconnect")
            check("/mcp reconnect: 重读配置并重连", "✓ fake" in reply, reply)
            check("/mcp: 用法提示", "用法" in await session.run_command("mcp", "bogus"))
        finally:
            api = session.resources.get_extension_api()
            await session.run_command("mcp", "warmup")
            # 关掉子进程：通过 reconnect 换掉的旧管理器已经关了；这里关当前的
            for hook in api.before_start_hooks:
                closure = getattr(hook, "__closure__", None) or ()
                for cell in closure:
                    value = cell.cell_contents
                    if isinstance(value, dict) and value.get("manager") is not None:
                        await value["manager"].close()


def test_parse_add() -> None:
    from pi_coding_agent.mcp.add import AddError, parse_add

    def servers(text):
        return parse_add(text).servers

    fetch = {"fetch": {"command": "uvx", "args": ["mcp-server-fetch"]}}
    check("add: 网站给的整段 mcpServers",
          servers('{"mcpServers": {"fetch": {"command": "uvx", "args": ["mcp-server-fetch"]}}}') == fetch)
    check("add: 只复制了其中一项（没有外层括号、末尾多逗号）",
          servers('"fetch": {"command": "uvx", "args": ["mcp-server-fetch"],},') == fetch)
    check("add: 只有配置时按包名取名（去 scope、版本、-mcp）",
          list(servers('{"command": "npx", "args": ["-y", "@upstash/context7-mcp@latest"]}')) == ["context7"])
    check("add: VS Code 的 name 字段 / mcp.servers",
          list(servers('{"name": "x", "command": "node", "args": ["a.js"]}')) == ["x"]
          and list(servers('{"mcp": {"servers": {"gh": {"type": "http", "url": "https://g/mcp"}}}}')) == ["gh"])
    r = parse_add("claude mcp add github -e GITHUB_TOKEN=abc -- npx -y @modelcontextprotocol/server-github")
    check("add: claude mcp add 的 -e 和 -- 命令", r.servers == {"github": {
        "command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"], "env": {"GITHUB_TOKEN": "abc"}}},
          str(r.servers))
    r = parse_add('claude mcp add --transport http docs https://e.com/mcp -H "Authorization: Bearer x" -s project')
    check("add: claude 的远程写法，地址后面的 -H、-s project", r.servers == {"docs": {
        "url": "https://e.com/mcp", "type": "http", "headers": {"Authorization": "Bearer x"}}}
          and r.scope == "project", f"{r.servers} {r.scope}")
    check("add: claude mcp add-json", servers(
        "claude mcp add-json fetch '{\"command\": \"uvx\", \"args\": [\"mcp-server-fetch\"]}'") == fetch)
    check("add: codex mcp add", servers("codex mcp add fetch -- uvx mcp-server-fetch") == fetch)
    check("add: 直接写命令，按包名取名", list(servers("npx -y @modelcontextprotocol/server-memory")) == ["memory"])
    check("add: 名字 + 地址", servers("amap https://mcp.amap.com/sse") == {"amap": {"url": "https://mcp.amap.com/sse"}})
    check("add: 只有地址时从路径取名", list(servers("https://server.smithery.ai/@upstash/context7-mcp/mcp"))
          == ["context7"])
    docker = servers("docker run -i --rm -e GITHUB_PERSONAL_ACCESS_TOKEN ghcr.io/github/github-mcp-server")
    check("add: docker 的 -e 留给 docker、按镜像名取名", list(docker) == ["github"]
          and docker["github"]["args"][3] == "-e" and "env" not in docker["github"], str(docker))
    check("add: 开头的 KEY=值 变成 env", servers("GITHUB_TOKEN=abc npx -y @modelcontextprotocol/server-github")
          ["github"]["env"] == {"GITHUB_TOKEN": "abc"})
    check("add: python -m 取模块名", list(servers("python -m mcp_server_time --local-timezone Asia/Shanghai"))
          == ["time"])
    check("add: uvx --from 跳过来源", list(servers("uvx --from git+https://x/y mcp-server-git")) == ["git"])
    r = parse_add("--project --force fetch uvx mcp-server-fetch")
    check("add: 开头的 --project / --force", r.scope == "project" and r.force and r.servers == fetch)
    for bad, word in (("", "要添加什么"), ("npx -y @smithery/cli install @x/y --client claude", "Smithery"),
                      ('{"mcpServers": {"a": {"command": ', "解析不了"), ('{"foo": 1}', "没在 JSON 里找到"),
                      ('{"mcpServers": {"a": {"env": {}}}}', "没有 command 也没有 url")):
        try:
            parse_add(bad)
            msg = ""
        except AddError as exc:
            msg = str(exc)
        check(f"add: 说清楚错在哪（{word}）", word in msg, msg)


async def case_add_command() -> None:
    import shlex

    with Sandbox() as cwd:
        models, _ = fake_models([[TextContent(text="好")]])
        runtime = ModelRuntime(models, cwd / "agent")
        services = create_agent_session_services(cwd=cwd, agent_dir=cwd / "agent", model_runtime=runtime)
        session = AgentSession(services, SessionManager.in_memory(cwd), model=FAKE_MODEL, tools=["read"])
        launch = f"{shlex.quote(sys.executable)} {shlex.quote(SERVER)}"
        try:
            help_text = await session.run_command("mcp", "add")
            check("/mcp add: 不带参数时给示例", "要添加什么" in help_text and "/mcp add npx" in help_text, help_text)
            progress: list[str] = []
            session.subscribe(lambda e: progress.append(e.text) if e.type == "progress" else None)
            reply = await session.run_command("mcp", f"add fake {launch}")
            check("/mcp add: 连接前报进度给界面", any("正在连接 fake" in p for p in progress), str(progress))
            global_file = cwd / "agent" / "mcp.json"
            written = json.loads(global_file.read_text())["mcpServers"]
            check("/mcp add: 写进全局配置（原样，不展开变量）", written["fake"] == {
                "command": sys.executable, "args": [SERVER]}, str(written))
            check("/mcp add: 马上连上并列出工具", "✓ fake 连上了" in reply and "echo" in reply, reply)
            await session._prepare_run()
            check("/mcp add: 下一轮工具集里就有", "mcp__fake__echo" in {t.name for t in session.agent.state.tools})

            again = await session.run_command("mcp", f"add fake {launch}")
            check("/mcp add: 同名的不覆盖、提示 --force", "已经有叫 fake" in again and "--force" in again, again)
            forced = await session.run_command("mcp", f"add --force fake {launch}")
            check("/mcp add: --force 覆盖", "✓ fake 连上了" in forced, forced)

            missing = await session.run_command(
                "mcp", 'add --project {"needkey": {"command": "x", "env": {"K": "${PI_TEST_ADD_MISSING}"}}}')
            project = json.loads((cwd / ".pi" / "mcp.json").read_text())["mcpServers"]
            check("/mcp add: --project 写进项目配置、保留 ${VAR}", project["needkey"]["env"]["K"]
                  == "${PI_TEST_ADD_MISSING}", str(project))
            check("/mcp add: 缺环境变量时告诉怎么设", "✗ needkey" in missing and "export PI_TEST_ADD_MISSING" in missing,
                  missing)
            check("/mcp add: 坏输入说明并给示例", "添加失败" in await session.run_command("mcp", "add {坏的"))

            removed = await session.run_command("mcp", "remove fake")
            check("/mcp remove: 从配置删掉并关闭", "已删除 fake" in removed
                  and "fake" not in json.loads(global_file.read_text())["mcpServers"], removed)
            await session._prepare_run()
            check("/mcp remove: 下一轮没有它的工具", not any(t.name.startswith("mcp__fake__")
                                                         for t in session.agent.state.tools))
            check("/mcp remove: 没有这个名字时说清楚", "没有叫 nope" in await session.run_command("mcp", "remove nope"))

            global_file.write_text("{坏的")
            broken = await session.run_command("mcp", f"add other {launch}")
            check("/mcp add: 配置文件坏了不覆盖", "不是合法的 JSON" in broken and global_file.read_text() == "{坏的",
                  broken)
        finally:
            for hook in session.resources.get_extension_api().before_start_hooks:
                for cell in getattr(hook, "__closure__", None) or ():
                    value = cell.cell_contents
                    if isinstance(value, dict) and value.get("manager") is not None:
                        await value["manager"].close()


# --------------------------------------------------------------------------- #
# pytest 兼容层
# --------------------------------------------------------------------------- #

def test_stdio() -> None:
    asyncio.run(case_stdio())


def test_http() -> None:
    asyncio.run(case_http())


def test_extension() -> None:
    asyncio.run(case_extension())


def test_add_command() -> None:
    asyncio.run(case_add_command())


async def main() -> int:
    print("配置");        test_config(); test_import()
    print("stdio");       await case_stdio()
    print("HTTP / SSE");  await case_http()
    print("小函数");      test_helpers(); test_permissions()
    print("扩展");        await case_extension()
    print("/mcp add");    test_parse_add(); await case_add_command()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} 处失败：")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("全部通过 —— MCP 可用")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
