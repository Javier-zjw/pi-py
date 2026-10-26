"""
MCP：把 MCP 服务器的工具接进 pi（同 Claude Code / Cursor / Claude Desktop 的 mcpServers）。

配置写在 ``~/.pi/agent/mcp.json``（全局）或 ``<cwd>/.pi/mcp.json``（项目），格式和别的客户端一样，
可以原样贴；``/mcp import`` 把 Claude Desktop、Claude Code、Cursor、VS Code、Windsurf、Codex
里已有的服务器一次导进来。

什么时候连：界面打开会话时在后台先连（``/mcp warmup``），没连完的话第一次提问前等它连完；
连不上的服务器只是没有它的工具，不影响 pi。每轮开始前（before_agent_start，模式已经把工具重置回
基线之后）把已连上服务器的工具加进本轮工具集；计划模式只加声明了 readOnlyHint 的。
服务器初始化时给的使用说明（instructions）附在系统提示后面。

权限：MCP 工具不在只读白名单里，默认每次都问（可选"本会话不再询问"）；
settings 的 permissions.allow 写 ``mcp__github__*`` 或 ``mcp__github`` 放行整个服务器。

命令：``/mcp``（状态）、``/mcp tools [服务器]``、``/mcp add <配置>``（网站给的 JSON、
``claude mcp add`` / ``codex mcp add`` 命令、``npx ...`` 或地址，解析见 mcp/add.py；写进配置后只连这一个）、
``/mcp remove <服务器>``、``/mcp reconnect [服务器]``（重读配置并重连）、``/mcp import``。设置：``mcp.startupTimeout``（秒，默认 60：npx 第一次要下载）、
``mcp.toolTimeout``（默认 120，服务器配置里的 timeout 优先）、``mcp.maxOutputChars``、
``mcp.promptWait``（提问前最多等还没连上的服务器几秒，默认 3；没连上的这一轮先不用）。
"""

from __future__ import annotations

import re
from pathlib import Path

NAME = "mcp"
DESCRIPTION = "MCP：接入 MCP 服务器的工具（/mcp 查看状态、/mcp import 导入别的客户端的配置）"

INSTRUCTION_CHARS = 2000
USAGE = "用法：/mcp [tools [服务器] | add <配置> | remove <服务器> | reconnect [服务器] | import]"
ADD_HELP = """把网站上复制的东西贴在 /mcp add 后面，例如：
  /mcp add {"mcpServers": {"fetch": {"command": "uvx", "args": ["mcp-server-fetch"]}}}
  /mcp add claude mcp add github -e GITHUB_TOKEN=xxx -- npx -y @modelcontextprotocol/server-github
  /mcp add npx -y @modelcontextprotocol/server-memory
  /mcp add amap https://mcp.amap.com/sse
默认写进全局配置；加 --project 只给这个项目用，--force 覆盖同名的"""
MARKS = {"ready": "✓", "failed": "✗", "disabled": "○", "connecting": "…", "pending": "·"}


def activate(pi) -> None:
    holder: dict = {"manager": None}

    def agent_dir(session) -> Path:
        from pi_coding_agent.session.manager import DEFAULT_AGENT_DIR
        return Path(getattr(session.resources, "agent_dir", None) or DEFAULT_AGENT_DIR).expanduser()

    def manager(session):
        if holder["manager"] is None:
            from pi_coding_agent.mcp.config import load_servers
            from pi_coding_agent.mcp.manager import McpManager

            servers, problems = load_servers(session.cwd, agent_dir(session))
            for problem in problems:
                session.services.diagnostics.append(f"MCP 配置：{problem}")
            get = session.settings.get
            holder["manager"] = McpManager(
                servers, cwd=session.cwd,
                startup_timeout=float(get("mcp.startupTimeout") or 60),
                tool_timeout=float(get("mcp.toolTimeout") or 120),
                max_output=int(get("mcp.maxOutputChars") or 50_000))
        return holder["manager"]

    async def before_start(session) -> None:
        mgr = manager(session)
        if not mgr.servers:
            return
        mgr.start()
        pending = await mgr.wait(0)
        if pending:
            # 只等一小会儿：没连完的这一轮先不用（连上后下一轮自动加进来），别让提问干等
            limit = float(session.settings.get("mcp.promptWait") or 3)
            progress(session, f"等待 MCP 服务器连接：{'、'.join(pending)}（最多 {limit:g} 秒）")
            pending = await mgr.wait(limit)
            if pending:
                progress(session, f"{'、'.join(pending)} 还在启动，这一轮先不用（/mcp 查看）")
        await mgr.refresh_changed()
        present = {t.name for t in session.agent.state.tools}
        extra = [t for t in mgr.agent_tools(read_only=session.mode == "plan") if t.name not in present]
        if extra:
            session.agent.set_tools(list(session.agent.state.tools) + extra)
        notes = []
        for state in mgr.servers.values():
            text = (state.client.instructions if state.client and state.status == "ready" else "").strip()
            if text:
                notes.append(f"## {state.name}\n{text[:INSTRUCTION_CHARS]}")
        if notes:
            session.agent.state.system_prompt = (
                (session.agent.state.system_prompt or "")
                + "\n\n<mcp_server_instructions>\nUsage notes from the connected MCP servers "
                "(their tools are named mcp__<server>__<tool>):\n\n" + "\n\n".join(notes)
                + "\n</mcp_server_instructions>")

    pi.before_agent_start(before_start)

    # -- /mcp ------------------------------------------------------------ #

    def status_text(session) -> str:
        from pi_coding_agent.mcp.config import config_paths, importable

        mgr = manager(session)
        paths = [str(p).replace(str(Path.home()), "~") for p in config_paths(session.cwd, agent_dir(session))]
        lines = []
        if not mgr.servers:
            lines += ["还没有配置 MCP 服务器。",
                      (f"配置文件：{paths[0]}（全局）或 {paths[1]}（项目），格式同 "
                       "Claude Desktop / Cursor 的 mcpServers，例如："),
                      ('  {"mcpServers": {"fs": {"command": "npx", "args": '
                       '["-y", "@modelcontextprotocol/server-filesystem", "."]}}}'),
                      "也可以直接 /mcp add 加上网站给的配置（不带参数看示例）"]
        else:
            lines.append("MCP 服务器：")
            width = max(len(name) for name in mgr.servers)
            for state in mgr.servers.values():
                config = state.config
                kind = state.transport_used or config.transport
                head = f"  {MARKS.get(state.status, '·')} {state.name:<{width}}  {kind:<5}"
                if state.status == "ready":
                    lines.append(f"{head}  {len(state.tools)} 个工具  {config.describe()}")
                elif state.status == "failed":
                    lines.append(f"{head}  连接失败：{state.error}")
                elif state.status == "disabled":
                    lines.append(f"{head}  已停用（配置里 disabled）")
                else:
                    lines.append(f"{head}  {'连接中…' if state.status == 'connecting' else '下次提问时连接'}"
                                 f"  {config.describe()}")
            lines.append("配置：" + "、".join(paths) + " · /mcp tools 看工具 · /mcp add 添加 · "
                         "/mcp remove 删除 · /mcp reconnect 重连")
        found = importable(session.cwd, agent_dir(session))
        if found:
            lines.append(f"别的客户端里还有 {len(found)} 个可导入：{'、'.join(found[:8])}"
                         f"{' …' if len(found) > 8 else ''} —— 输入 /mcp import 导入")
        return "\n".join(lines)

    async def reload(session, name: str | None = None) -> None:
        old = holder["manager"]
        if name is None or old is None:
            if old is not None:
                await old.close()
            holder["manager"] = None
            await manager(session).ensure_started()
        else:
            await old.reconnect(name)

    def short(path) -> str:
        return str(path).replace(str(Path.home()), "~")

    def progress(session, text: str) -> None:
        """耗时步骤告诉界面在干什么（npx / uvx 第一次要下载，可能几十秒）"""
        from pi_coding_agent.session_events import ProgressEvent
        session.notify(ProgressEvent(text))

    def connecting(config) -> str:
        first = ("；第一次用要先下载，可能要等一会儿" if config.transport == "stdio"
                 and config.command.lower() in ("npx", "uvx", "bunx", "docker", "pnpm") else "")
        return f"正在连接 {config.name}（{config.describe()[:60]}）{first}"

    async def add_servers(session, text: str) -> str:
        from pi_coding_agent.mcp.add import AddError, parse_add
        from pi_coding_agent.mcp.config import load_servers, target_path, write_server

        try:
            request = parse_add(text)
        except AddError as exc:
            return f"添加失败：{exc}\n\n{ADD_HELP}"
        mgr = manager(session)
        clashes = [name for name in request.servers if name in mgr.servers]
        if clashes and not request.force:
            return (f"已经有叫 {'、'.join(clashes)} 的服务器了：换个名字"
                    "（例如 /mcp add 新名字 npx …），或者加 --force 覆盖")
        path = target_path(session.cwd, agent_dir(session), request.scope)
        try:
            for name, raw in request.servers.items():
                write_server(path, name, raw)
        except (OSError, ValueError) as exc:
            return f"添加失败：{exc}"
        lines = [f"已写进 {short(path)}：{'、'.join(request.servers)}"]
        effective = {s.name: s for s in load_servers(session.cwd, agent_dir(session))[0]}
        for name in request.servers:
            config = effective.get(name)
            if config is None:
                continue
            if config.source != str(path):
                lines.append(f"  注意：{short(config.source)} 里也有 {name}，那一份优先")
            progress(session, connecting(config))
            state = await mgr.add(config)
            if state.status == "ready":
                names = [str(t["name"]) for t in state.tools]
                more = f" …（共 {len(names)} 个）" if len(names) > 6 else ""
                lines.append(f"  ✓ {name} 连上了，工具：{'、'.join(names[:6])}{more}")
            elif state.status == "disabled":
                lines.append(f"  ○ {name} 配置里写了 disabled，没有启动")
            else:
                lines.append(f"  ✗ {name} 连接失败：{state.error}")
                if config.missing_vars:
                    var = config.missing_vars[0]
                    lines.append(f"    在 ~/.zshrc 里加 export {var}=… 后重开终端，或把值直接写进 "
                                 f"{short(path)}，然后 /mcp reconnect {name}")
        lines.append("下一次提问时模型就能用它的工具（/mcp tools 查看）")
        return "\n".join(lines)

    async def remove_servers(session, name: str) -> str:
        from pi_coding_agent.mcp.config import remove_server

        if not name:
            return "用法：/mcp remove <服务器名>（/mcp 查看有哪些）"
        try:
            changed = remove_server(session.cwd, agent_dir(session), name)
        except ValueError as exc:
            return f"删除失败：{exc}"
        await manager(session).remove(name)
        if not changed:
            return f"pi 的配置里没有叫 {name} 的服务器"
        return f"已删除 {name}（{'、'.join(short(p) for p in changed)}），进程已关闭"

    async def cmd_mcp(rest: str) -> str:
        from pi_coding_agent.mcp.config import import_servers

        session = pi.session
        if session is None:
            return "还没有活跃会话"
        words = rest.split()
        action = words[0] if words else ""
        if action == "warmup":      # 界面打开会话时在后台调：先连上，第一次提问不用等
            mgr = manager(session)
            await mgr.ensure_started()
            failed = [s.name for s in mgr.servers.values() if s.status == "failed"]
            return f"MCP：{'、'.join(failed)} 连接失败（/mcp 查看原因）" if failed else ""
        if action == "add":
            return await add_servers(session, re.sub(r"^\s*add(?=\s|$)", "", rest, count=1))
        if action in ("remove", "rm"):
            return await remove_servers(session, words[1] if len(words) > 1 else "")
        if action == "import":
            done, skipped = import_servers(session.cwd, agent_dir(session))
            if not done:
                return "没有可导入的服务器" + (f"（跳过：{'、'.join(skipped)}）" if skipped else "")
            progress(session, f"已导入 {len(done)} 个，正在连接（npx / uvx 第一次要下载，可能要等一会儿）")
            await reload(session)
            text = "已导入：" + "、".join(done)
            if skipped:
                text += "\n跳过：" + "、".join(skipped)
            return text + "\n\n" + status_text(session)
        if action == "reconnect":
            target = words[1] if len(words) > 1 else None
            if target and target not in manager(session).servers:
                return f"没有叫 {target} 的服务器"
            progress(session, f"正在重连 {target or '全部服务器'}")
            await reload(session, target)
            return status_text(session)
        if action == "tools":
            mgr = manager(session)
            if any(s.status == "pending" for s in mgr.servers.values()):
                progress(session, "正在连接 MCP 服务器")
            await mgr.ensure_started()
            from pi_coding_agent.mcp.manager import tool_name
            lines = []
            for state in mgr.servers.values():
                if (len(words) > 1 and state.name != words[1]) or state.status != "ready":
                    continue
                lines.append(f"{state.name}（{len(state.tools)} 个）：")
                for spec in state.tools:
                    desc = " ".join(str(spec.get("description") or "").split())[:70]
                    ro = " [只读]" if (spec.get("annotations") or {}).get("readOnlyHint") else ""
                    lines.append(f"  {tool_name(state.name, spec['name'])}{ro}  {desc}")
            return "\n".join(lines) or "没有已连上的服务器（/mcp 查看状态）"
        if action:
            return USAGE
        return status_text(session)

    pi.register_command("mcp", "MCP 服务器：状态、添加 / 删除、工具、重连、从别的客户端导入", cmd_mcp,
                        usage="/mcp [tools|add|remove|reconnect|import]")
