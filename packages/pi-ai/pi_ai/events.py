"""
所有模型服务商输出统一格式的流式事件。
每个服务商实现的 `stream()` 方法是异步生成器。
`partial` 字段携带当前已经接收完成的部分 AssistantMessage，使用者可以直接增量渲染，不需要自己拼接消息状态。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, AsyncIterable, Literal, Union

from .types import AssistantMessage, StopReason, ToolCall


@dataclass
class StartEvent:
    partial: AssistantMessage
    type: Literal["start"] = "start"


@dataclass
class TextStartEvent:
    content_index: int
    partial: AssistantMessage
    type: Literal["text_start"] = "text_start"


@dataclass
class TextDeltaEvent:
    content_index: int
    delta: str
    partial: AssistantMessage
    type: Literal["text_delta"] = "text_delta"


@dataclass
class TextEndEvent:
    content_index: int
    text: str
    partial: AssistantMessage
    type: Literal["text_end"] = "text_end"


@dataclass
class ThinkingStartEvent:
    content_index: int
    partial: AssistantMessage
    type: Literal["thinking_start"] = "thinking_start"


@dataclass
class ThinkingDeltaEvent:
    content_index: int
    delta: str
    partial: AssistantMessage
    type: Literal["thinking_delta"] = "thinking_delta"


@dataclass
class ThinkingEndEvent:
    content_index: int
    thinking: str
    partial: AssistantMessage
    type: Literal["thinking_end"] = "thinking_end"


@dataclass
class ToolCallStartEvent:
    content_index: int
    tool_call_id: str
    tool_name: str
    partial: AssistantMessage
    type: Literal["toolcall_start"] = "toolcall_start"


@dataclass
class ToolCallDeltaEvent:
    content_index: int
    tool_call_id: str
    delta: str
    "服务商下发的原始JSON片段"
    arguments: dict[str, Any] = field(default_factory=dict)
    "解析当前已接收的分片数据"
    partial: AssistantMessage | None = None
    type: Literal["toolcall_delta"] = "toolcall_delta"


@dataclass
class ToolCallEndEvent:
    content_index: int
    tool_call: ToolCall
    partial: AssistantMessage
    type: Literal["toolcall_end"] = "toolcall_end"


@dataclass
class DoneEvent:
    reason: StopReason
    message: AssistantMessage
    type: Literal["done"] = "done"


@dataclass
class ErrorEvent:
    error: str
    message: AssistantMessage
    type: Literal["error"] = "error"


AssistantMessageEvent = Union[
    StartEvent,
    TextStartEvent,
    TextDeltaEvent,
    TextEndEvent,
    ThinkingStartEvent,
    ThinkingDeltaEvent,
    ThinkingEndEvent,
    ToolCallStartEvent,
    ToolCallDeltaEvent,
    ToolCallEndEvent,
    DoneEvent,
    ErrorEvent
]

AssistantMessageEventStream = AsyncIterable[AssistantMessageEvent]
