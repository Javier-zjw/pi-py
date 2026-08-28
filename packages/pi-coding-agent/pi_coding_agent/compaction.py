"""
上下文压缩。
当会话记录接近模型上下文窗口上限时，请求模型对除最近几轮之外的全部内容做摘要，并用这份摘要替换原有消息。
压缩记录节点会保留尾部未压缩会话，形成自完备的检查点；
重建上下文时无需读取该节点之前的历史消息。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pi_agent import AgentMessage, CustomMessage
from pi_ai import (
    AssistantMessage,
    Content,
    Model,
    SimpleStreamOptions,
    TextContent,
    ToolResultMessage,
    Usage,
    UserMessage, Context
)

COMPACTION_PROMPT = """
Summarize the conversation above for a coding agent that will continue the work with no other memory of it. Write dense prose, no pleasantries.

Cover:
1. What the user asked for, including constraints and preferences they stated.
2. What has been done so far: files created or modified, and the reasoning behind
   non-obvious choices.
3. Current state: what works, what is broken, what was tried and rejected.
4. The immediate next step.

Name files by exact path. Keep code snippets only where the exact text matters.
"""


@dataclass
class CompactionResult:
    summary: str
    tokens_before: int
    retained_tail: list[AgentMessage]
    usage: Usage
    details: dict[str, Any]


def estimate_tokens(messages: list[AgentMessage]) -> int:
    """基于字符的低成本估算；足以用来判断**何时触发压缩**。"""
    total = 0
    for m in messages:
        if isinstance(m, AssistantMessage):
            total += sum(
                len(getattr(c, "text", "") or getattr(c, "thinking", "") or "") for c in m.content
            )
            total += sum(len(str(c.arguments)) for c in m.tool_calls())

        elif isinstance(m, (UserMessage, ToolResultMessage, CustomMessage)):
            total += len(m.text())

    return total // 4


def last_reported_tokens(messages: list[AgentMessage]) -> int:
    """优先使用服务商自身给出的数值（如果可获取）"""
    for m in reversed(messages):
        if isinstance(m, AssistantMessage) and m.usage.input:
            return m.usage.input + m.usage.output + m.usage.cache_read

    return estimate_tokens(messages)


def should_compact(messages: list[AgentMessage], model: Model, threshold: float = 0.85) -> bool:
    if not model.context_window:
        return False
    return last_reported_tokens(messages) >= model.context_window * threshold


def split_tail(messages: list[AgentMessage], keep_last_turns: int = 4) -> tuple[list[AgentMessage], list[AgentMessage]]:
    """以用户消息边界切分为（待摘要部分，保留尾部）"""
    boundaries = [i for i, m in enumerate(messages) if isinstance(m, UserMessage)]
    if len(boundaries) <= keep_last_turns:
        return messages, []

    cut = boundaries[-keep_last_turns]
    return messages[:cut], messages[cut:]


def collect_file_activity(messages: list[AgentMessage]) -> dict[str, list[str]]:
    read_files: list[str] = []
    modified: list[str] = []

    for m in messages:
        if isinstance(m, ToolResultMessage) and isinstance(m.details, dict):
            path = m.details.get("path")
            if not path:
                continue
            if m.tool_name == "read" and path not in read_files:
                read_files.append(path)
            elif m.tool_name in ("write", "edit") and path not in modified:
                modified.append(path)

    return {"readFiles": read_files, "modifiedFiles": modified}


async def compact(
        messages: list[AgentMessage],
        model: Model,
        complete_fn,
        keep_last_turns: int = 4,
        custom_instructions: str | None = None
) -> CompactionResult:
    """``complete_fn`` 即 ``Models.complete_simple``；采用注入方式传入，不直接import导入。"""

    to_summarize, retained = split_tail(messages, keep_last_turns)
    tokens_before = last_reported_tokens(messages)

    from pi_agent import default_convert_to_llm

    instructions = COMPACTION_PROMPT
    if custom_instructions:
        instructions += f"\n\nAdditional instructions from the user:\n{custom_instructions}"

    context = Context(
        system_prompt="You write handover notes for coding agents.",
        messages=[
            *default_convert_to_llm(to_summarize),
            UserMessage(content=instructions)
        ],
        tools=[]
    )
    response: AssistantMessage = await complete_fn(model, context, SimpleStreamOptions())
    summary = response.text().strip() or "(compaction produced no summary)"

    return CompactionResult(
        summary=summary,
        tokens_before=tokens_before,
        retained_tail=retained,
        usage=response.usage,
        details=collect_file_activity(to_summarize)
    )
