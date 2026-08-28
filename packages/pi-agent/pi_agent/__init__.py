"""
pi-agent — 分子层。
通用智能体运行时：包含循环、工具、钩子、事件、队列。仅依赖 pi-ai（消息与流式类型），
不感知文件、Shell、会话、命令行相关逻辑。
"""

from .agent import Agent
from .loop import (
    LoopConfig,
    agent_loop_continue,
    default_convert_to_llm,
    execute_tool_calls,
    run_agent_loop
)
from .queue import PendingMessageQueue
from .serde import agent_message_to_dict, agent_message_from_dict
from .types import (
    AfterToolCall,
    AfterToolCallResult,
    AgentEndEvent,
    AgentErrorEvent,
    AgentEvent,
    AgentMessage,
    AgentStartEvent,
    AgentState,
    AgentTool,
    AgentToolResult,
    BeforeToolCall,
    BeforeToolCallResult,
    ConvertToLlm,
    CustomMessage,
    Emit,
    EndReason,
    MessageEndEvent,
    MessageStartEvent,
    MessageUpdateEvent,
    QueueMode,
    StreamFn,
    ToolContext,
    ToolExecutionEndEvent,
    ToolExecutionMode,
    ToolExecutionStartEvent,
    ToolExecutionUpdateEvent,
    TransformContext,
    TurnStartEvent,
    TurnEndEvent
)

from .validation import ValidationError, validate_tool_arguments

__version__ = "0.1.0"

__all__ = [
    "AfterToolCall",
    "AfterToolCallResult",
    "Agent",
    "AgentEndEvent",
    "AgentErrorEvent",
    "AgentEvent",
    "AgentMessage",
    "AgentStartEvent",
    "AgentState",
    "AgentTool",
    "AgentToolResult",
    "BeforeToolCall",
    "BeforeToolCallResult",
    "ConvertToLlm",
    "CustomMessage",
    "Emit",
    "EndReason",
    "LoopConfig",
    "MessageEndEvent",
    "MessageStartEvent",
    "MessageUpdateEvent",
    "PendingMessageQueue",
    "QueueMode",
    "StreamFn",
    "ToolContext",
    "ToolExecutionEndEvent",
    "ToolExecutionMode",
    "ToolExecutionStartEvent",
    "ToolExecutionUpdateEvent",
    "TransformContext",
    "TurnEndEvent",
    "TurnStartEvent",
    "ValidationError",
    "agent_loop_continue",
    "agent_message_from_dict",
    "agent_message_to_dict",
    "default_convert_to_llm",
    "execute_tool_calls",
    "run_agent_loop",
    "validate_tool_arguments",
]