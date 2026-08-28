"""实现 AgentMessage 与 JSON 的互相转换，构建在 pi_ai 消息序列化/反序列化层之上。"""

from __future__ import annotations

from typing import Any

from pi_ai import message_from_dict, message_to_dict
from pi_ai.serde import content_from_dict, content_to_dict

from .types import AgentMessage, CustomMessage

def agent_message_to_dict(m: AgentMessage) -> dict[str, Any]:

    if isinstance(m, CustomMessage):
        content = (
            m.content if isinstance(m.content, str) else [content_to_dict(c) for c in m.content]
        )

        return {
            "role": "custom",
            "customType": m.custom_type,
            "content": content,
            "display": m.display,
            "includeInContext": m.include_in_context,
            "details": m.details,
            "timestamp": m.timestamp
        }

    return message_to_dict(m)

def agent_message_from_dict(d: dict[str, Any]) -> AgentMessage:

    if d.get("role") == "custom":
        raw = d.get("content", "")
        content = raw if isinstance(raw, str) else [content_to_dict(c) for c in raw]
        return CustomMessage(
            custom_type=d.get("customType", ""),
            content=content,
            display=d.get("display", True),
            include_in_context=d.get("includeInContext", True),
            details=d.get("details"),
            timestamp=d.get("timestamp", 0.0)
        )

    return message_from_dict(d)