"""
子智能体（同 Claude Code 的 Task 工具）：``task`` 工具把一段自包含的任务交给独立上下文的子智能体，
它做完只交回报告。实现见 ``pi_coding_agent/subagent.py``。

- 同一轮里调用多个 task 就是并行调研（主会话工具默认并行执行），同时最多 ``subagents.maxParallel`` 个；
- 类型：explore（默认，只读，不用确认）/ general（和主智能体一样的工具，改动照常确认）；
- 计划模式里也能用，但只能 explore（经 plan_mode 的 RESEARCH 挂上）；
- 运行中经 ctx.update 把"正在做什么"报给界面；用量计入结果的 details（界面加进费用）。

设置（settings.json）::

    "subagents": {"model": "", "maxTurns": 30, "maxParallel": 4, "maxOutputChars": 20000}

``model`` 留空用主会话当前的模型；可以写 ``provider/model`` 换一个便宜的。
"""

from __future__ import annotations

import asyncio

from pi_agent import AgentToolResult, ToolContext

NAME = "subagents"
DESCRIPTION = "子智能体：task 工具把调研、排查等任务交给独立上下文的子智能体，可并行"

TOOL_DESCRIPTION = (
    "Delegate a self-contained task to a sub-agent that works in its own context window and "
    "returns only a final report. Use it for broad or multi-step research that would otherwise flood "
    "this conversation with file contents and search results: exploring an unfamiliar part of the "
    "codebase, tracing how a feature works across many files, investigating a bug's cause, comparing "
    "options from documentation. Launch several in ONE message to run independent investigations in "
    "parallel. Do NOT use it for simple lookups you can do with one or two read/grep calls, or for "
    "tasks that need back-and-forth with the user.\n"
    "The sub-agent cannot see this conversation: write `prompt` as a complete brief: the goal, "
    "relevant paths or names you already know, constraints, and exactly what the report should "
    "contain. The user does not see the report; summarize what matters in your own reply.\n"
    "agent: 'explore' (default) is read-only and fast; 'general' can edit files and run commands "
    "(each change still needs the user's approval), use it only for a well-scoped change."
)

PARAMETERS = {
    "type": "object",
    "properties": {
        "description": {"type": "string",
                        "description": "A short (3-8 words) label for the task, shown to the user"},
        "prompt": {"type": "string", "description": "The complete task brief for the sub-agent"},
        "agent": {"type": "string", "enum": ["explore", "general"],
                  "description": "explore (read-only, default) or general (can edit and run commands)"},
    },
    "required": ["description", "prompt"],
}


def activate(pi) -> None:
    gate: dict = {"sem": None, "size": 0}

    def semaphore(size: int) -> asyncio.Semaphore:
        if gate["sem"] is None or gate["size"] != size:
            gate["sem"], gate["size"] = asyncio.Semaphore(size), size
        return gate["sem"]

    async def task(args: dict, ctx: ToolContext) -> AgentToolResult:
        from pi_coding_agent.subagent import pick_tools, read_only_guard, run_subagent

        session = pi.session
        if session is None:
            return AgentToolResult.text("no active session", is_error=True)
        get = session.settings.get
        description = " ".join(str(args.get("description") or "").split())[:60] or "子任务"
        brief = str(args.get("prompt") or "").strip()
        if not brief:
            return AgentToolResult.text("prompt is empty: write the complete task brief", is_error=True)
        kind = str(args.get("agent") or "explore")
        if kind not in ("explore", "general"):
            kind = "explore"
        forced = ""
        if session.mode == "plan" and kind != "explore":
            kind, forced = "explore", "（计划模式只能用只读的 explore 子智能体）"

        model = session.agent.state.model
        spec = str(get("subagents.model") or "").strip()
        if spec:
            chosen = session.model_runtime.resolve(spec)
            if chosen is None:
                return AgentToolResult.text(f"settings subagents.model = {spec!r} is not a known model",
                                            is_error=True)
            model = chosen
        tools = pick_tools(kind, list(session.agent.state.tools), session.cwd)
        guard = read_only_guard() if kind == "explore" else session._before_tool_call

        def activity(step: str, count: int) -> None:
            ctx.update(AgentToolResult.text(f"⏺ {step}", details={
                "activity": step, "tools": count, "agent": kind, "description": description}))

        ctx.update(AgentToolResult.text("排队中", details={"activity": "排队中", "tools": 0,
                                                           "agent": kind, "description": description}))
        async with semaphore(max(int(get("subagents.maxParallel") or 4), 1)):
            if ctx.cancelled:
                return AgentToolResult.text("cancelled before it started", is_error=True)
            activity("开始", 0)
            result = await run_subagent(
                task=brief, kind=kind, model=model, stream_fn=session.model_runtime.stream_fn,
                system_prompt=session._base_system_prompt, tools=tools, cwd=session.cwd,
                thinking_level=session.agent.state.thinking_level, before_tool_call=guard,
                max_turns=max(int(get("subagents.maxTurns") or 30), 2), cancel=ctx.cancel_event,
                on_activity=activity)

        limit = int(get("subagents.maxOutputChars") or 20000)
        report = result.report
        if len(report) > limit:
            report = report[:limit] + f"\n\n…（报告太长，省略了后面 {len(report) - limit} 字）"
        details = {
            "description": description, "agent": kind, "tools": result.tool_calls, "turns": result.turns,
            "seconds": round(result.seconds, 1), "steps": result.steps, "cost": result.cost,
            "input": result.input_tokens, "output": result.output_tokens,
            "model": getattr(model, "key", "") or getattr(model, "id", ""),
        }
        head = (f"[sub-agent '{description}' ({kind}) finished: {result.tool_calls} tool calls, "
                f"{result.turns} turns, {result.seconds:.0f}s]{forced}")
        if not result.ok:
            partial = f"\n\nPartial report:\n{report}" if report else ""
            details["error"] = result.error
            return AgentToolResult.text(f"{head}\nThe sub-agent did not finish: {result.error}{partial}",
                                        is_error=True, details=details)
        return AgentToolResult.text(f"{head}\n\n{report}", details=details)

    pi.register_tool("task", TOOL_DESCRIPTION, PARAMETERS, task, label="子智能体")
