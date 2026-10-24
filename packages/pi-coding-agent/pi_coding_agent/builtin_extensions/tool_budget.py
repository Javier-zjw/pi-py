"""
工具调用预算 + 目标重锚：长工具链里定期把模型拉回用户的请求。

起因：问一句"分析当前代码"，模型一路读了 97 次文件、从"分析"滑到了"找 bug"，一直不给回答。
工具调用一多，用户的请求早被几十条工具输出淹没在上下文中间，模型越来越只盯着眼前那个文件。
这里不强行打断（打断就一个字的回答都拿不到），而是在工具批次之间插提醒（steering）：

- **重锚**：每 reanchorEvery 次工具调用，把"用户最近的请求 + 当前计划"放到上下文末尾，
  让模型自查方向、判断能不能回答了。轻量、不打扰：界面上不单独显示（ctrl+o 里看得到）。
- **收尾**：到 softLimit 次时明确要求停止探索、给出回答，之后每 every 次再提醒；界面显示一行说明。

update_plan 本身不算工具调用（它就是在对方向）。

设置（settings.json）：

    "toolBudget": {"enabled": true, "softLimit": 40, "every": 20, "reanchorEvery": 15}

提醒是 CustomMessage（custom_type="tool_budget"）：会给模型看、会存进会话。
details["notice"] 是界面上的一行说明；details["quiet"] 为真时界面不单独显示。
"""

from __future__ import annotations

from pi_agent import CustomMessage

NAME = "tool_budget"
DESCRIPTION = "长工具链里定期把模型拉回用户的请求；调用过多时提醒停止探索、给出回答"

REQUEST_LIMIT = 600     # 引用用户请求的最大字数

REANCHOR = "[系统提醒] 已调用 {count} 次工具，对照一下方向。"
WRAP_UP = (
    "[系统提醒] 这次请求已经调用了 {count} 次工具。请停止继续探索，"
    "用已经掌握的信息直接回答用户；如果确实还缺关键信息，先简要说明还要查什么、为什么，再继续。"
)
CHECK = (
    "问自己：接下来要做的是否还在为这个请求服务？已有的信息够不够回答？"
    "够了就直接回答；不在请求范围内的发现只在回答里提一句，不要展开。"
)


def activate(pi) -> None:
    state = {"count": 0, "next": 0}

    def setting(key: str, default):
        session = pi.session
        return session.settings.get(f"toolBudget.{key}", default) if session is not None else default

    def on_agent_start(event) -> None:
        state["count"] = 0
        state["next"] = int(setting("softLimit", 40))

    def on_tool_end(event) -> None:
        if getattr(event, "tool_name", "") == "update_plan":
            return
        state["count"] += 1
        session = pi.session
        if session is None or not setting("enabled", True):
            return
        count = state["count"]
        if 0 < state["next"] <= count:
            state["next"] += max(int(setting("every", 20)), 1)
            session.steer(reminder(session, WRAP_UP.format(count=count), count, {
                "notice": f"已调用 {count} 次工具，提醒模型尽快给出回答"}))
            return
        every = int(setting("reanchorEvery", 15))
        if every > 0 and count % every == 0:
            session.steer(reminder(session, REANCHOR.format(count=count), count, {
                "notice": f"第 {count} 次工具调用：提醒模型对照用户的请求", "quiet": True}))

    pi.on("agent_start", on_agent_start)
    pi.on("tool_execution_end", on_tool_end)


def reminder(session, headline: str, count: int, details: dict) -> CustomMessage:
    return CustomMessage(
        custom_type="tool_budget",
        content=anchor_text(session, headline),
        details={**details, "count": count},
    )


def anchor_text(session, headline: str) -> str:
    """提醒正文：标题 + 用户最近的请求 + 当前计划 + 自查问题"""
    lines = [headline]
    request = latest_request(session)
    if request:
        lines += ["", f"用户的请求：「{request}」"]
    plan = current_plan(session)
    if plan:
        lines += ["", "当前计划：", plan]
    else:
        lines += ["", "（多步任务可以先用 update_plan 列个计划，按计划推进。）"]
    lines += ["", CHECK]
    return "\n".join(lines)


def latest_request(session) -> str:
    """最近一条用户消息（插话 / 追问也算：它们是最新的要求）"""
    for message in reversed(session.messages):
        if getattr(message, "role", "") == "user":
            text = " ".join(message.text().split())
            if len(text) > REQUEST_LIMIT:
                text = text[:REQUEST_LIMIT] + "…"
            return text
    return ""


def current_plan(session) -> str:
    from pi_coding_agent.builtin_extensions.todo import MARKS, latest_todo

    record = latest_todo(session)
    if not record or all(s["status"] == "completed" for s in record["plan"]):
        return ""
    return "\n".join(f"- {MARKS.get(s['status'], '[ ]')} {s['step']}" for s in record["plan"])
