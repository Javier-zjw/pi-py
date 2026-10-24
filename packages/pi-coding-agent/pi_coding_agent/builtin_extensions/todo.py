"""
任务计划（同 Codex 的 update_plan）：模型把多步任务拆成几步，边做边标状态。

为什么需要：长工具链里模型最容易丢的是"我现在在做哪一步、还剩什么"。没有一个显式的状态，
读着读着就从"分析代码"滑到了"找 bug"。有了计划：

1. 模型每做完一步要回来更新它——等于每隔几步被迫对一次方向；
2. 计划存进会话记录（custom entry "todo"），压缩之后通过 compaction_context 补回给模型，
   resume 之后也还在；
3. 界面把它显示成一张勾选单，用户看得到进度。

和计划模式（plan_mode）不是一回事：计划模式是"只读调研、出方案、等确认"，
这里是执行过程中的进度单，任何模式下都能用。
"""

from __future__ import annotations

from pi_agent import AgentToolResult, ToolContext

NAME = "todo"
DESCRIPTION = "任务计划：多步任务拆成步骤并跟踪进度（update_plan 工具）"

ENTRY_TYPE = "todo"
STATUSES = ("pending", "in_progress", "completed")
MARKS = {"completed": "[x]", "in_progress": "[>]", "pending": "[ ]"}

TOOL_DESCRIPTION = (
    "Create or update the task plan: a short list of steps with a status each "
    "(pending / in_progress / completed). Use it for non-trivial work that takes several steps; "
    "skip it for simple questions you can answer directly. Keep 3-7 concrete, verifiable steps, "
    "exactly one in_progress while working. After finishing a step, mark it completed before "
    "moving on. If the plan changes, send the full updated plan with an explanation. "
    "The plan is shown to the user, so do not repeat it in your reply."
)

PARAMETERS = {
    "type": "object",
    "properties": {
        "explanation": {"type": "string", "description": "Why the plan changed (optional)"},
        "plan": {
            "type": "array",
            "description": "The full plan, in order",
            "items": {
                "type": "object",
                "properties": {
                    "step": {"type": "string"},
                    "status": {"type": "string", "enum": list(STATUSES)},
                },
                "required": ["step", "status"],
            },
        },
    },
    "required": ["plan"],
}


def activate(pi) -> None:
    async def update_plan(args: dict, ctx: ToolContext) -> AgentToolResult:
        steps = [{"step": str(item["step"]).strip(), "status": item["status"]}
                 for item in args["plan"] if str(item.get("step", "")).strip()]
        if not steps:
            return AgentToolResult.text("plan is empty: give at least one step", is_error=True)
        if sum(s["status"] == "in_progress" for s in steps) > 1:
            return AgentToolResult.text(
                "only one step can be in_progress at a time; mark finished steps completed",
                is_error=True)
        record = {"plan": steps}
        if (args.get("explanation") or "").strip():
            record["explanation"] = args["explanation"].strip()
        session = pi.session
        if session is not None:
            save_todo(session, record)
        done = sum(s["status"] == "completed" for s in steps)
        current = next((s["step"] for s in steps if s["status"] == "in_progress"), None)
        text = f"Plan updated ({done}/{len(steps)} done)."
        if current:
            text += f" Current step: {current}"
        elif done == len(steps):
            text += " All steps completed."
        return AgentToolResult.text(text, details=record)

    pi.register_tool("update_plan", TOOL_DESCRIPTION, PARAMETERS, update_plan, label="更新计划")

    async def todo_context(session) -> str | None:
        """压缩后补回还没做完的计划：模型接着按它往下走，不会忘了自己在第几步"""
        record = latest_todo(session)
        if not record or all(s["status"] == "completed" for s in record["plan"]):
            return None
        return render_todo_context(record)

    pi.compaction_context(todo_context)


def save_todo(session, record: dict) -> None:
    """存进会话记录，并通知界面刷新勾选单（批准计划时生成清单也走这里）"""
    from pi_coding_agent.session_events import TodoUpdatedEvent

    session.session.append_custom_entry(ENTRY_TYPE, record)
    session.notify(TodoUpdatedEvent(plan=record["plan"], explanation=record.get("explanation", "")))


def latest_todo(session) -> dict | None:
    """当前分支上最近一次 update_plan 的结果"""
    for entry in reversed(session.session.get_branch()):
        if getattr(entry, "type", "") == "custom" and getattr(entry, "custom_type", "") == ENTRY_TYPE:
            data = entry.data
            return data if isinstance(data, dict) and data.get("plan") else None
    return None


def render_todo_context(record: dict) -> str:
    lines = ["## 当前任务计划",
             "压缩之前你在按这份计划工作。继续推进 [>] 那一步；做完一步就用 update_plan 更新状态。", ""]
    lines += [f"- {MARKS.get(s['status'], '[ ]')} {s['step']}" for s in record["plan"]]
    return "\n".join(lines)
