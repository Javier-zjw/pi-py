"""
压缩编排：切点 → 构造请求 → 流式调用模型 → 校验 / 重试 / 降级 → 组装状态。

稳定性约定：
- 本模块只计算结果，不改动任何会话状态；调用方拿到 CompactionOutcome 后再一次性替换。
- 服务商出错（网络、限流）重试一次后抛 CompactionFailed，调用方保持原历史不变。
- 模型输出格式不合格时带着错误原因重试一次，仍不合格则降级（确定性字段仍完整）。
- 取消时抛 CompactionCancelled，同样不产生任何副作用。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from pi_agent import AgentMessage, CustomMessage, StreamFn, default_convert_to_llm
from pi_ai import (
    AssistantMessage,
    Context,
    DoneEvent,
    ErrorEvent,
    Message,
    Model,
    SimpleStreamOptions,
    TextContent,
    TextDeltaEvent,
    ThinkingLevel,
    Tool,
    Usage,
    UserMessage,
)

from .budget import (
    STATE_MESSAGE_TYPE,
    CompactionBudget,
    context_tokens,
    estimate_text_tokens,
    estimate_tokens,
    estimate_tools_tokens,
)
from .cut import CutPoint, chunk_ranges, find_cut
from .prompt import build_instruction, build_retry_message
from .state import (
    build_state,
    degraded_llm_state,
    extract_facts,
    merge_facts,
    normalize_llm_state,
    parse_json_block,
    render_markdown,
    restore_dropped,
    trim_to_budget,
)

MIN_KEEP_RECENT = 1_000
SAFETY_MARGIN = 2_000


class CompactionFailed(Exception):
    """模型调用失败（网络、限流、服务商报错）。原历史保持不变。"""


class CompactionCancelled(Exception):
    """用户取消。原历史保持不变。"""


@dataclass
class CompactionProgress:
    phase: str                 # "summarizing" | "retrying"
    chunk: int = 1
    chunks: int = 1
    delta: str = ""            # 流式摘要文本


@dataclass
class CompactionRequest:
    messages: Sequence[AgentMessage]
    model: Model
    stream_fn: StreamFn
    budget: CompactionBudget
    system_prompt: str | None = None
    tools: Sequence[Tool] = ()
    previous_state: dict[str, Any] | None = None
    cwd: str | None = None
    reason: str = "threshold"          # threshold | manual | overflow
    custom_instructions: str | None = None
    reasoning: ThinkingLevel = "off"


@dataclass
class CompactionOutcome:
    state: dict[str, Any]
    summary: str                       # 渲染后的 Markdown
    cut: CutPoint
    tokens_before: int
    tokens_after: int
    usage: Usage = field(default_factory=Usage)
    degraded: bool = False
    restored: list[str] = field(default_factory=list)
    trimmed: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    chunks: int = 1
    retained: list[AgentMessage] = field(default_factory=list)

    def state_message(self) -> CustomMessage:
        return make_state_message(self.summary, self.state, tail_length=len(self.retained))


def make_state_message(summary: str, state: dict[str, Any] | None, tail_length: int) -> CustomMessage:
    return CustomMessage(
        custom_type=STATE_MESSAGE_TYPE,
        content=summary,
        display=True,
        details={"tailLength": tail_length, "seq": (state or {}).get("seq")},
        timestamp=time.time(),
    )


def _fixed_tokens(req: CompactionRequest) -> int:
    return estimate_text_tokens(req.system_prompt or "") + estimate_tools_tokens(req.tools)


def _choose_cut(req: CompactionRequest) -> CutPoint | None:
    """从配置的保留量开始，必要时逐步减半，保证压缩后落在可用预算的一半以内，
    避免刚压缩完又触发压缩。"""
    budget = req.budget
    target = int(budget.usable * 0.5)
    keep = budget.keep_recent
    fixed = _fixed_tokens(req)
    best: CutPoint | None = None
    while True:
        cut = find_cut(req.messages, keep)
        if cut is None:
            return best
        best = cut
        summarized = estimate_tokens(req.messages[cut.start:cut.cut])
        after = fixed + budget.summary_budget(summarized) + estimate_tokens(req.messages[cut.cut:])
        if after <= target or keep <= MIN_KEEP_RECENT:
            return cut
        keep = max(MIN_KEEP_RECENT, keep // 2)


async def _stream_once(
        req: CompactionRequest,
        messages: list[Message],
        max_tokens: int,
        progress: Callable[[CompactionProgress], None],
        chunk: int,
        chunks: int,
        phase: str,
        cancel_event: asyncio.Event | None,
) -> AssistantMessage:
    tools = [Tool(name=t.name, description=t.description, parameters=t.parameters)
             for t in req.tools]
    options = SimpleStreamOptions(
        reasoning=req.reasoning,
        max_tokens=max_tokens,
        temperature=0.0 if req.reasoning == "off" else None,
        tool_choice="none" if tools else None,
    )
    context = Context(system_prompt=req.system_prompt, messages=messages, tools=tools)
    stream = req.stream_fn(req.model, context, options)
    final: AssistantMessage | None = None
    async for event in stream:
        if cancel_event is not None and cancel_event.is_set():
            aclose = getattr(stream, "aclose", None)
            if aclose:
                await aclose()
            raise CompactionCancelled()
        if isinstance(event, TextDeltaEvent):
            progress(CompactionProgress(phase=phase, chunk=chunk, chunks=chunks, delta=event.delta))
        elif isinstance(event, (DoneEvent, ErrorEvent)):
            final = event.message
    if cancel_event is not None and cancel_event.is_set():
        raise CompactionCancelled()
    if final is None:
        final = AssistantMessage(stop_reason="error",
                                 error_message="stream ended without a terminal event")
    return final


async def _summarize(
        req: CompactionRequest,
        base: list[Message],
        instruction: str,
        max_tokens: int,
        progress: Callable[[CompactionProgress], None],
        chunk: int,
        chunks: int,
        cancel_event: asyncio.Event | None,
        usage: list[Usage],
) -> tuple[dict[str, Any] | None, list[str]]:
    """返回 (llm 状态, 错误)；llm 为 None 表示格式两次不合格，需要降级。"""
    messages: list[Message] = [*base, UserMessage(content=instruction)]
    errors: list[str] = []
    provider_retried = False
    format_retried = False
    phase = "summarizing"
    while True:
        reply = await _stream_once(req, messages, max_tokens, progress, chunk, chunks, phase,
                                   cancel_event)
        usage.append(reply.usage)
        if reply.stop_reason in ("error", "aborted"):
            if provider_retried:
                raise CompactionFailed(reply.error_message or "模型调用失败")
            provider_retried = True
            phase = "retrying"
            continue

        errors = []
        if reply.tool_calls():
            errors.append("不要调用工具")
        if reply.stop_reason == "length":
            errors.append("输出被截断，请更精简")
        raw = parse_json_block(reply.text())
        if raw is None:
            errors.append("没有找到可解析的 JSON 代码块")
            llm = None
        else:
            llm, schema_errors = normalize_llm_state(raw)
            errors += schema_errors
        if not errors and llm is not None:
            return llm, []
        if format_retried:
            return None, errors
        format_retried = True
        phase = "retrying"
        messages = [
            *messages,
            AssistantMessage(content=[TextContent(text=reply.text() or "（空）")],
                             stop_reason="stop"),
            UserMessage(content=build_retry_message(errors)),
        ]


async def run_compaction(
        req: CompactionRequest,
        on_progress: Callable[[CompactionProgress], None] | None = None,
        cancel_event: asyncio.Event | None = None,
) -> CompactionOutcome | None:
    """没有可压缩的内容时返回 None。"""
    progress = on_progress or (lambda _p: None)
    messages = list(req.messages)
    cut = _choose_cut(req)
    if cut is None:
        return None

    previous = req.previous_state or {}
    previous_llm = previous.get("llm")
    seq = int(previous.get("seq") or 0) + 1
    summarized = messages[cut.start:cut.cut]
    facts = merge_facts(previous or None, extract_facts(summarized, req.cwd))
    budget_tokens = req.budget.summary_budget(estimate_tokens(summarized))
    max_tokens = int(budget_tokens * 1.5) + 1_024
    has_previous_message = cut.start > 0

    instruction = build_instruction(
        has_previous=has_previous_message,
        budget_tokens=budget_tokens,
        split_turn=cut.split_turn,
        custom_instructions=req.custom_instructions,
    )
    fixed = _fixed_tokens(req)
    tokens_before = context_tokens(messages, req.system_prompt, req.tools)
    room = req.budget.context_window - max_tokens - SAFETY_MARGIN
    usage: list[Usage] = []
    errors: list[str] = []
    degraded = False

    single = fixed + estimate_tokens(messages[:cut.cut]) + estimate_text_tokens(instruction)
    if single <= room:
        # 与正文请求同一前缀：命中前缀缓存
        base = default_convert_to_llm(messages[:cut.cut])
        llm, errors = await _summarize(req, base, instruction, max_tokens,
                                       progress, 1, 1, cancel_event, usage)
        chunks = 1
        if llm is None:
            degraded = True
            llm = degraded_llm_state(previous_llm, f"第 {seq} 次压缩未能生成摘要（{'；'.join(errors)}）")
    else:
        # 待压缩内容本身放不下：分块依次合并进同一份状态
        running_llm = previous_llm
        running_facts = previous or None
        chunk_instruction = build_instruction(True, budget_tokens, cut.split_turn,
                                              req.custom_instructions)
        # 滚动状态的大小受输出上限约束，按 max_tokens 预留
        max_chunk = max(1_000, room - fixed - max_tokens - estimate_text_tokens(chunk_instruction))
        ranges = chunk_ranges(messages, cut.start, cut.cut, max_chunk)
        chunks = len(ranges)
        for index, (lo, hi) in enumerate(ranges, 1):
            base: list[Message] = []
            if running_llm is not None:
                running_state = build_state(running_llm, merge_facts(
                    running_facts, extract_facts(messages[cut.start:lo], req.cwd)), seq)
                base.append(UserMessage(content=render_markdown(running_state)))
            elif index == 1 and cut.start > 0:
                # 开头是旧格式（纯文本）的摘要，没有结构化状态：原样带上
                base += default_convert_to_llm(messages[:cut.start])
            base += default_convert_to_llm(messages[lo:hi])
            text = chunk_instruction if running_llm is not None or cut.start > 0 else instruction
            llm, chunk_errors = await _summarize(req, base, text, max_tokens,
                                                 progress, index, chunks, cancel_event, usage)
            if llm is None:
                degraded = True
                errors += chunk_errors
                llm = degraded_llm_state(
                    running_llm, f"第 {seq} 次压缩第 {index}/{chunks} 块未能生成摘要")
            elif running_llm is not None:
                restore_dropped(llm, running_llm)
            running_llm = llm
        llm = running_llm or degraded_llm_state(None, "未能生成摘要")

    restored = restore_dropped(llm, previous_llm)
    meta = {
        "reason": req.reason,
        "tokensBefore": tokens_before,
        "degraded": degraded,
        "createdAt": time.time(),
    }
    if cut.split_turn and cut.turn_request:
        meta["turnRequest"] = cut.turn_request
    state = build_state(llm, facts, seq, meta)
    state, trimmed = trim_to_budget(state, budget_tokens)
    summary = render_markdown(state)
    retained = messages[cut.cut:]
    tokens_after = fixed + estimate_text_tokens(summary) + estimate_tokens(retained)
    state["meta"]["tokensAfter"] = tokens_after

    total = Usage()
    for u in usage:
        total += u
    return CompactionOutcome(
        state=state,
        summary=summary,
        cut=cut,
        tokens_before=tokens_before,
        tokens_after=tokens_after,
        usage=total,
        degraded=degraded,
        restored=restored,
        trimmed=trimmed,
        errors=errors,
        chunks=chunks,
        retained=retained,
    )
