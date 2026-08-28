"""
### 引导队列与后续消息队列
引导消息：在本轮全部工具调用完成后立即下发；
后续消息：仅当智能体按原有逻辑即将停止运行时，才进行下发。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .types import AgentMessage, QueueMode

@dataclass
class PendingMessageQueue:
    mode: QueueMode = "all"
    items: list[AgentMessage] = field(default_factory=list)

    def push(self, message: AgentMessage) -> None:
        self.items.append(message)

    def take(self) -> list[AgentMessage]:
        if not self.items:
            return []
        if self.mode == "one-at-a-time":
            return [self.items.pop(0)]

        taken, self.items = self.items, []
        return taken

    def clear(self) -> None:
        self.items.clear()

    def __len__(self) -> int:
        return len(self.items)

    def __bool__(self) -> bool:
        return bool(self.items)
