"""
**服务商契约（Provider 接口规范）**
一个 Provider 的职责：接收单个 `Context`，产出一串 `AssistantMessageEvent` 流式事件。
上层模块**只允许依赖这一套对外接口，除此之外不感知任何内部细节**。
"""

from __future__ import annotations

from typing import Protocol

from ..cost import apply_cost
from ..events import AssistantMessageEventStream
from ..types import AssistantMessage, Context, Model, StopReason, StreamOptions


class Provider(Protocol):
    id: str
    name: str
    api: str

    def stream(
            self,
            model: Model,
            context: Context,
            options: StreamOptions | None = None,
            api_key: str | None = None
    ) -> AssistantMessageEventStream: ...


def new_partial(model: Model) -> AssistantMessage:
    return AssistantMessage(
        content=[],
        api=model.api,
        provider=model.provider,
        model=model.id,
        stop_reason="pending"
    )


def finalize(message: AssistantMessage, model: Model, stop_reason: StopReason) -> AssistantMessage:
    message.stop_reason = stop_reason
    apply_cost(model, message.usage)
    return message


def resolve_thinking_budget(model: Model, reasoning: str) -> int | None:
    if reasoning in ("off", "", None):
        return None

    mapped = model.thinking_level_map.get(reasoning)
    if isinstance(mapped, int):
        return mapped

    defaults = {"minimal": 1024, "low": 2048, "medium": 8192, "high": 16384, "xhigh": 32768, "max": 63999}

    return defaults.get(reasoning)
