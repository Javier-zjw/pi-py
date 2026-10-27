"""自定义子智能体测试（custom_agents.py + task 工具）：不联网，假模型按"谁在问"回答。

    python tests/test_custom_agents.py
    pytest tests/test_custom_agents.py

覆盖：读定义（pi 格式、Claude Code 格式和工具名换算、YAML 列表、同名覆盖顺序、跳过的文件和原因）、
挑工具（继承、指定、MCP 通配、只读、NEVER）、接进会话（task 说明列出、可选值、角色说明进子智能体
提示、工具、模型找不到时用当前的、maxTurns、未知名字、计划模式只读、改动照常确认、文件改了下一轮更新、
没变时说明不动）、/agents。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[1]))                     # packages/pi-coding-agent
sys.path.insert(0, str(HERE.parents[2] / "pi-agent"))
sys.path.insert(0, str(HERE.parents[2] / "pi-ai"))
sys.path.insert(0, str(HERE.parent))

from fakes import Sandbox
from pi_agent import AgentTool
from pi_ai import TextContent, ToolCall
from pi_coding_agent.custom_agents import load_agents, pick_tools, tool_listing
from pi_coding_agent.subagent import NEVER, WRAP_UP
from test_subagents import is_child, make_session, task_call, tool_results

FAILURES: list[str] = []


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


def text_of(message) -> str:
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    return "".join(getattr(c, "text", "") or "" for c in content)


@contextlib.contextmanager
def home(path: Path):
    """~/.claude/agents 读的是家目录：测试时指到沙盒里"""
    old = os.environ.get("HOME")
    os.environ["HOME"] = str(path)
    try:
        yield path
    finally:
        if old is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = old


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


REVIEWER = """---
name: reviewer
description: 审查改动有没有 bug、是否符合项目约定。改完一批代码后用。
tools: read, grep, bash
maxTurns: 3
---
你是严格的代码审查员。只报告真正的问题，按严重程度排序。
"""

CLAUDE_STYLE = """---
name: Doc-Writer
description: Writes docs
tools: Read, Glob, WebFetch, NotebookEdit, mcp__github__*
model: sonnet
---
You write docs.
"""


def test_load() -> None:
    with Sandbox() as root, home(root / "home") as fake_home:
        cwd, agent_dir = root / "proj", root / "agent"
        write(cwd / ".pi" / "agents" / "reviewer.md", REVIEWER)
        write(fake_home / ".claude" / "agents" / "doc-writer.md", CLAUDE_STYLE)
        write(fake_home / ".claude" / "agents" / "shared.md", "---\ndescription: 来自 Claude 全局\n---\nA")
        write(cwd / ".pi" / "agents" / "shared.md", "---\ndescription: 来自项目\n---\nB")
        write(agent_dir / "agents" / "listy.md",
              "---\ndescription: 列表写法\ntools:\n  - Read\n  - Grep\nmaxTurns: lots\n---\n")
        write(cwd / ".pi" / "agents" / "nodesc.md", "---\nname: nodesc\n---\n没有描述")
        write(cwd / ".pi" / "agents" / "explore.md", "---\ndescription: 想覆盖内置\n---\n")
        write(cwd / ".pi" / "agents" / "README.md", "说明文件，不是子智能体")
        agents, skipped = load_agents(cwd, agent_dir)

        reviewer = agents.get("reviewer")
        check("pi 格式: 名字、描述、工具、轮数、正文", reviewer is not None
              and reviewer.tools == ["read", "grep", "bash"] and reviewer.max_turns == 3
              and reviewer.prompt.startswith("你是严格的代码审查员") and reviewer.source == "project",
              str(reviewer))
        doc = agents.get("doc-writer")
        check("Claude Code 格式: 名字转小写、工具名换成 pi 的", doc is not None
              and doc.tools == ["read", "find", "web_fetch", "mcp__github__*"] and doc.model == "sonnet"
              and doc.source == "claude-user", str(doc))
        check("Claude Code 格式: pi 没有的工具忽略并提示", doc is not None
              and any("NotebookEdit" in n for n in doc.notes), str(doc.notes if doc else ""))
        check("YAML 列表写法的 tools", agents.get("listy") is not None
              and agents["listy"].tools == ["read", "grep"]
              and any("maxTurns" in n for n in agents["listy"].notes), str(agents.get("listy")))
        check("同名: 项目的覆盖 Claude 全局的", agents["shared"].description == "来自项目")
        check("跳过: 缺 description、占用内置名字，说明原因；README 不算",
              "nodesc" not in agents and "explore" not in agents and "readme" not in agents
              and any("nodesc.md" in s and "description" in s for s in skipped)
              and any("explore.md" in s and "内置" in s for s in skipped) and len(skipped) == 2,
              str(skipped))
        with home(cwd):     # 在家目录里启动：~/.claude/agents 就是 <cwd>/.claude/agents
            write(cwd / ".claude" / "agents" / "broken.md", "---\nname: broken\n---\n")
            _, again = load_agents(cwd, agent_dir)
        check("家目录里启动: 同一个目录只读一次", sum("broken.md" in s for s in again) == 1, str(again))
        listing = tool_listing(agents)
        check("task 说明里的列表", "- reviewer: 审查改动" in listing and "- doc-writer: Writes docs" in listing)


def test_pick_tools() -> None:
    async def noop(args, ctx):
        return None

    parent = [AgentTool(name=n, description="", parameters={"type": "object"}, execute=noop)
              for n in ("read", "write", "edit", "bash", "web_search", "web_fetch", "task", "memory",
                        "mcp__github__search", "mcp__github__create_issue", "mcp__slack__post")]
    with Sandbox() as root, home(root):
        write(root / ".pi" / "agents" / "a.md", "---\ndescription: d\n---\n")
        write(root / ".pi" / "agents" / "b.md",
              "---\ndescription: d\ntools: read, grep, mcp__github__*, task, edit\n---\n")
        agents, _ = load_agents(root, root / "agent")
    inherit = {t.name for t in pick_tools(agents["a"], parent, ".", NEVER)}
    check("继承: 主会话的工具去掉 NEVER", "write" in inherit and "mcp__slack__post" in inherit
          and not inherit & NEVER, str(inherit))
    chosen = {t.name for t in pick_tools(agents["b"], parent, ".", NEVER)}
    check("指定: 内置文件工具现建（主会话没 grep 也给）、MCP 按通配、task 不给",
          chosen == {"read", "grep", "edit", "mcp__github__search", "mcp__github__create_issue"},
          str(chosen))
    readonly = {t.name for t in pick_tools(agents["a"], parent, ".", NEVER, read_only=True)}
    check("只读: 去掉改文件和 MCP", readonly == {"read", "bash", "web_search", "web_fetch"}, str(readonly))


async def case_session() -> None:
    with Sandbox() as cwd, home(cwd / "home"):
        write(cwd / ".pi" / "agents" / "reviewer.md", REVIEWER)
        write(cwd / ".pi" / "agents" / "writer.md",
              "---\ndescription: 写文件\ntools: write, bash\nmodel: sonnet\n---\n你负责写文件。")
        plan_mode = {"on": False}

        async def respond(context):
            if not is_child(context):
                done = tool_results(context)
                if not done:
                    return [task_call("t1", "审查", "看看 app.py", agent="reviewer"),
                            task_call("t2", "写配置", "创建 out.txt", agent="writer"),
                            task_call("t3", "乱写名字", "x", agent="nobody")], None
                return [TextContent(text="好")], None
            role = "审查员" if "代码审查员" in context.system_prompt else "写手"
            if role == "写手" and not tool_results(context):
                return [ToolCall(id="w1", name="write", arguments={"path": "out.txt", "content": "ok"})], None
            if role == "审查员" and len(tool_results(context)) < 5:
                # 一直读，看 maxTurns 能不能让它收尾
                n = len(tool_results(context))
                return [ToolCall(id=f"r{n}", name="read", arguments={"path": "missing.txt"})], None
            return [TextContent(text=f"{role}的报告")], None

        session, router = make_session(cwd, respond)
        asked: list = []

        async def handler(request):
            asked.append(request)
            return {"decision": "allow"}

        session.permission_handler = handler
        await session.prompt("审查并写配置")
        task_tool = next(t for t in router.calls[0].tools if t.name == "task")
        check("说明: task 工具里列出自定义子智能体", "- reviewer: 审查改动" in task_tool.description
              and "- writer: 写文件" in task_tool.description, task_tool.description[-300:])
        check("说明: agent 的可选值带上它们", task_tool.parameters["properties"]["agent"]["enum"]
              == ["explore", "general", "reviewer", "writer"])
        results = {m.tool_call_id: m for m in session.messages if getattr(m, "role", "") == "toolResult"}

        children = [c for c in router.calls if is_child(c)]
        reviewer_calls = [c for c in children if "代码审查员" in c.system_prompt]
        check("角色: 正文进子智能体的系统提示（在子智能体规则之后）", reviewer_calls
              and '<agent_role name="reviewer">' in reviewer_calls[0].system_prompt
              and reviewer_calls[0].system_prompt.index("<subagent>")
              < reviewer_calls[0].system_prompt.index("<agent_role"))
        check("工具: 按定义给（主会话没 grep 也有）", reviewer_calls
              and {t.name for t in reviewer_calls[0].tools} == {"read", "grep", "bash"},
              str({t.name for t in reviewer_calls[0].tools} if reviewer_calls else ""))
        check("轮数: maxTurns 到了要它收尾", any(WRAP_UP in text_of(m) for c in reviewer_calls
                                               for m in c.messages if getattr(m, "role", "") == "user"))
        check("结果: 标出用的哪个子智能体", "(reviewer) finished" in text_of(results["t1"])
              and results["t1"].details["agent"] == "reviewer", text_of(results["t1"])[:120])
        check("模型: 写的 sonnet 找不到时用当前模型、说一声", not results["t2"].is_error
              and "模型 sonnet 找不到" in text_of(results["t2"]), text_of(results["t2"])[:200])
        check("权限: 改文件照常经主会话确认", [r["tool"] for r in asked] == ["write"]
              and (cwd / "out.txt").read_text() == "ok", str(asked))
        check("未知名字: 报错并列出能用的（参数校验就拦下）", results["t3"].is_error
              and all(n in text_of(results["t3"]) for n in ("explore", "general", "reviewer", "writer")),
              text_of(results["t3"]))

        description = task_tool.description
        await session.prompt("再来")
        same = next(t for t in router.calls[-1].tools if t.name == "task")
        check("缓存: 文件没变，说明一字不动", same.description == description)
        write(cwd / ".pi" / "agents" / "tester.md", "---\ndescription: 跑测试\n---\n")
        await session.prompt("又来")
        updated = next(t for t in router.calls[-1].tools if t.name == "task")
        check("文件改了: 下一轮的说明里就有", "- tester: 跑测试" in updated.description)

        listing = await session.run_command("agents", "")
        check("/agents: 内置 + 自定义 + 工具 / 模型 / 位置", "explore" in listing and "reviewer" in listing
              and "工具 read, grep, bash" in listing and "模型 sonnet" in listing
              and ".pi/agents/reviewer.md" in listing, listing)
        del plan_mode


async def case_plan_mode() -> None:
    with Sandbox() as cwd, home(cwd / "home"):
        write(cwd / ".pi" / "agents" / "writer.md",
              "---\ndescription: 写文件\ntools: write, edit, bash, read\n---\n你负责写文件。")

        async def respond(context):
            if not is_child(context):
                if not tool_results(context):
                    return [task_call("t1", "写", "创建 out.txt", agent="writer")], None
                return [TextContent(text="计划好了")], None
            if not tool_results(context):
                return [ToolCall(id="b1", name="bash", arguments={"command": "echo x > out.txt"})], None
            return [TextContent(text="报告")], None

        session, router = make_session(cwd, respond)
        await session.set_mode("plan")
        await session.prompt("规划一下")
        child = next(c for c in router.calls if is_child(c))
        result = next(m for m in session.messages if getattr(m, "role", "") == "toolResult")
        check("计划模式: 自定义子智能体只给只读工具", not {t.name for t in child.tools} & {"write", "edit"}
              and "计划模式里只给它只读工具" in text_of(result), str({t.name for t in child.tools}))
        check("计划模式: 写文件的 bash 被拦、提示里写明只读", not (cwd / "out.txt").exists()
              and "Read-only" in child.system_prompt)


def test_session() -> None:
    asyncio.run(case_session())


def test_plan_mode() -> None:
    asyncio.run(case_plan_mode())


async def main() -> int:
    print("读定义");       test_load()
    print("挑工具");       test_pick_tools()
    print("接进会话");     await case_session()
    print("计划模式");     await case_plan_mode()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} 处失败：")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("全部通过 —— 自定义子智能体可用")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
