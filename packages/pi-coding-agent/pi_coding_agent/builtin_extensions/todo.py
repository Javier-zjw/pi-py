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

清单来自用户批准的计划（source="plan"）时多一层校验——那是用户点头同意的任务表，模型不能
悄悄改：已批准的任务不能删掉或改写，不能在前面还没开始时去做后面的。确需调整就写 explanation，
校验放行，界面显示调整原因。每项的完成标准（done_when）按标题从上一份清单带过来。
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
        explanation = (args.get("explanation") or "").strip()
        record = {"plan": steps}
        if explanation:
            record["explanation"] = explanation
        session = pi.session
        previous = latest_todo(session) if session is not None else None
        if previous and previous.get("source") == "plan":
            problem = check_approved(previous["plan"], steps, explanation)
            if problem:
                return AgentToolResult.text(problem, is_error=True)
            carry_over(previous["plan"], steps)
            record["source"] = "plan"
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


def check_approved(approved: list[dict], steps: list[dict], explanation: str) -> str | None:
    """批准的清单不许悄悄改：返回给模型的错误说明，没问题返回 None。写了 explanation 就放行"""
    if explanation:
        return None
    titles = {s["step"] for s in steps}
    missing = [s["step"] for s in approved if s["step"] not in titles]
    if missing:
        return ("This checklist comes from the plan the user approved. Keep every approved step "
                "with its exact text; missing: " + "; ".join(missing) + ". If the plan really has "
                "to change (split, skip or add steps), send it again with an explanation.")
    for i, step in enumerate(steps):
        if step["status"] == "pending":
            continue
        skipped = [s["step"] for s in steps[:i] if s["status"] == "pending"]
        if skipped:
            return (f"Work through the approved steps in order: \"{step['step']}\" is "
                    f"{step['status']} while earlier steps are still pending: "
                    + "; ".join(skipped) + ". Finish them first, or send the plan again with an "
                    "explanation of why the order changes.")
    return None


def carry_over(previous: list[dict], steps: list[dict]) -> None:
    """update_plan 只传标题和状态：完成标准按标题从上一份清单带过来"""
    done_when = {s["step"]: s["done_when"] for s in previous if s.get("done_when")}
    for step in steps:
        if step["step"] in done_when:
            step["done_when"] = done_when[step["step"]]


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
    for step in record["plan"]:
        lines.append(f"- {MARKS.get(step['status'], '[ ]')} {step['step']}")
        if step.get("done_when") and step["status"] != "completed":
            lines.append(f"  完成标准：{step['done_when']}")
    return "\n".join(lines)
