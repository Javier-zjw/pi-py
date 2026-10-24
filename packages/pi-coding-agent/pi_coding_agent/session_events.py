"""会话层事件：和 agent 事件、压缩事件走同一条 subscribe 通道（按 event.type 区分）。

扩展通过 ``AgentSession.notify(event)`` 发出，界面（pi-tui / 网页）订阅后刷新显示。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass
class ModeChangedEvent:
    """工作模式切换（执行 ⇄ 计划）"""
    mode: str
    previous: str
    type: Literal["mode_changed"] = "mode_changed"


@dataclass
class PlanProposedEvent:
    """计划模式下模型提交了计划，等用户确认。plan 是 submit_plan 的结构化内容"""
    plan: dict[str, Any]
    type: Literal["plan_proposed"] = "plan_proposed"


@dataclass
class RetryEvent:
    """模型调用临时失败（限流、过载、断网），等 delay 秒后重试第 attempt 次（共 max_attempts 次）"""
    attempt: int
    max_attempts: int
    delay: float
    error: str
    type: Literal["retry"] = "retry"


@dataclass
class TodoUpdatedEvent:
    """任务清单变化：update_plan 更新、或批准计划时按任务生成。plan 是 [{step, status}]"""
    plan: list[dict[str, Any]] = field(default_factory=list)
    explanation: str = ""
    type: Literal["todo_updated"] = "todo_updated"
