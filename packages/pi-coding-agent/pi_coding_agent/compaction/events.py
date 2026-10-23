"""
压缩事件。由 AgentSession 发给订阅者（终端、网页、扩展），让压缩过程可见：

    compaction_start → compaction_progress* → compaction_end

pi-agent 不认识它们；它们和 agent 事件走同一条订阅通道，按 ``type`` 区分。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass
class CompactionStartEvent:
    reason: str                 # threshold | manual | overflow
    tokens_before: int
    context_window: int
    type: Literal["compaction_start"] = "compaction_start"


@dataclass
class CompactionProgressEvent:
    phase: str                  # summarizing | retrying
    chunk: int = 1
    chunks: int = 1
    delta: str = ""
    type: Literal["compaction_progress"] = "compaction_progress"


@dataclass
class CompactionEndEvent:
    reason: str
    tokens_before: int
    tokens_after: int | None = None
    summary: str = ""
    state: dict[str, Any] | None = None
    degraded: bool = False
    restored: list[str] = field(default_factory=list)
    error: str | None = None
    cancelled: bool = False
    skipped: bool = False       # 没有可压缩的内容
    type: Literal["compaction_end"] = "compaction_end"

    @property
    def ok(self) -> bool:
        return not (self.error or self.cancelled or self.skipped)


@dataclass
class ContextPrunedEvent:
    """L1：旧工具输出被换成占位。不调模型、毫秒级，只发这一个事件。"""
    count: int
    tokens_before: int
    tokens_after: int
    type: Literal["context_pruned"] = "context_pruned"


CompactionEvent = (
    CompactionStartEvent | CompactionProgressEvent | CompactionEndEvent | ContextPrunedEvent
)
