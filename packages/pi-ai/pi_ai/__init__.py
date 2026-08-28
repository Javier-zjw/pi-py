"""
**pi-ai —— 原子层（atomic layer）。
统一服务商的 LLM 底层 API：包含类型定义、流式事件、服务商适配器、凭证管理与费用统计模块。
该包不依赖任何其他 pi 系列包，并且**永远不能引入对其它 pi 包的依赖。
"""

from .auth import (
    ChainedCredentialStore,
    CredentialStore,
    EnvCredentialStore,
    FileCredentialStore,
    InMemoryCredentialStore,
    default_credential_store,
)
from .catalog import BUILTIN_MODELS
from .cost import apply_cost, calculate_cost
from .events import (
    AssistantMessageEvent,
    AssistantMessageEventStream,
    DoneEvent,
    ErrorEvent,
    StartEvent,
    TextDeltaEvent,
    TextEndEvent,
    TextStartEvent,
    ThinkingDeltaEvent,
    ThinkingEndEvent,
    ThinkingStartEvent,
    ToolCallDeltaEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
)
from .models import Models, create_models, get_model
from .providers.anthropic import AnthropicProvider, anthropic_provider
from .providers.base import Provider
from .providers.openai import OpenAIProvider, openai_compatible_provider, openai_provider
from .serde import message_from_dict, message_to_dict, usage_from_dict, usage_to_dict
from .transport import HttpTransport, HttpxTransport, LLMError, SSEEvent
from .types import (
    THINKING_LEVELS,
    Api,
    AssistantMessage,
    Content,
    Context,
    Cost,
    ImageContent,
    Message,
    Model,
    ModelCost,
    SimpleStreamOptions,
    StopReason,
    StreamOptions,
    TextContent,
    ThinkingContent,
    ThinkingLevel,
    Tool,
    ToolCall,
    ToolResultMessage,
    Usage,
    UserMessage,
)

__version__ = "0.1.0"

__all__ = [
    "AnthropicProvider",
    "Api",
    "AssistantMessage",
    "AssistantMessageEvent",
    "AssistantMessageEventStream",
    "BUILTIN_MODELS",
    "ChainedCredentialStore",
    "Content",
    "Context",
    "Cost",
    "CredentialStore",
    "DoneEvent",
    "EnvCredentialStore",
    "ErrorEvent",
    "FileCredentialStore",
    "HttpTransport",
    "HttpxTransport",
    "ImageContent",
    "InMemoryCredentialStore",
    "LLMError",
    "Message",
    "Model",
    "ModelCost",
    "Models",
    "OpenAIProvider",
    "Provider",
    "SSEEvent",
    "SimpleStreamOptions",
    "StartEvent",
    "StopReason",
    "StreamOptions",
    "THINKING_LEVELS",
    "TextContent",
    "TextDeltaEvent",
    "TextEndEvent",
    "TextStartEvent",
    "ThinkingContent",
    "ThinkingDeltaEvent",
    "ThinkingEndEvent",
    "ThinkingLevel",
    "ThinkingStartEvent",
    "Tool",
    "ToolCall",
    "ToolCallDeltaEvent",
    "ToolCallEndEvent",
    "ToolCallStartEvent",
    "ToolResultMessage",
    "Usage",
    "UserMessage",
    "anthropic_provider",
    "apply_cost",
    "calculate_cost",
    "create_models",
    "default_credential_store",
    "get_model",
    "message_from_dict",
    "message_to_dict",
    "openai_compatible_provider",
    "openai_provider",
    "usage_from_dict",
    "usage_to_dict",
]
