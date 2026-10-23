"""
切点选择：messages[start:cut] 交给模型压缩，messages[cut:] 原样保留。

规则：
- 按 token 预算保留尾部，不按轮数——agent 场景一轮可能有上百次工具调用。
- 合法切点不能落在工具结果上，否则会把 tool_call 和它的结果拆开，服务商会拒绝请求。
- 优先切在用户消息处；最后一轮本身就超预算时，在轮内的 assistant 边界切开（split turn），
  这一轮的用户请求原文由调用方写进状态。
- 开头的状态消息（上一次压缩的结果）不参与切分，它作为"上一份状态"由提示词处理。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from pi_agent import AgentMessage
from pi_ai import AssistantMessage, ToolResultMessage, UserMessage

from .budget import estimate_message_tokens, is_state_message


@dataclass
class CutPoint:
    start: int
    cut: int
    split_turn: bool = False
    turn_request: str | None = None


def summarizable_start(messages: Sequence[AgentMessage]) -> int:
    return 1 if messages and is_state_message(messages[0]) else 0


def is_valid_boundary(messages: Sequence[AgentMessage], index: int) -> bool:
    return index == len(messages) or not isinstance(messages[index], ToolResultMessage)


def find_cut(messages: Sequence[AgentMessage], keep_recent_tokens: int) -> CutPoint | None:
    """没有可压缩的内容时返回 None。"""
    start = summarizable_start(messages)
    n = len(messages)
    if n - start < 2:
        return None

    # 尾部能放进预算的最小下标；连最后一条都放不下时为 n
    fit_from = n
    acc = 0
    for i in range(n - 1, start - 1, -1):
        acc += estimate_message_tokens(messages[i])
        if acc > keep_recent_tokens:
            break
        fit_from = i

    lower = max(fit_from, start + 1)
    cut = next((i for i in range(lower, n) if isinstance(messages[i], UserMessage)), None)
    if cut is None:
        cut = next((i for i in range(lower, n) if is_valid_boundary(messages, i)), None)
    if cut is None:
        # 最后一组 assistant + 工具结果本身就超预算：至少保留它
        cut = next(
            (i for i in range(n - 1, start, -1) if isinstance(messages[i], AssistantMessage)),
            None,
        )
    if cut is None or cut <= start:
        return None

    if isinstance(messages[cut], UserMessage):
        return CutPoint(start=start, cut=cut)

    request = next(
        (messages[i] for i in range(cut - 1, start - 1, -1) if isinstance(messages[i], UserMessage)),
        None,
    )
    return CutPoint(
        start=start,
        cut=cut,
        split_turn=request is not None,
        turn_request=request.text() if request is not None else None,
    )


def _groups(messages: Sequence[AgentMessage], start: int, end: int) -> list[tuple[int, int]]:
    """按合法切点分组：一组 = 一条消息 + 紧随其后的工具结果。"""
    groups: list[tuple[int, int]] = []
    group_start = start
    for i in range(start + 1, end + 1):
        if i == end or is_valid_boundary(messages, i):
            groups.append((group_start, i))
            group_start = i
    return groups


def chunk_ranges(
        messages: Sequence[AgentMessage], start: int, end: int, max_tokens: int
) -> list[tuple[int, int]]:
    """把 [start, end) 按整组打包成若干块，每块尽量不超过 max_tokens。
    单组本身超限时独占一块（由调用方决定如何处理）。"""
    ranges: list[tuple[int, int]] = []
    chunk_start: int | None = None
    acc = 0
    for group_start, group_end in _groups(messages, start, end):
        size = sum(estimate_message_tokens(m) for m in messages[group_start:group_end])
        if chunk_start is not None and acc + size > max_tokens:
            ranges.append((chunk_start, group_start))
            chunk_start, acc = None, 0
        if chunk_start is None:
            chunk_start = group_start
        acc += size
    if chunk_start is not None:
        ranges.append((chunk_start, end))
    return ranges
