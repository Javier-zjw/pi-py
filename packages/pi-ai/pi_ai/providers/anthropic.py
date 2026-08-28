"""
Anthropic Messages API
"""

from __future__ import annotations

import json
from typing import Any

from ..events import (
    AssistantMessageEventStream,
    DoneEvent,
    ErrorEvent,
    StartEvent,
    TextStartEvent,
    TextDeltaEvent,
    TextEndEvent,
    ThinkingStartEvent,
    ThinkingDeltaEvent,
    ThinkingEndEvent,
    ToolCallStartEvent,
    ToolCallDeltaEvent,
    ToolCallEndEvent
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
    UserMessage
)

from .base import finalize, new_partial, resolve_thinking_budget

DEFAULT_BASE_URL = "https://api.anthropic.com"
API_VERSION = "2023-06-01"

_STOP_REASONS = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "max_tokens": "length",
    "tool_use": "toolUse"
}


def _content_to_blocks(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}]

    blocks: list[dict[str, Any]] = []
    for c in content:
        if isinstance(c, TextContent):
            blocks.append({"type": "text", "text": c.text})
        elif isinstance(c, ImageContent):
            blocks.append(
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": c.mime_type, "data": c.data}
                }
            )

    return blocks


def build_payload(model: Model, context: Context, options: StreamOptions) -> dict[str, Any]:
    messages: list[dict[str, Any]] = []
    for m in context.messages:
        if isinstance(m, UserMessage):
            messages.append({"role": "user", "content": _content_to_blocks(m.content)})
        elif isinstance(m, AssistantMessage):
            blocks: list[dict[str, Any]] = []
            for c in m.content:
                if isinstance(c, TextContent):
                    if c.text:
                        blocks.append({"type": "text", "text": c.text})
                elif isinstance(c, ThinkingContent):
                    if c.signature and m.provider == model.provider:
                        blocks.append({
                            "type": "thinking", "thinking": c.thinking, "signature": c.signature
                        })
                elif isinstance(c, ToolCall):
                    blocks.append({
                        "type": "tool_use", "id": c.id, "name": c.name, "input": c.arguments
                    })

            if blocks:
                messages.append({"role": "assistant", "content": blocks})

        elif isinstance(m, ToolResultMessage):
            block = {
                "type": "tool_result",
                "tool_use_id": m.tool_call_id,
                "content": _content_to_blocks(m.content) or [{"type": "text", "text": ""}]
            }
            if m.is_error:
                block["is_error"] = True
            if messages and messages[-1]["role"] == "user" and isinstance(messages[-1]["content"], list):
                messages[-1]["content"].append(block)
            else:
                messages.append({"role": "user", "content": [block]})

    payload: dict[str, Any] = {
        "model": model.id,
        "messages": messages,
        "max_tokens": options.max_tokens or model.max_tokens,
        "stream": True
    }

    if context.system_prompt:
        payload["system"] = context.system_prompt

    if context.tools:
        payload["tools"] = [
            {"name": t.name, "description": t.description, "input_schema": t.parameters}
            for t in context.tools
        ]

    if options.temperature is not None:
        payload["temperature"] = options.temperature

    reasoning = getattr(options, "reasoning", "off")
    budget = resolve_thinking_budget(model, reasoning) if model.reasoning else None

    if budget:
        payload["thinking"] = {"type": "enabled", "budget_tokens": budget}
        payload["max_tokens"] = max(payload["max_tokens"], budget + 1024)
        payload.pop("temperature", None)
    elif model.reasoning:
        payload["thinking"] = {"type": "disabled"}

    payload.update(options.extra)

    return payload


class AnthropicProvider:
    id = "anthropic"
    name = "Anthropic"
    api = "anthropic-messages"

    def __init__(self, transport: HttpTransport | None = None, base_url: str | None = None) -> None:
        self.transport = transport or default_transport()
        self.base_url = base_url or DEFAULT_BASE_URL

    async def stream(
            self,
            model: Model,
            context: Context,
            options: StreamOptions | None = None,
            api_key: str | None = None
    ) -> AssistantMessageEventStream:
        options = options or SimpleStreamOptions()
        message = new_partial(model)
        yield StartEvent(partial=message)

        if not api_key:
            message.error_message = "missing API key for provider 'anthropic'"
            yield ErrorEvent(error=message.error_message, message=finalize(message, model, "error"))
            return

        url = (options.base_url or model.base_url or self.base_url).rstrip("/") + "/v1/messages"

        headers = {
            "x-api-key": api_key,
            "anthropic-version": API_VERSION,
            "content-type": "application/json",
            "accept": "text/event-stream"
        }

        payload = build_payload(model, context, options)

        index_map: dict[int, int] = {}
        json_buffers: dict[int, str] = {}
        stop_reason = "stop"

        try:
            async for sse in self.transport.stream_sse(url, headers, payload):
                data = sse.data
                etype = data.get("type") or sse.event

                if etype == "message_start":
                    usage = (data.get("message") or {}).get("usage") or {}
                    message.usage.input = usage.get("input_tokens", 0)
                    message.usage.cache_read = usage.get("cache_read_input_tokens", 0)
                    message.usage.cache_write = usage.get("cache_creation_input_tokens", 0)
                    message.response_id = (data.get("message") or {}).get("id")

                elif etype == "content_block_start":
                    block = data.get("content_block") or {}
                    provider_index = data.get("index", 0)
                    our_index = len(message.content)
                    index_map[provider_index] = our_index

                    if block.get("type") == "text":
                        message.content.append(TextContent(text=""))
                        yield TextStartEvent(content_index=our_index, partial=message)
                    elif block.get("type") == "thinking":
                        message.content.append(ThinkingContent(thinking=""))
                        yield ThinkingStartEvent(content_index=our_index, partial=message)
                    elif block.get("type") == "tool_use":
                        message.content.append(
                            ToolCall(id=block.get("id", ""), name=block.get("name", ""))
                        )

                        json_buffers[provider_index] = ""
                        yield ToolCallStartEvent(
                            content_index=our_index,
                            tool_call_id=block.get("id", ""),
                            tool_name=block.get("name", ""),
                            partial=message
                        )

                elif etype == "content_block_delta":
                    provider_index = data.get("index", 0)
                    our_index = index_map.get(provider_index, 0)
                    delta = data.get("delta") or {}
                    dtype = delta.get("type")
                    block = message.content[our_index]
                    if dtype == "text_delta" and isinstance(block, TextContent):
                        block.text += delta.get("text", "")
                        yield TextDeltaEvent(
                            content_index=our_index,
                            delta=delta.get("text", ""),
                            partial=message
                        )
                    elif dtype == "thinking_delta" and isinstance(block, ThinkingContent):
                        block.thinking += delta.get("thinking", "")
                        yield ThinkingDeltaEvent(
                            content_index=our_index,
                            delta=delta.get("thinking", ""),
                            partial=message
                        )

                    elif dtype == "signature_delta" and isinstance(block, ThinkingContent):
                        block.signature = (block.signature or "") + delta.get("signature", "")

                    elif dtype == "input_json_delta" and isinstance(block, ToolCall):
                        fragment = delta.get("partial_json", "")
                        json_buffers[provider_index] = json_buffers.get(provider_index, "") + fragment
                        block.arguments = parse_streaming_json(json_buffers[provider_index])

                        yield ToolCallDeltaEvent(
                            content_index=our_index,
                            tool_call_id=block.id,
                            delta=fragment,
                            arguments=block.arguments,
                            partial=message
                        )

                elif etype == "content_block_stop":
                    provider_index = data.get("index", 0)
                    our_index = index_map.get(provider_index, 0)
                    block = message.content[our_index]
                    if isinstance(block, TextContent):
                        yield TextEndEvent(content_index=our_index, text=block.text, partial=message)

                    elif isinstance(block, ThinkingContent):
                        yield ThinkingEndEvent(
                            content_index=our_index,
                            thinking=block.thinking,
                            partial=message
                        )

                    elif isinstance(block, ToolCall):
                        raw = json_buffers.get(provider_index, "")
                        if raw.strip():
                            try:
                                block.arguments = json.loads(raw)
                            except json.JSONDecodeError:
                                block.arguments = parse_streaming_json(raw)

                        yield ToolCallEndEvent(
                            content_index=our_index,
                            tool_call=block,
                            partial=message
                        )

                elif etype == "message_delta":
                    delta = data.get("delta") or {}
                    if delta.get("stop_reason"):
                        stop_reason = _STOP_REASONS.get(delta["stop_reason"], "stop")

                    usage = data.get("usage") or {}
                    if "output_tokens" in usage:
                        message.usage.output = usage["output_tokens"]
                    if "input_tokens" in usage:
                        message.usage.input = usage["input_tokens"]

                elif etype == "error":
                    err = (data.get("error") or {}).get("message", "provider error")
                    message.error_message = err
                    yield ErrorEvent(error=err, message=finalize(message, model, "error"))
                    return

        except LLMError as exc:
            message.error_message = str(exc)
            yield ErrorEvent(error=str(exc), message=finalize(message, model, "error"))
            return

        yield DoneEvent(reason=stop_reason, message=finalize(message, model, stop_reason))


def anthropic_provider(transport: HttpTransport | None = None, base_url: str | None = None) -> AnthropicProvider:
    return AnthropicProvider(transport=transport, base_url=base_url)
