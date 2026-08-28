"""
**原子类型的 JSON 序列化与反序列化。**
线上传输格式遵循 pi 的会话规范（键名使用小驼峰 camelCase），以此保证会话记录（transcripts）能够和 TypeScript 实现版本互相兼容、直接交换。
"""

from __future__ import annotations

from typing import Any

from .types import (AssistantMessage, Cost, ImageContent, Message, TextContent,
                    ThinkingContent, ToolCall, ToolResultMessage, Usage, UserMessage)


def content_to_dict(c: Any) -> dict[str, Any]:
    if isinstance(c, TextContent):
        return {"type": "text", "text": c.text}

    if isinstance(c, ImageContent):
        return {"type": "image", "data": c.data, "mimeType": c.mime_type}

    if isinstance(c, ThinkingContent):
        d: dict[str, Any] = {"type": "thinking", "thinking": c.thinking}
        if c.signature:
            d["signature"] = c.signature
        return d

    if isinstance(c, ToolCall):
        return {"type": "toolCall", "id": c.id, "name": c.name, "arguments": c.arguments}

    raise TypeError(f"unknown content block: {c!r}")


def content_from_dict(d: dict[str, Any]) -> Any:
    t = d.get("type")
    if t == "text":
        return TextContent(text=d.get("text", ""))
    if t == "image":
        return ImageContent(data=d.get("data", ""), mime_type=d.get("mimeType", "image/png"))
    if t == "thinking":
        return ThinkingContent(thinking=d.get("thinking", ""), signature=d.get("signature"))
    if t == "toolCall":
        return ToolCall(id=d["id"], name=d["name"], arguments=d.get("arguments") or {})

    raise TypeError(f"unknown content block: {d!r}")


def usage_to_dict(u: Usage) -> dict[str, Any]:
    return {
        "input": u.input,
        "output": u.output,
        "cacheRead": u.cache_read,
        "cacheWrite": u.cache_write,
        "totalTokens": u.total_tokens,
        "cost": {
            "input": u.cost.input,
            "output": u.cost.output,
            "cacheRead": u.cost.cache_read,
            "cacheWrite": u.cost.cache_write,
            "total": u.cost.total
        }
    }


def usage_from_dict(d: dict[str, Any] | None) -> Usage:
    if not d:
        return Usage()

    c = d.get("cost") or {}

    return Usage(
        input=d.get("input", 0),
        output=d.get("output", 0),
        cache_read=d.get("cacheRead", 0),
        cache_write=d.get("cacheWrite", 0),
        total_tokens=d.get("totalTokens", 0),
        cost=Cost(
            input=c.get("input", 0.0),
            output=c.get("output", 0.0),
            cache_read=c.get("cacheRead", 0.0),
            cache_write=c.get("cacheWrite", 0.0),
            total=c.get("total", 0.0)
        )
    )


def message_to_dict(m: Message) -> dict[str, Any]:
    if isinstance(m, UserMessage):
        content = m.content if isinstance(m.content, str) else [content_to_dict(c) for c in m.content]
        return {"role": "user", "content": content, "timestamp": m.timestamp}
    if isinstance(m, AssistantMessage):
        d = {
            "role": "assistant",
            "content": [content_to_dict(c) for c in m.content],
            "api": m.api,
            "provider": m.provider,
            "model": m.model,
            "usage": usage_to_dict(m.usage),
            "stopReason": m.stop_reason,
            "timestamp": m.timestamp
        }
        if m.error_message:
            d["errorMessage"] = m.error_message
        if m.response_id:
            d["responseId"] = m.response_id
        return d

    if isinstance(m, ToolResultMessage):
        d = {
            "role": "toolResult",
            "toolCallId": m.tool_call_id,
            "toolName": m.tool_name,
            "content": [content_to_dict(c) for c in m.content],
            "isError": m.is_error,
            "timestamp": m.timestamp
        }
        if m.details is not None:
            d["details"] = m.details
        if m.usage is not None:
            d["usage"] = usage_to_dict(m.usage)
        return d

    raise TypeError(f"unknown message: {m!r}")


def message_from_dict(d: dict[str, Any]) -> Message:
    role = d.get("role")
    if role == "user":
        raw = d.get("content", "")
        content = raw if isinstance(raw, str) else [content_from_dict(c) for c in raw]
        return UserMessage(content=content, timestamp=d.get("timestamp", 0.0))
    if role == "assistant":
        return AssistantMessage(
            content=[content_from_dict(c) for c in d.get("content", [])],
            api=d.get("api", ""),
            provider=d.get("provider", ""),
            model=d.get("model", ""),
            usage=usage_from_dict(d.get("usage")),
            stop_reason=d.get("stopReason", "stop"),
            error_message=d.get("errorMessage"),
            response_id=d.get("responseId"),
            timestamp=d.get("timestamp", 0.0)
        )

    if role == "toolResult":
        return ToolResultMessage(
            tool_call_id=d.get("toolCallId", ""),
            tool_name=d.get("toolName", ""),
            content=[content_from_dict(c) for c in d.get("content", [])],
            details=d.get("details"),
            usage=usage_from_dict(d["usage"]) if d.get("usage") else None,
            is_error=d.get("isError", False),
            timestamp=d.get("timestamp", 0.0)
        )

    raise TypeError(f"unknown message role: {role!r}")
