"""
计划模式（plan → execute）：先只读调研、出一份带任务清单的计划，用户批准后切到执行模式，
按清单逐项完成，界面实时打勾。参照 Claude Code（shift+tab 切模式、提交计划后选择是否执行）
和 Codex（Plan Mode 协作模式：先探索再提问，计划要"决策完备"，批准后切回 Default 执行）。

为什么稳：

1. **工具层面强制只读** —— 不是靠提示词请求模型"别改文件"，而是计划模式下只挂 read / grep /
   find / ls 和 submit_plan。模型想改也调不到。update_plan（执行进度单）在计划模式下也不给，
   避免和"计划"混淆（Codex 同样禁止）。
2. **提交即停** —— submit_plan 的结果带 terminate，本轮立刻结束、不再调模型，
   等用户决定：执行 / 继续完善 / 放弃。
3. **计划落进会话记录** —— custom entry "plan"（status: proposed → approved），
   刷新页面、换终端、resume 都还在；压缩后经 compaction_context 补回。
4. **批准即生成进度单** —— 计划的任务直接变成 update_plan 的清单（第一项 in_progress），
   执行时模型每完成一项就打勾，界面实时显示；目标重锚提醒里也带着这张清单。

模式的注册和生效走 ExtensionAPI 的 register_mode，核心层不认识"计划"这个概念。
界面（pi-tui / 网页）调 approve_plan() 或 /build 来执行计划。
"""

from __future__ import annotations

from pi_agent import AgentTool, AgentToolResult, ToolContext

NAME = "plan_mode"
DESCRIPTION = "计划模式：只读调研、提交带任务清单的计划，批准后切到执行模式按清单逐项完成"

ENTRY_TYPE = "plan"
READ_ONLY = ("read", "grep", "find", "ls")

# 结构参照 Codex 的 Plan Mode 协作模式提示（collaboration-mode-templates/templates/plan.md）
PLAN_PROMPT = """

<plan_mode>
You are in PLAN MODE. Your job is to produce a plan, not to do the work.

Mode rules:
- You stay in plan mode until the user switches modes. If the user asks you to implement \\
something while in plan mode, treat it as a request to plan the implementation.
- Only read-only tools are available (read, grep, find, ls). Do not try to edit files or run \\
commands.

Work in three phases:
1. Explore first. Ground yourself in the code: read the relevant files, entry points and \\
configs. Answer every question that the repository can answer by looking, not by asking.
2. Clarify only what you cannot discover. If a decision would materially change the plan \\
(scope, behavior, trade-offs) and the code cannot settle it, ask the user a few concise \\
questions in your reply, each with options and a recommended default, then stop without \\
submitting a plan.
3. Submit. When the plan is decision complete (an implementer would not need to make any \\
decisions), call `submit_plan` exactly once:
   - title: a short name for the change
   - summary: Markdown, concise: approach, key changes by area, how it will be tested, \\
assumptions
   - steps: 3-10 ordered tasks. Each is one concrete, verifiable unit of work ("Add X to Y", \\
"Run the Z tests"); they become the execution checklist the implementer ticks off
   - files: the main files to be changed or created
   - questions: remaining open questions or assumptions (may be empty)
After calling `submit_plan`, stop. The user will approve it, ask for changes, or drop it. \\
If they ask for changes, submit a complete replacement plan.

Write in the language the user writes in.
</plan_mode>"""

SUBMIT_DESCRIPTION = (
    "Submit the finished implementation plan for the user's approval. Call it once the plan is "
    "decision complete; the turn ends right after, waiting for the user. `steps` becomes the "
    "execution checklist, so make each step concrete and verifiable."
)

SUBMIT_PARAMETERS = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "Short name for the change"},
        "summary": {"type": "string",
                    "description": "Markdown: approach, key changes, test plan, assumptions"},
        "steps": {"type": "array", "items": {"type": "string"},
                  "description": "3-10 ordered, concrete, verifiable tasks"},
        "files": {"type": "array", "items": {"type": "string"},
                  "description": "Main files to change or create"},
        "questions": {"type": "array", "items": {"type": "string"},
                      "description": "Open questions or assumptions; empty if none"},
    },
    "required": ["summary", "steps"],
}


def activate(pi) -> None:

    async def apply_plan_mode(session) -> None:
        """每轮开始前生效。session 已经把系统提示和工具重置回基线了"""
        from pi_coding_agent.tools import create_tools

        session.agent.state.system_prompt = (session.agent.state.system_prompt or "") + PLAN_PROMPT
        excluded = set(session.settings.get("excludeTools") or [])
        tools = create_tools([name for name in READ_ONLY if name not in excluded], session.cwd)
        tools.append(submit_tool)
        session.agent.set_tools(tools)

    async def submit_plan(args: dict, ctx: ToolContext) -> AgentToolResult:
        steps = [str(s).strip() for s in args.get("steps") or [] if str(s).strip()]
        if not steps:
            return AgentToolResult.text("steps is empty: give the ordered tasks of the plan",
                                        is_error=True)
        summary = str(args.get("summary") or "").strip()
        plan = {
            "title": str(args.get("title") or "").strip() or _first_line(summary) or "实施计划",
            "summary": summary,
            "steps": steps,
            "files": [str(f) for f in args.get("files") or []],
            "questions": [str(q) for q in args.get("questions") or []],
            "status": "proposed",
        }
        pi.state["plan"] = plan
        session = pi.session
        if session is not None:
            from pi_coding_agent.session_events import PlanProposedEvent

            # 存进会话记录：刷新页面、换终端、下次 resume 都还在
            session.session.append_custom_entry(ENTRY_TYPE, plan)
            session.notify(PlanProposedEvent(plan=plan))
            pi.events.emit("plan-submitted", plan)
        return AgentToolResult.text(
            f"Plan submitted ({len(steps)} steps). Waiting for the user to approve, revise or drop "
            "it. Do not continue.",
            details={"plan": plan},
            terminate=True,     # 提交即停：本轮结束，不再调模型
        )

    # 不走 pi.register_tool：那会把它加进每个会话的工具基线，执行模式下模型也能调。
    # 它只属于计划模式，由 apply_plan_mode 按需挂上
    submit_tool = AgentTool(
        name="submit_plan",
        label="提交计划",
        description=SUBMIT_DESCRIPTION,
        parameters=SUBMIT_PARAMETERS,
        execute=submit_plan,
    )

    pi.register_mode(
        id="plan",
        label="计划模式",
        description="只读调研，提交带任务清单的计划，批准后再执行",
        apply=apply_plan_mode,
        badge="计划",
    )
    pi.register_mode(id="chat", label="执行模式", description="全部工具可用，按计划逐项执行", badge="")

    async def cmd_plan(rest: str) -> str | None:
        session = pi.session
        if session is None:
            return "还没有活跃会话"
        session.switch_mode("plan")
        if rest.strip():
            await session.prompt(rest.strip())
            return None
        return "已进入计划模式：只读调研，先出带任务清单的计划。描述你的需求开始。"

    async def cmd_build(rest: str) -> str | None:
        """批准最近一份计划：切到执行模式、生成进度单、开始执行"""
        session = pi.session
        if session is None:
            return "还没有活跃会话"
        try:
            task = approve_plan(session, rest.strip())
        except LookupError as exc:
            return str(exc)
        await session.prompt(task)
        return None

    async def cmd_show_plan(rest: str) -> str:
        session = pi.session
        plan = (latest_plan(session) if session is not None else None) or pi.state.get("plan")
        return plan_markdown(plan) if plan else "当前没有计划"

    async def plan_context(session) -> str | None:
        """上下文压缩后把当前计划补回给模型，否则执行到一半压缩就忘了计划"""
        plan = latest_plan(session) or pi.state.get("plan")
        if not plan:
            return None
        if session.mode == "plan":
            note = "当前仍在计划模式：只读调研；计划提交后等用户确认。"
        elif plan.get("status") == "approved":
            note = "这是用户已批准、正在执行的计划，执行时以它为准（进度见任务计划）。"
        else:
            note = "这是最近一次提交的计划，执行时以它为准。"
        return render_plan_context(plan, note)

    pi.compaction_context(plan_context)

    pi.register_command("plan", "进入计划模式（可直接带上需求）", cmd_plan, usage="/plan [需求]")
    pi.register_command("build", "批准最近的计划并开始执行", cmd_build, usage="/build [补充说明]")
    pi.register_command("show_plan", "查看当前计划", cmd_show_plan)


def approve_plan(session, extra: str = "") -> str:
    """批准最近一份计划：标记 approved、切到执行模式、把任务生成进度单（第一项 in_progress）。
    返回交给模型执行的任务说明，由调用方 prompt 出去（界面可以只回显一行标题）。
    没有计划时抛 LookupError"""
    from pi_coding_agent.builtin_extensions.todo import save_todo

    plan = latest_plan(session)
    if not plan:
        raise LookupError("还没有可执行的计划。先进入计划模式让模型提交一份。")
    approved = {**plan, "status": "approved"}
    session.session.append_custom_entry(ENTRY_TYPE, approved)
    session.switch_mode("chat")
    steps = [{"step": step, "status": "in_progress" if i == 0 else "pending"}
             for i, step in enumerate(plan["steps"])]
    save_todo(session, {"plan": steps, "source": "plan"})
    return render_plan_as_task(approved, extra)


def latest_plan(session) -> dict | None:
    """当前分支上最近一份计划（提交或批准的）"""
    for entry in reversed(session.session.get_branch()):
        if getattr(entry, "type", "") == "custom" and getattr(entry, "custom_type", "") == ENTRY_TYPE:
            return entry.data if isinstance(entry.data, dict) and entry.data.get("steps") else None
    return None


def _first_line(text: str) -> str:
    for line in text.splitlines():
        line = line.strip().lstrip("#").strip()
        if line:
            return line[:60]
    return ""


def plan_markdown(plan: dict) -> str:
    """给人看的计划（界面渲染成 Markdown）"""
    lines = []
    if plan.get("summary"):
        lines += [plan["summary"].strip(), ""]
    lines.append("**任务**")
    lines += [f"{i}. {s}" for i, s in enumerate(plan.get("steps") or [], 1)]
    if plan.get("files"):
        lines += ["", "**涉及文件**：" + "、".join(f"`{f}`" for f in plan["files"])]
    if plan.get("questions"):
        lines += ["", "**待确认**", *[f"- {q}" for q in plan["questions"]]]
    return "\n".join(lines)


def render_plan_context(plan: dict, note: str) -> str:
    lines = ["## 当前计划", note, "", f"目标：{plan.get('title') or plan.get('summary', '')}", ""]
    if plan.get("summary") and plan.get("title"):
        lines += [plan["summary"], ""]
    lines.append("步骤：")
    lines += [f"{i}. {s}" for i, s in enumerate(plan.get("steps") or [], 1)]
    if plan.get("files"):
        lines += ["", "涉及文件：" + "、".join(plan["files"])]
    if plan.get("questions"):
        lines += ["", "待确认：", *[f"- {q}" for q in plan["questions"]]]
    return "\n".join(lines)


def render_plan_as_task(plan: dict, extra: str = "") -> str:
    """批准后交给模型执行的任务说明"""
    lines = [f"执行已批准的计划：{plan.get('title') or ''}".rstrip("："), ""]
    if plan.get("summary"):
        lines += [plan["summary"], ""]
    lines.append("任务清单（已写进 update_plan，第 1 项已设为 in_progress）：")
    lines += [f"{i}. {s}" for i, s in enumerate(plan.get("steps") or [], 1)]
    if plan.get("files"):
        lines += ["", "涉及文件：" + "、".join(plan["files"])]
    if extra:
        lines += ["", f"用户补充：{extra}"]
    lines += [
        "",
        "执行要求：",
        "- 按清单顺序逐项完成。做完一项立刻用 update_plan 把它标为 completed，并把下一项设为 in_progress。",
        "- 不做清单以外的改动。遇到计划没预料到的情况，先说明原因，再用 update_plan 调整清单（写 explanation）。",
        "- 全部完成后，简要汇报改了什么、怎么验证的。",
    ]
    return "\n".join(lines)
