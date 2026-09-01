"""
计划模式：先出方案，确认后再动手。

真正落地的三件事，缺一不可：

1. **工具层面强制只读** —— 不是靠提示词请求模型"别改文件"，而是把
   write/edit/bash 从工具集里拿掉。模型想改也调不到。
2. **计划落进会话记录** —— 存成 custom entry，刷新页面、换终端都还在。
3. **一键执行** —— 切回聊天模式并把计划作为任务发出去，带全部工具。

模式的注册和生效走的是 ExtensionAPI 的 register_mode + before_agent_start，
核心层不认识"计划"这个概念，所以加别的模式也不用动下层。
"""

from __future__ import annotations

import json

from pi_agent import AgentToolResult, ToolContext
from requests import session

NAME = "plan_mode"
DESCRIPTION = "计划模式：只读分析并产出可执行计划，确认后再切换执行"

PLAN_PROMPT = """

<plan_mode>
当前处于**计划模式**。你现在只有只读工具，无法修改任何文件或执行命令。

你的任务是先把方案想清楚：
1. 用只读工具充分调研（read / grep / find / ls）
2. 想清楚要改哪些文件、每处改什么、为什么
3. 调用 `submit_plan` 提交计划

计划要具体到文件和改动内容，不要写"重构一下"这种没法执行的描述。
不确定的地方明确列成待确认问题，不要自己假设。
提交计划之后就停下，等用户确认。
</plan_mode>"""

READ_ONLY = ("read", "grep", "find", "ls")

def activate(pi) -> None:

    async def apply_plan_mode(session) -> None:
        """每轮开始前生效。session 已经把系统提示和工具重置回基线了"""
        from pi_coding_agent.tools import create_tools

        session.agent.state.system_prompt = (
            session.agent.state.system_prompt or ""
        ) + PLAN_PROMPT

        allowed = {t.name for t in session._base_tools} & set(READ_ONLY)
        tools = create_tools(sorted(allowed) or list(READ_ONLY), session.cwd)
        tools.append(submit_tool)
        session.agent.set_tools(tools)

    async def submit_plan(args: dict, ctx: ToolContext) -> AgentToolResult:
        session = pi.session
        plan = {
            "summary": args["summary"],
            "steps": args.get("steps") or [],
            "files": args.get("files") or [],
            "questions": args.get("questions") or []
        }
        pi.state["plan"] = plan
        if session is not None:
            # 存进会话记录：刷新页面、换终端、下次 resume 都还在
            session.session.append_custom_entry("plan", plan)
            pi.events.emit("plan-submitted", plan)

        lines = [f"计划已提交：{plan['summary']}", ""]
        for i, step in enumerate(plan["steps"], 1):
            lines.append(f"{i}. {step}")
        if plan["files"]:
            lines.append("")
            lines.append("涉及文件：" + "、".join(plan["files"]))
        if plan["questions"]:
            lines.append("")
            lines.append("待确认：")
            lines += [f"- {q}" for q in plan["questions"]]

        lines.append("")
        lines.append("等待用户确认后执行。用 /build 开始执行，或继续讨论调整计划。")
        return AgentToolResult.text("\n".join(lines), details={"plan": plan})

    submit_tool = pi.register_tool(
        name="submit_plan",
        label="提交计划",
        description=(
            "提交一份可执行的实施计划。调研清楚之后调用本工具，"
            "然后停下等待用户确认，不要继续往下做。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "一句话说明这个计划要做什么"},
                "steps": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "按顺序的执行步骤，每步具体到文件和改动内容",
                },
                "files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "会被修改或新建的文件路径"
                },
                "questions": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "需要用户确认的问题，没有就留空"
                }
            },
            "required": ["summary", "steps"]
        },
        execute=submit_plan
    )

    pi.register_mode(
        id="plan",
        label="计划模式",
        description="只读调研并产出计划，确认后再执行",
        apply=apply_plan_mode,
        badge="计划"
    )

    pi.register_mode(id="chat", label="聊天模式", description="全部工具可用", badge="")

    async def cmd_plan(rest: str) -> str | None:
        session = pi.session
        if session is None:
            return "还没有活跃会话"
        await session.set_mode("plan")
        if rest.strip():
            await session.prompt(rest.strip())
            return None
        return "已进入计划模式：只读工具，先出方案。描述你的需求开始调研。"

    async def cmd_build(rest: str) -> str | None:
        """把已提交的计划变成实际任务"""
        session = pi.session
        if session is None:
            return "还没有活跃会话"
        plan = pi.state.get("plan")
        if not plan:
            return "还没有计划可执行。先 /plan 让模型产出一份。"
        await session.set_mode("chat")
        task = render_plan_as_task(plan, extra=rest.strip())
        await session.prompt(task)
        return None

    async def cmd_show_plan(rest: str) -> str:
        plan = pi.state.get("plan")
        if not plan:
            return "当前没有计划"
        return json.dumps(plan, ensure_ascii=False, indent=2)

    pi.register_command("plan", "进入计划模式（可直接带上需求）", cmd_plan, usage="/plan [需求]")
    pi.register_command("build", "执行已确认的计划", cmd_build, usage="/build [补充说明]")
    pi.register_command("show_plan", "查看当前计划", cmd_show_plan)

def render_plan_as_task(plan: dict, extra: str = "") -> str:
    """把结构化计划渲染成给模型的任务描述。"""
    lines = ["按下面这份已确认的计划执行。", "", f"目标：{plan['summary']}", "", "步骤："]
    lines += [f"{i}. {s}" for i, s in enumerate(plan.get("steps") or [], 1)]
    if plan.get("files"):
        lines += ["", "涉及文件：" + "、".join(plan["files"])]
    if extra:
        lines += ["", f"用户补充：{extra}"]
    lines += ["", "逐步执行，每改完一处简短说明。遇到计划里没预料到的情况就停下来问。"]
    return "\n".join(lines)