"""
token 计量与压缩预算。

- 估算按字符类别区分：ASCII 约 4 字符 / token，中文等非 ASCII 字符按 1 字符 / token 保守计。
  旧实现统一按 4 字符 / token，中文会被低估 3～4 倍，导致来不及压缩就超窗口。
- 以服务商实报的 usage 为锚：最近一次 assistant 的 usage 是"那次请求的真实上下文大小"，
  只需再加上它之后追加的消息（往往是体积很大的工具结果）的估算。
- 压缩后保留尾部里的 assistant 消息，其 usage 是压缩**前**的数值，不能当锚，
  否则会误判"仍然超标"而反复压缩。状态消息的 details.tailLength 标出了这段范围。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from pi_agent import AgentMessage, CustomMessage
from pi_ai import (
    AssistantMessage,
    ImageContent,
    Model,
    TextContent,
    ThinkingContent,
    Tool,
    ToolCall,
    ToolResultMessage,
    UserMessage,
)

STATE_MESSAGE_TYPE = "compaction_summary"
IMAGE_TOKENS = 1600
MESSAGE_OVERHEAD = 4


def estimate_text_tokens(text: str) -> int:
    if not text:
        return 0
    ascii_chars = len(text.encode("ascii", "ignore"))
    return (ascii_chars + 3) // 4 + (len(text) - ascii_chars)


def _content_tokens(content: Any) -> int:
    if isinstance(content, str):
        return estimate_text_tokens(content)
    total = 0
    for block in content or []:
        if isinstance(block, TextContent):
            total += estimate_text_tokens(block.text)
        elif isinstance(block, ThinkingContent):
            total += estimate_text_tokens(block.thinking)
        elif isinstance(block, ToolCall):
            total += estimate_text_tokens(block.name)
            total += estimate_text_tokens(json.dumps(block.arguments, ensure_ascii=False))
        elif isinstance(block, ImageContent):
            total += IMAGE_TOKENS
    return total


def estimate_message_tokens(message: AgentMessage) -> int:
    if isinstance(message, CustomMessage):
        if not message.include_in_context:
            return 0
        return MESSAGE_OVERHEAD + _content_tokens(message.content)
    if isinstance(message, (UserMessage, AssistantMessage, ToolResultMessage)):
        return MESSAGE_OVERHEAD + _content_tokens(message.content)
    return 0


def estimate_tokens(messages: Sequence[AgentMessage]) -> int:
    return sum(estimate_message_tokens(m) for m in messages)


def estimate_tools_tokens(tools: Sequence[Tool]) -> int:
    return sum(
        estimate_text_tokens(t.name)
        + estimate_text_tokens(t.description)
        + estimate_text_tokens(json.dumps(t.parameters, ensure_ascii=False))
        for t in tools
    )


def is_state_message(message: AgentMessage) -> bool:
    return isinstance(message, CustomMessage) and message.custom_type == STATE_MESSAGE_TYPE


def anchor_start(messages: Sequence[AgentMessage]) -> int:
    """可作为 usage 锚点的最小下标：最近一个状态消息及其保留尾部之后。"""
    for i in range(len(messages) - 1, -1, -1):
        m = messages[i]
        if is_state_message(m):
            details = m.details if isinstance(m.details, dict) else {}
            return i + 1 + int(details.get("tailLength", 0))
    return 0


def _reported_prompt_tokens(message: AssistantMessage) -> int:
    u = message.usage
    return u.input + u.cache_read + u.cache_write + u.output


def context_tokens(
        messages: Sequence[AgentMessage],
        system_prompt: str | None = "",
        tools: Sequence[Tool] = (),
        min_anchor: int = 0,
) -> int:
    """下一次请求的上下文大小（tokens）。

    ``min_anchor``：此下标之前的 usage 不可信——比如清理旧工具输出之后，
    之前那些回复报的 usage 还是清理前的大小。"""
    start = max(anchor_start(messages), min_anchor)
    for i in range(len(messages) - 1, start - 1, -1):
        m = messages[i]
        if (
                isinstance(m, AssistantMessage)
                and m.stop_reason not in ("error", "aborted")
                and _reported_prompt_tokens(m) > 0
        ):
            return _reported_prompt_tokens(m) + estimate_tokens(messages[i + 1:])

    return (
            estimate_text_tokens(system_prompt or "")
            + estimate_tools_tokens(tools)
            + estimate_tokens(messages)
    )


@dataclass(frozen=True)
class CompactionBudget:
    context_window: int
    reserve_tokens: int = 16_384
    keep_recent_tokens: int = 20_000
    trigger_ratio: float = 0.85
    max_summary_tokens: int = 8_000
    # L1 清理旧工具输出：到 prune_trigger_ratio 时清，清到 prune_target_ratio 以下
    prune_enabled: bool = True
    prune_trigger_ratio: float = 0.6
    prune_target_ratio: float = 0.4
    protect_tokens: int = 40_000
    prune_min_tokens: int = 200

    @property
    def usable(self) -> int:
        """可用于输入的上下文：窗口减去输出预留（预留过大时至少留一半窗口）。"""
        return max(self.context_window - self.reserve_tokens, self.context_window // 2)

    @property
    def trigger(self) -> int:
        return int(self.usable * self.trigger_ratio)

    @property
    def keep_recent(self) -> int:
        return max(1, min(self.keep_recent_tokens, int(self.usable * 0.2)))

    @property
    def prune_trigger(self) -> int:
        return int(self.usable * self.prune_trigger_ratio)

    @property
    def prune_target(self) -> int:
        return int(self.usable * self.prune_target_ratio)

    @property
    def protect(self) -> int:
        """最近这段工具输出不清理：模型很可能还要用"""
        return max(1, min(self.protect_tokens, int(self.usable * 0.3)))

    def should_prune(self, tokens: int) -> bool:
        return self.prune_enabled and self.context_window > 0 and tokens >= self.prune_trigger

    def summary_budget(self, summarized_tokens: int) -> int:
        """被压缩内容的 6%，夹在 [1.5k, min(上限, 5% 可用预算)] 之间。"""
        upper = max(1_500, min(self.max_summary_tokens, int(self.usable * 0.05)))
        return max(1_500, min(int(summarized_tokens * 0.06), upper))

    def should_compact(self, tokens: int) -> bool:
        return self.context_window > 0 and tokens >= self.trigger

    @classmethod
    def from_settings(cls, model: Model, get: Callable[[str, Any], Any]) -> CompactionBudget:
        return cls(
            context_window=int(model.context_window or 0),
            reserve_tokens=int(get("compaction.reserveTokens", 16_384)),
            keep_recent_tokens=int(get("compaction.keepRecentTokens", 20_000)),
            trigger_ratio=float(get("compaction.threshold", 0.85)),
            max_summary_tokens=int(get("compaction.maxSummaryTokens", 8_000)),
            prune_enabled=bool(get("compaction.prune.enabled", True)),
            prune_trigger_ratio=float(get("compaction.prune.triggerRatio", 0.6)),
            prune_target_ratio=float(get("compaction.prune.targetRatio", 0.4)),
            protect_tokens=int(get("compaction.prune.protectTokens", 40_000)),
            prune_min_tokens=int(get("compaction.prune.minTokens", 200)),
        )
