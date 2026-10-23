"""
L1：清理旧工具输出（不调用模型，毫秒级）。

agent 上下文里工具输出通常占大头，而它们都能重新获取（再 read 一次、再跑一次命令），
所以把"受保护的最近区间"之外的旧输出换成一行占位，基本无损：

    [已清理的 read 输出：src/a.py（第 1-200 行），约 8.2k tokens。需要时重新读取]

约定：
- 只替换内存里发给模型的那份：换成**副本**，原对象（也是会话条目里的那个）不动，
  会话文件保留完整原文，分支 / 导出不受影响。
- 工具调用本身（参数）保留，模型仍知道做过什么；报错结果保留开头几行。
- 一次清到目标线以下再停（滞回），清理集合持久化成 context_prune 条目——
  每轮发出的前缀保持稳定，不会每轮都打掉前缀缓存；恢复会话后结果一致。
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from pi_agent import AgentMessage
from pi_ai import AssistantMessage, TextContent, ToolCall, ToolResultMessage

from .budget import estimate_message_tokens

PRUNE_ENTRY_TYPE = "context_prune"
STUB_PREFIX = "[已清理的 "
ERROR_KEEP_CHARS = 300


@dataclass
class PrunePlan:
    tool_call_ids: list[str] = field(default_factory=list)
    tokens_saved: int = 0


def _tool_calls_by_id(messages: Sequence[AgentMessage]) -> dict[str, ToolCall]:
    calls: dict[str, ToolCall] = {}
    for m in messages:
        if isinstance(m, AssistantMessage):
            for call in m.tool_calls():
                calls[call.id] = call
    return calls


def is_pruned(message: AgentMessage) -> bool:
    return (
        isinstance(message, ToolResultMessage)
        and len(message.content) == 1
        and isinstance(message.content[0], TextContent)
        and message.content[0].text.startswith(STUB_PREFIX)
    )


def _short(value: object, limit: int = 120) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _k(tokens: int) -> str:
    return f"{tokens / 1000:.1f}k" if tokens >= 1000 else str(tokens)


def stub_text(result: ToolResultMessage, call: ToolCall | None, tokens: int) -> str:
    args = call.arguments if call is not None and isinstance(call.arguments, dict) else {}
    details = result.details if isinstance(result.details, dict) else {}
    name = result.tool_name
    size = f"约 {_k(tokens)} tokens"

    if name == "read":
        path = details.get("path") or args.get("path") or "?"
        span = ""
        if args.get("offset") or args.get("limit"):
            start = int(args.get("offset") or 1)
            span = f"（第 {start}-{start + int(args['limit']) - 1} 行）" if args.get("limit") \
                else f"（从第 {start} 行起）"
        head = f"read 输出：{path}{span}"
        tail = "需要时重新读取"
    elif name == "bash":
        command = details.get("command") or args.get("command") or "?"
        exit_code = details.get("exit_code")
        head = f"bash 输出：`{_short(command)}`" + (f"，exit {exit_code}" if exit_code is not None else "")
        tail = "需要结果时重新执行（注意命令的副作用）"
    else:
        shown = {k: v for k, v in args.items() if k not in ("content", "old_text", "new_text")}
        head = f"{name} 输出" + (f"：{_short(shown)}" if shown else "")
        tail = "需要时重新调用"

    text = f"{STUB_PREFIX}{head}，{size}。{tail}]"
    if result.is_error:
        original = result.text().strip()
        if original:
            text += "\n报错开头：" + original[:ERROR_KEEP_CHARS]
    return text


def stub_result(result: ToolResultMessage, call: ToolCall | None) -> ToolResultMessage:
    """返回替换了内容的副本；details 原样保留（不发给模型，压缩时提取文件 / 命令记录要用）。"""
    tokens = estimate_message_tokens(result)
    return dataclasses.replace(
        result, content=[TextContent(text=stub_text(result, call, tokens))]
    )


def plan_prune(
        messages: Sequence[AgentMessage],
        tokens_now: int,
        target_tokens: int,
        protect_tokens: int,
        min_tokens: int = 200,
) -> PrunePlan:
    """从最旧的开始挑，直到预计降到 target_tokens 以下。最近 protect_tokens 内的不动。"""
    # 受保护区间：从尾部往前累计到 protect_tokens
    protected_from = len(messages)
    acc = 0
    for i in range(len(messages) - 1, -1, -1):
        acc += estimate_message_tokens(messages[i])
        if acc > protect_tokens:
            break
        protected_from = i

    calls = _tool_calls_by_id(messages)
    plan = PrunePlan()
    for m in messages[:protected_from]:
        if tokens_now - plan.tokens_saved <= target_tokens:
            break
        if not isinstance(m, ToolResultMessage) or is_pruned(m):
            continue
        size = estimate_message_tokens(m)
        if size < min_tokens:
            continue
        saved = size - estimate_message_tokens(stub_result(m, calls.get(m.tool_call_id)))
        if saved <= 0:
            continue
        plan.tool_call_ids.append(m.tool_call_id)
        plan.tokens_saved += saved
    return plan


def apply_prune(
        messages: Sequence[AgentMessage], tool_call_ids: Iterable[str]
) -> tuple[list[AgentMessage], int]:
    """把指定工具结果换成占位副本。返回 (新列表, 实际替换条数)；已清理过的不重复处理。"""
    targets = set(tool_call_ids)
    if not targets:
        return list(messages), 0
    calls = _tool_calls_by_id(messages)
    out: list[AgentMessage] = []
    count = 0
    for m in messages:
        if isinstance(m, ToolResultMessage) and m.tool_call_id in targets and not is_pruned(m):
            out.append(stub_result(m, calls.get(m.tool_call_id)))
            count += 1
        else:
            out.append(m)
    return out, count
