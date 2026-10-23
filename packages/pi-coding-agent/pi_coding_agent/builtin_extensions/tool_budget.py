"""
工具调用预算：一次请求调用工具太多时，提醒模型收尾回答。

起因：问一句"分析当前代码"，模型一路读了 97 次文件、找了半天 bug，一直不给回答。
这里不强行打断（打断就一个字的回答都拿不到），而是在第 softLimit 次工具调用之后插一条
提醒（steering，在这批工具执行完、下一次调模型前注入），之后每多 every 次再提醒一次。

设置（settings.json）：

    "toolBudget": {"enabled": true, "softLimit": 40, "every": 20}

提醒是 CustomMessage（custom_type="tool_budget"）：会给模型看、会存进会话；
details["notice"] 给界面显示一行说明，而不是当成用户说的话回显。
"""

from __future__ import annotations

from pi_agent import CustomMessage

NAME = "tool_budget"
DESCRIPTION = "一次请求调用工具过多时，提醒模型停止探索、给出回答"

REMINDER = (
    "[系统提醒] 这次请求已经调用了 {count} 次工具。请停止继续探索，"
    "用已经掌握的信息直接回答用户；如果确实还缺关键信息，先简要说明还要查什么、为什么，再继续。"
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
        state["count"] += 1
        session = pi.session
        if session is None or not setting("enabled", True) or state["next"] <= 0:
            return
        if state["count"] < state["next"]:
            return
        state["next"] += max(int(setting("every", 20)), 1)
        count = state["count"]
        session.steer(CustomMessage(
            custom_type="tool_budget",
            content=REMINDER.format(count=count),
            details={"notice": f"已调用 {count} 次工具，提醒模型尽快给出回答", "count": count},
        ))

    pi.on("agent_start", on_agent_start)
    pi.on("tool_execution_end", on_tool_end)
