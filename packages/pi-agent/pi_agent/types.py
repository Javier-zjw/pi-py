"""
**分子层类型**
保障分层架构正常运作的唯一准则：本模块**只允许从 pi_ai 导入，不引入其他任何依赖**。
应用需要注入的所有能力（LLM 流式接口、工具实现、上下文转换器），都以协议（Protocol）或可调用对象的形式传入；以此确保智能体不会向上层主动寻址依赖。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable, Literal, Protocol, Union

from pi_ai import (
    AssistantMessage,
    AssistantMessageEvent,
    Context,
    ImageContent,
    Message,
    Model,
    SimpleStreamOptions,
    TextContent,
    ThinkingLevel,
    ToolCall,
    ToolResultMessage,
    Usage,
    UserMessage
)

ToolExecutionMode = Literal["parallel", "sequential"]
QueueMode = Literal["all", "one-at-a-time"]


@dataclass
class CustomMessage:
    """
    **应用自定义消息**
    这是代码智能体（coding-agent）层用于承载各类拓展内容的扩展点，例如上下文压缩摘要、Shell 命令输出等：
    智能体会保存并回放这类消息，但是**只有 `include_in_context` 标识能够决定这条消息是否最终送入 LLM。**
    """

    custom_type: str
    content: str | list[Any] = ""
    display: bool = True
    include_in_context: bool = True
    details: Any = None
    timestamp: float = 0.0
    role: Literal["custom"] = "custom"

    def text(self) -> str:
        if isinstance(self.content, str):
            return self.content
        return "".join(c.text for c in self.content if isinstance(c, TextContent))


AgentMessage = Union[
    UserMessage,
    AssistantMessage,
    ToolResultMessage,
    CustomMessage
]


@dataclass
class AgentToolResult:
    content: list[Union[TextContent, ImageContent]] = field(default_factory=list)
    details: Any = None
    is_error: bool = False
    usage: Usage | None = None

    @staticmethod
    def text(text: str, *, is_error: bool = False, details: Any = None) -> "AgentToolResult":
        return AgentToolResult(content=[TextContent(text=text)], is_error=is_error, details=details)


@dataclass
class ToolContext:
    """工具在被调用时，**被允许访问全部信息集合**"""

    tool_call_id: str
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    on_update: Callable[[AgentToolResult], None] | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def cancelled(self) -> bool:
        return self.cancel_event.is_set()

    def update(self, partial: AgentToolResult) -> None:
        if self.on_update:
            self.on_update(partial)


ToolExecutionFn = Callable[[dict[str, Any], ToolContext], Awaitable[AgentToolResult]]


@dataclass
class AgentTool:
    name: str
    description: str
    parameters: dict[str, Any] = field(
        default_factory=lambda: {"type": "object", "properties": {}}
    )
    execute: ToolExecutionFn | None = None
    label: str | None = None

    async def __call__(self, args: dict[str, Any], ctx: ToolContext) -> AgentToolResult:
        if self.execute is None:
            raise NotImplementedError(f"tool '{self.name}' has no execute function")
        return await self.execute(args, ctx)


@dataclass
class BeforeToolCallResult:
    block: bool = False
    reason: str | None = None
    """此字段存放钩子修改后的工具调用参数，作为可选覆盖值；框架以此实现工具参数前置拦截与改写扩展。"""
    arguments: dict[str, Any] | None = None


@dataclass
class AfterToolCallResult:
    """替换结果,当钩子需要修改工具输出内容时使用。"""
    result: AgentToolResult | None = None
    """请求本轮结束后终止循环"""
    terminate: bool = False

BeforeToolCall = Callable[[ToolCall, "AgentState"], Awaitable[BeforeToolCallResult | None]]
AfterToolCall = Callable[[ToolCall, AgentToolResult, "AgentState"], Awaitable[AfterToolCallResult | None]]

class StreamFn(Protocol):
    """
    **能够将 `Context` 转换为事件流的任意实现。**
    `pi_ai.Models.stream_simple` 符合该契约；单元测试中使用的模拟实现也同样满足。
    智能体层**绝不直接导入任何服务商适配器（provider）**。
    """

    def __call__(
            self,
            model: Model,
            context: Context,
            options: SimpleStreamOptions | None = None
    ) -> AsyncIterator[AssistantMessageEvent]: ...

TransformContext = Callable[[list[AgentMessage]], Awaitable[list[AgentMessage]]]
ConvertToLlm = Callable[[list[AgentMessage]], list[Message]]

@dataclass
class AgentState:
    system_prompt: str | None = None
    model: Model | None = None
    thinking_level: ThinkingLevel = "off"
    tools: list[AgentTool] = field(default_factory=list)
    messages: list[AgentMessage] = field(default_factory=list)
    is_streaming: bool = False
    streaming_message: AssistantMessage | None = None
    pending_tool_calls: set[str] = field(default_factory=set)
    error_message: str | None = None

    def tool_by_name(self, name: str) -> AgentTool | None:
        for t in self.tools:
            if t.name == name:
                return t

        return None

    def usage(self) -> Usage:
        total = Usage()
        for m in self.messages:
            if isinstance(m, AssistantMessage):
                total += m.usage
            elif isinstance(m, ToolResultMessage) and m.usage:
                total += m.usage

        return total

@dataclass
class AgentStartEvent:
    messages: list[AgentMessage]
    type: Literal["agent_start"] = "agent_start"

@dataclass
class TurnStartEvent:
    turn: int
    type: Literal["turn_start"] = "turn_start"

@dataclass
class MessageStartEvent:
    message: AgentMessage
    type: Literal["message_start"] = "message_start"

@dataclass
class MessageUpdateEvent:
    assistant_message_event: AssistantMessageEvent
    type: Literal["message_update"] = "message_update"

@dataclass
class MessageEndEvent:
    message: AgentMessage
    type: Literal["message_end"] = "message_end"

@dataclass
class ToolExecutionStartEvent:
    tool_call_id: str
    tool_name: str
    arguments: dict[str, Any]
    type: Literal["tool_execution_start"] = "tool_execution_start"

@dataclass
class ToolExecutionUpdateEvent:
    tool_call_id: str
    tool_name: str
    partial: AgentToolResult
    type: Literal["tool_execution_update"] = "tool_execution_update"

@dataclass
class ToolExecutionEndEvent:
    tool_call_id: str
    tool_name: str
    result: AgentToolResult
    is_error: bool
    type: Literal["tool_execution_end"] = "tool_execution_end"

@dataclass
class TurnEndEvent:
    turn: int
    message: AssistantMessage
    tool_results: list[ToolResultMessage]
    type: Literal["turn_end"] = "turn_end"

EndReason = Literal["stop", "aborted", "error", "max_turns", "terminated"]

@dataclass
class AgentEndEvent:
    messages: list[AgentMessage]
    reason: Literal["stop", "aborted", "error", "max_turns", "terminated"]
    type: Literal["agent_end"] = "agent_end"

@dataclass
class AgentErrorEvent:
    error: str
    type: Literal["error"] = "error"

AgentEvent = Union[
    AgentStartEvent,
    TurnStartEvent,
    MessageStartEvent,
    MessageUpdateEvent,
    MessageEndEvent,
    ToolExecutionStartEvent,
    ToolExecutionUpdateEvent,
    ToolExecutionEndEvent,
    TurnEndEvent,
    AgentEndEvent,
    AgentErrorEvent,
]

Emit = Callable[[AgentEvent], None]

