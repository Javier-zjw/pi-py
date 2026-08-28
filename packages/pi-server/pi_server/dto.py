"""事件 → 线上 JSON。

刻意不 import fastapi：这一层是纯函数，可以脱离 web 框架单测。所有 SSE
帧的形状都定义在这里，前端只认这一份契约。

和 pi-app 的渲染器一样，按 ``event.type`` 字符串分派而不是 isinstance，
这样下层加了新事件类型也不会让服务端崩。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

# 工具参数里最值得展示的字段，别把整个 JSON 甩给前端
SUMMARY_KEYS = ("path", "command", "pattern", "url", "query", "old_text")


def summarize_arguments(arguments: Any) -> str:
    if not isinstance(arguments, dict):
        return ""
    for key in SUMMARY_KEYS:
        value = arguments.get(key)
        if value:
            text = str(value).replace("\n", " ")
            return text[:120]
    try:
        return json.dumps(arguments, ensure_ascii=False)[:120]
    except (TypeError, ValueError):
        return ""


def preview_of(result: Any, limit: int = 400) -> str:
    content = getattr(result, "content", None) or []
    for block in content:
        text = getattr(block, "text", None)
        if text:
            return text[:limit]
    return ""


def usage_dto(usage: Any) -> dict[str, Any]:
    if usage is None:
        return {}
    return {
        "input": usage.input,
        "output": usage.output,
        "cacheRead": usage.cache_read,
        "total": usage.total_tokens,
        "cost": round(usage.cost.total, 6),
    }


@dataclass
class EventTranslator:
    """把一轮 prompt 的事件流翻译成前端帧。

    只保留最小状态：这一轮什么时候开始的，以便算耗时。其余状态都在前端。
    """

    turn_started: float = 0.0
    own_prompt: str = ""
    """本轮用户自己提交的那句话。前端已经显示过，不该再当"插话"发一遍。"""
    _text_seen: bool = False
    _tool_names: dict[str, str] = field(default_factory=dict)

    def start_turn(self) -> dict[str, Any]:
        self.turn_started = time.monotonic()
        self._text_seen = False
        self._tool_names.clear()
        return {"type": "turn_start", "at": time.time()}

    @property
    def elapsed(self) -> float:
        return round(time.monotonic() - self.turn_started, 2) if self.turn_started else 0.0

    def translate(self, event: Any) -> list[dict[str, Any]]:
        """一个 agent 事件可能产生 0 到 n 个前端帧。"""
        handler = getattr(self, f"_on_{event.type}", None)
        return handler(event) if handler else []

    # -- 具体事件 ------------------------------------------------------ #

    def _on_agent_start(self, event) -> list[dict]:
        return [self.start_turn()]

    def _on_message_update(self, event) -> list[dict]:
        inner = event.assistant_message_event
        kind = inner.type
        if kind == "thinking_delta":
            return [{"type": "thinking_delta", "text": inner.delta}]
        if kind == "text_delta":
            frames: list[dict] = []
            if not self._text_seen:
                # 第一个正文 token = 思考阶段结束，前端据此收起思考块
                self._text_seen = True
                frames.append({"type": "text_start"})
            frames.append({"type": "text_delta", "text": inner.delta})
            return frames
        return []

    def _on_message_end(self, event) -> list[dict]:
        message = event.message
        role = getattr(message, "role", "")
        if role in ("user", "custom"):
            text = message.text()
            if text.strip() and text.strip() == self.own_prompt.strip():
                # 用户自己提交的那条：前端在 send() 时就已经渲染了
                self.own_prompt = ""
                return []
            # 真正的插话（steering / follow-up）才需要显示
            return [{"type": "injected", "text": text}]
        if role != "assistant":
            return []
        return [
            {
                "type": "message_end",
                "text": message.text(),
                "stopReason": getattr(message, "stop_reason", ""),
                "error": getattr(message, "error_message", None),
                "usage": usage_dto(getattr(message, "usage", None)),
                "elapsed": self.elapsed,
            }
        ]

    def _on_tool_execution_start(self, event) -> list[dict]:
        self._tool_names[event.tool_call_id] = event.tool_name
        return [
            {
                "type": "tool_start",
                "id": event.tool_call_id,
                "name": event.tool_name,
                "summary": summarize_arguments(event.arguments),
                "arguments": event.arguments if isinstance(event.arguments, dict) else {},
            }
        ]

    def _on_tool_execution_update(self, event) -> list[dict]:
        return [
            {
                "type": "tool_update",
                "id": event.tool_call_id,
                "name": event.tool_name,
                "preview": preview_of(event.partial, 200),
            }
        ]

    def _on_tool_execution_end(self, event) -> list[dict]:
        details = getattr(event.result, "details", None)
        patch = details.get("patch") if isinstance(details, dict) else None
        return [
            {
                "type": "tool_end",
                "id": event.tool_call_id,
                "name": event.tool_name,
                "ok": not event.is_error,
                "preview": preview_of(event.result),
                "patch": patch,
            }
        ]

    def _on_agent_end(self, event) -> list[dict]:
        return [{"type": "done", "reason": event.reason, "elapsed": self.elapsed}]

    def _on_error(self, event) -> list[dict]:
        return [{"type": "error", "message": event.error}]


def sse_frame(payload: dict[str, Any]) -> str:
    """一个 SSE 帧。data 必须单行，所以 JSON 里不能有裸换行。"""
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
