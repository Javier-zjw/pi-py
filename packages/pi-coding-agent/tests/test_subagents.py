"""子智能体测试（task 工具）：不联网，假模型按"谁在问、问什么"回答。

    python tests/test_subagents.py
    pytest tests/test_subagents.py

覆盖：并行派出两个、上下文隔离（子智能体的读文件过程不进主对话）、explore 只读（写命令被拦）、
general 的工具调用经主会话的权限钩子、轮数上限时要求收尾、中断、计划模式强制只读、
模型配置错误、子智能体出错、进度更新、工具集挑选。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[1]))                     # packages/pi-coding-agent
sys.path.insert(0, str(HERE.parents[2] / "pi-agent"))
sys.path.insert(0, str(HERE.parents[2] / "pi-ai"))
sys.path.insert(0, str(HERE.parent))

from fakes import FAKE_MODEL, Sandbox  # noqa: E402
from pi_ai import (  # noqa: E402
    AssistantMessage,
    DoneEvent,
    Models,
    StartEvent,
    TextContent,
    TextDeltaEvent,
    ToolCall,
    ToolCallEndEvent,
    Usage,
)
from pi_coding_agent import (  # noqa: E402
    AgentSession,
    ModelRuntime,
    SessionManager,
    create_agent_session_services,
)
from pi_coding_agent.subagent import WRAP_UP, pick_tools  # noqa: E402

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


# --------------------------------------------------------------------------- #
# 按请求内容回答的假模型
# --------------------------------------------------------------------------- #

def text_of(message) -> str:
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    return "".join(getattr(c, "text", "") or "" for c in content)


class Router:
    """respond(context) → (blocks, error)。记录每次调用；子智能体按系统提示里的 <subagent> 认"""

    api = "anthropic-messages"

    def __init__(self, respond) -> None:
        self.id = self.name = "fake"
        self.respond = respond
        self.calls: list = []

    def stream(self, model, context, options=None, api_key=None):
        self.calls.append(context)

        async def gen():
            blocks, error = await self.respond(context)
            message = AssistantMessage(api=model.api, provider=model.provider, model=model.id,
                                       usage=Usage(input=100, output=20))
            message.usage.cost.total = 0.01
            yield StartEvent(partial=message)
            if error:
                message.stop_reason, message.error_message = "error", error
                yield DoneEvent(reason="error", message=message)
                return
            for block in blocks:
                message.content.append(block)
                index = len(message.content) - 1
                if isinstance(block, TextContent):
                    yield TextDeltaEvent(content_index=index, delta=block.text, partial=message)
                else:
                    yield ToolCallEndEvent(content_index=index, tool_call=block, partial=message)
            message.stop_reason = "toolUse" if any(isinstance(b, ToolCall) for b in blocks) else "stop"
            yield DoneEvent(reason=message.stop_reason, message=message)

        return gen()


def is_child(context) -> bool:
    return "<subagent>" in (context.system_prompt or "")


def task_text(context) -> str:
    return text_of(next(m for m in context.messages if getattr(m, "role", "") == "user"))


def tool_results(context) -> list:
    return [m for m in context.messages if getattr(m, "role", "") == "toolResult"]


def make_session(cwd: Path, respond, **settings) -> tuple[AgentSession, Router]:
    router = Router(respond)
    models = Models(models=[FAKE_MODEL])
    models.set_provider(router)
    if settings:
        (cwd / ".pi").mkdir(exist_ok=True)
        (cwd / ".pi" / "settings.json").write_text(json.dumps(settings))
    services = create_agent_session_services(cwd=cwd, agent_dir=cwd / "agent",
                                             model_runtime=ModelRuntime(models, cwd / "agent"))
    session = AgentSession(services, SessionManager.in_memory(cwd), model=FAKE_MODEL,
                           tools=["read", "bash", "write"])
    return session, router


def task_call(call_id: str, description: str, prompt: str, agent: str | None = None) -> ToolCall:
    args = {"description": description, "prompt": prompt}
    if agent:
        args["agent"] = agent
    return ToolCall(id=call_id, name="task", arguments=args)


def results_by_id(session) -> dict:
    return {m.tool_call_id: m for m in session.messages if getattr(m, "role", "") == "toolResult"}


# --------------------------------------------------------------------------- #
# 1. 并行、隔离、只读
# --------------------------------------------------------------------------- #

async def case_parallel_explore() -> None:
    with Sandbox() as cwd:
        (cwd / "README.md").write_text("# demo 项目\n第二行\n")
        both_started = asyncio.Event()
        started: set[str] = set()
        overlapped: list[bool] = []     # 每个子智能体第一次调用时，另一个是不是也已经在跑

        async def respond(context):
            if not is_child(context):
                if not tool_results(context):
                    return [task_call("t1", "读 README", "任务A：读 README.md 的第一行"),
                            task_call("t2", "试着写文件", "任务B：创建 x.txt")], None
                return [TextContent(text="主智能体：两个子任务都回来了")], None
            task = task_text(context)
            started.add(task[:3])
            if len(started) == 2:
                both_started.set()
            done = tool_results(context)
            if not done:
                try:        # 两个子智能体的第一次调用要同时在等：串行执行时等不到另一个
                    await asyncio.wait_for(both_started.wait(), 2)
                    overlapped.append(True)
                except asyncio.TimeoutError:
                    overlapped.append(False)
            if task.startswith("任务A"):
                if not done:
                    return [ToolCall(id="a1", name="read", arguments={"path": "README.md"})], None
                return [TextContent(text=f"报告A：第一行是 {text_of(done[0]).splitlines()[0]}")], None
            if not done:
                return [ToolCall(id="b1", name="bash", arguments={"command": "echo hi > x.txt"})], None
            return [TextContent(text=f"报告B：写文件被拦下了（{text_of(done[0])[:40]}）")], None

        session, router = make_session(cwd, respond)
        updates: list = []
        session.subscribe(lambda e: updates.append(e.partial.details)
                          if e.type == "tool_execution_update" else None)
        await session.prompt("调研一下")

        check("并行: 两个子智能体同时在跑", overlapped == [True, True], str(overlapped))
        results = results_by_id(session)
        a, b = results["t1"], results["t2"]
        check("报告: 子智能体的最终回复交回给主智能体", "报告A：第一行是" in text_of(a)
              and "# demo 项目" in text_of(a) and not a.is_error, text_of(a))
        check("报告: 开头一行写明工具次数和轮数", "1 tool calls, 2 turns" in text_of(a), text_of(a)[:120])
        check("只读: explore 里写文件的 bash 被拦", not (cwd / "x.txt").exists() and "read-only" in text_of(b),
              text_of(b))
        check("隔离: 子智能体读文件的过程不进主对话", [getattr(m, "role", "") for m in session.messages]
              == ["user", "assistant", "toolResult", "toolResult", "assistant"],
              str([getattr(m, "role", "") for m in session.messages]))
        child_calls = [c for c in router.calls if is_child(c)]
        child_tools = {t.name for t in child_calls[0].tools}
        check("工具: explore 只给只读工具 + 上网", child_tools == {"read", "grep", "find", "ls", "bash",
                                                                "web_search", "web_fetch"}, str(child_tools))
        check("提示: 子智能体规则 + 只读说明 + 主会话的系统提示", "<subagent>" in child_calls[0].system_prompt
              and "<subagent_mode>" in child_calls[0].system_prompt
              and session._base_system_prompt in child_calls[0].system_prompt)
        check("隔离: 子智能体看不到主对话（只有任务这一条用户消息）",
              sum(getattr(m, "role", "") == "user" for m in child_calls[0].messages) == 1)
        details = a.details
        check("细节: 步骤、次数、用量都记下", details["steps"] == ["read README.md"] and details["tools"] == 1
              and details["turns"] == 2 and details["cost"] > 0 and details["agent"] == "explore", str(details))
        activities = [d.get("activity") for d in updates if isinstance(d, dict)]
        check("进度: 运行中报告正在做什么", "read README.md" in activities and "排队中" in activities,
              str(activities))


# --------------------------------------------------------------------------- #
# 2. general：经主会话的权限钩子
# --------------------------------------------------------------------------- #

async def case_general_permissions() -> None:
    with Sandbox() as cwd:
        async def respond(context):
            if not is_child(context):
                if not tool_results(context):
                    return [task_call("g1", "写配置", "创建 config.txt，内容 ok", agent="general")], None
                return [TextContent(text="好")], None
            if not tool_results(context):
                return [ToolCall(id="w1", name="write", arguments={"path": "config.txt", "content": "ok"})], None
            return [TextContent(text="报告：已创建 config.txt")], None

        session, router = make_session(cwd, respond)
        asked: list[dict] = []

        async def handler(request):
            asked.append(request)
            return {"decision": "allow"}

        session.permission_handler = handler
        await session.prompt("改一下")
        result = results_by_id(session)["g1"]
        check("general: 改文件经主会话的权限确认", [r["tool"] for r in asked] == ["write"], str(asked))
        check("general: 同意后真的改了", (cwd / "config.txt").read_text() == "ok" and not result.is_error,
              text_of(result))
        child_tools = {t.name for t in next(c for c in router.calls if is_child(c)).tools}
        check("general: 和主智能体一样的工具，但没有 task / update_plan（不能再派、不管清单）",
              {"read", "bash", "write", "web_search"} <= child_tools
              and not child_tools & {"task", "update_plan", "ask_user", "submit_plan"}, str(child_tools))


# --------------------------------------------------------------------------- #
# 3. 轮数上限、中断、出错、计划模式、模型配置
# --------------------------------------------------------------------------- #

async def case_limits() -> None:
    with Sandbox() as cwd:
        async def respond(context):
            if not is_child(context):
                if not tool_results(context):
                    return [task_call("m1", "停不下来", "一直列目录")], None
                return [TextContent(text="好")], None
            if any(WRAP_UP in text_of(m) for m in context.messages if getattr(m, "role", "") == "user"):
                return [TextContent(text="收尾报告：目录里没什么")], None
            n = len(tool_results(context))
            return [ToolCall(id=f"ls{n}", name="ls", arguments={"path": "."})], None

        session, _ = make_session(cwd, respond, subagents={"maxTurns": 3})
        await session.prompt("查")
        result = results_by_id(session)["m1"]
        check("上限: 到 maxTurns 要求收尾、交出报告", "收尾报告" in text_of(result) and not result.is_error
              and result.details["turns"] == 4, f"{result.details.get('turns')} {text_of(result)[:200]}")

    with Sandbox() as cwd:
        hold = asyncio.Event()

        async def respond(context):
            if not is_child(context):
                if not tool_results(context):
                    return [task_call("c1", "慢任务", "慢慢做")], None
                return [TextContent(text="好")], None
            hold.set()
            await asyncio.sleep(30)
            return [TextContent(text="不该到这")], None

        session, _ = make_session(cwd, respond)
        run = asyncio.ensure_future(session.prompt("查"))
        await asyncio.wait_for(hold.wait(), 5)
        session.abort()
        await asyncio.wait_for(run, 5)
        result = results_by_id(session).get("c1")
        check("中断: Esc 时子智能体一起停", result is not None and result.is_error
              and "被中断" in text_of(result), text_of(result) if result else "没有结果")

    with Sandbox() as cwd:
        async def respond(context):
            if not is_child(context):
                if not tool_results(context):
                    return [task_call("e1", "出错", "做点事")], None
                return [TextContent(text="好")], None
            return [], "401 invalid api key"

        session, _ = make_session(cwd, respond)
        await session.prompt("查")
        result = results_by_id(session)["e1"]
        check("出错: 模型报错原样交回", result.is_error and "invalid api key" in text_of(result), text_of(result))

    with Sandbox() as cwd:
        async def respond(context):
            if not is_child(context):
                if not tool_results(context):
                    return [task_call("p1", "改代码", "把 x 改成 y", agent="general")], None
                return [TextContent(text="好")], None
            return [TextContent(text="只读调研的报告")], None

        session, router = make_session(cwd, respond)
        session.switch_mode("plan")
        await session.prompt("规划")
        result = results_by_id(session)["p1"]
        child = next(c for c in router.calls if is_child(c))
        check("计划模式: 有 task 工具", "task" in {t.name for t in router.calls[0].tools})
        check("计划模式: general 被换成只读 explore", result.details["agent"] == "explore"
              and "计划模式只能用只读" in text_of(result) and "<subagent_mode>" in child.system_prompt,
              text_of(result)[:200])

    with Sandbox() as cwd:
        async def respond(context):
            if not tool_results(context):
                return [task_call("x1", "x", "y")], None
            return [TextContent(text="好")], None

        session, _ = make_session(cwd, respond, subagents={"model": "nope/missing"})
        await session.prompt("查")
        result = results_by_id(session)["x1"]
        check("配置: 子智能体模型写错时说清楚", result.is_error and "nope/missing" in text_of(result),
              text_of(result))


def test_pick_tools() -> None:
    from pi_agent import AgentTool

    parent = [AgentTool(name=n, description="") for n in
              ("read", "write", "bash", "task", "update_plan", "web_search", "mcp__gh__search")]
    explore = {t.name for t in pick_tools("explore", parent, ".")}
    general = {t.name for t in pick_tools("general", parent, ".")}
    check("挑工具: explore 不含写文件和 MCP", "write" not in explore and "mcp__gh__search" not in explore
          and "web_search" in explore, str(explore))
    check("挑工具: general 含 MCP、不含 task / update_plan", "mcp__gh__search" in general
          and "task" not in general and "update_plan" not in general, str(general))


# --------------------------------------------------------------------------- #
# pytest 兼容层
# --------------------------------------------------------------------------- #

def test_parallel_explore() -> None:
    asyncio.run(case_parallel_explore())


def test_general_permissions() -> None:
    asyncio.run(case_general_permissions())


def test_limits() -> None:
    asyncio.run(case_limits())


async def main() -> int:
    print("并行与只读");   await case_parallel_explore()
    print("general");      await case_general_permissions()
    print("边界");         await case_limits(); test_pick_tools()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} 处失败：")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("全部通过 —— 子智能体可用")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
