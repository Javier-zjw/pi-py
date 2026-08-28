"""
原子层的核心、与具体服务商无关的基础类型。
本文件内所有类型都是标准数据载体，支持无损 JSON 序列化与反序列化。
这一层设计的核心目标：由某一个服务商生成的「上下文（Context）对象」，能够持久化存储，并且可以在另一个服务商上重新加载、复现执行流程。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Literal, Union

Api = Literal["anthropic-messages", "openai-completions"]
StopReason = Literal["pending", "stop", "length", "toolUse", "error", "aborted"]
ThinkingLevel = Literal["off", "minimal", "low", "medium", "high", "xhigh", "max"]

THINKING_LEVELS: tuple[str, ...] = ("off", "minimal", "low", "medium", "high", "xhigh", "max")


@dataclass
class TextContent:
    text: str
    type: Literal["text"] = "text"


@dataclass
class ImageContent:
    data: str  # base64,不能是url链接
    mime_type: str
    type: Literal["image"] = "image"


@dataclass
class ThinkingContent:
    thinking: str
    signature: str | None = None
    type: Literal["thinking"] = "thinking"


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    type: Literal["toolCall"] = "toolCall"


Content = Union[TextContent, ImageContent, ThinkingContent, ToolCall]
UserContent = Union[TextContent, ImageContent]
AssistantContent = Union[TextContent, ThinkingContent, ToolCall]


@dataclass
class Cost:
    input: float = 0.0
    output: float = 0.0
    cache_read: float = 0.0
    cache_write: float = 0.0
    total: float = 0.0

    def __add__(self, other: "Cost") -> "Cost":
        return Cost(
            input=self.input + other.input,
            output=self.output + other.output,
            cache_read=self.cache_read + other.cache_read,
            cache_write=self.cache_write + other.cache_write,
            total=self.total + other.total
        )


@dataclass
class Usage:
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    total_tokens: int = 0
    cost: Cost = field(default_factory=Cost)

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            input=self.input + other.input,
            output=self.output + other.output,
            cache_read=self.cache_read + other.cache_read,
            cache_write=self.cache_write + other.cache_write,
            total_tokens=self.total_tokens + other.total_tokens,
            cost=self.cost + other.cost
        )


def _now() -> float:
    return time.time()


@dataclass
class UserMessage:
    content: str | list[UserContent]
    timestamp: float = field(default_factory=_now)
    role: Literal["user"] = "user"

    def text(self) -> str:
        if isinstance(self.content, str):
            return self.content
        return "".join(c.text for c in self.content if isinstance(c, TextContent))


@dataclass
class AssistantMessage:
    content: list[AssistantContent] = field(default_factory=list)
    api: str = ""
    provider: str = ""
    model: str = ""
    usage: Usage = field(default_factory=Usage)
    stop_reason: StopReason = "pending"
    error_message: str | None = None
    response_id: str | None = None
    timestamp: float = field(default_factory=_now)
    role: Literal["assistant"] = "assistant"

    def text(self) -> str:
        return "".join(c.text for c in self.content if isinstance(c, TextContent))

    def tool_calls(self) -> list[ToolCall]:
        return [c for c in self.content if isinstance(c, ToolCall)]


@dataclass
class ToolResultMessage:
    tool_call_id: str
    tool_name: str
    content: list[UserContent] = field(default_factory=list)
    details: Any = None
    usage: Usage | None = None
    is_error: bool = False
    timestamp: float = field(default_factory=_now)
    role: Literal["toolResult"] = "toolResult"

    def text(self) -> str:
        return "".join(c.text for c in self.content if isinstance(c, TextContent))


Message = Union[UserMessage, AssistantMessage, ToolResultMessage]


@dataclass
class Tool:
    """
    可供模型调用的函数,`parameters` 是一个 JSON Schema 对象。
    """
    name: str
    description: str
    parameters: dict[str, Any] = field(
        default_factory=lambda: {"type": "object", "properties": {}}
    )


@dataclass
class Context:
    """
    单次模型调用的完整输入,设计为可序列化。
    """
    system_prompt: str | None = None
    messages: list[Message] = field(default_factory=list)
    tools: list[Tool] = field(default_factory=list)


@dataclass
class ModelCost:
    input: float = 0.0
    output: float = 0.0
    cache_read: float = 0.0
    cache_write: float = 0.0


@dataclass
class Model:
    id: str
    provider: str
    api: Api
    name: str = ""
    cost: ModelCost = field(default_factory=ModelCost)
    context_window: int = 128_000
    max_tokens: int = 8_192
    reasoning: bool = False
    input_modalities: tuple[str, ...] = ("text",)
    base_url: str | None = None
    thinking_level_map: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.provider}/{self.id}"


@dataclass
class StreamOptions:
    """
    服务商原生参数
    """
    max_tokens: int | None = None
    temperature: float | None = None
    api_key: str | None = None
    base_url: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class SimpleStreamOptions(StreamOptions):
    """
    服务商无关的通用选项：`reasoning`（推理 / 思考强度）会由框架根据不同服务商分别映射适配。
    """
    reasoning: ThinkingLevel = "off"
