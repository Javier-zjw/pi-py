"""
**OpenAI 对话补全传输协议（wire protocol）。**
同时依托 `base_url` 兼容所有遵循 OpenAI 接口格式的端点：Ollama、vLLM、LM Studio、DeepSeek、OpenRouter 等等。
"""

from __future__ import annotations

import json
from typing import Any

from ..events import (
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
from ..json_parse import parse_streaming_json
from ..transport import HttpTransport, LLMError, default_transport
from ..types import (
    AssistantMessage,
    Context,
    ImageContent,
    Model,
    SimpleStreamOptions,
    StreamOptions,
    TextContent,
    ThinkingContent,
    ToolCall,
    ToolResultMessage,
    UserMessage,
)
from .base import finalize, new_partial

DEFAULT_BASE_URL = "https://api.openai.com/v1"

_STOP_REASONS = {
    "stop": "stop",
    "length": "length",
    "tool_calls": "toolUse",
    "function_call": "toolUse"
}

_REASONING_EFFORT = {"minimal": "minimal", "low": "low", "medium": "medium", "high": "high", "xhigh": "high",
                     "max": "high"}


def _user_content(content: Any) -> Any:
    if isinstance(content, str):
        return content

    parts: list[dict[str, Any]] = []

    for c in content:
        if isinstance(c, TextContent):
            parts.append({"type": "text", "text": c.text})

        elif isinstance(c, ImageContent):
            parts.append(
                {"type": "image_url", "image_url": {"url": f"data:{c.mime_type};base64,{c.data}"}}
            )

    return parts


def build_payload(model: Model, context: Context, options: StreamOptions) -> dict[str, Any]:
    messages: list[dict[str, Any]] = []
    if context.system_prompt:
        messages.append({"role": "system", "content": context.system_prompt})

    for m in context.messages:
        if isinstance(m, UserMessage):
            messages.append({"role": "user", "content": _user_content(m.content)})

        elif isinstance(m, AssistantMessage):
            text = "".join(c.text for c in m.content if isinstance(c, TextContent))

            tool_calls = [
                {
                    "id": c.id,
                    "type": "function",
                    "function": {"name": c.name, "arguments": json.dumps(c.arguments)}
                }
                for c in m.content
                if isinstance(c, ToolCall)
            ]

            entry: dict[str, Any] = {"role": "assistant", "content": text or None}
            if tool_calls:
                entry["tool_calls"] = tool_calls
            if text or tool_calls:
                messages.append(entry)

        elif isinstance(m, ToolResultMessage):
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": m.tool_call_id,
                    "content": "".join(
                        c.text for c in m.content if isinstance(c, TextContent)
                    ) or ("error" if m.is_error else "")
                }
            )

    payload: dict[str, Any] = {
        "model": model.id,
        "messages": messages,
        "stream": True,
        "stream_options": {"include_usage": True}
    }

    if options.max_tokens or model.max_tokens:
        payload["max_tokens"] = options.max_tokens or model.max_tokens

    if options.temperature is not None:
        payload["temperature"] = options.temperature

    if context.tools:
        payload["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters
                },
            }
            for t in context.tools
        ]

    reasoning = getattr(options, "reasoning", "off")

    if model.reasoning and reasoning != "off":
        payload["reasoning_effort"] = model.thinking_level_map.get(
            reasoning, _REASONING_EFFORT.get(reasoning, "medium")
        )
    elif model.reasoning:
        payload["thinking"] = {"type": "disabled"}
        payload["reasoning_effort"] = "none"

    payload.update(options.extra)
    return payload


class OpenAIProvider:
    api = "openai-completions"

    def __init__(
            self,
            provider_id: str = "openai",
            name: str = "OpenAI",
            base_url: str = DEFAULT_BASE_URL,
            transport: HttpTransport | None = None,
    ) -> None:
        self.id = provider_id
        self.name = name
        self.base_url = base_url
        self.transport = transport or default_transport()

    async def stream(
            self,
            model: Model,
            context: Context,
            options: StreamOptions | None = None,
            api_key: str | None = None,
    ) -> AssistantMessageEventStream:
        options = options or SimpleStreamOptions()
        message = new_partial(model)
        yield StartEvent(partial=message)

        url = (options.base_url or model.base_url or self.base_url).rstrip("/") + "/chat/completions"
        headers = {
            "content-type": "application/json",
            "accept": "text/event-stream"
        }

        if api_key:
            headers["authorization"] = f"Bearer {api_key}"

        payload = build_payload(model, context, options)

        text_index: int | None = None
        think_index: int | None = None
        tool_slots: dict[int, int] = {}
        tool_buffers: dict[int, str] = {}
        stop_reason = "stop"

        try:
            async for sse in self.transport.stream_sse(url, headers, payload):
                data = sse.data
                if data.get("error"):
                    err = data["error"].get("message", "provider error")
                    message.error_message = err
                    yield ErrorEvent(error=err, message=finalize(message, model, "error"))
                    return

                usage = data.get("usage")

                if usage:
                    if usage.get("prompt_tokens"):
                        message.usage.input = usage["prompt_tokens"]
                    if usage.get("completion_tokens"):
                        message.usage.output = usage["completion_tokens"]
                    details = usage.get("prompt_tokens_details") or {}
                    if details.get("cached_tokens"):
                        message.usage.cache_read = details["cached_tokens"]

                choices = data.get("choices") or []
                if not choices:
                    continue

                choice = choices[0]
                delta = choice.get("delta") or {}

                reasoning_delta = delta.get("reasoning_content") or delta.get("reasoning")
                if reasoning_delta:
                    if think_index is None:
                        think_index = len(message.content)
                        message.content.append(ThinkingContent(thinking=""))
                        yield ThinkingStartEvent(content_index=think_index, partial=message)
                    block = message.content[think_index]
                    assert isinstance(block, ThinkingContent)
                    block.thinking += reasoning_delta
                    yield ThinkingDeltaEvent(
                        content_index=think_index, delta=reasoning_delta, partial=message
                    )

                if delta.get("content"):
                    if think_index is not None and text_index is None:
                        block = message.content[think_index]
                        assert isinstance(block, ThinkingContent)
                        yield ThinkingEndEvent(
                            content_index=think_index, thinking=block.thinking, partial=message
                        )

                    if text_index is None:
                        text_index = len(message.content)
                        message.content.append(TextContent(text=""))
                        yield TextStartEvent(content_index=text_index, partial=message)

                    block = message.content[text_index]
                    assert isinstance(block, TextContent)
                    block.text += delta["content"]
                    yield TextDeltaEvent(
                        content_index=text_index, delta=delta["content"], partial=message
                    )

                for tc in delta.get("tool_calls") or []:
                    slot = tc.get("index", 0)
                    if slot not in tool_slots:
                        our_index = len(message.content)
                        tool_slots[slot] = our_index
                        tool_buffers[slot] = ""
                        message.content.append(
                            ToolCall(
                                id=tc.get("id") or f"call_{slot}",
                                name=(tc.get("function") or {}).get("name", "")
                            )
                        )
                        yield ToolCallStartEvent(
                            content_index=our_index,
                            tool_call_id=message.content[our_index].id,
                            tool_name=message.content[our_index].name,
                            partial=message
                        )

                    our_index = tool_slots[slot]
                    block = message.content[our_index]
                    assert isinstance(block, ToolCall)
                    fn = tc.get("function") or {}
                    if tc.get("id"):
                        block.id = tc["id"]
                    if fn.get("name"):
                        block.name = fn["name"]

                    fragment = fn.get("arguments") or ""
                    if fragment:
                        tool_buffers[slot] += fragment
                        block.arguments = parse_streaming_json(tool_buffers[slot])
                        yield ToolCallDeltaEvent(
                            content_index=our_index,
                            tool_call_id=block.id,
                            delta=fragment,
                            arguments=block.arguments,
                            partial=message
                        )

                if choice.get("finish_reason"):
                    stop_reason = _STOP_REASONS.get(choice["finish_reason"], "stop")

        except LLMError as exc:
            message.error_message = str(exc)
            yield ErrorEvent(error=str(exc), message=finalize(message, model, "error"))
            return

        if think_index is not None and text_index is None:
            block = message.content[think_index]
            assert isinstance(block, ThinkingContent)
            yield ThinkingEndEvent(
                content_index=think_index, thinking=block.thinking, partial=message
            )

        if text_index is not None:
            block = message.content[text_index]
            assert isinstance(block, TextContent)
            yield TextEndEvent(content_index=text_index, text=block.text, partial=message)

        for slot, our_index in tool_slots.items():
            block = message.content[our_index]
            assert isinstance(block, ToolCall)

            raw = tool_buffers.get(slot, "")
            if raw.strip():
                try:
                    block.arguments = json.loads(raw)
                except json.JSONDecodeError:
                    block.arguments = parse_streaming_json(raw)

            yield ToolCallEndEvent(content_index=our_index, tool_call=block, partial=message)

        yield DoneEvent(reason=stop_reason, message=finalize(message, model, stop_reason))


def openai_provider(transport: HttpTransport | None = None) -> OpenAIProvider:
    return OpenAIProvider(transport=transport)


def openai_compatible_provider(
        provider_id: str,
        base_url: str,
        name: str | None = None,
        transport: HttpTransport | None = None
) -> OpenAIProvider:
    return OpenAIProvider(
        provider_id=provider_id,
        name=name or provider_id,
        base_url=base_url,
        transport=transport
    )
