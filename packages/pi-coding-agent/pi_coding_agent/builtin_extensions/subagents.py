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
import copy

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

AGENT_PARAM = "explore (read-only, default) or general (can edit and run commands)"

PARAMETERS = {
    "type": "object",
    "properties": {
        "description": {"type": "string",
                        "description": "A short (3-8 words) label for the task, shown to the user"},
        "prompt": {"type": "string", "description": "The complete task brief for the sub-agent"},
        "agent": {"type": "string", "enum": ["explore", "general"], "description": AGENT_PARAM},
    },
    "required": ["description", "prompt"],
}


def activate(pi) -> None:
    from pi_coding_agent.custom_agents import load_agents, signature, tool_listing
    from pi_coding_agent.subagent import AGENT_TYPES

    gate: dict = {"sem": None, "size": 0}
    catalog: dict = {"sig": None, "agents": {}, "skipped": []}
    parameters = copy.deepcopy(PARAMETERS)

    def refresh(session) -> dict:
        """读自定义子智能体；文件有变化才重读、才改 task 工具的说明和 agent 的可选值
        （工具说明在前缀缓存里，没变就别动）"""
        from pi_coding_agent.session.manager import DEFAULT_AGENT_DIR

        agent_dir = getattr(session.resources, "agent_dir", None) or DEFAULT_AGENT_DIR
        sig = (str(session.cwd), signature(session.cwd, agent_dir))
        if sig != catalog["sig"]:
            agents, skipped = load_agents(session.cwd, agent_dir)
            catalog.update(sig=sig, agents=agents, skipped=skipped)
            tool.description = TOOL_DESCRIPTION + tool_listing(agents)
            agent_param = parameters["properties"]["agent"]
            agent_param["enum"] = [*AGENT_TYPES, *agents]
            agent_param["description"] = AGENT_PARAM + (
                "; or a custom agent's name (see the list above)" if agents else "")
        return catalog["agents"]

    async def before_start(session) -> None:
        refresh(session)

    pi.before_agent_start(before_start)

    def semaphore(size: int) -> asyncio.Semaphore:
        if gate["sem"] is None or gate["size"] != size:
            gate["sem"], gate["size"] = asyncio.Semaphore(size), size
        return gate["sem"]

    async def task(args: dict, ctx: ToolContext) -> AgentToolResult:
        from pi_coding_agent.custom_agents import pick_tools as pick_custom_tools
        from pi_coding_agent.subagent import (
            AGENT_TYPES,
            NEVER,
            pick_tools,
            read_only_guard,
            run_subagent,
        )

        session = pi.session
        if session is None:
            return AgentToolResult.text("no active session", is_error=True)
        get = session.settings.get
        description = " ".join(str(args.get("description") or "").split())[:60] or "子任务"
        brief = str(args.get("prompt") or "").strip()
        if not brief:
            return AgentToolResult.text("prompt is empty: write the complete task brief", is_error=True)
        kind = str(args.get("agent") or "explore").strip()
        custom = None
        if kind not in AGENT_TYPES:
            custom = refresh(session).get(kind.lower())
            if custom is None:
                names = ", ".join([*AGENT_TYPES, *catalog["agents"]])
                return AgentToolResult.text(f"unknown agent {kind!r}; available: {names}", is_error=True)
            kind = custom.name
        forced = ""
        plan = session.mode == "plan"
        if plan and kind == "general":
            kind, forced = "explore", "（计划模式只能用只读的 explore 子智能体）"
        elif plan and custom is not None:
            forced = "（计划模式里只给它只读工具）"

        model = session.agent.state.model
        spec = (custom.model if custom and custom.model else "") or str(get("subagents.model") or "").strip()
        if spec:
            chosen = session.model_runtime.resolve(spec)
            if chosen is not None:
                model = chosen
            elif custom is not None and custom.model:
                # 自定义子智能体写的模型找不到（Claude Code 的 sonnet / haiku 这类别名）：用当前的，说一声
                forced += f"（模型 {spec} 找不到，用了当前模型）"
            else:
                return AgentToolResult.text(f"settings subagents.model = {spec!r} is not a known model",
                                            is_error=True)
        parent_tools = list(session.agent.state.tools)
        if custom is not None:
            tools = pick_custom_tools(custom, parent_tools, session.cwd, NEVER, read_only=plan)
        else:
            tools = pick_tools(kind, parent_tools, session.cwd)
        read_only = kind == "explore" or (custom is not None and plan)
        guard = read_only_guard() if read_only else session._before_tool_call
        max_turns = (custom.max_turns if custom and custom.max_turns else None) \
            or max(int(get("subagents.maxTurns") or 30), 2)

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
                # 工具执行后的钩子（改完就检查等）和主会话一样
                after_tool_call=session._after_tool_call,
                max_turns=max_turns, cancel=ctx.cancel_event,
                on_activity=activity, role=custom.prompt if custom else "", read_only=read_only)

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

    tool = pi.register_tool("task", TOOL_DESCRIPTION, parameters, task, label="子智能体")

    async def agents_command(rest: str) -> str:
        session = pi.session
        if session is None:
            return "没有会话"
        agents = refresh(session)
        lines = ["内置：", "  explore  只读调研（默认）", "  general  和主智能体一样的工具，改动照常确认", ""]
        if agents:
            lines.append("自定义：")
            for agent in agents.values():
                tools = "全部（同 general）" if agent.tools is None else (", ".join(agent.tools) or "无")
                extra = [f"工具 {tools}"]
                if agent.model:
                    extra.append(f"模型 {agent.model}")
                if agent.max_turns:
                    extra.append(f"最多 {agent.max_turns} 轮")
                lines.append(f"  {agent.name}  {agent.description}")
                lines.append(f"      {' · '.join(extra)} · {_short(agent.path)}")
                lines += [f"      ⚠ {note}" for note in agent.notes]
        else:
            lines.append("还没有自定义子智能体。")
        if catalog["skipped"]:
            lines += ["", "没加载的文件："] + [f"  ⚠ {_short(item)}" for item in catalog["skipped"]]
        lines += ["", ("在 .pi/agents/<名字>.md（项目）或 ~/.pi/agent/agents/（全局）写一个：头部 name / "
                       "description（必填，写清什么时候用）/ tools / model，正文是角色说明；"
                       ".claude/agents/ 里 Claude Code 的也能直接用。主智能体按 description 挑，"
                       "也可以直接说「用 reviewer 子智能体看看」")]
        return "\n".join(lines)

    pi.register_command("agents", "查看可用的子智能体（含自定义的）", agents_command)


def _short(text: str) -> str:
    from pathlib import Path

    home = str(Path.home())
    return text.replace(home, "~", 1)
