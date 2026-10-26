"""子智能体：主智能体把一段自包含的任务交出去，子智能体在**独立的上下文**里做完，只交回一份报告。

为什么要它（同 Claude Code 的 Task / 子代理）：大范围调研要读几十个文件、搜很多次，过程全堆在主对话里，
上下文很快就满了、压缩还会丢细节；而主对话真正需要的只是结论。交给子智能体，过程留在它那边，
主对话只多一条报告。几个互不相关的调研还可以同一轮里并行派出去。

怎么做的：不新建 AgentSession——它构造时会把自己挂到扩展的 ``pi.session`` 上，权限、MCP 这些扩展
就认错会话了。直接用底层的 ``pi_agent.Agent``：同一个模型（可在设置里换便宜的）、同一份系统提示
（AGENTS.md、技能都在）再加子智能体的规则，工具按类型给：

- ``explore``（默认）：只读——read / grep / find / ls / web_search / web_fetch，bash 只放行只读命令
  （同权限扩展的 is_read_only），不用用户确认；
- ``general``：和主智能体一样的工具（不含 task 本身和计划 / 提问类工具），工具调用经主会话的
  before_tool_call 钩子——改文件、跑命令照常弹确认。

不会失控：子智能体没有 task 工具（不能再派）；轮数到 ``maxTurns`` 用 steering 要它收尾，再多两轮强制停；
主智能体被中断（Esc）时一起停；没有压缩，靠轮数上限和工具自身的输出截断控制上下文。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pi_agent import Agent, AgentState, AgentTool, BeforeToolCallResult
from pi_ai import AssistantMessage

READ_ONLY_TOOLS = ("read", "grep", "find", "ls", "bash")
RESEARCH_TOOLS = ("web_search", "web_fetch")
# general 子智能体也不给的：不能再派子智能体；计划 / 提问 / 任务清单是主对话和用户之间的事
NEVER = {"task", "update_plan", "ask_user", "submit_plan"}
AGENT_TYPES = ("explore", "general")
STEP_LIMIT = 60

SUBAGENT_PROMPT = """

<subagent>
You are a sub-agent. The main agent delegated ONE task to you; you are not talking to the user.
- You cannot see the main conversation. Everything you know about the task is in the task message.
- Work autonomously: you cannot ask questions. If something is ambiguous, pick the most reasonable \
interpretation and state your assumption in the report.
- Be efficient: batch independent tool calls in parallel, read only what the task needs, stop once you \
can answer. There is no update_plan tool here; ignore guidance about it.
- Your final message is the only thing the main agent receives. Make it a self-contained report: \
direct answers first, then evidence with file paths and line numbers (path:line), commands you ran \
and their results, and anything you could not verify. Keep it focused; no preamble.
</subagent>"""

READ_ONLY_PROMPT = """
<subagent_mode>
Read-only: you can read, search and run read-only shell commands (ls, cat, git log/diff/status, grep...). \
You cannot create, edit or delete files, or run commands that change anything; such calls are blocked.
</subagent_mode>"""

WRAP_UP = ("[system] You have reached your step budget. Stop exploring now and write your final report "
           "with what you have found so far, noting what remains unverified.")


@dataclass
class SubagentResult:
    report: str
    ok: bool = True
    error: str = ""
    turns: int = 0
    tool_calls: int = 0
    seconds: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cost: float = 0.0
    steps: list[str] = field(default_factory=list)


def describe_call(name: str, args: dict) -> str:
    """一次工具调用一行：名字 + 最能说明它的参数"""
    for key in ("path", "command", "pattern", "query", "url"):
        value = args.get(key) if isinstance(args, dict) else None
        if value:
            text = " ".join(str(value).split())
            return f"{name} {text[:80]}"
    return name


def pick_tools(kind: str, parent_tools: list[AgentTool], cwd: str) -> list[AgentTool]:
    from .tools import create_tools

    if kind == "explore":
        tools = create_tools(READ_ONLY_TOOLS, cwd)
        tools += [t for t in parent_tools if t.name in RESEARCH_TOOLS]
        return tools
    return [t for t in parent_tools if t.name not in NEVER]


def read_only_guard():
    """explore 的工具闸门：bash 只放行只读命令，其余工具本来就是只读的"""
    from .builtin_extensions.permissions import is_read_only

    async def guard(call, state) -> BeforeToolCallResult | None:
        if call.name == "bash" and not is_read_only(str((call.arguments or {}).get("command") or "")):
            return BeforeToolCallResult(block=True, reason=(
                "This sub-agent is read-only: only read-only shell commands are allowed "
                "(ls, cat, grep, git log/diff/status...). Do not retry; report what would need to be done."))
        return None

    return guard


def _last_text(messages: list) -> tuple[str, AssistantMessage | None]:
    for message in reversed(messages):
        if isinstance(message, AssistantMessage):
            text = "".join(getattr(c, "text", "") or "" for c in message.content
                           if getattr(c, "type", "") == "text")
            return text.strip(), message
    return "", None


async def run_subagent(
    *,
    task: str,
    kind: str,
    model: Any,
    stream_fn: Any,
    system_prompt: str,
    tools: list[AgentTool],
    cwd: str,
    thinking_level: str = "off",
    before_tool_call: Any = None,
    max_turns: int = 30,
    cancel: asyncio.Event | None = None,
    on_activity: Callable[[str, int], None] | None = None,
) -> SubagentResult:
    """跑一个子智能体直到它交出报告（或被中断、出错、超出轮数）"""
    from pi_ai import is_retryable, retry_after

    prompt = system_prompt + SUBAGENT_PROMPT + (READ_ONLY_PROMPT if kind == "explore" else "")
    state = AgentState(system_prompt=prompt, model=model, thinking_level=thinking_level, tools=tools)
    retries = {"n": 0}

    async def on_stream_error(message: AssistantMessage) -> bool:
        """临时失败（限流、过载、断网）等一会儿重试，最多 3 次；别的错误交给结果"""
        if not is_retryable(message) or retries["n"] >= 3:
            return False
        retries["n"] += 1
        delay = retry_after(message) or 2.0 * 2 ** (retries["n"] - 1)
        return not await agent.wait_cancelled(min(delay, 30.0))

    agent = Agent(stream_fn=stream_fn, initial_state=state, tool_execution="parallel",
                  max_turns=max_turns + 2, before_tool_call=before_tool_call,
                  on_stream_error=on_stream_error, max_error_retries=4)
    result = SubagentResult(report="")
    started = time.monotonic()

    def listen(event) -> None:
        kind_ = getattr(event, "type", "")
        if kind_ == "tool_execution_start":
            result.tool_calls += 1
            step = describe_call(event.tool_name, event.arguments or {})
            if len(result.steps) < STEP_LIMIT:
                result.steps.append(step)
            if on_activity:
                on_activity(step, result.tool_calls)
        elif kind_ == "message_end" and isinstance(event.message, AssistantMessage):
            usage = event.message.usage
            if usage is not None:
                result.input_tokens += usage.input + usage.cache_read + usage.cache_write
                result.output_tokens += usage.output
                result.cost += usage.cost.total if usage.cost else 0.0
        elif kind_ == "turn_end":
            result.turns += 1
            if result.turns == max_turns:
                agent.steer(WRAP_UP)

    agent.subscribe(listen)
    run = asyncio.ensure_future(agent.prompt(task))
    stopper = asyncio.ensure_future(cancel.wait()) if cancel is not None else None
    try:
        waiters = {run} | ({stopper} if stopper else set())
        done, _ = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
        if run not in done:            # 主智能体被中断：子智能体一起停
            agent.abort()
        try:
            await run
        except Exception as exc:  # noqa: BLE001 —— 子智能体崩了也要给主智能体一个交代
            result.ok, result.error = False, f"{type(exc).__name__}: {exc}"
    finally:
        if stopper:
            stopper.cancel()
    result.seconds = time.monotonic() - started
    text, last = _last_text(agent.state.messages)
    result.report = text
    if cancel is not None and cancel.is_set():
        result.ok, result.error = False, "被中断（用户停止了这一轮）"
    elif last is not None and last.stop_reason == "error":
        result.ok, result.error = False, last.error_message or "模型调用出错"
    elif last is not None and last.stop_reason == "aborted":
        result.ok, result.error = False, "被中断"
    elif not text and result.ok:
        result.ok, result.error = False, "子智能体没有给出报告"
    return result
